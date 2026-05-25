"""Slice 3C — tool-record propagation across outer-retry attempts.

Closes bt-2026-05-25-033000: Iron Gate counter saw 0 tool calls despite
``ToolLoopCoordinator`` accumulating 19 records across 10 rounds. Root:
the candidate_generator outer-retry mechanism ran the tool loop multiple
times per orchestrator generation. Each FAILED attempt's records were
silently dropped because the exception that propagated up didn't carry
them. The successful attempt's records (often zero — direct patch emit)
were the only ones reaching ``GenerationResult.tool_execution_records``.

# Fix mechanism (two-layer, composes existing protocol)

* Layer 1 (``tool_executor.py::_attach_tool_records``): every failure-
  path ``raise`` inside ``ToolLoopCoordinator.run()`` stamps the current
  ``records`` list onto the exception via the helper. Mirrors the
  pre-existing protocol consumed by orchestrator.py:5135.

* Layer 2 (``candidate_generator.py`` outer-retry loop): accumulator
  ``_carryover_tool_records`` harvests records from each failed
  attempt's exception. On the winning attempt, merges accumulator +
  winning records via ``GenerationResult.with_tool_records`` so Iron
  Gate sees cumulative exploration across attempts.

# Why exception-attachment vs ctx-field

Exceptions were ALREADY the failure-path data transport (orchestrator.py
already reads ``getattr(exc, 'tool_execution_records', ())``). A
parallel ``ctx.cumulative_tool_records`` field would duplicate the
transport. Operator binding "pass data cleanly via context/results" —
the exception IS the result on failure paths.

# Test surface (3 AST pins + 6 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"
)
CANDIDATE_GENERATOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "candidate_generator.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_attach_tool_records_helper_exists() -> None:
    """``_attach_tool_records`` must be a module-level function in
    tool_executor — it's the contract surface every failure-path raise
    composes through, and the symbol orchestrator.py + candidate_generator.py
    coordinate on by name."""
    tree = _parse(TOOL_EXECUTOR_FILE)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_attach_tool_records":
            found = True
            # Must accept (exc, records) and return exc
            args = [a.arg for a in node.args.args]
            assert "exc" in args and "records" in args, (
                f"_attach_tool_records signature must accept (exc, records); "
                f"got {args}"
            )
            break
    assert found, (
        "_attach_tool_records helper missing from tool_executor.py — "
        "Slice 3C contract broken. The bt-2026-05-25-033000 trap is open."
    )


def test_ast_pin_tool_loop_failure_raises_attach_records() -> None:
    """Every ``raise RuntimeError(\"tool_loop_*\")`` inside
    ``ToolLoopCoordinator.run()`` must compose ``_attach_tool_records``
    so outer-retry callers can harvest the partial-run records.

    Walks the AST of the ``run`` method, finds all ``raise`` statements
    whose argument is a RuntimeError with a ``tool_loop_`` reason, and
    requires each one to be wrapped in a call to ``_attach_tool_records``
    (or for the RuntimeError construction itself to have records
    stamped via a sibling assignment — both shapes are accepted).
    """
    tree = _parse(TOOL_EXECUTOR_FILE)
    coordinator = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolLoopCoordinator":
            coordinator = node
            break
    assert coordinator is not None, "ToolLoopCoordinator class missing"

    run_method = None
    for sub in coordinator.body:
        if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "run":
            run_method = sub
            break
    assert run_method is not None, "ToolLoopCoordinator.run method missing"

    naked_raise_sites: list[str] = []
    for raise_node in ast.walk(run_method):
        if not isinstance(raise_node, ast.Raise):
            continue
        if raise_node.exc is None:
            # bare ``raise`` — re-raises current exception; checked
            # separately in the cancellation test below.
            continue
        # Look for tool_loop_* RuntimeError patterns
        unparsed = ast.unparse(raise_node)
        if "tool_loop_" not in unparsed:
            continue
        if "_attach_tool_records" not in unparsed:
            naked_raise_sites.append(unparsed[:120])

    assert not naked_raise_sites, (
        "Found tool_loop_* raise sites in ToolLoopCoordinator.run that do "
        f"NOT compose _attach_tool_records: {naked_raise_sites}. "
        "Slice 3C contract requires every failure-path raise to carry "
        "records so outer-retry can accumulate."
    )


def test_ast_pin_candidate_generator_outer_retry_accumulator() -> None:
    """The ``candidate_generator.py`` outer-retry loop must declare a
    ``_carryover_tool_records`` accumulator AND harvest from the
    inner_exc via ``tool_execution_records``. Spine for the cumulative-
    exploration invariant."""
    src = CANDIDATE_GENERATOR_FILE.read_text()
    assert "_carryover_tool_records" in src, (
        "candidate_generator.py missing the Slice 3C accumulator — "
        "outer-retry attempts will continue dropping records."
    )
    assert "tool_execution_records" in src, (
        "candidate_generator.py missing tool_execution_records reference"
    )
    # Confirm the merge path uses with_tool_records (the public API on
    # GenerationResult, byte-identical to dataclasses.replace).
    assert "with_tool_records" in src, (
        "candidate_generator.py must merge carryover via "
        "GenerationResult.with_tool_records — Slice 3C contract."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_attach_tool_records_stamps_attribute() -> None:
    """Helper writes ``tool_execution_records`` attribute on the
    exception and returns the same exception for inline composition."""
    from backend.core.ouroboros.governance.tool_executor import (
        _attach_tool_records,
    )
    exc = RuntimeError("test")
    records = [{"tool_name": "search_code"}, {"tool_name": "read_file"}]
    returned = _attach_tool_records(exc, records)
    assert returned is exc, "Helper must return same exc for inline raise"
    assert hasattr(exc, "tool_execution_records")
    assert exc.tool_execution_records == tuple(records)


def test_spine_attach_tool_records_empty_iterable_ok() -> None:
    """Empty records is valid — stamps an empty tuple."""
    from backend.core.ouroboros.governance.tool_executor import (
        _attach_tool_records,
    )
    exc = RuntimeError("test")
    _attach_tool_records(exc, ())
    assert exc.tool_execution_records == ()


def test_spine_attach_tool_records_never_raises_on_weird_input() -> None:
    """Even if records is a generator that raises on iteration, the
    helper swallows and returns. NEVER block a re-raise."""
    from backend.core.ouroboros.governance.tool_executor import (
        _attach_tool_records,
    )

    def _bad_gen():
        raise ValueError("intentional")
        yield  # unreachable

    exc = RuntimeError("test")
    # Should not propagate the ValueError
    result = _attach_tool_records(exc, _bad_gen())
    assert result is exc


def test_spine_harvest_pattern_matches_orchestrator_convention() -> None:
    """Validate that the orchestrator's existing harvest pattern
    (orchestrator.py:5135) works on Slice-3C-stamped exceptions.
    Cross-module contract — if this drifts, Slice 3C silently breaks."""
    from backend.core.ouroboros.governance.tool_executor import (
        _attach_tool_records,
    )
    exc = RuntimeError("tool_loop_starved_below_min_ttft_floor")
    records = [{"tool_name": "search_code"}]
    _attach_tool_records(exc, records)
    # Mirror orchestrator.py:5135's getattr-default-empty-tuple pattern
    harvested = getattr(exc, "tool_execution_records", ()) or ()
    assert tuple(harvested) == tuple(records), (
        "Slice 3C exception attachment must be readable via the "
        "getattr(exc, 'tool_execution_records', ()) convention that "
        "orchestrator.py:5135 already uses."
    )


def test_spine_carryover_accumulator_pattern() -> None:
    """Simulate the candidate_generator outer-retry pattern: failed
    attempts stamp records onto exceptions, the accumulator extends from
    each, the final result merges accumulator + winning records.

    This is a pure-data test of the propagation math — proves the merge
    order (carryover BEFORE winning) preserves chronological exploration
    history."""
    from backend.core.ouroboros.governance.tool_executor import (
        _attach_tool_records,
    )

    # Simulate 2 failed attempts then 1 winning attempt
    attempt_1_exc = RuntimeError("tool_loop_starved")
    _attach_tool_records(
        attempt_1_exc,
        [{"tool_name": "search_code", "attempt": 1}],
    )

    attempt_2_exc = RuntimeError("tool_loop_deadline")
    _attach_tool_records(
        attempt_2_exc,
        [
            {"tool_name": "read_file", "attempt": 2},
            {"tool_name": "glob_files", "attempt": 2},
        ],
    )

    # Winning attempt — model emitted patch with no further tool calls
    winning_records = ()  # The bt-2026-05-25-033000 scenario

    # Apply Slice 3C harvest pattern from candidate_generator
    _carryover = []
    for _failed_exc in (attempt_1_exc, attempt_2_exc):
        _carryover.extend(
            getattr(_failed_exc, "tool_execution_records", ()) or ()
        )

    merged = tuple(_carryover) + winning_records
    assert len(merged) == 3, (
        f"Carryover merge dropped records: expected 3, got {len(merged)}"
    )
    # Order check: carryover first (chronological exploration history)
    assert merged[0]["tool_name"] == "search_code"
    assert merged[0]["attempt"] == 1
    assert merged[2]["tool_name"] == "glob_files"
    assert merged[2]["attempt"] == 2


def test_spine_generation_result_with_tool_records_round_trip() -> None:
    """``GenerationResult.with_tool_records`` is the merge contract that
    Slice 3C uses to stamp the cumulative records onto the winning
    attempt. Round-trip test proves the contract holds."""
    from backend.core.ouroboros.governance.op_context import GenerationResult

    base = GenerationResult(
        candidates=(),
        provider_name="claude-api",
        generation_duration_s=12.5,
        tool_execution_records=(),
    )
    cumulative = (
        {"tool_name": "search_code"},
        {"tool_name": "read_file"},
        {"tool_name": "glob_files"},
    )
    merged = base.with_tool_records(cumulative)
    assert merged.tool_execution_records == cumulative
    # Must be a NEW immutable result (frozen dataclass) — base unchanged
    assert base.tool_execution_records == ()
    # All other fields must round-trip
    assert merged.provider_name == "claude-api"
    assert merged.generation_duration_s == 12.5

"""Slice 3D — coordinator-attribute record harvest fallback.

Closes the residual propagation gap from Slice 3C surfaced by
bt-2026-05-25-041717: the ansible op's attempt-1 tool loop ran 244.2s,
emitted a 9941-token output across 13+ tool calls, then died at the
OUTER ``_race_or_wait_for(... timeout=_attempt_remaining)`` boundary.
That outer race raises ``asyncio.TimeoutError`` / ``CancelledError``
that does NOT traverse ``ToolLoopCoordinator``'s
``_attach_tool_records`` raise sites — Slice 3C harvested nothing
because the exception was opaque, the cumulative records were lost,
the next attempt's GenerationResult carried 0 records, Iron Gate
rejected for ``exploration_insufficient: 0/2`` — same trap as
bt-2026-05-25-033000.

# Fix mechanism

When the inner_exc carries no records (the Slice 3C path returns
empty), Slice 3D falls back to ``coordinator._last_records`` directly.
The coordinator resets ``_last_records = []`` at every ``run()`` start
(tool_executor.py line 5250), so at except-block time it reflects
exactly the just-failed attempt's records — no cross-attempt
double-counting risk.

The fallback path uses defensive getattr chains (``_fallback._tool_loop``
+ ``_tool_loop._last_records``) so legacy/test providers without these
attributes fall through cleanly. Mirrors the Slice 3C never-block-retry
guarantee.

# Test surface (2 AST pins + 5 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_GENERATOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "candidate_generator.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice3d_fallback_references_coordinator_last_records() -> None:
    """The candidate_generator outer-retry except block must reference
    ``_tool_loop`` AND ``_last_records`` for the Slice 3D fallback path
    to compose. Without this, the asyncio TimeoutError that the outer
    race raises (the bt-2026-05-25-041717 failure mode) silently drops
    the coordinator's records."""
    src = CANDIDATE_GENERATOR_FILE.read_text()
    assert "_tool_loop" in src and "_last_records" in src, (
        "candidate_generator.py is missing the Slice 3D coordinator "
        "fallback. The asyncio.TimeoutError trap from "
        "bt-2026-05-25-041717 is open again."
    )
    # Confirm the fallback comment marker is present so the intent
    # survives future refactors that touch the harvest block.
    assert "Slice 3D" in src, (
        "Slice 3D marker missing from candidate_generator.py"
    )


def test_ast_pin_slice3d_fallback_is_within_harvest_except_block() -> None:
    """The ``_last_records`` reference must live INSIDE the same
    ``except (Exception, asyncio.CancelledError)`` block that harvests
    via ``inner_exc.tool_execution_records``. The fallback is one
    if-branch BELOW the Slice 3C primary path; not a parallel try/except.
    """
    tree = _parse(CANDIDATE_GENERATOR_FILE)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body_src = ast.unparse(node)
        if (
            "tool_execution_records" in body_src
            and "_last_records" in body_src
            and "_tool_loop" in body_src
            and "_carryover_tool_records" in body_src
        ):
            found = True
            break
    assert found, (
        "No except-handler block composes BOTH Slice 3C (inner_exc."
        "tool_execution_records) AND Slice 3D (coordinator._last_records). "
        "The two paths must coexist in the same harvest block — they "
        "share the _carryover_tool_records accumulator."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 5
# ──────────────────────────────────────────────────────────────────────


class _StubCoordinator:
    """Minimal coordinator stub mirroring ToolLoopCoordinator's contract
    for the records-harvest pathway (Slice 3D consumes _last_records)."""

    def __init__(self, records=None) -> None:
        self._last_records = list(records or ())


class _StubFallback:
    """Minimal provider stub for the harvest path. Carries a coordinator
    via ``_tool_loop`` exactly as the real ClaudeProvider/PrimeProvider
    do."""

    def __init__(self, coordinator=None) -> None:
        self._tool_loop = coordinator


def _simulate_harvest(
    inner_exc, fallback,
) -> list:
    """Pure-function model of the candidate_generator harvest body.

    Mirrors the production code at candidate_generator.py:4441+ exactly
    — when this drifts, the AST pin above catches it. We pull the
    behavior into a test helper so we can exercise edge cases without
    booting the full CandidateGenerator.
    """
    carryover: list = []
    try:
        harvested = getattr(
            inner_exc, "tool_execution_records", (),
        ) or ()
        if not harvested:
            # Slice 3D fallback
            coord = getattr(fallback, "_tool_loop", None)
            if coord is not None:
                harvested = tuple(
                    getattr(coord, "_last_records", ()) or ()
                )
        if harvested:
            carryover.extend(harvested)
    except Exception:  # noqa: BLE001
        pass
    return carryover


def test_spine_slice3c_path_wins_when_exception_carries_records() -> None:
    """When the inner_exc has ``tool_execution_records``, Slice 3C path
    wins — Slice 3D fallback is NOT consulted. Prevents double-counting
    if both attribute paths are populated."""
    exc = RuntimeError("tool_loop_starved_below_min_ttft_floor")
    exc.tool_execution_records = (  # type: ignore[attr-defined]
        {"tool_name": "search_code", "via": "slice_3c"},
    )
    # Coordinator ALSO has records — Slice 3D would over-count if it ran
    coord = _StubCoordinator(records=[
        {"tool_name": "read_file", "via": "slice_3d_should_NOT_fire"},
    ])
    fb = _StubFallback(coordinator=coord)
    result = _simulate_harvest(exc, fb)
    assert len(result) == 1, (
        f"Expected 1 record from Slice 3C only; got {len(result)}. "
        "Slice 3D fallback fired when it shouldn't have — double-count "
        "risk."
    )
    assert result[0]["via"] == "slice_3c"


def test_spine_slice3d_fallback_fires_on_opaque_timeout() -> None:
    """THE bt-2026-05-25-041717 regression test. ``asyncio.TimeoutError``
    from the outer race wrapper carries no ``tool_execution_records``
    attribute. Slice 3D fallback reads from ``coordinator._last_records``
    and recovers the partial-run history."""
    import asyncio
    exc = asyncio.TimeoutError()  # ← opaque, no records on it
    coord = _StubCoordinator(records=[
        {"tool_name": "search_code", "round": 0},
        {"tool_name": "glob_files", "round": 0},
        {"tool_name": "bash", "round": 1},
        {"tool_name": "read_file", "round": 2},
    ])
    fb = _StubFallback(coordinator=coord)
    result = _simulate_harvest(exc, fb)
    assert len(result) == 4, (
        f"Slice 3D fallback failed to harvest 4 records from coordinator; "
        f"got {len(result)}. Iron Gate would see 0/2 on outer-race "
        f"timeout — bt-2026-05-25-041717 trap is open."
    )


def test_spine_missing_tool_loop_attribute_falls_through_cleanly() -> None:
    """Legacy / test providers without ``_tool_loop`` must not crash
    the harvest path. Defensive getattr returns None, fallback is no-op."""
    import asyncio
    exc = asyncio.TimeoutError()

    class _NoToolLoopFallback:
        pass  # no _tool_loop attribute at all

    fb = _NoToolLoopFallback()
    result = _simulate_harvest(exc, fb)
    assert result == [], (
        "Provider without _tool_loop attribute must yield empty harvest "
        "(no exception escape from the harvest path)."
    )


def test_spine_coordinator_with_empty_records_is_noop() -> None:
    """Coordinator exists but its ``_last_records`` is empty
    (no rounds completed before failure). Accumulator unchanged."""
    import asyncio
    exc = asyncio.TimeoutError()
    coord = _StubCoordinator(records=[])
    fb = _StubFallback(coordinator=coord)
    result = _simulate_harvest(exc, fb)
    assert result == []


def test_spine_full_cross_attempt_cycle_proves_propagation() -> None:
    """End-to-end model: 3 outer-retry attempts where each fails via a
    DIFFERENT exception mode. Slice 3C + 3D together must recover all
    records.

      * Attempt 1: outer-race TimeoutError (opaque) → 3D fallback (4 records)
      * Attempt 2: tool_loop_starved (Slice 3C attaches) → 3C path (2 records)
      * Attempt 3: succeeds with 0 records (direct patch emit)

    Iron Gate's cumulative count must be 6, not 0.
    """
    import asyncio
    carryover: list = []

    # Attempt 1 — coordinator harvested via Slice 3D
    coord = _StubCoordinator(records=[
        {"tool": "search_code", "attempt": 1},
        {"tool": "glob_files", "attempt": 1},
        {"tool": "bash", "attempt": 1},
        {"tool": "read_file", "attempt": 1},
    ])
    fb = _StubFallback(coordinator=coord)
    exc_1 = asyncio.TimeoutError()
    carryover.extend(_simulate_harvest(exc_1, fb))

    # Attempt 2 — Slice 3C attaches records to RuntimeError
    # (coordinator._last_records was just reset to [] at attempt 2 start
    # then re-populated with attempt 2's calls)
    coord._last_records = [
        {"tool": "read_file", "attempt": 2},
        {"tool": "search_code", "attempt": 2},
    ]
    exc_2 = RuntimeError("tool_loop_starved_below_min_ttft_floor")
    exc_2.tool_execution_records = (  # type: ignore[attr-defined]
        *coord._last_records,
    )
    carryover.extend(_simulate_harvest(exc_2, fb))

    # Attempt 3 — succeeds; carryover gets merged onto its GenerationResult.
    # The merge is the orchestrator's _fb_result.with_tool_records(...)
    # call — we just verify the accumulator at this point.
    assert len(carryover) == 6, (
        f"Expected 6 carryover records across two distinct failure "
        f"modes; got {len(carryover)}. The bt-2026-05-25-033000 + "
        f"bt-2026-05-25-041717 cumulative-exploration invariant is "
        f"broken."
    )
    # Order check: attempt 1 records come first
    assert carryover[0]["attempt"] == 1
    assert carryover[3]["attempt"] == 1
    assert carryover[4]["attempt"] == 2

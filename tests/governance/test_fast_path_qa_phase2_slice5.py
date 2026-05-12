"""Regression spine for §41.3 #26 Phase 2 Slice 5 — ``/fast_path_qa``
REPL dispatcher.

Pins:

* §33.3 naming-cage compliance — module name matches verb name
  matches dispatcher function name; the repl_dispatch_registry
  auto-discovers it zero-edit.
* All 7 sub-verbs dispatch correctly (recent / path / op / ref /
  stats / help / unknown).
* Master-flag gate defers to canonical fast_path_qa.master_enabled
  — no parallel flag.
* help bypass: ``/fast_path_qa help`` works even when master is
  off (discoverability).
* Authority asymmetry (AST-pinned via register_shipped_invariants).
* Read-only contract: source has zero ``store.store(`` mutations.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.fast_path_qa_repl import (
    FastPathQAReplDispatchResult,
    dispatch_fast_path_qa_command,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.fast_path_qa import (
    _ENV_MASTER,
    get_default_qa_store,
    reset_default_qa_store,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    reset_default_qa_store()
    yield
    reset_default_qa_store()


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_MASTER, "true")


def _seed(op_id: str = "op-x", path: str = "claude_direct") -> None:
    get_default_qa_store().store(
        question="q?", answer="a.",
        op_id=op_id, cost_usd=0.001,
        model="claude-sonnet-4-5", elapsed_s=0.1,
        retrieval_path=path, top_score=0.5,
    )


# ---------------------------------------------------------------------------
# §33.3 naming-cage — module + function + verb parity
# ---------------------------------------------------------------------------


def test_module_filename_matches_verb_name():
    """``fast_path_qa_repl.py`` → verb ``fast_path_qa`` →
    dispatcher ``dispatch_fast_path_qa_command``. Parity is
    what repl_dispatch_registry walks."""
    import backend.core.ouroboros.governance.fast_path_qa_repl as mod
    # Module file exists at expected path.
    assert mod.__file__ is not None
    assert mod.__file__.endswith("fast_path_qa_repl.py")
    # Dispatcher symbol present at module level.
    assert callable(mod.dispatch_fast_path_qa_command)


def test_dispatcher_signature_one_string_arg():
    """repl_dispatch_registry's signature validator expects
    exactly one positional ``line: str`` parameter. Drift here
    silently breaks auto-discovery."""
    import inspect
    sig = inspect.signature(dispatch_fast_path_qa_command)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"


def test_result_dataclass_frozen():
    """§33.5 frozen-artifact contract."""
    result = FastPathQAReplDispatchResult(ok=True, text="x")
    with pytest.raises(Exception):
        result.ok = False  # type: ignore[misc]


def test_result_to_dict_shape():
    result = FastPathQAReplDispatchResult(ok=True, text="hello")
    d = result.to_dict()
    assert d == {"ok": True, "text": "hello", "matched": True}


# ---------------------------------------------------------------------------
# Match-gate: only ``/fast_path_qa`` lines are claimed
# ---------------------------------------------------------------------------


def test_does_not_match_unrelated_lines():
    """matched=False signals the caller to route elsewhere."""
    cases = [
        "",
        "  ",
        "/help",
        "/tool_permissions",
        "/qa",  # not the substrate verb name
        "/fast_path_qaX recent",
        "fast_path_qaX",
    ]
    for line in cases:
        r = dispatch_fast_path_qa_command(line)
        assert r.matched is False, f"{line!r} should not match"


def test_matches_canonical_invocations():
    cases = [
        "/fast_path_qa",
        "/fast_path_qa recent",
        "/fast_path_qa help",
        "fast_path_qa",
        "fast_path_qa stats",
        "  /fast_path_qa recent 5  ",
    ]
    for line in cases:
        r = dispatch_fast_path_qa_command(line)
        assert r.matched is True, f"{line!r} should match"


# ---------------------------------------------------------------------------
# Master-flag gate
# ---------------------------------------------------------------------------


def test_disabled_when_master_off(monkeypatch):
    # master is unset → defaults False per §33.1.
    r = dispatch_fast_path_qa_command("/fast_path_qa recent")
    assert r.matched is True
    assert r.ok is False
    assert "JARVIS_FAST_PATH_QA_ENABLED" in r.text


def test_help_bypasses_master_gate(monkeypatch):
    """help is operator-discoverability — must work even when
    the substrate is master-off."""
    r = dispatch_fast_path_qa_command("/fast_path_qa help")
    assert r.matched is True
    assert r.ok is True
    assert "Subcommands" in r.text


# ---------------------------------------------------------------------------
# Sub-verbs — recent
# ---------------------------------------------------------------------------


def test_recent_empty_ring(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command("/fast_path_qa recent")
    assert r.ok is True
    assert "no Q&A artifacts recorded yet" in r.text


def test_recent_with_artifacts(monkeypatch):
    _enable(monkeypatch)
    for i in range(3):
        _seed(op_id=f"op-{i}")
    r = dispatch_fast_path_qa_command("/fast_path_qa recent")
    assert r.ok is True
    # All 3 op_ids surfaced (truncated to first 18 chars).
    for i in range(3):
        assert f"op-{i}" in r.text


def test_recent_alias_no_args(monkeypatch):
    """Bare ``/fast_path_qa`` aliases to recent."""
    _enable(monkeypatch)
    _seed()
    r = dispatch_fast_path_qa_command("/fast_path_qa")
    assert r.ok is True
    assert "recent" in r.text.lower()


def test_recent_respects_limit(monkeypatch):
    _enable(monkeypatch)
    for i in range(5):
        _seed(op_id=f"op-{i}")
    r = dispatch_fast_path_qa_command("/fast_path_qa recent 2")
    assert r.ok is True
    # Line count: 1 header + 2 records = 3 lines.
    assert r.text.count("\n") == 2


def test_recent_clamps_huge_limit(monkeypatch):
    _enable(monkeypatch)
    _seed()
    # Limit > 200 (ceiling) clamps to 200 → returns all 1.
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa recent 99999",
    )
    assert r.ok is True


# ---------------------------------------------------------------------------
# Sub-verbs — path
# ---------------------------------------------------------------------------


def test_path_missing_arg(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command("/fast_path_qa path")
    assert r.ok is False
    assert "missing retrieval_path" in r.text


def test_path_filters_correctly(monkeypatch):
    _enable(monkeypatch)
    _seed(op_id="o1", path="claude_direct")
    _seed(op_id="o2", path="hybrid_grounded")
    _seed(op_id="o3", path="hybrid_grounded")
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa path hybrid_grounded",
    )
    assert r.ok is True
    assert "o2" in r.text
    assert "o3" in r.text
    assert "o1" not in r.text  # filtered out


def test_path_unknown_returns_empty(monkeypatch):
    _enable(monkeypatch)
    _seed()
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa path nonexistent_xyz",
    )
    assert r.ok is True
    assert "no artifacts recorded for this retrieval path" in r.text


# ---------------------------------------------------------------------------
# Sub-verbs — op
# ---------------------------------------------------------------------------


def test_op_missing_arg(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command("/fast_path_qa op")
    assert r.ok is False
    assert "missing op_id" in r.text


def test_op_filters_correctly(monkeypatch):
    _enable(monkeypatch)
    _seed(op_id="alpha")
    _seed(op_id="beta")
    _seed(op_id="alpha")
    r = dispatch_fast_path_qa_command("/fast_path_qa op alpha")
    assert r.ok is True
    # Two alpha artifacts surface; no beta in header (op_id
    # filter is exact).
    assert "alpha" in r.text


# ---------------------------------------------------------------------------
# Sub-verbs — ref
# ---------------------------------------------------------------------------


def test_ref_missing_arg(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command("/fast_path_qa ref")
    assert r.ok is False
    assert "missing q-N ref" in r.text


def test_ref_not_found(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa ref q-9999",
    )
    assert r.ok is False
    assert "not found" in r.text


def test_ref_resolves_to_artifact(monkeypatch):
    _enable(monkeypatch)
    _seed(op_id="op-y")
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa ref q-1",
    )
    assert r.ok is True
    # Detail block includes op_id + retrieval_path + cost.
    assert "op-y" in r.text
    assert "retrieval_path" in r.text
    assert "claude_direct" in r.text


# ---------------------------------------------------------------------------
# Sub-verbs — stats
# ---------------------------------------------------------------------------


def test_stats_shape(monkeypatch):
    _enable(monkeypatch)
    _seed()
    _seed()
    r = dispatch_fast_path_qa_command("/fast_path_qa stats")
    assert r.ok is True
    # All canonical snapshot fields surfaced.
    for token in (
        "capacity:", "size:", "next_seq:",
        "utilization:", "schema:",
    ):
        assert token in r.text
    # Q&A-specific: daily budget tracking.
    assert "today_spend:" in r.text
    assert "daily_cap:" in r.text


# ---------------------------------------------------------------------------
# Help + unknown subcommand
# ---------------------------------------------------------------------------


def test_help_lists_all_subcommands():
    r = dispatch_fast_path_qa_command("/fast_path_qa help")
    assert r.ok is True
    for verb in ("recent", "path", "op", "ref", "stats", "help"):
        assert verb in r.text


def test_help_short_form_question_mark():
    r = dispatch_fast_path_qa_command("/fast_path_qa ?")
    assert r.ok is True
    assert "Subcommands" in r.text


def test_unknown_subcommand(monkeypatch):
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command(
        "/fast_path_qa nonexistent_subcommand",
    )
    assert r.ok is False
    assert "unknown subcommand" in r.text


# ---------------------------------------------------------------------------
# Defensive — NEVER raises
# ---------------------------------------------------------------------------


def test_parse_error_does_not_raise(monkeypatch):
    """Unbalanced quotes → shlex.ValueError → graceful error."""
    _enable(monkeypatch)
    r = dispatch_fast_path_qa_command('/fast_path_qa op "unterminated')
    assert r.matched is True
    assert r.ok is False
    assert "parse error" in r.text


def test_never_raises_on_garbage_inputs():
    """The contract is NEVER raises — every garbage input must
    yield a dataclass result, never an exception."""
    for line in (None, 42, [], {}, b"bytes"):  # type: ignore[var-annotated]
        try:
            r = dispatch_fast_path_qa_command(line)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"dispatcher raised on {line!r}: {exc!r}")
        assert isinstance(r, FastPathQAReplDispatchResult)


# ---------------------------------------------------------------------------
# AST-pinned authority + READ-ONLY invariants
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_pin():
    pins = register_shipped_invariants()
    assert len(pins) == 1
    assert pins[0].invariant_name == "fast_path_qa_repl_substrate"


def test_authority_pin_passes_on_current_source():
    pins = register_shipped_invariants()
    target = pins[0]
    src_path = Path(
        "backend/core/ouroboros/governance/fast_path_qa_repl.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations = target.validate(tree, source)
    assert violations == (), f"AST pin drift: {violations}"


def test_source_has_no_mutation_calls():
    """READ-ONLY enforcement — REPL surface must NEVER mutate
    the q-N ring. AST-walk for ``.store(`` Call expressions
    (substring matching is unsafe because the validator's own
    text references ``.store()`` for documentation)."""
    src_path = Path(
        "backend/core/ouroboros/governance/fast_path_qa_repl.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "store"
            ):
                pytest.fail(
                    "fast_path_qa_repl.py contains a "
                    f".store() Call at line {node.lineno} — "
                    "READ-ONLY invariant violated"
                )


# ---------------------------------------------------------------------------
# Auto-discovery: repl_dispatch_registry sees our module
# ---------------------------------------------------------------------------


def test_repl_dispatch_registry_auto_discovers():
    """The substrate must be pickable by the canonical
    repl_dispatch_registry walker without any manual wiring.
    Closes the §33.3 naming-cage contract end-to-end:
    fast_path_qa_repl.py → verb ``fast_path_qa`` appears in the
    registry's discovered verb list."""
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    assert "fast_path_qa" in report.verbs

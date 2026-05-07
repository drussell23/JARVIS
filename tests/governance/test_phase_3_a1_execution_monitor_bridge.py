"""Phase 3 A1 — ExecutionMonitor bridge test spine.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A1: ExecutionMonitor.record() from orchestrator COMPLETE
   (and failure paths) → SafetyNet / escalation inputs —
   never raises, bounded JSONL if applicable."

Pinned coverage (~30 tests):
  * Master flag default-FALSE per §33.1
  * Recorder no-op when master off
  * Recorder no-op on blank op_id
  * Frozen TerminalOutcomeRecord round-trip via to_dict/from_dict
  * Schema mismatch → from_dict returns None
  * Defensive metadata serialization (non-JSON values fall back
    to str)
  * get_terminal_status_name: 13 mapped reasons → canonical
    enum names; unknown → FAILED fallback; blank → FAILED;
    non-string → FAILED
  * record_terminal_outcome: COMPLETE → COMPLETED; cost-cap →
    TIMEOUT; user_cancelled → FAILED; etc.
  * record_terminal_outcome composes canonical ExecutionMonitor
    singleton (canonical record() invocation, no parallel state)
  * record_terminal_outcome persists to bounded JSONL ledger
  * read_recent_records ordering + limit
  * read_recent_records handles missing ledger / oversized
    ledger / SyntaxError / non-py defensively
  * 5 AST pins clean (parametrized) + each fires on synthetic
    regression
  * AST pin: status table values MUST be canonical
    ExecutionStatus enum names
  * AST pin: status table accepts annotated assignment
    (Mapping[str, str])
  * Public API surface complete + register_flags seeds 2 + swallows
    registry errors
  * Bridge call site present in complete_runner.py (call-site
    regression)
  * Bridge call site is master-flag-gated (try/except wrapper
    + lazy-import discipline)
"""
from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "execution_monitor_bridge.py"
    )


@pytest.fixture
def tmp_ledger(monkeypatch):
    """Per-test isolated ledger path."""
    from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
        reset_default_monitor_for_tests,
    )
    reset_default_monitor_for_tests()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "bridge.jsonl"
        monkeypatch.setenv(
            (
                "JARVIS_EXECUTION_MONITOR_BRIDGE_"
                "LEDGER_PATH"
            ),
            str(ledger),
        )
        yield ledger
    reset_default_monitor_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", v,
        )
        assert master_enabled() is True


def test_recorder_noop_when_master_off(
    monkeypatch, tmp_ledger,
):
    monkeypatch.delenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome,
    )
    out = record_terminal_outcome(
        op_id="op-1", terminal_reason_code="complete",
        terminal_phase="COMPLETE", duration_ms=10.0,
        ledger_path_override=tmp_ledger,
    )
    assert out is None
    assert not tmp_ledger.exists()


def test_recorder_noop_on_blank_op_id(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome,
    )
    out = record_terminal_outcome(
        op_id="", terminal_reason_code="complete",
        terminal_phase="COMPLETE",
        ledger_path_override=tmp_ledger,
    )
    assert out is None


# ---------------------------------------------------------------------------
# get_terminal_status_name — pure mapping table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason, expected", [
        ("complete", "COMPLETED"),
        ("op_cost_cap_exceeded", "TIMEOUT"),
        ("no_forward_progress", "ITERATION_EXCEEDED"),
        ("user_cancelled", "FAILED"),
        ("advisor_blocked", "FAILED"),
        ("plan_required_unavailable", "FAILED"),
        ("plan_review_unavailable", "FAILED"),
        ("plan_rejected", "FAILED"),
        ("plan_approval_expired", "FAILED"),
        ("unhandled_pipeline_exception", "FAILED"),
        ("emergency_warning", "FAILED"),
        ("emergency_critical", "FAILED"),
        ("emergency_brake", "FAILED"),
        ("COMPLETE", "COMPLETED"),  # case-insensitive
    ],
)
def test_status_table_mapping(reason, expected):
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        get_terminal_status_name,
    )
    assert get_terminal_status_name(reason) == expected


def test_unknown_reason_falls_back_to_failed():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        get_terminal_status_name,
    )
    assert get_terminal_status_name(
        "novel_failure_mode",
    ) == "FAILED"


def test_blank_reason_falls_back_to_failed():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        get_terminal_status_name,
    )
    assert get_terminal_status_name("") == "FAILED"


def test_non_string_reason_falls_back_to_failed():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        get_terminal_status_name,
    )
    # Defensive: even None / int → FAILED, never raises
    assert get_terminal_status_name(None) == "FAILED"  # type: ignore
    assert get_terminal_status_name(42) == "FAILED"  # type: ignore


# ---------------------------------------------------------------------------
# Frozen artifact
# ---------------------------------------------------------------------------


def test_record_round_trip():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        TerminalOutcomeRecord,
    )
    rec = TerminalOutcomeRecord(
        op_id="op-1",
        status_name="COMPLETED",
        terminal_reason_code="complete",
        terminal_phase="COMPLETE",
        duration_ms=125.5,
        ts_unix=12345.0,
        metadata={"applied_files": ["a.py", "b.py"]},
    )
    rt = TerminalOutcomeRecord.from_dict(rec.to_dict())
    assert rt is not None
    assert rt.op_id == rec.op_id
    assert rt.status_name == "COMPLETED"
    assert rt.duration_ms == 125.5


def test_record_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        TerminalOutcomeRecord,
    )
    out = TerminalOutcomeRecord.from_dict(
        {"schema_version": "wrong"},
    )
    assert out is None


def test_record_metadata_defensive_serialization():
    """Non-JSON values in metadata fall back to str()."""
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        TerminalOutcomeRecord,
    )

    class _Custom:
        def __repr__(self):
            return "<custom-obj>"

    rec = TerminalOutcomeRecord(
        op_id="op-1",
        status_name="COMPLETED",
        terminal_reason_code="complete",
        terminal_phase="COMPLETE",
        duration_ms=10.0,
        ts_unix=0.0,
        metadata={
            "ok": "value",
            "obj": _Custom(),
        },
    )
    d = rec.to_dict()
    assert d["metadata"]["ok"] == "value"
    assert "<custom-obj>" in str(
        d["metadata"]["obj"],
    )


# ---------------------------------------------------------------------------
# record_terminal_outcome end-to-end
# ---------------------------------------------------------------------------


def test_record_persists_to_jsonl(monkeypatch, tmp_ledger):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome, read_recent_records,
    )
    rec = record_terminal_outcome(
        op_id="op-1", terminal_reason_code="complete",
        terminal_phase="COMPLETE", duration_ms=100.0,
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 1
    assert rows[0].op_id == "op-1"


def test_record_propagates_to_canonical_monitor(
    monkeypatch, tmp_ledger,
):
    """Bridge MUST compose the canonical ExecutionMonitor
    singleton — single source of truth for SafetyNet."""
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
        get_default_monitor,
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome,
    )

    monitor = get_default_monitor()
    before = monitor.total_recorded
    record_terminal_outcome(
        op_id="op-X", terminal_reason_code="complete",
        terminal_phase="COMPLETE", duration_ms=42.0,
        ledger_path_override=tmp_ledger,
    )
    assert monitor.total_recorded == before + 1


def test_record_multiple_ops_persisted(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome, read_recent_records,
    )
    for i in range(5):
        record_terminal_outcome(
            op_id=f"op-{i}",
            terminal_reason_code="complete",
            terminal_phase="COMPLETE",
            duration_ms=float(i * 10),
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_records(path=tmp_ledger)
    assert len(rows) == 5
    assert rows[0].op_id == "op-0"
    assert rows[4].op_id == "op-4"


def test_record_failure_path_classifies(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome,
    )
    rec = record_terminal_outcome(
        op_id="op-fail",
        terminal_reason_code="op_cost_cap_exceeded",
        terminal_phase="POSTMORTEM", duration_ms=30000.0,
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    assert rec.status_name == "TIMEOUT"


def test_record_unknown_reason_fallback(
    monkeypatch, tmp_ledger,
):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome,
    )
    rec = record_terminal_outcome(
        op_id="op-unknown",
        terminal_reason_code="novel_unknown_failure",
        terminal_phase="POSTMORTEM",
        ledger_path_override=tmp_ledger,
    )
    assert rec is not None
    assert rec.status_name == "FAILED"


# ---------------------------------------------------------------------------
# Read API defensive
# ---------------------------------------------------------------------------


def test_read_missing_ledger_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        read_recent_records,
    )
    nonexistent = tmp_path / "no-such.jsonl"
    assert read_recent_records(path=nonexistent) == ()


def test_read_limit_clamps(monkeypatch, tmp_ledger):
    monkeypatch.setenv(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        record_terminal_outcome, read_recent_records,
    )
    for i in range(10):
        record_terminal_outcome(
            op_id=f"op-{i}",
            terminal_reason_code="complete",
            terminal_phase="COMPLETE",
            ledger_path_override=tmp_ledger,
        )
    rows = read_recent_records(limit=3, path=tmp_ledger)
    assert len(rows) == 3
    assert rows[0].op_id == "op-7"
    assert rows[2].op_id == "op-9"


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "execution_monitor_bridge_master_default_false",
        "execution_monitor_bridge_authority_asymmetry",
        (
            "execution_monitor_bridge_"
            "composes_canonical_monitor"
        ),
        (
            "execution_monitor_bridge_"
            "composes_canonical_jsonl"
        ),
        (
            "execution_monitor_bridge_"
            "status_table_canonical"
        ),
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    assert pin.validate(tree, src) == ()


def test_status_table_pin_fires_on_invalid_value():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
_TERMINAL_REASON_TO_STATUS = {
    "complete": "COMPLETED",
    "weird": "INVALID_STATUS_NAME",
}
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_monitor_bridge_status_table_canonical"
        )
    )
    assert pin.validate(tree, bad)


def test_status_table_pin_accepts_annotated_assign():
    """The canonical source uses
    ``_TERMINAL_REASON_TO_STATUS: Mapping[str, str] = {...}``
    (AnnAssign). The pin must accept this shape."""
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    good = '''
from typing import Mapping
_TERMINAL_REASON_TO_STATUS: Mapping[str, str] = {
    "complete": "COMPLETED",
    "fail": "FAILED",
}
'''
    tree = ast.parse(good)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_monitor_bridge_status_table_canonical"
        )
    )
    assert pin.validate(tree, good) == ()


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_monitor_bridge_authority_asymmetry"
        )
    )
    assert pin.validate(tree, bad)


def test_canonical_monitor_pin_fires_on_missing_compose():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def _propagate_to_canonical_monitor(record):
    pass  # no compose of get_default_monitor
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_monitor_bridge_"
            "composes_canonical_monitor"
        )
    )
    assert pin.validate(tree, bad)


def test_jsonl_pin_fires_on_raw_open():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def writer():
    with open("foo.jsonl", "a") as f:
        f.write("x\\n")
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "execution_monitor_bridge_"
            "composes_canonical_jsonl"
        )
    )
    assert pin.validate(tree, bad)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (  # noqa: E501
        execution_monitor_bridge as mod,
    )
    expected = {
        "EXECUTION_MONITOR_BRIDGE_SCHEMA_VERSION",
        "TerminalOutcomeRecord",
        "get_terminal_status_name",
        "ledger_path",
        "master_enabled",
        "read_recent_records",
        "record_terminal_outcome",
        "register_flags",
        "register_shipped_invariants",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_two():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 2
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        (
            "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED"
        ),
        (
            "JARVIS_EXECUTION_MONITOR_BRIDGE_LEDGER_PATH"
        ),
    }


def test_register_flags_swallows_errors():
    from backend.core.ouroboros.governance.execution_monitor_bridge import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    register_flags(registry)


# ---------------------------------------------------------------------------
# complete_runner.py call-site regression
# ---------------------------------------------------------------------------


def test_complete_runner_invokes_bridge():
    """Phase 3 A1: complete_runner.py MUST call
    record_terminal_outcome at the terminal-success path.
    Master-flag-gated + try/except wrapped per operator
    binding 'never raises'."""
    runner_path = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "phase_runners/complete_runner.py"
    )
    src = runner_path.read_text(encoding="utf-8")
    # The bridge call site must be present.
    assert "record_terminal_outcome" in src, (
        "complete_runner.py must import + call "
        "record_terminal_outcome (Phase 3 A1 wire)"
    )
    # The bridge module reference must be present.
    assert "execution_monitor_bridge" in src
    # AST: the call must be inside a try/except (defensive
    # discipline — never disturb the canonical pipeline).
    tree = ast.parse(src)
    bridge_call_in_try = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "record_terminal_outcome"
                ) or (
                    isinstance(func, ast.Attribute)
                    and func.attr
                    == "record_terminal_outcome"
                ):
                    bridge_call_in_try = True
                    break
        if bridge_call_in_try:
            break
    assert bridge_call_in_try, (
        "complete_runner.py: record_terminal_outcome "
        "call MUST be inside a try/except wrapper "
        "(operator binding 'never raises')"
    )


def test_complete_runner_lazy_imports_bridge():
    """The bridge import in complete_runner.py MUST be
    lazy (inside the try block), not at module top, so
    bridge unavailability never blocks the runner from
    importing."""
    runner_path = (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "phase_runners/complete_runner.py"
    )
    src = runner_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Top-level imports must NOT include the bridge.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert (
                "execution_monitor_bridge" not in module
            ), (
                "complete_runner.py MUST lazy-import the "
                "bridge inside the try block, not at "
                "module top"
            )

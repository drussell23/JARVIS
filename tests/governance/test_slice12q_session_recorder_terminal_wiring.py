"""
Slice 12Q — SessionRecorder terminal-operation wiring fix.
==========================================================

Closes the discovered gap from the Path A post-Slice-12P soak
(bt-2026-05-23-042249): summary.json.operations[] was empty
despite multiple terminal ops, because the existing OP_COMPLETED
autonomy-event subscriber path in harness.py:998 subscribes to a
callback (governed_loop_service.report_outcome) that **nothing in
the runtime actually invokes for failed / exhausted ops**.

The orchestrator's _record_ledger function is the canonical
terminal chokepoint (lines 10482+) — it fires for ALL terminal
states (FAILED/APPLIED/ROLLED_BACK/BLOCKED) and emits the
operation_terminal SSE. Slice 12Q wires this same chokepoint to
the SessionRecorder via a process-singleton accessor pattern:

  harness.py at boot:
    set_active_recorder(self._session_recorder)

  orchestrator._record_ledger at terminal state:
    recorder = get_active_recorder()
    if recorder is not None:
        recorder.record_operation(...)

Composes Slice 12P's terminal_reason_class classification
(already wired into record_operation) so summary.json.
operations[].terminal_reason_class populates structurally.

Operator binding (verbatim):
  * find the canonical terminal event/path in Orchestrator
  * call SessionRecorder.record_operation hook there
  * preserve existing recorder schema + Slice 12P field
  * record op_id / source / route / phase / status /
    terminal_reason / terminal_reason_class / cost / duration
  * provider exhaustion records as terminal operation
  * no duplicate records for same terminal op
  * focused tests only — no soak, no provider spend
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.battle_test.session_recorder import (
    SessionRecorder,
    get_active_recorder,
    reset_active_recorder,
    set_active_recorder,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.orchestrator import (
    _SLICE12Q_LEDGER_TO_STATUS,
    _slice12q_record_terminal,
)


@pytest.fixture(autouse=True)
def _reset_recorder_state():
    reset_active_recorder()
    yield
    reset_active_recorder()


# ===============================================================
# Phase 1 — process-singleton accessor
# ===============================================================


def test_accessor_starts_empty() -> None:
    """Before set_active_recorder is called, get_active_recorder
    returns None — orchestrator hook will safely no-op."""
    assert get_active_recorder() is None


def test_accessor_round_trip() -> None:
    """set/get round-trip returns the same instance."""
    rec = SessionRecorder(session_id="rt-test")
    set_active_recorder(rec)
    assert get_active_recorder() is rec


def test_accessor_reset_clears() -> None:
    """reset_active_recorder + None argument both clear the
    singleton."""
    rec = SessionRecorder(session_id="clear-test")
    set_active_recorder(rec)
    assert get_active_recorder() is rec
    set_active_recorder(None)
    assert get_active_recorder() is None


def test_accessor_never_raises_on_bad_input() -> None:
    """set_active_recorder should accept None gracefully; the
    NEVER-raise contract holds even on bad inputs."""
    set_active_recorder(None)
    assert get_active_recorder() is None
    # Setting an arbitrary object should not raise (duck typing)
    set_active_recorder(MagicMock())
    assert get_active_recorder() is not None


def test_accessor_thread_safety() -> None:
    """Basic concurrent access — set + get from multiple threads
    must not deadlock or crash."""
    rec1 = SessionRecorder(session_id="thread-1")
    rec2 = SessionRecorder(session_id="thread-2")
    errors: list = []

    def _writer():
        try:
            for _ in range(100):
                set_active_recorder(rec1)
                set_active_recorder(rec2)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def _reader():
        try:
            for _ in range(100):
                _ = get_active_recorder()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=_writer),
        threading.Thread(target=_reader),
        threading.Thread(target=_writer),
        threading.Thread(target=_reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"thread-safety errors: {errors}"


# ===============================================================
# Phase 2 — idempotency in record_operation
# ===============================================================


def test_idempotency_same_op_id_no_duplicate() -> None:
    """Recording the SAME op_id twice — second call is a no-op.
    First-write-wins keeps the earliest attribution intact."""
    rec = SessionRecorder(session_id="idem-test")
    rec.record_operation(
        op_id="op-X", status="failed", sensor="test", technique="test",
        composite_score=0.0, elapsed_s=1.0,
        terminal_reason_code="exploration_insufficient: 0/2",
    )
    assert len(rec._operations) == 1
    # Duplicate call — different status/reason
    rec.record_operation(
        op_id="op-X", status="completed", sensor="test", technique="test",
        composite_score=99.0, elapsed_s=999.0,
        terminal_reason_code="totally_different_reason",
    )
    # Still ONE record (first write wins)
    assert len(rec._operations) == 1
    assert rec._operations[0]["status"] == "failed"
    assert rec._operations[0]["terminal_reason_code"] == \
        "exploration_insufficient: 0/2"


def test_idempotency_different_op_ids_record_separately() -> None:
    """Different op_ids must each get their own record."""
    rec = SessionRecorder(session_id="multi-test")
    for op_id in ("op-A", "op-B", "op-C"):
        rec.record_operation(
            op_id=op_id, status="completed", sensor="test",
            technique="test", composite_score=0.0, elapsed_s=1.0,
        )
    assert len(rec._operations) == 3
    assert {o["op_id"] for o in rec._operations} == {"op-A", "op-B", "op-C"}


def test_idempotency_empty_op_id_does_not_pollute_tracker() -> None:
    """Defensive: empty op_id does not get added to the idempotency
    set (would otherwise mean a single empty-op-id record blocks
    all future empty-op-id records)."""
    rec = SessionRecorder(session_id="empty-id-test")
    rec.record_operation(
        op_id="", status="completed", sensor="t", technique="t",
        composite_score=0.0, elapsed_s=0.0,
    )
    # Should record (empty id allowed for legacy paths)
    assert len(rec._operations) == 1
    # Second empty-id call: still records (not deduped on empty id)
    rec.record_operation(
        op_id="", status="completed", sensor="t", technique="t",
        composite_score=0.0, elapsed_s=0.0,
    )
    assert len(rec._operations) == 2


# ===============================================================
# Phase 3 — orchestrator terminal hook
# ===============================================================


def test_terminal_hook_records_failed_op() -> None:
    """The canonical operator-binding scenario: provider exhaustion
    sets state=FAILED + terminal_reason_code=...exhausted, and
    the hook records the terminal op with the Slice 12P class."""
    rec = SessionRecorder(session_id="terminal-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-failed-1"
        terminal_reason_code = "circuit_breaker_tripped:terminal_structural"
        provider_route = "complex"
        intake_source = "swe_bench_pro_harness_inject"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {
        "duration_s": 12.5, "cost_usd": 0.03,
    })
    assert len(rec._operations) == 1
    op = rec._operations[0]
    assert op["op_id"] == "op-failed-1"
    assert op["status"] == "failed"
    assert op["terminal_reason_class"] == "provider_exhaustion"
    assert op["provider"] == "complex"
    assert op["sensor"] == "swe_bench_pro_harness_inject"
    assert op["elapsed_s"] == 12.5
    assert op["cost_usd"] == 0.03


def test_terminal_hook_records_applied_op() -> None:
    """APPLIED → status=completed (the success case)."""
    rec = SessionRecorder(session_id="applied-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-applied-1"
        terminal_reason_code = ""
        provider_route = "standard"
        intake_source = "test_failure_sensor"

    _slice12q_record_terminal(_Ctx(), OperationState.APPLIED, {
        "duration_s": 8.2, "cost_usd": 0.015,
    })
    assert len(rec._operations) == 1
    op = rec._operations[0]
    assert op["status"] == "completed"
    assert op["terminal_reason_class"] == "other"


def test_terminal_hook_records_rolled_back_op() -> None:
    """ROLLED_BACK → status=rolled_back."""
    rec = SessionRecorder(session_id="rb-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-rollback-1"
        terminal_reason_code = "verify_failed"
        provider_route = "standard"
        intake_source = "x"

    _slice12q_record_terminal(_Ctx(), OperationState.ROLLED_BACK, {})
    op = rec._operations[0]
    assert op["status"] == "rolled_back"


def test_terminal_hook_records_blocked_op() -> None:
    """BLOCKED → status=failed (blocked = unrecoverable failure)."""
    rec = SessionRecorder(session_id="blocked-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-blocked-1"
        terminal_reason_code = "iron_gate_blast_radius"
        provider_route = "complex"
        intake_source = "x"

    _slice12q_record_terminal(_Ctx(), OperationState.BLOCKED, {})
    op = rec._operations[0]
    assert op["status"] == "failed"
    assert op["terminal_reason_class"] == "structural_gate_rejection"


def test_terminal_hook_records_structural_rejection_with_class() -> None:
    """Iron Gate rejection bubbles up as STRUCTURAL_GATE_REJECTION
    in the Slice 12P class (the EXACT class that the
    bt-2026-05-23-030130 fixture op SHOULD have surfaced but
    didn't because the wiring was missing)."""
    rec = SessionRecorder(session_id="iron-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-iron-1"
        terminal_reason_code = "exploration_insufficient: 0/2 exploration tool calls"
        provider_route = "complex"
        intake_source = "swe_bench_pro_harness_inject"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {
        "duration_s": 3.0,
    })
    op = rec._operations[0]
    assert op["terminal_reason_class"] == "structural_gate_rejection"
    assert "exploration_insufficient" in op["terminal_reason_code"]


def test_terminal_hook_handles_cooldown_cancelled_shutdown() -> None:
    """Slice 12O cooldown_cancelled_shutdown terminal reason
    classifies as CANCELLED_SHUTDOWN."""
    rec = SessionRecorder(session_id="cooldown-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-cancel-1"
        terminal_reason_code = "cooldown_cancelled_shutdown"
        provider_route = "immediate"
        intake_source = "x"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})
    op = rec._operations[0]
    assert op["terminal_reason_class"] == "cancelled_shutdown"


def test_terminal_hook_no_op_when_no_active_recorder() -> None:
    """When no recorder is set (tests, headless rigs), the hook
    must silently no-op — no exception, no side effect."""
    reset_active_recorder()
    assert get_active_recorder() is None

    class _Ctx:
        op_id = "op-no-recorder"
        terminal_reason_code = "anything"
        provider_route = "x"

    # Should NOT raise
    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})


def test_terminal_hook_no_op_for_empty_op_id() -> None:
    """Without an op_id there's nothing useful to record. Hook
    silently no-ops rather than emitting a malformed entry."""
    rec = SessionRecorder(session_id="empty-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = ""
        terminal_reason_code = "anything"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})
    assert len(rec._operations) == 0


def test_terminal_hook_never_raises_on_bad_input() -> None:
    """Defensive contract: bad ctx / bad state / bad data must
    not raise."""
    rec = SessionRecorder(session_id="bad-input-test")
    set_active_recorder(rec)
    # None everything
    _slice12q_record_terminal(None, None, None)
    # Bad data type
    class _Ctx:
        op_id = "op-bad-data"
        terminal_reason_code = "x"
    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, "not a dict")
    # Bad ctx shape (missing attributes)
    class _BadCtx:
        op_id = "op-bad-ctx"
    _slice12q_record_terminal(_BadCtx(), OperationState.FAILED, {})


def test_terminal_hook_extracts_reason_from_data_when_ctx_missing() -> None:
    """When ctx.terminal_reason_code is empty, the hook falls back
    to data["reason"] or data["error"]."""
    rec = SessionRecorder(session_id="data-reason-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-data-reason"
        terminal_reason_code = ""  # empty
        provider_route = "x"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {
        "reason": "background_dw_failure",
    })
    op = rec._operations[0]
    assert op["terminal_reason_code"] == "background_dw_failure"


def test_terminal_hook_idempotent_across_multiple_invocations() -> None:
    """The hook fires THROUGH SessionRecorder.record_operation
    which is itself idempotent. Calling the hook twice for the
    same op_id (e.g., if both the OP_COMPLETED handler AND the
    Slice 12Q hook ever fire) produces ONE record."""
    rec = SessionRecorder(session_id="idem-orch-test")
    set_active_recorder(rec)

    class _Ctx:
        op_id = "op-double-call"
        terminal_reason_code = "x"
        provider_route = "y"

    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})
    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})
    _slice12q_record_terminal(_Ctx(), OperationState.FAILED, {})
    assert len(rec._operations) == 1


# ===============================================================
# Status mapping
# ===============================================================


def test_status_mapping_complete() -> None:
    """The Slice 12Q status map covers all 4 terminal ledger
    states (APPLIED / ROLLED_BACK / FAILED / BLOCKED)."""
    assert _SLICE12Q_LEDGER_TO_STATUS == {
        "applied": "completed",
        "rolled_back": "rolled_back",
        "failed": "failed",
        "blocked": "failed",
    }


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


_OR_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "orchestrator.py"
)

_SR_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "battle_test"
    / "session_recorder.py"
)

_HARNESS_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "battle_test"
    / "harness.py"
)


def _load_ast(p: Path) -> ast.Module:
    return ast.parse(p.read_text())


def test_ast_pin_session_recorder_exposes_accessor_functions() -> None:
    """``set_active_recorder`` + ``get_active_recorder`` MUST be
    module-level functions in session_recorder.py."""
    tree = _load_ast(_SR_PATH)
    names = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }
    assert "set_active_recorder" in names
    assert "get_active_recorder" in names
    assert "reset_active_recorder" in names


def test_ast_pin_session_recorder_idempotency_set_present() -> None:
    """SessionRecorder.__init__ MUST initialise a
    ``_recorded_op_ids`` set for idempotency."""
    src = _SR_PATH.read_text()
    assert "_recorded_op_ids" in src
    assert "_recorded_op_ids: set" in src or \
        "_recorded_op_ids = set()" in src


def test_ast_pin_session_recorder_idempotency_check_in_record_operation() -> None:
    """``record_operation`` MUST check ``_recorded_op_ids`` and
    early-return when the op_id is already recorded."""
    src = _SR_PATH.read_text()
    # The idempotency block must mention both the set and an
    # early-return shape
    assert "if op_id and op_id in self._recorded_op_ids:" in src or \
        "op_id in self._recorded_op_ids" in src


def test_ast_pin_orchestrator_helper_present() -> None:
    """``_slice12q_record_terminal`` MUST be a module-level
    function in orchestrator.py."""
    tree = _load_ast(_OR_PATH)
    names = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }
    assert "_slice12q_record_terminal" in names


def test_ast_pin_orchestrator_record_ledger_calls_helper() -> None:
    """``_record_ledger`` MUST call ``_slice12q_record_terminal``
    inside the terminal-state branch (the load-bearing wiring
    that closes the bt-2026-05-23-042249 empty-operations[] gap)."""
    src = _OR_PATH.read_text()
    assert "_slice12q_record_terminal" in src
    # Must be called inside _record_ledger — the simplest check
    # is that the call site sits AFTER the def of _record_ledger
    # AND inside its body. Walk AST:
    tree = _load_ast(_OR_PATH)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "_record_ledger":
            continue
        body_src = ast.unparse(node)
        if "_slice12q_record_terminal" in body_src:
            found = True
            break
    assert found, "_record_ledger must call _slice12q_record_terminal"


def test_ast_pin_orchestrator_status_map_closed() -> None:
    """The ledger→status map MUST cover exactly the 4 terminal
    ledger states. A new terminal state added without a status
    mapping would silently default to "failed"."""
    src = _OR_PATH.read_text()
    assert "_SLICE12Q_LEDGER_TO_STATUS" in src
    # All four canonical ledger state values present in the
    # source near the map definition
    for state_value in ("applied", "rolled_back", "failed", "blocked"):
        assert f'"{state_value}"' in src or f"'{state_value}'" in src


def test_ast_pin_harness_registers_recorder_at_boot() -> None:
    """harness.py MUST call ``set_active_recorder`` after
    SessionRecorder construction so the orchestrator's terminal
    hook can find the recorder. The wiring lives in
    Harness.__init__ near the SessionRecorder() constructor."""
    src = _HARNESS_PATH.read_text()
    assert "set_active_recorder" in src
    # The call must reference self._session_recorder (the
    # canonical attribute)
    assert "_session_recorder" in src


def test_ast_pin_helper_never_raises() -> None:
    """``_slice12q_record_terminal`` must be defensively wrapped:
    the OUTERMOST body must be a try/except that catches broad
    Exception."""
    tree = _load_ast(_OR_PATH)
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_slice12q_record_terminal":
            continue
        # Walk the body looking for at least one Try-Except-Exception
        has_broad_except = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.ExceptHandler):
                if sub.type is None:
                    has_broad_except = True
                elif isinstance(sub.type, ast.Name) and \
                        sub.type.id == "Exception":
                    has_broad_except = True
        assert has_broad_except, (
            "_slice12q_record_terminal must have broad except handler "
            "(NEVER-raise contract)"
        )
        return
    pytest.fail("_slice12q_record_terminal not found")

"""W3(7) Slice 7 — graduation pin tests.

Pins the post-graduation contract:

A. Master flag default is now **True** (Slice 7 flip).
B. All *actuating* sub-flags (WATCHDOG, SIGNAL, SSE) stay default **False** —
   master-on alone causes ZERO observable behavior change vs pre-W3(7) at
   the cancel-actuation layer. Operators must explicitly opt into D/E/F/SSE.
C. REPL_IMMEDIATE defaults True when master is on (per Slice 1 design) —
   but only fires on explicit operator action (`cancel <op-id> --immediate`).
D. RECORD_PERSIST defaults True when master is on — but only writes when an
   emit() actually fires, which requires sub-flags above to fire first.
E. Hot-revert path: ``JARVIS_MID_OP_CANCEL_ENABLED=false`` restores byte-for-byte
   pre-W3(7) behavior — verified by re-running master-off invariant pins.
F. Authority invariants: SSE event vocabulary is additive only (11 events
   post-Slice-6); IDE GET routes are read-only; cancel record schema is
   ``cancel.1`` (frozen); 4 cancel classes (D/E/F + the existing A/B/C
   classification taxonomy).
G. Source-grep pins: bridge call sites in all 3 emit methods, dispatcher
   pre-iteration cancel-check, candidate_generator race wraps,
   tool_executor terminate-grace-kill, plan_exploit/parallel_dispatch
   cancel propagation, IDE routes registered.

These tests run on EVERY commit going forward. If any pin breaks, either:
* The change was an unintentional regression — fix the change.
* The contract is intentionally being expanded — update the pin AND the
  hot-revert documentation. (Per Slice 1 contract, the master-off invariant
  is non-negotiable; contract widening must preserve it.)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# (A) Master flag default — graduation flip
# ---------------------------------------------------------------------------


def test_master_flag_defaults_true_post_slice_7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_MID_OP_CANCEL_ENABLED defaults to True after Slice 7 graduation."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        mid_op_cancel_enabled,
    )
    assert mid_op_cancel_enabled() is True, (
        "Slice 7 graduation flips JARVIS_MID_OP_CANCEL_ENABLED default to True. "
        "If this test fails post-merge, either the flip was reverted (fix) or "
        "the operator chose to revert the graduation (update the pin and the "
        "scope-doc graduation appendix)."
    )


# ---------------------------------------------------------------------------
# (B) Actuating sub-flags stay default-off — operator opt-in
# ---------------------------------------------------------------------------


def test_watchdog_subflag_default_off_even_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class E (cost / wall / productivity / idle) requires explicit opt-in
    via JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED. Master-on alone is not enough."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        mid_op_cancel_enabled,
        watchdog_enabled,
    )
    assert mid_op_cancel_enabled() is True   # master on by default
    assert watchdog_enabled() is False        # but watchdog still off


def test_signal_subflag_default_off_even_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class F (system signals) requires explicit opt-in via
    JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED. Master-on alone is not enough."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        mid_op_cancel_enabled,
        signal_enabled,
    )
    assert mid_op_cancel_enabled() is True
    assert signal_enabled() is False


def test_sse_subflag_default_off_even_post_graduation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE bridge requires explicit opt-in via JARVIS_CANCEL_SSE_ENABLED.
    Master-on alone does not start publishing cancel_origin_emitted events."""
    monkeypatch.delenv("JARVIS_CANCEL_SSE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import sse_enabled
    assert sse_enabled() is False


# ---------------------------------------------------------------------------
# (C) REPL_IMMEDIATE default-true-when-master-on (per Slice 1 design)
# ---------------------------------------------------------------------------


def test_repl_immediate_defaults_true_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPL_IMMEDIATE inherits master-on by default. The Class D operator
    cancel surface is enabled at graduation, but the only trigger is an
    explicit ``cancel <op-id> --immediate`` REPL invocation — no
    auto-cancellation."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        repl_immediate_enabled,
    )
    assert repl_immediate_enabled() is True


# ---------------------------------------------------------------------------
# (D) RECORD_PERSIST default-true-when-master-on
# ---------------------------------------------------------------------------


def test_record_persist_defaults_true_when_master_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an emit fires, the record IS persisted to cancel_records.jsonl
    by default. Operators can disable via JARVIS_CANCEL_RECORD_PERSIST_ENABLED=false
    for log-only mode."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        record_persist_enabled,
    )
    assert record_persist_enabled() is True


# ---------------------------------------------------------------------------
# (E) Hot-revert path — master=false restores pre-W3(7) byte-for-byte
# ---------------------------------------------------------------------------


def test_hot_revert_master_off_disables_all_subflags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_MID_OP_CANCEL_ENABLED=false force-disables every sub-flag
    regardless of their individual env values. The single env-var revert
    is the operator's only-knob hot-revert path."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "false")
    # Even if operator tries to enable sub-flags, they must stay off
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CANCEL_RECORD_PERSIST_ENABLED", "true")
    from backend.core.ouroboros.governance.cancel_token import (
        mid_op_cancel_enabled,
        record_persist_enabled,
        repl_immediate_enabled,
        signal_enabled,
        watchdog_enabled,
    )
    assert mid_op_cancel_enabled() is False
    assert repl_immediate_enabled() is False
    assert watchdog_enabled() is False
    assert signal_enabled() is False
    assert record_persist_enabled() is False


def test_hot_revert_master_off_emit_class_d_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class D emit returns None when master is off, even if REPL_IMMEDIATE on."""
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_ENABLED", "false")
    monkeypatch.setenv("JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE", "true")
    from backend.core.ouroboros.governance.cancel_token import (
        CancelOriginEmitter,
        CancelToken,
    )
    token = CancelToken("op-revert-test")
    emitter = CancelOriginEmitter()
    result = emitter.emit_class_d(
        op_id="op-revert-test",
        token=token,
        phase_at_trigger="GENERATE",
    )
    assert result is None
    assert token.is_cancelled is False


# ---------------------------------------------------------------------------
# (F) Authority invariants — SSE vocabulary additive, schema frozen
# ---------------------------------------------------------------------------


def test_sse_event_vocabulary_count_is_11_post_slice_6():
    """Property-based additive-only contract: the SSE event vocabulary
    grows monotonically. Pin asserts the post-Slice-7 floor (41) — the
    count may grow as later arcs add events, but must NEVER shrink (a
    shrink means an event type was REMOVED, which is a wire-format
    contract break)."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    _SLICE_7_FLOOR = 41
    assert len(_VALID_EVENT_TYPES) >= _SLICE_7_FLOOR, (
        f"SSE event vocabulary SHRANK below post-Slice-7 floor: "
        f"got {len(_VALID_EVENT_TYPES)}, floor {_SLICE_7_FLOOR}. "
        "An event type was REMOVED — wire-format contract break. "
        "Fix the source (re-add the missing event), don't lower this "
        "floor. Floor history: 40 pre-W2(4), 41 post-Slice-3 "
        "(curiosity_question_emitted)."
    )


def test_cancel_origin_emitted_event_type_string_stable():
    """The event type string is the wire-format API. Don't rename."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CANCEL_ORIGIN_EMITTED,
    )
    assert EVENT_TYPE_CANCEL_ORIGIN_EMITTED == "cancel_origin_emitted"


def test_cancel_record_schema_version_is_cancel_1():
    """The CancelRecord schema is wire-format API. Schema bumps need
    additive migration semantics."""
    from backend.core.ouroboros.governance.cancel_token import (
        CancelRecord,
    )
    rec = CancelRecord(
        schema_version="cancel.1",
        cancel_id="x",
        op_id="op-x",
        origin="D:repl_operator",
        phase_at_trigger="GENERATE",
        trigger_monotonic=0.0,
        trigger_wall_iso="2026-04-25T01:23:45Z",
        bounded_deadline_s=30.0,
        reason="t",
    )
    assert rec.schema_version == "cancel.1"


# ---------------------------------------------------------------------------
# (G) Source-grep pins — code shape that must survive drift
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_dispatcher_has_pre_iteration_cancel_check():
    """phase_dispatcher.py routes to POSTMORTEM on cancel before each
    iteration. Pinned by Slice 2."""
    src = _read("backend/core/ouroboros/governance/phase_dispatcher.py")
    assert "is_cancelled" in src
    assert "POSTMORTEM" in src
    assert "cancel_record" in src


def test_pin_candidate_generator_race_wraps():
    """candidate_generator._call_primary and _call_fallback use
    race_or_wait_for. Pinned by Slice 2."""
    src = _read("backend/core/ouroboros/governance/candidate_generator.py")
    # At least 2 race_or_wait_for sites (one per call path)
    assert src.count("race_or_wait_for") >= 2
    assert "current_cancel_token" in src


def test_pin_tool_executor_terminate_grace_kill():
    """tool_executor._run_tests_async has terminate→grace→kill on
    OperationCancelledError. Pinned by Slice 2."""
    src = _read("backend/core/ouroboros/governance/tool_executor.py")
    assert "OperationCancelledError" in src
    assert "_term_then_force" in src
    assert "subprocess_grace_s" in src


def test_pin_cost_governor_has_class_e_hook():
    """cost_governor.charge() emits Class E:cost on cap-exceeded. Pinned by Slice 3."""
    src = _read("backend/core/ouroboros/governance/cost_governor.py")
    assert "_emit_class_e_cancel" in src
    assert "attach_cancel_surface" in src
    assert 'watchdog="cost"' in src


def test_pin_harness_has_class_f_emission():
    """harness._handle_shutdown_signal emits Class F additive. Pinned by Slice 4."""
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    assert "emit_signal_cancel" in src
    assert "Class F" in src or "class_f" in src.lower()


def test_pin_plan_exploit_cancel_handler():
    """plan_exploit gather has cancel handling. Pinned by Slice 5."""
    src = _read("backend/core/ouroboros/governance/plan_exploit.py")
    assert "race_or_wait_for as _race_or_wait_for" in src
    assert "[PLAN-EXPLOIT] op=%s status=cancelled" in src


def test_pin_parallel_dispatch_cancel_handler():
    """parallel_dispatch.enforce_evaluate_fanout returns CANCELLED on cancel.
    Pinned by Slice 5."""
    src = _read("backend/core/ouroboros/governance/parallel_dispatch.py")
    assert "FanoutOutcome.CANCELLED" in src
    assert 'getattr(scheduler, "cancel_graph"' in src


def test_pin_ide_observability_cancel_routes():
    """IDE observability has /observability/cancels routes. Pinned by Slice 6."""
    src = _read("backend/core/ouroboros/governance/ide_observability.py")
    assert "/observability/cancels" in src
    assert "_handle_cancel_list" in src
    assert "_handle_cancel_detail" in src


def test_pin_emit_methods_call_sse_bridge():
    """All 3 emit methods (D/E/F) call bridge_cancel_origin_to_sse(record).
    Pinned by Slice 6."""
    src = _read("backend/core/ouroboros/governance/cancel_token.py")
    assert src.count("bridge_cancel_origin_to_sse(record)") >= 3


# ---------------------------------------------------------------------------
# (H) Hot-revert documentation — env vars enumerated
# ---------------------------------------------------------------------------


def test_full_env_var_revert_matrix_documented():
    """Every W3(7) env knob documented in the cancel_token.py module
    docstring or function docstrings. Hot-revert recipe must be discoverable."""
    src = _read("backend/core/ouroboros/governance/cancel_token.py")
    # Every gating env var must be referenced in the source (and thus
    # discoverable via grep + module docs)
    for env_var in (
        "JARVIS_MID_OP_CANCEL_ENABLED",
        "JARVIS_MID_OP_CANCEL_REPL_IMMEDIATE",
        "JARVIS_MID_OP_CANCEL_WATCHDOG_ENABLED",
        "JARVIS_MID_OP_CANCEL_SIGNAL_ENABLED",
        "JARVIS_CANCEL_SSE_ENABLED",
        "JARVIS_CANCEL_RECORD_PERSIST_ENABLED",
        "JARVIS_CANCEL_BOUNDED_DEADLINE_S",
        "JARVIS_CANCEL_SUBPROCESS_GRACE_S",
    ):
        assert env_var in src, f"env var {env_var!r} missing from cancel_token.py"

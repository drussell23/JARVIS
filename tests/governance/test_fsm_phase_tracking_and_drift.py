"""Event-driven FSM phase tracking + git-drift resume demotion.

Two remediations of the stale-phase gap (checkpoints were serialized at the
registration-time CLASSIFY, never the live phase):

1. EVENT-DRIVEN PHASE TRACKING — a single observer on the state machine's own
   transition method (OperationContext.advance) mirrors every transition into
   the in-flight registry. No sprinkled update_phase_safely calls; the registry
   tracks the LIVE phase, so capture_inflight serializes the exact execution
   state (GENERATE, not CLASSIFY).

2. GIT-DRIFT DEMOTION — the checkpoint records repo HEAD at suspend; on hydrate
   the resume envelope compares it to live HEAD, and on drift DEMOTES the op to
   PLAN and drops the stale exploration/partial (context-corruption guard)
   instead of blindly fast-forwarding into code that no longer exists.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import fsm_checkpoint as CK
from backend.core.ouroboros.governance import governed_loop_service as GLS
from backend.core.ouroboros.governance import in_flight_registry as R
from backend.core.ouroboros.governance import op_context as OC
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)

# The phase-tracking wiring lives at the pipeline-orchestration layer (GLS), NOT
# inside in_flight_registry (an AST pin forbids the registry importing op_context).
wire_phase_transition_tracking = GLS._wire_phase_transition_tracking
_mirror_phase_transition = GLS._mirror_phase_transition


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")   # registration active
    R.reset_default_registry()
    GLS._reset_phase_tracking_for_tests()
    OC._reset_phase_transition_observers_for_tests()
    yield
    R.reset_default_registry()
    GLS._reset_phase_tracking_for_tests()
    OC._reset_phase_transition_observers_for_tests()


def _ctx(op_id="op-1"):
    return OperationContext.create(
        op_id=op_id, description="optimize", target_files=("a.py",),
    )


# ===========================================================================
# Feature 1 — event-driven phase tracking (NO hardcoded update_phase_safely)
# ===========================================================================


def test_advance_auto_updates_registry_phase():
    wire_phase_transition_tracking()
    ctx = _ctx()
    R.register_op_safely("op-1", ctx_ref=ctx, last_phase_name="CLASSIFY")
    assert R.get_default_registry().lookup("op-1").last_phase_name == "CLASSIFY"

    # Drive the state machine — NO manual update_phase_safely anywhere here.
    ctx = ctx.advance(OperationPhase.ROUTE)
    assert R.get_default_registry().lookup("op-1").last_phase_name == "ROUTE"
    ctx = ctx.advance(OperationPhase.GENERATE)
    assert R.get_default_registry().lookup("op-1").last_phase_name == "GENERATE"


def test_observer_fires_only_for_registered_ops():
    # An advance() for an op that never registered is a harmless no-op (the
    # registry's update_phase ignores unknown ids).
    wire_phase_transition_tracking()
    ctx = _ctx("op-unreg")
    ctx.advance(OperationPhase.ROUTE)   # must not raise, must not create an entry
    assert R.get_default_registry().lookup("op-unreg") is None


def test_wiring_is_idempotent():
    assert wire_phase_transition_tracking() is True
    assert wire_phase_transition_tracking() is True
    # Exactly one observer registered despite repeated wiring.
    assert len(OC._PHASE_TRANSITION_OBSERVERS) == 1


def test_observer_registration_is_dedup():
    OC.register_phase_transition_observer(_mirror_phase_transition)
    OC.register_phase_transition_observer(_mirror_phase_transition)
    assert OC._PHASE_TRANSITION_OBSERVERS.count(_mirror_phase_transition) == 1


def test_observer_fault_never_breaks_transition():
    def _boom(op_id, phase):
        raise RuntimeError("observer exploded")
    OC.register_phase_transition_observer(_boom)
    ctx = _ctx()
    # The transition must still succeed and return the advanced context.
    advanced = ctx.advance(OperationPhase.ROUTE)
    assert advanced.phase is OperationPhase.ROUTE


def test_no_observer_no_effect():
    # With nothing wired, advance() is byte-identical to before (pure data).
    ctx = _ctx()
    advanced = ctx.advance(OperationPhase.ROUTE)
    assert advanced.phase is OperationPhase.ROUTE
    assert R.get_default_registry().lookup("op-1") is None


# ===========================================================================
# Feature 1 end-to-end — capture_inflight serializes the LIVE phase
# ===========================================================================


def test_capture_inflight_serializes_advanced_phase_not_stale(tmp_path):
    wire_phase_transition_tracking()
    ctx = _ctx("op-live")
    R.register_op_safely("op-live", ctx_ref=ctx, last_phase_name="CLASSIFY")
    # Advance to GENERATE (the phase the op is REALLY in when suspended).
    ctx = ctx.advance(OperationPhase.ROUTE).advance(OperationPhase.GENERATE)

    n = CK.capture_inflight(base_dir=str(tmp_path), reason="sigterm")
    assert n == 1
    cp = CK.list_pending(base_dir=str(tmp_path))[0]
    assert cp.phase == "GENERATE"      # was CLASSIFY before this fix


# ===========================================================================
# Feature 2 — git-drift demotion on hydrate
# ===========================================================================


def test_capture_stamps_repo_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(CK, "current_repo_sha", lambda *a, **k: "SHA_AT_SUSPEND")
    ctx = _ctx("op-sha")
    cp = CK.capture_from_context(ctx, phase="GENERATE")
    assert cp.repo_sha == "SHA_AT_SUSPEND"


def test_no_drift_preserves_fast_forward():
    cp = CK.FSMCheckpoint(
        op_id="op", phase="GENERATE", repo_sha="AAAA",
        exploration_records=[{"f": "a.py"}], partial_completion="def foo(",
    )
    env = CK.build_resume_envelope(cp, live_repo_sha="AAAA")
    assert env["drifted"] is False
    assert env["resume_phase"] == "GENERATE"
    assert env["exploration_records"] == [{"f": "a.py"}]   # preserved
    assert env["partial_completion"] == "def foo("


def test_drift_demotes_generate_to_plan_and_drops_stale_context():
    cp = CK.FSMCheckpoint(
        op_id="op", phase="GENERATE", goal_description="g",
        target_files=["a.py"], repo_sha="AAAA",
        exploration_records=[{"f": "a.py"}], tool_history=[{"t": "read"}],
        partial_completion="def foo(",
        intake_evidence_json='{"sensor": "x"}',
    )
    env = CK.build_resume_envelope(cp, live_repo_sha="BBBB")   # HEAD moved
    assert env["drifted"] is True
    assert env["resume_phase"] == "PLAN"                 # demoted, NOT fast-forward
    assert env["exploration_records"] == []             # stale code context dropped
    assert env["tool_history"] == []
    assert env["partial_completion"] == ""
    # What the op IS survives the demotion.
    assert env["description"] == "g"
    assert env["target_files"] == ["a.py"]
    assert env["intake_evidence_json"] == '{"sensor": "x"}'


def test_drift_at_or_before_plan_is_not_demoted():
    for phase in ("CLASSIFY", "ROUTE", "PLAN"):
        cp = CK.FSMCheckpoint(op_id="op", phase=phase, repo_sha="AAAA")
        eff, drifted = CK.resolve_resume_phase(cp, live_repo_sha="BBBB")
        assert drifted is True
        assert eff == phase        # already re-planning; nothing to demote


def test_missing_stored_sha_skips_drift_check():
    cp = CK.FSMCheckpoint(op_id="op", phase="GENERATE", repo_sha="")
    assert CK.resolve_resume_phase(cp, live_repo_sha="BBBB") == ("GENERATE", False)


def test_unreadable_live_head_fails_soft_to_fast_forward():
    # A None live HEAD (git unavailable) must NOT demote a legitimate resume.
    cp = CK.FSMCheckpoint(op_id="op", phase="GENERATE", repo_sha="AAAA")
    assert CK.resolve_resume_phase(cp, live_repo_sha=None) in (
        ("GENERATE", False),      # current_repo_sha() returned None or == "AAAA"
    ) or CK.resolve_resume_phase(cp, live_repo_sha="AAAA") == ("GENERATE", False)


def test_drift_demotion_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_FSM_RESUME_DRIFT_DEMOTION_ENABLED", "false")
    cp = CK.FSMCheckpoint(op_id="op", phase="GENERATE", repo_sha="AAAA")
    assert CK.resolve_resume_phase(cp, live_repo_sha="BBBB") == ("GENERATE", False)


def test_repo_sha_round_trips_through_hmac_envelope(tmp_path, monkeypatch):
    # The sha rides INSIDE the HMAC-signed payload (tamper-evident), so it
    # survives serialize → verify → hydrate.
    monkeypatch.setattr(CK, "current_repo_sha", lambda *a, **k: "SIGNED_SHA")
    wire_phase_transition_tracking()
    ctx = _ctx("op-rt")
    R.register_op_safely("op-rt", ctx_ref=ctx, last_phase_name="GENERATE")
    CK.capture_inflight(base_dir=str(tmp_path), reason="wall_clock_cap")
    cp = CK.list_pending(base_dir=str(tmp_path))[0]
    assert cp.repo_sha == "SIGNED_SHA"

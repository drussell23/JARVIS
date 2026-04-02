"""
Tests for backend.hive.cognitive_fsm

Covers:
- Initial state is BASELINE
- All valid transitions with correct reason_codes
- Noop transitions (SPINDOWN from BASELINE, FLOW_TRIGGER while in FLOW, REM_TRIGGER while in FLOW)
- Safety: no state stacking, crash recovery to BASELINE, state persistence, user spindown from any state
- Pure decide() does not mutate state; apply_last_decision() commits
"""

import json
from pathlib import Path

import pytest

from backend.hive.cognitive_fsm import (
    CognitiveEvent,
    CognitiveFsm,
    CognitiveTransition,
)
from backend.hive.thread_models import CognitiveState


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture()
def state_file(tmp_path: Path) -> Path:
    """Return a state file path inside tmp_path (file does not exist yet)."""
    return tmp_path / "cognitive_state.json"


@pytest.fixture()
def fsm(state_file: Path) -> CognitiveFsm:
    """Fresh FSM rooted in tmp_path (no prior state)."""
    return CognitiveFsm(state_file=state_file)


def _force_state(fsm: CognitiveFsm, target: CognitiveState) -> None:
    """Helper to drive the FSM into a specific state for testing."""
    if target == CognitiveState.BASELINE:
        return  # already there

    if target == CognitiveState.REM:
        t = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=999_999,
            system_load_pct=0.0,
        )
        assert not t.noop
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.REM
        return

    if target == CognitiveState.FLOW:
        t = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert not t.noop
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW
        return


# ============================================================================
# Initial State
# ============================================================================


class TestInitialState:
    def test_starts_at_baseline(self, fsm: CognitiveFsm):
        assert fsm.state == CognitiveState.BASELINE

    def test_starts_at_baseline_no_state_file(self, tmp_path: Path):
        sf = tmp_path / "nonexistent" / "state.json"
        f = CognitiveFsm(state_file=sf)
        assert f.state == CognitiveState.BASELINE


# ============================================================================
# BASELINE -> REM transitions
# ============================================================================


class TestBaselineToRem:
    def test_rem_trigger_success(self, fsm: CognitiveFsm):
        """BASELINE -> REM when idle >= 6h AND load < 30%."""
        t = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=6 * 3600,  # exactly 6 hours
            system_load_pct=20.0,
        )
        assert t.from_state == CognitiveState.BASELINE
        assert t.to_state == CognitiveState.REM
        assert t.reason_code == "T1_REM_TRIGGER"
        assert not t.noop

        # State not yet mutated (pure decide)
        assert fsm.state == CognitiveState.BASELINE

        # Apply commits the transition
        applied = fsm.apply_last_decision()
        assert applied is not None
        assert fsm.state == CognitiveState.REM

    def test_rem_trigger_blocked_low_idle(self, fsm: CognitiveFsm):
        """REM blocked when idle < 6h."""
        t = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=5 * 3600,  # 5 hours — not enough
            system_load_pct=10.0,
        )
        assert t.reason_code == "T1_BLOCKED_LOW_IDLE"
        assert t.noop
        assert t.from_state == CognitiveState.BASELINE
        assert t.to_state == CognitiveState.BASELINE

    def test_rem_trigger_blocked_high_load(self, fsm: CognitiveFsm):
        """REM blocked when load >= 30%."""
        t = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=7 * 3600,
            system_load_pct=30.0,  # exactly at threshold -> blocked
        )
        assert t.reason_code == "T1_BLOCKED_HIGH_LOAD"
        assert t.noop

    def test_rem_trigger_blocked_high_load_above(self, fsm: CognitiveFsm):
        """REM blocked when load well above 30%."""
        t = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=10 * 3600,
            system_load_pct=85.0,
        )
        assert t.reason_code == "T1_BLOCKED_HIGH_LOAD"
        assert t.noop


# ============================================================================
# BASELINE -> FLOW transitions
# ============================================================================


class TestBaselineToFlow:
    def test_flow_trigger_unconditional(self, fsm: CognitiveFsm):
        """BASELINE -> FLOW on FLOW_TRIGGER (unconditional)."""
        t = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert t.from_state == CognitiveState.BASELINE
        assert t.to_state == CognitiveState.FLOW
        assert t.reason_code == "T2_FLOW_TRIGGER"
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW


# ============================================================================
# REM -> FLOW (council escalation)
# ============================================================================


class TestRemToFlow:
    def test_council_escalation(self, fsm: CognitiveFsm):
        """REM -> FLOW on COUNCIL_ESCALATION."""
        _force_state(fsm, CognitiveState.REM)

        t = fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
        assert t.from_state == CognitiveState.REM
        assert t.to_state == CognitiveState.FLOW
        assert t.reason_code == "T2B_COUNCIL_ESCALATION"
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW


# ============================================================================
# REM -> BASELINE
# ============================================================================


class TestRemToBaseline:
    def test_council_complete(self, fsm: CognitiveFsm):
        """REM -> BASELINE on COUNCIL_COMPLETE."""
        _force_state(fsm, CognitiveState.REM)

        t = fsm.decide(CognitiveEvent.COUNCIL_COMPLETE)
        assert t.from_state == CognitiveState.REM
        assert t.to_state == CognitiveState.BASELINE
        assert t.reason_code == "T3B_COUNCIL_COMPLETE"
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_spindown_from_rem(self, fsm: CognitiveFsm):
        """REM -> BASELINE on SPINDOWN."""
        _force_state(fsm, CognitiveState.REM)

        t = fsm.decide(CognitiveEvent.SPINDOWN)
        assert t.from_state == CognitiveState.REM
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE


# ============================================================================
# FLOW -> BASELINE (spindown variants)
# ============================================================================


class TestFlowToBaseline:
    def test_spindown_pr_merged(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        assert t.reason_code == "T3_SPINDOWN_PR_MERGED"
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

    def test_spindown_debate_timeout(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="debate_timeout")
        assert t.reason_code == "T3_SPINDOWN_DEBATE_TIMEOUT"
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

    def test_spindown_token_budget_exhausted(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(
            CognitiveEvent.SPINDOWN, spindown_reason="token_budget_exhausted"
        )
        assert t.reason_code == "T3_SPINDOWN_TOKEN_BUDGET_EXHAUSTED"
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

    def test_spindown_iron_gate_hard_reject(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(
            CognitiveEvent.SPINDOWN, spindown_reason="iron_gate_hard_reject"
        )
        assert t.reason_code == "T3_SPINDOWN_IRON_GATE_HARD_REJECT"
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

    def test_spindown_user_manual(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(
            CognitiveEvent.SPINDOWN, spindown_reason="user_manual_spindown"
        )
        assert t.reason_code == "USER_MANUAL_SPINDOWN"
        assert t.to_state == CognitiveState.BASELINE
        assert not t.noop

    def test_spindown_commits_state(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE


# ============================================================================
# USER_SPINDOWN from any state
# ============================================================================


class TestUserSpindown:
    def test_user_spindown_from_rem(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.REM)
        t = fsm.decide(CognitiveEvent.USER_SPINDOWN)
        assert t.from_state == CognitiveState.REM
        assert t.to_state == CognitiveState.BASELINE
        assert t.reason_code == "USER_MANUAL_SPINDOWN"
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_user_spindown_from_flow(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(CognitiveEvent.USER_SPINDOWN)
        assert t.from_state == CognitiveState.FLOW
        assert t.to_state == CognitiveState.BASELINE
        assert t.reason_code == "USER_MANUAL_SPINDOWN"
        assert not t.noop

        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_user_spindown_from_baseline_is_noop(self, fsm: CognitiveFsm):
        t = fsm.decide(CognitiveEvent.USER_SPINDOWN)
        assert t.noop
        assert t.from_state == CognitiveState.BASELINE
        assert t.to_state == CognitiveState.BASELINE


# ============================================================================
# Noop transitions
# ============================================================================


class TestNoopTransitions:
    def test_spindown_from_baseline(self, fsm: CognitiveFsm):
        """SPINDOWN from BASELINE is a noop."""
        t = fsm.decide(CognitiveEvent.SPINDOWN)
        assert t.noop
        assert t.from_state == CognitiveState.BASELINE
        assert t.to_state == CognitiveState.BASELINE

    def test_flow_trigger_while_in_flow(self, fsm: CognitiveFsm):
        """FLOW_TRIGGER while already in FLOW is a noop (no state stacking)."""
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert t.noop
        assert fsm.state == CognitiveState.FLOW

    def test_rem_trigger_while_in_flow(self, fsm: CognitiveFsm):
        """REM_TRIGGER while in FLOW is a noop (no downgrade)."""
        _force_state(fsm, CognitiveState.FLOW)
        t = fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=999_999)
        assert t.noop
        assert fsm.state == CognitiveState.FLOW

    def test_council_complete_from_baseline(self, fsm: CognitiveFsm):
        """COUNCIL_COMPLETE from BASELINE is a noop."""
        t = fsm.decide(CognitiveEvent.COUNCIL_COMPLETE)
        assert t.noop

    def test_council_escalation_from_baseline(self, fsm: CognitiveFsm):
        """COUNCIL_ESCALATION from BASELINE is a noop."""
        t = fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
        assert t.noop

    def test_noop_does_not_mutate_state(self, fsm: CognitiveFsm):
        """Applying a noop decision does not change state."""
        t = fsm.decide(CognitiveEvent.SPINDOWN)
        assert t.noop
        applied = fsm.apply_last_decision()
        assert applied is not None
        assert applied.noop
        assert fsm.state == CognitiveState.BASELINE


# ============================================================================
# Safety: no state stacking
# ============================================================================


class TestNoStateStacking:
    def test_rem_trigger_while_in_rem_is_noop(self, fsm: CognitiveFsm):
        _force_state(fsm, CognitiveState.REM)
        t = fsm.decide(CognitiveEvent.REM_TRIGGER, idle_seconds=999_999)
        assert t.noop
        assert fsm.state == CognitiveState.REM

    def test_flow_trigger_while_in_rem_is_noop(self, fsm: CognitiveFsm):
        """FLOW_TRIGGER from REM should be noop; escalation requires COUNCIL_ESCALATION."""
        _force_state(fsm, CognitiveState.REM)
        t = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert t.noop
        assert fsm.state == CognitiveState.REM


# ============================================================================
# Pure decide() does NOT mutate state
# ============================================================================


class TestDecidePurity:
    def test_decide_does_not_mutate(self, fsm: CognitiveFsm):
        """decide() returns a transition but does NOT change fsm.state."""
        t = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert t.to_state == CognitiveState.FLOW
        # State must still be BASELINE
        assert fsm.state == CognitiveState.BASELINE

    def test_apply_without_decide_returns_none(self, fsm: CognitiveFsm):
        """apply_last_decision() returns None when no decide() has been called."""
        result = fsm.apply_last_decision()
        assert result is None

    def test_double_apply_returns_none(self, fsm: CognitiveFsm):
        """Second apply_last_decision() returns None (decision already consumed)."""
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        first = fsm.apply_last_decision()
        assert first is not None
        second = fsm.apply_last_decision()
        assert second is None


# ============================================================================
# CognitiveTransition dataclass
# ============================================================================


class TestCognitiveTransition:
    def test_frozen(self):
        t = CognitiveTransition(
            from_state=CognitiveState.BASELINE,
            to_state=CognitiveState.REM,
            event=CognitiveEvent.REM_TRIGGER,
            reason_code="T1_REM_TRIGGER",
        )
        with pytest.raises(AttributeError):
            t.reason_code = "changed"  # type: ignore[misc]

    def test_defaults(self):
        t = CognitiveTransition(
            from_state=CognitiveState.BASELINE,
            to_state=CognitiveState.FLOW,
            event=CognitiveEvent.FLOW_TRIGGER,
            reason_code="T2_FLOW_TRIGGER",
        )
        assert t.noop is False
        assert t.metadata == {}
        assert t.decided_at is not None

    def test_metadata_preserved(self):
        t = CognitiveTransition(
            from_state=CognitiveState.REM,
            to_state=CognitiveState.FLOW,
            event=CognitiveEvent.COUNCIL_ESCALATION,
            reason_code="T2B_COUNCIL_ESCALATION",
            metadata={"graduation_candidates": 3},
        )
        assert t.metadata["graduation_candidates"] == 3


# ============================================================================
# CognitiveEvent enum
# ============================================================================


class TestCognitiveEvent:
    def test_member_count(self):
        assert len(CognitiveEvent) == 6

    def test_values(self):
        assert CognitiveEvent.REM_TRIGGER == "rem_trigger"
        assert CognitiveEvent.FLOW_TRIGGER == "flow_trigger"
        assert CognitiveEvent.COUNCIL_ESCALATION == "council_escalation"
        assert CognitiveEvent.COUNCIL_COMPLETE == "council_complete"
        assert CognitiveEvent.SPINDOWN == "spindown"
        assert CognitiveEvent.USER_SPINDOWN == "user_spindown"


# ============================================================================
# Crash Recovery
# ============================================================================


class TestCrashRecovery:
    def test_crash_recovery_resets_to_baseline(self, state_file: Path):
        """Crash recovery always resets to BASELINE regardless of persisted state."""
        # Simulate persisted FLOW state
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"state": "flow", "persisted_at": "2026-01-01T00:00:00+00:00"}))

        fsm = CognitiveFsm(state_file=state_file, crash_recovery=True)
        assert fsm.state == CognitiveState.BASELINE

    def test_crash_recovery_resets_rem_to_baseline(self, state_file: Path):
        """Crash recovery resets REM -> BASELINE."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"state": "rem", "persisted_at": "2026-01-01T00:00:00+00:00"}))

        fsm = CognitiveFsm(state_file=state_file, crash_recovery=True)
        assert fsm.state == CognitiveState.BASELINE

    def test_crash_recovery_without_state_file(self, tmp_path: Path):
        """Crash recovery with no state file -> stays BASELINE."""
        sf = tmp_path / "no_such_file.json"
        fsm = CognitiveFsm(state_file=sf, crash_recovery=True)
        assert fsm.state == CognitiveState.BASELINE

    def test_crash_recovery_persists_reset(self, state_file: Path):
        """After crash recovery, the BASELINE state is persisted."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"state": "flow"}))

        CognitiveFsm(state_file=state_file, crash_recovery=True)

        # Verify the file now has BASELINE
        data = json.loads(state_file.read_text())
        assert data["state"] == "baseline"


# ============================================================================
# State Persistence
# ============================================================================


class TestStatePersistence:
    def test_transition_persists_state(self, fsm: CognitiveFsm, state_file: Path):
        """Non-noop transitions persist state to disk."""
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["state"] == "flow"

    def test_noop_does_not_persist(self, fsm: CognitiveFsm, state_file: Path):
        """Noop transitions do NOT write to disk."""
        fsm.decide(CognitiveEvent.SPINDOWN)  # noop from BASELINE
        fsm.apply_last_decision()

        # State file should not exist (no prior persist, and noop doesn't create one)
        assert not state_file.exists()

    def test_state_survives_reload(self, state_file: Path):
        """State persisted by one FSM is loaded by another."""
        fsm1 = CognitiveFsm(state_file=state_file)
        fsm1.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm1.apply_last_decision()
        assert fsm1.state == CognitiveState.FLOW

        # New FSM loads the persisted state
        fsm2 = CognitiveFsm(state_file=state_file)
        assert fsm2.state == CognitiveState.FLOW

    def test_corrupted_state_file_defaults_baseline(self, state_file: Path):
        """Corrupted state file -> default to BASELINE."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("NOT VALID JSON!!!")

        fsm = CognitiveFsm(state_file=state_file)
        assert fsm.state == CognitiveState.BASELINE

    def test_invalid_state_value_defaults_baseline(self, state_file: Path):
        """Invalid state value in JSON -> default to BASELINE."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"state": "hyperspace"}))

        fsm = CognitiveFsm(state_file=state_file)
        assert fsm.state == CognitiveState.BASELINE


# ============================================================================
# Full lifecycle integration
# ============================================================================


class TestFullLifecycle:
    def test_baseline_rem_flow_baseline(self, fsm: CognitiveFsm):
        """Complete cycle: BASELINE -> REM -> FLOW -> BASELINE."""
        # Step 1: BASELINE -> REM
        t1 = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=8 * 3600,
            system_load_pct=15.0,
        )
        assert t1.reason_code == "T1_REM_TRIGGER"
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.REM

        # Step 2: REM -> FLOW (council escalation)
        t2 = fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
        assert t2.reason_code == "T2B_COUNCIL_ESCALATION"
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # Step 3: FLOW -> BASELINE (spindown)
        t3 = fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        assert t3.reason_code == "T3_SPINDOWN_PR_MERGED"
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_baseline_flow_user_spindown(self, fsm: CognitiveFsm):
        """BASELINE -> FLOW -> BASELINE via USER_SPINDOWN."""
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        fsm.decide(CognitiveEvent.USER_SPINDOWN)
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_multiple_transitions_with_noops(self, fsm: CognitiveFsm):
        """Verify noops don't disrupt valid transitions."""
        # Noop: SPINDOWN from BASELINE
        t0 = fsm.decide(CognitiveEvent.SPINDOWN)
        assert t0.noop
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

        # Valid: BASELINE -> FLOW
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # Noop: FLOW_TRIGGER in FLOW
        t1 = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert t1.noop
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # Valid: FLOW -> BASELINE
        fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="debate_timeout")
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

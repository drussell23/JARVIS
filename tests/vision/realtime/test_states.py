"""
Tests for the real-time vision pipeline state machine.

Tests are written TDD-first: they import from backend.vision.realtime.states,
which does not exist until Step 4.  Running before implementation must yield
ModuleNotFoundError.
"""
from __future__ import annotations

import time
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from backend.vision.realtime.states import (
    TransitionError,
    VisionEvent,
    VisionState,
    VisionStateMachine,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_sm() -> VisionStateMachine:
    """Return a fresh state machine starting in IDLE."""
    return VisionStateMachine()


# ---------------------------------------------------------------------------
# TestLegalTransitions
# ---------------------------------------------------------------------------

class TestLegalTransitions:
    def test_idle_to_watching(self):
        sm = _make_sm()
        new = sm.transition(VisionEvent.START)
        assert new == VisionState.WATCHING
        assert sm.state == VisionState.WATCHING

    def test_watching_to_change_detected(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)  # → WATCHING
        new = sm.transition(VisionEvent.MOTION_DETECTED)
        assert new == VisionState.CHANGE_DETECTED

    def test_change_to_analyzing(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        new = sm.transition(VisionEvent.SAMPLE_FRAME)
        assert new == VisionState.ANALYZING

    def test_analyzing_to_watching(self):
        """ANALYSIS_COMPLETE with no action needed returns to WATCHING."""
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        new = sm.transition(VisionEvent.ANALYSIS_COMPLETE)
        assert new == VisionState.WATCHING

    def test_analyzing_to_action_targeting(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        new = sm.transition(VisionEvent.ACTION_REQUESTED)
        assert new == VisionState.ACTION_TARGETING

    def test_action_targeting_to_precheck(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        new = sm.transition(VisionEvent.BURST_COMPLETE)
        assert new == VisionState.PRECHECK

    def test_precheck_to_acting(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        new = sm.transition(VisionEvent.ALL_GUARDS_PASS)
        assert new == VisionState.ACTING

    def test_acting_to_verifying(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        new = sm.transition(VisionEvent.ACTION_DISPATCHED)
        assert new == VisionState.VERIFYING

    def test_verifying_success_to_watching(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        new = sm.transition(VisionEvent.POSTCONDITION_MET)
        assert new == VisionState.WATCHING

    def test_verifying_fail_to_retry(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        new = sm.transition(VisionEvent.POSTCONDITION_FAIL)
        assert new == VisionState.RETRY_TARGETING

    def test_full_happy_path(self):
        """End-to-end happy path through the entire pipeline."""
        sm = _make_sm()
        assert sm.state == VisionState.IDLE

        sm.transition(VisionEvent.START)
        assert sm.state == VisionState.WATCHING

        sm.transition(VisionEvent.MOTION_DETECTED)
        assert sm.state == VisionState.CHANGE_DETECTED

        sm.transition(VisionEvent.SAMPLE_FRAME)
        assert sm.state == VisionState.ANALYZING

        sm.transition(VisionEvent.ACTION_REQUESTED)
        assert sm.state == VisionState.ACTION_TARGETING

        sm.transition(VisionEvent.BURST_COMPLETE)
        assert sm.state == VisionState.PRECHECK

        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        assert sm.state == VisionState.ACTING

        sm.transition(VisionEvent.ACTION_DISPATCHED)
        assert sm.state == VisionState.VERIFYING

        sm.transition(VisionEvent.POSTCONDITION_MET)
        assert sm.state == VisionState.WATCHING


# ---------------------------------------------------------------------------
# TestIllegalTransitions
# ---------------------------------------------------------------------------

class TestIllegalTransitions:
    def test_cannot_skip_precheck(self):
        """Jumping from WATCHING directly with ACTION_DISPATCHED must fail."""
        sm = _make_sm()
        sm.transition(VisionEvent.START)  # → WATCHING
        with pytest.raises(TransitionError):
            sm.transition(VisionEvent.ACTION_DISPATCHED)

    def test_cannot_go_idle_to_acting(self):
        sm = _make_sm()
        with pytest.raises(TransitionError):
            sm.transition(VisionEvent.ALL_GUARDS_PASS)

    def test_failed_only_accepts_ack_or_stop(self):
        """FAILED state rejects START but accepts USER_ACKNOWLEDGES → WATCHING."""
        sm = _make_sm()
        # Reach FAILED: exceed retries
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        sm.transition(VisionEvent.POSTCONDITION_FAIL)  # → RETRY_TARGETING

        # Retry once
        sm.transition(VisionEvent.RETRY)               # → ACTION_TARGETING
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        sm.transition(VisionEvent.POSTCONDITION_FAIL)  # → RETRY_TARGETING

        # Retry twice (MAX_RETRIES=2)
        sm.transition(VisionEvent.RETRY)               # → ACTION_TARGETING
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        sm.transition(VisionEvent.POSTCONDITION_FAIL)  # → RETRY_TARGETING

        # Now retry count == MAX_RETRIES; RETRY_EXCEEDED fires
        sm.transition(VisionEvent.RETRY_EXCEEDED)      # → FAILED
        assert sm.state == VisionState.FAILED

        # START must be rejected
        with pytest.raises(TransitionError):
            sm.transition(VisionEvent.START)

        # USER_ACKNOWLEDGES brings it back to WATCHING
        new = sm.transition(VisionEvent.USER_ACKNOWLEDGES)
        assert new == VisionState.WATCHING


# ---------------------------------------------------------------------------
# TestDegradedPath
# ---------------------------------------------------------------------------

class TestDegradedPath:
    def _reach_analyzing(self) -> VisionStateMachine:
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        return sm

    def _reach_action_targeting(self) -> VisionStateMachine:
        sm = self._reach_analyzing()
        sm.transition(VisionEvent.ACTION_REQUESTED)
        return sm

    def test_analyzing_to_degraded(self):
        sm = self._reach_analyzing()
        assert sm.state == VisionState.ANALYZING
        new = sm.transition(VisionEvent.VISION_UNAVAILABLE)
        assert new == VisionState.DEGRADED

    def test_action_targeting_to_degraded(self):
        sm = self._reach_action_targeting()
        assert sm.state == VisionState.ACTION_TARGETING
        new = sm.transition(VisionEvent.VISION_UNAVAILABLE)
        assert new == VisionState.DEGRADED

    def test_degraded_to_recovering(self):
        sm = self._reach_analyzing()
        sm.transition(VisionEvent.VISION_UNAVAILABLE)
        new = sm.transition(VisionEvent.HEALTH_CHECK_PASS)
        assert new == VisionState.RECOVERING

    def test_recovering_to_watching(self):
        sm = self._reach_analyzing()
        sm.transition(VisionEvent.VISION_UNAVAILABLE)
        sm.transition(VisionEvent.HEALTH_CHECK_PASS)
        new = sm.transition(VisionEvent.N_CONSECUTIVE_HEALTHY)
        assert new == VisionState.WATCHING

    def test_recovering_fail_back_to_degraded(self):
        sm = self._reach_analyzing()
        sm.transition(VisionEvent.VISION_UNAVAILABLE)
        sm.transition(VisionEvent.HEALTH_CHECK_PASS)  # → RECOVERING
        new = sm.transition(VisionEvent.HEALTH_CHECK_FAIL)
        assert new == VisionState.DEGRADED


# ---------------------------------------------------------------------------
# TestRetryBounds
# ---------------------------------------------------------------------------

class TestRetryBounds:
    def _reach_retry_targeting(self) -> VisionStateMachine:
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        sm.transition(VisionEvent.POSTCONDITION_FAIL)  # → RETRY_TARGETING
        return sm

    def test_retry_increments_count(self):
        sm = self._reach_retry_targeting()
        before = sm.retry_count
        sm.transition(VisionEvent.RETRY)  # → ACTION_TARGETING, count++
        assert sm.retry_count == before + 1

    def test_retry_exceeded_to_failed(self):
        sm = self._reach_retry_targeting()
        # Exhaust retries
        for _ in range(VisionStateMachine.MAX_RETRIES):
            sm.transition(VisionEvent.RETRY)
            sm.transition(VisionEvent.BURST_COMPLETE)
            sm.transition(VisionEvent.ALL_GUARDS_PASS)
            sm.transition(VisionEvent.ACTION_DISPATCHED)
            sm.transition(VisionEvent.POSTCONDITION_FAIL)
        # Now trigger RETRY_EXCEEDED
        new = sm.transition(VisionEvent.RETRY_EXCEEDED)
        assert new == VisionState.FAILED

    def test_retry_count_resets_on_success(self):
        sm = self._reach_retry_targeting()
        sm.transition(VisionEvent.RETRY)  # → ACTION_TARGETING (count=1)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)
        sm.transition(VisionEvent.ACTION_DISPATCHED)
        sm.transition(VisionEvent.POSTCONDITION_MET)  # → WATCHING
        assert sm.state == VisionState.WATCHING
        assert sm.retry_count == 0


# ---------------------------------------------------------------------------
# TestTelemetry
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_transition_emits_record(self):
        records: List[Dict] = []
        sm = VisionStateMachine(on_transition=records.append)
        sm.transition(VisionEvent.START)
        assert len(records) == 1
        rec = records[0]
        assert rec["from_state"] == VisionState.IDLE
        assert rec["to_state"] == VisionState.WATCHING
        assert rec["event"] == VisionEvent.START

    def test_every_transition_has_timestamp(self):
        records: List[Dict] = []
        sm = VisionStateMachine(on_transition=records.append)
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        assert all("timestamp" in r for r in records)
        # Timestamps must be numeric (monotonic or epoch float)
        for r in records:
            assert isinstance(r["timestamp"], float)

    def test_telemetry_includes_retry_count(self):
        records: List[Dict] = []
        sm = VisionStateMachine(on_transition=records.append)
        sm.transition(VisionEvent.START)
        assert "retry_count" in records[0]

    def test_callback_called_on_every_transition(self):
        cb = MagicMock()
        sm = VisionStateMachine(on_transition=cb)
        sm.transition(VisionEvent.START)             # 1
        sm.transition(VisionEvent.MOTION_DETECTED)   # 2
        sm.transition(VisionEvent.SAMPLE_FRAME)      # 3
        assert cb.call_count == 3


# ---------------------------------------------------------------------------
# TestStopFromAnyState
# ---------------------------------------------------------------------------

class TestStopFromAnyState:
    def test_stop_from_watching(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)  # → WATCHING
        new = sm.transition(VisionEvent.STOP)
        assert new == VisionState.IDLE

    def test_stop_from_acting(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.ACTION_REQUESTED)
        sm.transition(VisionEvent.BURST_COMPLETE)
        sm.transition(VisionEvent.ALL_GUARDS_PASS)  # → ACTING
        new = sm.transition(VisionEvent.STOP)
        assert new == VisionState.IDLE

    def test_stop_from_degraded(self):
        sm = _make_sm()
        sm.transition(VisionEvent.START)
        sm.transition(VisionEvent.MOTION_DETECTED)
        sm.transition(VisionEvent.SAMPLE_FRAME)
        sm.transition(VisionEvent.VISION_UNAVAILABLE)  # → DEGRADED
        new = sm.transition(VisionEvent.STOP)
        assert new == VisionState.IDLE

    def test_stop_from_every_non_idle_state(self):
        """STOP transitions every non-IDLE state to IDLE."""
        non_idle_states = [s for s in VisionState if s != VisionState.IDLE]
        for state in non_idle_states:
            sm = VisionStateMachine()
            # Force internal state directly for this exhaustive check
            sm._state = state
            new = sm.transition(VisionEvent.STOP)
            assert new == VisionState.IDLE, (
                f"STOP from {state} did not reach IDLE (got {new})"
            )

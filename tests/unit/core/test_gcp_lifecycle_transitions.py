# tests/unit/core/test_gcp_lifecycle_transitions.py
"""Tests for GCP lifecycle transition table — deterministic validation."""
import pytest
from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_transitions import (
    TRANSITION_TABLE,
    is_valid_transition,
    get_transition,
    ILLEGAL_TRANSITIONS,
)


class TestTransitionTable:
    def test_idle_pressure_triggered_goes_to_triggering(self):
        t = get_transition(State.IDLE, Event.PRESSURE_TRIGGERED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_triggering_budget_approved_goes_to_provisioning(self):
        t = get_transition(State.TRIGGERING, Event.BUDGET_APPROVED)
        assert t is not None
        assert t.next_state == State.PROVISIONING

    def test_triggering_budget_denied_goes_to_cooling_down(self):
        t = get_transition(State.TRIGGERING, Event.BUDGET_DENIED)
        assert t is not None
        assert t.next_state == State.COOLING_DOWN

    def test_booting_health_ok_goes_to_active(self):
        t = get_transition(State.BOOTING, Event.HEALTH_PROBE_OK)
        assert t is not None
        assert t.next_state == State.ACTIVE

    def test_active_preempted_goes_to_triggering(self):
        t = get_transition(State.ACTIVE, Event.SPOT_PREEMPTED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_active_unreachable_goes_to_triggering(self):
        t = get_transition(State.ACTIVE, Event.HEALTH_UNREACHABLE_CONSECUTIVE)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_stopping_vm_stopped_goes_to_idle(self):
        t = get_transition(State.STOPPING, Event.VM_STOPPED)
        assert t is not None
        assert t.next_state == State.IDLE

    def test_cooldown_pressure_returns_to_triggering(self):
        t = get_transition(State.COOLING_DOWN, Event.PRESSURE_TRIGGERED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_cooldown_expired_goes_to_stopping(self):
        t = get_transition(State.COOLING_DOWN, Event.COOLDOWN_EXPIRED)
        assert t is not None
        assert t.next_state == State.STOPPING


class TestWildcardTransitions:
    def test_session_shutdown_from_any_state_goes_to_stopping(self):
        for state in [State.IDLE, State.TRIGGERING, State.ACTIVE, State.BOOTING]:
            t = get_transition(state, Event.SESSION_SHUTDOWN)
            assert t is not None, f"No SESSION_SHUTDOWN transition from {state}"
            assert t.next_state == State.STOPPING

    def test_lease_lost_from_any_state_goes_to_idle(self):
        for state in [State.TRIGGERING, State.PROVISIONING, State.ACTIVE]:
            t = get_transition(state, Event.LEASE_LOST)
            assert t is not None, f"No LEASE_LOST transition from {state}"
            assert t.next_state == State.IDLE

    def test_fatal_error_goes_to_cooling_down(self):
        for state in [State.TRIGGERING, State.PROVISIONING, State.BOOTING, State.ACTIVE]:
            t = get_transition(state, Event.FATAL_ERROR)
            assert t is not None, f"No FATAL_ERROR transition from {state}"
            assert t.next_state == State.COOLING_DOWN


class TestIllegalTransitions:
    def test_idle_to_active_rejected(self):
        assert not is_valid_transition(State.IDLE, State.ACTIVE)

    def test_provisioning_to_active_rejected(self):
        assert not is_valid_transition(State.PROVISIONING, State.ACTIVE)

    def test_active_to_idle_rejected(self):
        assert not is_valid_transition(State.ACTIVE, State.IDLE)

    def test_illegal_transitions_documented(self):
        assert len(ILLEGAL_TRANSITIONS) >= 3


class TestTransitionTableCompleteness:
    def test_every_primary_state_has_shutdown(self):
        """Every non-terminal state must handle SESSION_SHUTDOWN."""
        primary = [State.IDLE, State.TRIGGERING, State.PROVISIONING,
                   State.BOOTING, State.ACTIVE, State.COOLING_DOWN, State.STOPPING]
        for state in primary:
            t = get_transition(state, Event.SESSION_SHUTDOWN)
            assert t is not None, f"{state} has no SESSION_SHUTDOWN handler"

    def test_every_primary_state_has_lease_lost(self):
        """Every non-terminal state must handle LEASE_LOST."""
        primary = [State.TRIGGERING, State.PROVISIONING, State.BOOTING,
                   State.ACTIVE, State.COOLING_DOWN, State.STOPPING]
        for state in primary:
            t = get_transition(state, Event.LEASE_LOST)
            assert t is not None, f"{state} has no LEASE_LOST handler"

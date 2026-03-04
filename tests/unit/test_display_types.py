"""Tests for display-related Memory Control Plane types."""
import pytest
from backend.core.memory_types import (
    DisplayState,
    DisplayFailureCode,
    MemoryBudgetEventType,
)


class TestDisplayState:
    def test_all_states_present(self):
        expected = {
            "INACTIVE", "ACTIVE",
            "DEGRADING", "DEGRADED_1", "DEGRADED_2", "MINIMUM",
            "RECOVERING", "DISCONNECTING", "DISCONNECTED",
        }
        assert {s.name for s in DisplayState} == expected

    def test_transitional_states(self):
        transitionals = {DisplayState.DEGRADING, DisplayState.RECOVERING, DisplayState.DISCONNECTING}
        for s in transitionals:
            assert s.is_transitional

    def test_stable_states_not_transitional(self):
        stables = {DisplayState.INACTIVE, DisplayState.ACTIVE, DisplayState.DEGRADED_1,
                    DisplayState.DEGRADED_2, DisplayState.MINIMUM, DisplayState.DISCONNECTED}
        for s in stables:
            assert not s.is_transitional

    def test_active_states(self):
        active = {DisplayState.ACTIVE, DisplayState.DEGRADED_1, DisplayState.DEGRADED_2,
                  DisplayState.MINIMUM, DisplayState.DEGRADING, DisplayState.RECOVERING}
        for s in active:
            assert s.is_display_connected

    def test_disconnected_states_not_connected(self):
        for s in (DisplayState.INACTIVE, DisplayState.DISCONNECTED, DisplayState.DISCONNECTING):
            assert not s.is_display_connected


class TestDisplayFailureCode:
    def test_all_codes_present(self):
        expected = {
            "COMMAND_TIMEOUT", "VERIFY_MISMATCH", "DEPENDENCY_BLOCKED",
            "PREEMPTED", "QUARANTINED", "CLI_ERROR", "COMPOSITOR_MISMATCH",
        }
        assert {c.name for c in DisplayFailureCode} == expected

    def test_transient_codes(self):
        assert DisplayFailureCode.COMMAND_TIMEOUT.failure_class == "transient"
        assert DisplayFailureCode.COMMAND_TIMEOUT.retryable is True

    def test_structural_codes(self):
        assert DisplayFailureCode.COMPOSITOR_MISMATCH.failure_class == "structural"
        assert DisplayFailureCode.COMPOSITOR_MISMATCH.retryable is False


class TestDisplayEventTypes:
    def test_all_display_events_present(self):
        display_events = {
            "DISPLAY_DEGRADE_REQUESTED", "DISPLAY_DEGRADED",
            "DISPLAY_DISCONNECT_REQUESTED", "DISPLAY_DISCONNECTED",
            "DISPLAY_RECOVERY_REQUESTED", "DISPLAY_RECOVERED",
            "DISPLAY_ACTION_FAILED", "DISPLAY_ACTION_PHASE",
        }
        actual = {e.name for e in MemoryBudgetEventType if e.name.startswith("DISPLAY_")}
        assert actual == display_events

    def test_display_event_values_snake_case(self):
        for e in MemoryBudgetEventType:
            if e.name.startswith("DISPLAY_"):
                assert e.value == e.name.lower()

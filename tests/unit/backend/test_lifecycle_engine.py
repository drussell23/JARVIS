#!/usr/bin/env python3
"""Tests for lifecycle state machine engine (Disease 5+6 MVP)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
# KernelState lives in unified_supervisor.py which is hard to import.
# We re-export it from lifecycle_engine for testability.
from backend.core.kernel_lifecycle_engine import (
    LifecycleEvent, TransitionRecord, VALID_TRANSITIONS, KernelState,
)


class TestLifecycleEventEnum:
    def test_all_events_exist(self):
        assert LifecycleEvent.PREFLIGHT_START == "preflight_start"
        assert LifecycleEvent.BRINGUP_START == "bringup_start"
        assert LifecycleEvent.BACKEND_START == "backend_start"
        assert LifecycleEvent.INTEL_START == "intel_start"
        assert LifecycleEvent.TRINITY_START == "trinity_start"
        assert LifecycleEvent.READY == "ready"
        assert LifecycleEvent.SHUTDOWN == "shutdown"
        assert LifecycleEvent.STOPPED == "stopped"
        assert LifecycleEvent.FATAL == "fatal"

    def test_exactly_nine_events(self):
        assert len(LifecycleEvent) == 9


class TestTransitionRecord:
    def test_record_fields(self):
        rec = TransitionRecord(
            old_state="initializing", event="preflight_start",
            new_state="preflight", epoch=1, actor="supervisor",
            at_monotonic=1000.0, reason="boot",
        )
        assert rec.old_state == "initializing"
        assert rec.epoch == 1
        assert rec.actor == "supervisor"

    def test_record_is_frozen(self):
        rec = TransitionRecord(
            old_state="a", event="b", new_state="c",
            epoch=0, actor="", at_monotonic=0.0, reason="",
        )
        with pytest.raises(AttributeError):
            rec.old_state = "changed"


class TestTransitionTable:
    def test_forward_startup_sequence(self):
        """Full startup path exists in table."""
        sequence = [
            (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START, KernelState.PREFLIGHT),
            (KernelState.PREFLIGHT, LifecycleEvent.BRINGUP_START, KernelState.STARTING_RESOURCES),
            (KernelState.STARTING_RESOURCES, LifecycleEvent.BACKEND_START, KernelState.STARTING_BACKEND),
            (KernelState.STARTING_BACKEND, LifecycleEvent.INTEL_START, KernelState.STARTING_INTELLIGENCE),
            (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.TRINITY_START, KernelState.STARTING_TRINITY),
            (KernelState.STARTING_TRINITY, LifecycleEvent.READY, KernelState.RUNNING),
        ]
        for from_state, event, expected_to in sequence:
            assert VALID_TRANSITIONS[(from_state, event)] == expected_to

    def test_shutdown_from_every_active_state(self):
        active_states = [
            KernelState.RUNNING, KernelState.PREFLIGHT,
            KernelState.STARTING_RESOURCES, KernelState.STARTING_BACKEND,
            KernelState.STARTING_INTELLIGENCE, KernelState.STARTING_TRINITY,
        ]
        for state in active_states:
            assert VALID_TRANSITIONS[(state, LifecycleEvent.SHUTDOWN)] == KernelState.SHUTTING_DOWN

    def test_duplicate_shutdown_is_idempotent(self):
        assert VALID_TRANSITIONS[
            (KernelState.SHUTTING_DOWN, LifecycleEvent.SHUTDOWN)
        ] == KernelState.SHUTTING_DOWN

    def test_fatal_from_every_non_terminal_state(self):
        non_terminal = [
            KernelState.INITIALIZING, KernelState.PREFLIGHT,
            KernelState.STARTING_RESOURCES, KernelState.STARTING_BACKEND,
            KernelState.STARTING_INTELLIGENCE, KernelState.STARTING_TRINITY,
            KernelState.RUNNING, KernelState.SHUTTING_DOWN,
        ]
        for state in non_terminal:
            assert VALID_TRANSITIONS[(state, LifecycleEvent.FATAL)] == KernelState.FAILED

    def test_stopped_and_failed_are_terminal(self):
        for event in LifecycleEvent:
            assert (KernelState.STOPPED, event) not in VALID_TRANSITIONS
            assert (KernelState.FAILED, event) not in VALID_TRANSITIONS

    def test_all_kernel_states_covered(self):
        """Every KernelState appears in the table as a from-state."""
        states_in_table = {k[0] for k in VALID_TRANSITIONS.keys()}
        non_terminal = set(KernelState) - {KernelState.STOPPED, KernelState.FAILED}
        assert non_terminal.issubset(states_in_table)


from backend.core.kernel_lifecycle_engine import LifecycleEngine
from backend.core.lifecycle_exceptions import (
    LifecycleFatalError, TransitionRejected,
)


class TestLifecycleEngine:
    """Guarded state machine with epoch tracking."""

    def test_initial_state(self):
        engine = LifecycleEngine()
        assert engine.state == KernelState.INITIALIZING
        assert engine.epoch == 0

    def test_valid_forward_transition(self):
        engine = LifecycleEngine()
        result = engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert result == KernelState.PREFLIGHT
        assert engine.state == KernelState.PREFLIGHT

    def test_epoch_increments_on_preflight(self):
        engine = LifecycleEngine()
        assert engine.epoch == 0
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert engine.epoch == 1

    def test_invalid_transition_non_terminal_raises_fatal(self):
        engine = LifecycleEngine()
        with pytest.raises(LifecycleFatalError) as exc_info:
            engine.transition(LifecycleEvent.READY, actor="test")
        assert exc_info.value.error_code == "transition_invalid"

    def test_invalid_transition_terminal_raises_rejected(self):
        engine = LifecycleEngine()
        # Drive to FAILED
        engine.transition(LifecycleEvent.FATAL, actor="test")
        with pytest.raises(TransitionRejected):
            engine.transition(LifecycleEvent.SHUTDOWN, actor="test")

    def test_duplicate_shutdown_is_idempotent(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        # Second shutdown should NOT raise
        result = engine.transition(LifecycleEvent.SHUTDOWN, actor="test2")
        assert result == KernelState.SHUTTING_DOWN

    def test_history_records_transitions(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="boot", reason="startup")
        history = engine.history
        assert len(history) == 1
        assert history[0].old_state == "initializing"
        assert history[0].event == "preflight_start"
        assert history[0].new_state == "preflight"
        assert history[0].actor == "boot"
        assert history[0].reason == "startup"
        assert history[0].epoch == 1

    def test_history_is_bounded(self):
        engine = LifecycleEngine()
        # Transitions: preflight -> shutdown -> stopped won't reach 100
        # Just verify deque has maxlen
        assert engine._history.maxlen == 100

    def test_listener_notified(self):
        engine = LifecycleEngine()
        events = []
        engine.subscribe(lambda old, ev, new: events.append((old, ev, new)))
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert len(events) == 1
        assert events[0] == (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START, KernelState.PREFLIGHT)

    def test_listener_not_notified_on_noop(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        events = []
        engine.subscribe(lambda old, ev, new: events.append(1))
        # Duplicate shutdown = no-op = no notification
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        assert len(events) == 0

    def test_broken_listener_does_not_break_transition(self):
        engine = LifecycleEngine()
        engine.subscribe(lambda old, ev, new: 1 / 0)  # raises ZeroDivisionError
        # Should NOT raise
        result = engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert result == KernelState.PREFLIGHT

    def test_full_startup_shutdown_cycle(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="boot")
        engine.transition(LifecycleEvent.BRINGUP_START, actor="boot")
        engine.transition(LifecycleEvent.BACKEND_START, actor="boot")
        engine.transition(LifecycleEvent.INTEL_START, actor="boot")
        engine.transition(LifecycleEvent.TRINITY_START, actor="boot")
        engine.transition(LifecycleEvent.READY, actor="boot")
        assert engine.state == KernelState.RUNNING
        engine.transition(LifecycleEvent.SHUTDOWN, actor="operator")
        engine.transition(LifecycleEvent.STOPPED, actor="cleanup")
        assert engine.state == KernelState.STOPPED

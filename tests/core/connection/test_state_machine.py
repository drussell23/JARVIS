"""
Tests for Atomic State Machine with CAS Pattern.
"""

import pytest
import asyncio
from backend.core.connection.state_machine import (
    AtomicStateMachine,
    CircuitState,
    StateTransition,
    StateTransitionError,
)


@pytest.mark.asyncio
async def test_concurrent_half_open_transition_only_one_wins():
    """Only one coroutine should win the OPEN -> HALF_OPEN transition."""
    machine = AtomicStateMachine(initial_state=CircuitState.OPEN)

    winners = []

    async def try_transition(task_id: int):
        success = await machine.try_transition(
            from_state=CircuitState.OPEN,
            to_state=CircuitState.HALF_OPEN,
        )
        if success:
            winners.append(task_id)

    # Launch 10 concurrent transition attempts
    await asyncio.gather(*[try_transition(i) for i in range(10)])

    # Exactly one should win
    assert len(winners) == 1
    assert machine.current_state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_transition_fails_if_state_changed():
    """Transition should fail if state doesn't match expected."""
    machine = AtomicStateMachine(initial_state=CircuitState.CLOSED)

    # Try to transition from OPEN (wrong state)
    success = await machine.try_transition(
        from_state=CircuitState.OPEN,
        to_state=CircuitState.HALF_OPEN,
    )

    assert success is False
    assert machine.current_state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_transition_records_history():
    """Transitions should be recorded in history."""
    machine = AtomicStateMachine(initial_state=CircuitState.CLOSED)

    await machine.try_transition(
        from_state=CircuitState.CLOSED,
        to_state=CircuitState.OPEN,
        reason="Test failure",
    )

    history = machine.get_history(limit=10)
    assert len(history) == 1
    assert history[0].from_state == CircuitState.CLOSED
    assert history[0].to_state == CircuitState.OPEN
    assert history[0].reason == "Test failure"


@pytest.mark.asyncio
async def test_sync_transition_works():
    """Synchronous transition should work correctly."""
    machine = AtomicStateMachine(initial_state=CircuitState.CLOSED)

    success = machine.try_transition_sync(
        from_state=CircuitState.CLOSED,
        to_state=CircuitState.OPEN,
        reason="Sync test",
    )

    assert success is True
    assert machine.current_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_transition_count_increments():
    """Transition count should increment on each transition."""
    machine = AtomicStateMachine(initial_state=CircuitState.CLOSED)

    assert machine.transition_count == 0

    await machine.try_transition(
        from_state=CircuitState.CLOSED,
        to_state=CircuitState.OPEN,
    )

    assert machine.transition_count == 1

    await machine.try_transition(
        from_state=CircuitState.OPEN,
        to_state=CircuitState.HALF_OPEN,
    )

    assert machine.transition_count == 2


@pytest.mark.asyncio
async def test_observer_is_called_on_transition():
    """Observers should be notified on state transitions."""
    machine = AtomicStateMachine(initial_state=CircuitState.CLOSED)

    observed_transitions = []

    def observer(transition: StateTransition):
        observed_transitions.append(transition)

    machine.add_observer(observer)

    await machine.try_transition(
        from_state=CircuitState.CLOSED,
        to_state=CircuitState.OPEN,
    )

    assert len(observed_transitions) == 1
    assert observed_transitions[0].to_state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_high_concurrency_stress():
    """High concurrency should not corrupt state."""
    machine = AtomicStateMachine(initial_state=CircuitState.OPEN)

    # 100 concurrent attempts - only 1 should win
    results = await asyncio.gather(*[
        machine.try_transition(
            from_state=CircuitState.OPEN,
            to_state=CircuitState.HALF_OPEN,
        )
        for _ in range(100)
    ])

    # Exactly one True
    assert sum(results) == 1
    assert machine.current_state == CircuitState.HALF_OPEN

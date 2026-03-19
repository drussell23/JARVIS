"""
Tests for Atomic Circuit Breaker with Thundering Herd Prevention.
"""

import pytest
import asyncio
from backend.core.connection.circuit_breaker import (
    AtomicCircuitBreaker,
    CircuitBreakerConfig,
)
from backend.core.connection.state_machine import CircuitState


@pytest.mark.asyncio
async def test_half_open_allows_limited_test_requests():
    """HALF_OPEN state should allow only N test requests."""
    config = CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout_seconds=0.1,
        half_open_max_requests=2,  # Only 2 test requests
    )
    breaker = AtomicCircuitBreaker(config)

    # Trigger failures to open circuit
    await breaker.record_failure("test error")
    await breaker.record_failure("test error")
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # First 2 requests should be allowed (HALF_OPEN)
    results = []
    for i in range(5):
        allowed = await breaker.can_execute()
        results.append(allowed)

    # Only first 2 should be True (first triggers HALF_OPEN, both within limit)
    assert results == [True, True, False, False, False]


@pytest.mark.asyncio
async def test_circuit_opens_after_failure_threshold():
    """Circuit should open after reaching failure threshold."""
    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout_seconds=60.0,
    )
    breaker = AtomicCircuitBreaker(config)

    # Initially closed
    assert breaker.state == CircuitState.CLOSED
    assert await breaker.can_execute() is True

    # Record failures up to threshold
    await breaker.record_failure("error 1")
    assert breaker.state == CircuitState.CLOSED

    await breaker.record_failure("error 2")
    assert breaker.state == CircuitState.CLOSED

    await breaker.record_failure("error 3")  # Threshold reached
    assert breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_closes_after_success_threshold():
    """Circuit should close after enough successes in HALF_OPEN."""
    config = CircuitBreakerConfig(
        failure_threshold=1,
        success_threshold=2,
        recovery_timeout_seconds=0.01,
        half_open_max_requests=5,  # Allow enough for test
    )
    breaker = AtomicCircuitBreaker(config)

    # Open the circuit
    await breaker.record_failure("test")
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery
    await asyncio.sleep(0.02)

    # Transition to HALF_OPEN
    assert await breaker.can_execute() is True
    assert breaker.state == CircuitState.HALF_OPEN

    # Record successes
    await breaker.record_success()
    assert breaker.state == CircuitState.HALF_OPEN  # Not enough yet

    await breaker.record_success()
    assert breaker.state == CircuitState.CLOSED  # Now closed


@pytest.mark.asyncio
async def test_failure_in_half_open_reopens_circuit():
    """A failure in HALF_OPEN should immediately reopen circuit."""
    config = CircuitBreakerConfig(
        failure_threshold=1,
        recovery_timeout_seconds=0.01,
        half_open_max_requests=5,
    )
    breaker = AtomicCircuitBreaker(config)

    # Open the circuit
    await breaker.record_failure("test")
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery
    await asyncio.sleep(0.02)

    # Transition to HALF_OPEN
    assert await breaker.can_execute() is True
    assert breaker.state == CircuitState.HALF_OPEN

    # Record failure
    await breaker.record_failure("test request failed")
    assert breaker.state == CircuitState.OPEN  # Back to OPEN


@pytest.mark.asyncio
async def test_concurrent_can_execute_only_one_transitions():
    """Only one concurrent caller should transition OPEN -> HALF_OPEN."""
    config = CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout_seconds=0.05,
        half_open_max_requests=1,
    )
    breaker = AtomicCircuitBreaker(config)

    # Open the circuit
    await breaker.record_failure("error 1")
    await breaker.record_failure("error 2")
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.06)

    # Launch 100 concurrent can_execute() calls
    results = await asyncio.gather(*[
        breaker.can_execute()
        for _ in range(100)
    ])

    # Only 1 should have been allowed
    assert sum(results) == 1
    assert breaker.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_get_state_info():
    """get_state_info should return comprehensive state information."""
    config = CircuitBreakerConfig(
        failure_threshold=3,
    )
    breaker = AtomicCircuitBreaker(config)

    await breaker.record_failure("error")
    await breaker.record_failure("error 2")
    # Note: record_success in CLOSED state resets failure_count
    # So we just check failures here

    info = breaker.get_state_info()

    assert info['state'] == 'CLOSED'
    assert info['failure_count'] == 2  # Two failures recorded
    assert 'last_failure' in info
    assert info['last_failure'] is not None
    assert 'config' in info
    assert info['config']['failure_threshold'] == 3


@pytest.mark.asyncio
async def test_reset_clears_state():
    """reset should clear all state."""
    config = CircuitBreakerConfig(
        failure_threshold=2,
    )
    breaker = AtomicCircuitBreaker(config)

    # Open the circuit
    await breaker.record_failure("error 1")
    await breaker.record_failure("error 2")
    assert breaker.state == CircuitState.OPEN

    # Reset
    breaker.reset()

    assert breaker.state == CircuitState.CLOSED
    info = breaker.get_state_info()
    assert info['failure_count'] == 0
    assert info['success_count'] == 0


@pytest.mark.asyncio
async def test_connection_refused_tracking():
    """Connection refused errors should be tracked separately."""
    config = CircuitBreakerConfig(failure_threshold=5)
    breaker = AtomicCircuitBreaker(config)

    await breaker.record_failure("Connection refused")
    await breaker.record_failure("Connection refused")
    await breaker.record_failure("Timeout error")

    info = breaker.get_state_info()
    assert info['connection_refused_count'] == 2


@pytest.mark.asyncio
async def test_closed_state_allows_all():
    """CLOSED state should allow all requests."""
    breaker = AtomicCircuitBreaker()

    # All requests should be allowed
    for _ in range(100):
        assert await breaker.can_execute() is True

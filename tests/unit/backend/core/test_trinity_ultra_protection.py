import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.trinity_integrator import TrinityUltraCoordinator


def _stub_circuit(*, can_execute=(True, "closed"), state="closed"):
    return SimpleNamespace(
        can_execute=AsyncMock(return_value=can_execute),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
        state=state,
    )


def _stub_backpressure(*, acquired=True, delay_ms=0.0):
    return SimpleNamespace(
        acquire=AsyncMock(return_value=(acquired, delay_ms)),
        release=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_execute_with_protection_allows_concurrent_requests_for_same_component():
    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit()
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure()

    release = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def _operation(name, started, cancelled):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return name

    first_call = asyncio.create_task(
        coordinator.execute_with_protection(
            component="prime_router",
            operation=lambda: _operation("first", first_started, first_cancelled),
            timeout=1.0,
        )
    )
    await first_started.wait()

    second_call = asyncio.create_task(
        coordinator.execute_with_protection(
            component="prime_router",
            operation=lambda: _operation("second", second_started, second_cancelled),
            timeout=1.0,
        )
    )
    await second_started.wait()

    release.set()
    first_result, second_result = await asyncio.gather(first_call, second_call)

    assert first_result[0] is True
    assert first_result[1] == "first"
    assert second_result[0] is True
    assert second_result[1] == "second"
    assert first_cancelled.is_set() is False
    assert second_cancelled.is_set() is False
    assert coordinator._shielded_tasks == {}


@pytest.mark.asyncio
async def test_timeout_cleanup_does_not_untrack_newer_request(monkeypatch):
    monkeypatch.setenv("JARVIS_SHIELD_GRACE_S", "0.01")

    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit()
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure()

    first_cancelled = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()

    async def _first_operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancelled.set()
            raise

    async def _second_operation():
        second_started.set()
        await second_release.wait()
        return "second"

    first_success, _, first_metadata = await coordinator.execute_with_protection(
        component="prime_router",
        operation=_first_operation,
        timeout=0.01,
    )

    assert first_success is False
    first_operation_id = first_metadata["operation_id"]
    assert first_operation_id in coordinator._shielded_tasks

    second_call = asyncio.create_task(
        coordinator.execute_with_protection(
            component="prime_router",
            operation=_second_operation,
            timeout=1.0,
        )
    )
    await second_started.wait()

    await asyncio.sleep(0.05)

    assert first_operation_id not in coordinator._shielded_tasks
    assert len(coordinator._shielded_tasks) == 1

    second_release.set()
    second_success, second_result, _ = await second_call

    assert second_success is True
    assert second_result == "second"
    assert first_cancelled.is_set() is True
    assert coordinator._shielded_tasks == {}


@pytest.mark.asyncio
async def test_execute_with_protection_returns_normalized_circuit_open_metadata():
    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit(
        can_execute=(False, "Circuit open, waiting for recovery"),
        state="open",
    )
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure()

    success, result, metadata = await coordinator.execute_with_protection(
        component="prime_router",
        operation=lambda: asyncio.sleep(0),
        timeout=1.0,
    )

    assert success is False
    assert result is None
    assert metadata["error_code"] == "circuit_open"
    assert metadata["failure_class"] == "circuit_open"
    assert metadata["origin_layer"] == "trinity_ultra_coordinator"
    assert metadata["retryable"] is True
    assert metadata["circuit_open"] is True
    assert "Circuit breaker open" in metadata["error_message"]


@pytest.mark.asyncio
async def test_execute_with_protection_returns_normalized_backpressure_metadata():
    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit()
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure(acquired=False, delay_ms=250.0)

    success, result, metadata = await coordinator.execute_with_protection(
        component="prime_router",
        operation=lambda: asyncio.sleep(0),
        timeout=1.0,
    )

    assert success is False
    assert result is None
    assert metadata["error_code"] == "backpressure_rejected"
    assert metadata["failure_class"] == "overload"
    assert metadata["retryable"] is True
    assert metadata["delay_ms"] == 250.0
    assert metadata["backpressure_dropped"] is True
    circuit.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_with_protection_returns_normalized_timeout_metadata(monkeypatch):
    monkeypatch.setenv("JARVIS_SHIELD_GRACE_S", "0.01")

    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit()
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure()

    cancelled = asyncio.Event()

    async def _slow_operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    success, result, metadata = await coordinator.execute_with_protection(
        component="prime_router",
        operation=_slow_operation,
        timeout=0.01,
    )
    await asyncio.sleep(0.05)

    assert success is False
    assert result is None
    assert metadata["error_code"] == "timeout"
    assert metadata["failure_class"] == "timeout"
    assert metadata["retryable"] is True
    assert metadata["timeout"] is True
    assert cancelled.is_set() is True
    assert coordinator._shielded_tasks == {}


@pytest.mark.asyncio
async def test_execute_with_protection_returns_normalized_exception_metadata():
    coordinator = TrinityUltraCoordinator()
    circuit = _stub_circuit()
    coordinator._circuit_breakers["prime_router"] = circuit
    coordinator._backpressure = _stub_backpressure()

    async def _failing_operation():
        raise RuntimeError("dependency blew up")

    success, result, metadata = await coordinator.execute_with_protection(
        component="prime_router",
        operation=_failing_operation,
        timeout=1.0,
    )

    assert success is False
    assert result is None
    assert metadata["error_code"] == "operation_exception"
    assert metadata["failure_class"] == "dependency_failure"
    assert metadata["retryable"] is False
    assert metadata["error_message"] == "dependency blew up"
    assert coordinator._shielded_tasks == {}

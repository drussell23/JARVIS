from unittest.mock import AsyncMock

import pytest

import backend.core.prime_router as prime_router_module
from backend.core.prime_router import PrimeRouter, RoutingDecision


@pytest.mark.asyncio
async def test_generate_normalizes_protection_failure_metadata(monkeypatch):
    router = PrimeRouter()
    router._initialized = True

    ultra_coord = AsyncMock()
    ultra_coord.execute_with_protection.return_value = (
        False,
        None,
        {
            "error_code": "timeout",
            "error_message": "Timeout after 1.0s",
            "failure_class": "timeout",
            "origin_layer": "trinity_ultra_coordinator",
            "retryable": True,
            "trace_id": "trace-123",
        },
    )

    monkeypatch.setattr(
        prime_router_module,
        "_get_ultra_coordinator",
        AsyncMock(return_value=ultra_coord),
    )

    response = await router.generate(prompt="hello")

    assert response.source == "degraded"
    assert response.metadata["error_code"] == "timeout"
    assert response.metadata["error_message"] == "Timeout after 1.0s"
    assert response.metadata["origin_layer"] == "trinity_ultra_coordinator"
    assert response.metadata["retryable"] is True
    assert response.metadata["trace_id"] == "trace-123"
    assert response.metadata["v88_error"] == "Timeout after 1.0s"
    assert response.metadata["reason"] == "timeout"


@pytest.mark.asyncio
async def test_generate_internal_exception_returns_normalized_degraded_metadata(monkeypatch):
    router = PrimeRouter()
    router._initialized = True

    monkeypatch.setattr(
        prime_router_module,
        "_get_ultra_coordinator",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(router, "_decide_route", lambda: RoutingDecision.CLOUD_CLAUDE)

    async def _fail_cloud(*_args, **_kwargs):
        raise RuntimeError("cloud unavailable")

    monkeypatch.setattr(router, "_generate_cloud", _fail_cloud)

    response = await router.generate(prompt="hello")

    assert response.source == "degraded"
    assert response.metadata["error_code"] == "dependency_unavailable"
    assert response.metadata["error_message"] == "cloud unavailable"
    assert response.metadata["origin_layer"] == "prime_router"
    assert response.metadata["retryable"] is True
    assert response.metadata["v88_error"] == "cloud unavailable"


def test_generate_degraded_uses_normalized_metadata():
    router = PrimeRouter()

    response = router._generate_degraded("hello")

    assert response.source == "degraded"
    assert response.metadata["error_code"] == "no_backend_available"
    assert response.metadata["error_message"] == "No backend available"
    assert response.metadata["origin_layer"] == "prime_router"
    assert response.metadata["retryable"] is True
    assert response.metadata["reason"] == "no_backend_available"

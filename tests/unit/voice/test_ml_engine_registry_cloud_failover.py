import time

import pytest

from backend.core.async_safety import LazyAsyncLock
from backend.voice_unlock.ml_engine_registry import (
    CloudEmbeddingCircuitBreaker,
    MLEngineRegistry,
)


def _make_registry() -> MLEngineRegistry:
    """Build a minimal registry instance without full engine initialization."""
    registry = MLEngineRegistry.__new__(MLEngineRegistry)
    registry._use_cloud = True
    registry._cloud_endpoint = "https://primary.example"
    registry._cloud_endpoint_source = "primary"
    registry._cloud_verified = True
    registry._cloud_last_verified = time.time()
    registry._cloud_contract_verified = True
    registry._cloud_contract_endpoint = "https://primary.example"
    registry._cloud_contract_last_checked = time.time()
    registry._cloud_contract_last_error = ""
    registry._cloud_api_failure_streak = 2
    registry._cloud_api_last_failure_at = time.time()
    registry._cloud_api_degraded_until = time.time() + 60.0
    registry._cloud_api_last_error = "primary failure"
    registry._cloud_api_last_cooldown_log_at = 0.0
    registry._cloud_endpoint_failure_streak = {}
    registry._cloud_endpoint_last_failure_at = {}
    registry._cloud_endpoint_degraded_until = {}
    registry._cloud_endpoint_last_error = {}
    registry._cloud_failover_lock = LazyAsyncLock()
    registry._cloud_embedding_cb = CloudEmbeddingCircuitBreaker()
    registry._startup_decision = None
    return registry


@pytest.mark.asyncio
async def test_cloud_failover_switches_to_healthy_alternate(monkeypatch):
    registry = _make_registry()

    async def _fake_discovery():
        return [
            ("https://primary.example", "primary"),
            ("https://backup.example", "backup"),
        ]

    async def _fake_verify(endpoint=None, timeout=None, force=False):
        if endpoint == "https://backup.example":
            return True, "ok"
        return False, "primary unhealthy"

    monkeypatch.setattr(registry, "_discover_cloud_endpoint_candidates", _fake_discovery)
    monkeypatch.setattr(registry, "_verify_cloud_endpoint_contract", _fake_verify)

    switched = await registry._attempt_cloud_endpoint_failover(
        trigger="test",
        failed_endpoint="https://primary.example",
    )

    assert switched is True
    assert registry._cloud_endpoint == "https://backup.example"
    assert registry._cloud_endpoint_source.endswith("|failover")
    assert registry._cloud_api_degraded_until == 0.0
    assert registry._cloud_api_failure_streak == 0


@pytest.mark.asyncio
async def test_cloud_failover_skips_endpoint_in_backoff(monkeypatch):
    registry = _make_registry()
    registry._cloud_endpoint_degraded_until["https://backup.example"] = time.time() + 120.0
    verify_calls = []

    async def _fake_discovery():
        return [
            ("https://primary.example", "primary"),
            ("https://backup.example", "backup"),
        ]

    async def _fake_verify(endpoint=None, timeout=None, force=False):
        verify_calls.append(endpoint)
        return True, "ok"

    monkeypatch.setattr(registry, "_discover_cloud_endpoint_candidates", _fake_discovery)
    monkeypatch.setattr(registry, "_verify_cloud_endpoint_contract", _fake_verify)

    switched = await registry._attempt_cloud_endpoint_failover(
        trigger="test",
        failed_endpoint="https://primary.example",
    )

    assert switched is False
    assert verify_calls == []
    assert registry._cloud_endpoint == "https://primary.example"


def test_endpoint_failure_backoff_grows_with_streak():
    registry = _make_registry()
    endpoint = "https://primary.example"

    registry._record_cloud_endpoint_failure(endpoint, reason="first")
    first_remaining = registry._cloud_endpoint_degraded_until[endpoint.rstrip("/")] - time.time()

    registry._record_cloud_endpoint_failure(endpoint, reason="second")
    second_remaining = registry._cloud_endpoint_degraded_until[endpoint.rstrip("/")] - time.time()

    assert second_remaining >= first_remaining
    assert registry._cloud_endpoint_failure_streak[endpoint.rstrip("/")] >= 2


@pytest.mark.asyncio
async def test_verify_cloud_backend_ready_uses_startup_authority_contract_timeout(monkeypatch):
    registry = _make_registry()
    registry._cloud_endpoint = "https://jarvis-ml.example.run.app"
    registry._cloud_endpoint_source = "cloud_run_env|startup_authority"

    captured = {"timeout": None}

    async def _fake_cross_repo_state():
        return {}

    async def _fake_verify_contract(endpoint=None, timeout=None, force=False):
        captured["timeout"] = timeout
        return False, "synthetic timeout"

    monkeypatch.setenv("JARVIS_CLOUD_STRICT_CONTRACT", "true")
    monkeypatch.setenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0")
    monkeypatch.setenv("JARVIS_CLOUD_CONTRACT_TIMEOUT_STARTUP_AUTHORITY", "12.0")
    monkeypatch.setattr(registry, "_read_cross_repo_ecapa_state", _fake_cross_repo_state)
    monkeypatch.setattr(registry, "_verify_cloud_endpoint_contract", _fake_verify_contract)

    ok, _reason = await registry._verify_cloud_backend_ready(timeout=30.0, retry_count=1)

    assert ok is False
    assert captured["timeout"] == 12.0


@pytest.mark.asyncio
async def test_startup_backend_probe_timeout_defaults_to_startup_authority_floor(monkeypatch):
    registry = _make_registry()
    registry._cloud_endpoint = None
    registry._cloud_endpoint_source = "none"
    registry._memory_gate_blocked = False
    registry._last_routing_reason = ""

    captured = {"timeout": None}

    async def _fake_discovery():
        return [("https://jarvis-ml.example.run.app", "cloud_run_env")]

    async def _fake_verify_ready(
        timeout=None,
        retry_count=None,
        test_extraction=None,
        wait_for_ecapa=None,
        ecapa_wait_timeout=None,
    ):
        captured["timeout"] = timeout
        return False, "synthetic startup probe fail"

    monkeypatch.delenv("JARVIS_ECAPA_STARTUP_PROBE_TIMEOUT", raising=False)
    monkeypatch.setenv("JARVIS_CLOUD_CONTRACT_TIMEOUT", "4.0")
    monkeypatch.setenv("JARVIS_CLOUD_CONTRACT_TIMEOUT_STARTUP_AUTHORITY", "12.0")
    monkeypatch.setattr(registry, "_discover_cloud_endpoint_candidates", _fake_discovery)
    monkeypatch.setattr(registry, "_verify_cloud_backend_ready", _fake_verify_ready)
    monkeypatch.setattr(registry, "_cloud_endpoint_probe_allowed", lambda _endpoint: True)
    monkeypatch.setattr(registry, "_is_local_startup_backend_allowed", lambda: (False, "blocked-for-test"))

    result = await registry.determine_startup_backend(source="test")

    assert result["selected_backend"] is None
    assert captured["timeout"] == 12.0

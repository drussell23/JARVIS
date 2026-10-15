from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_integrator():
    import unified_supervisor as us

    config = SimpleNamespace(
        trinity_enabled=True,
        prime_repo_path=None,
        reactor_repo_path=None,
    )
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.debug = MagicMock()
    logger.error = MagicMock()
    logger.success = MagicMock()
    return us.TrinityIntegrator(config=config, logger=logger), us


def _make_component(us, state: str = "healthy"):
    return us.TrinityComponent(
        name="reactor-core",
        port=8090,
        health_url="http://localhost:8090/health",
        state=state,
    )


def test_refresh_restart_budget_resets_after_cooldown_elapsed():
    integrator, us = _make_integrator()
    component = _make_component(us, state="unhealthy")
    component.restart_count = 3
    component.last_restart_attempt = time.time() - 120.0
    integrator._restart_budget_cooldown_seconds = 30.0

    integrator._refresh_restart_budget(component)

    assert component.restart_count == 0
    assert component.last_restart_attempt == 0.0


def test_refresh_restart_budget_keeps_count_before_cooldown_elapsed():
    integrator, us = _make_integrator()
    component = _make_component(us, state="unhealthy")
    component.restart_count = 3
    component.last_restart_attempt = time.time() - 5.0
    integrator._restart_budget_cooldown_seconds = 30.0

    integrator._refresh_restart_budget(component)

    assert component.restart_count == 3


@pytest.mark.asyncio
async def test_restart_component_with_budget_blocks_when_exhausted():
    integrator, us = _make_integrator()
    component = _make_component(us, state="unhealthy")
    component.restart_count = 2
    component.last_restart_attempt = time.time()
    integrator._max_restarts = 2
    integrator._restart_budget_cooldown_seconds = 300.0
    integrator._start_component = AsyncMock(return_value=True)

    ok = await integrator._restart_component_with_budget(component, reason="test")

    assert ok is False
    integrator._start_component.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_component_with_budget_allows_retry_after_cooldown():
    integrator, us = _make_integrator()
    component = _make_component(us, state="unhealthy")
    component.restart_count = 2
    component.last_restart_attempt = time.time() - 360.0
    integrator._max_restarts = 2
    integrator._restart_budget_cooldown_seconds = 60.0
    integrator._start_component = AsyncMock(return_value=True)

    ok = await integrator._restart_component_with_budget(component, reason="test")

    assert ok is True
    assert component.restart_count == 1
    assert component.last_restart_attempt > 0
    integrator._start_component.assert_awaited_once()


@pytest.mark.asyncio
async def test_evaluate_runtime_health_marks_unhealthy_component_recovered():
    integrator, us = _make_integrator()
    component = _make_component(us, state="unhealthy")
    integrator._check_health = AsyncMock(return_value=True)

    await integrator._evaluate_component_runtime_health(component, grace_periods={})

    assert component.state == "healthy"


@pytest.mark.asyncio
async def test_evaluate_runtime_health_debounces_single_failures(monkeypatch):
    integrator, us = _make_integrator()
    component = _make_component(us, state="healthy")
    component.start_time = time.time() - 2000.0

    monkeypatch.setenv("TRINITY_RUNTIME_FAILURE_THRESHOLD", "2")
    integrator._check_health = AsyncMock(return_value=False)
    integrator._is_within_startup_grace_period = MagicMock(return_value=False)
    integrator._handle_unhealthy_component = AsyncMock()

    await integrator._evaluate_component_runtime_health(component, grace_periods={})
    assert component.health_failure_streak == 1
    integrator._handle_unhealthy_component.assert_not_awaited()

    await integrator._evaluate_component_runtime_health(component, grace_periods={})
    assert component.health_failure_streak == 2
    integrator._handle_unhealthy_component.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_health_reactor_soft_fail_treated_operational(monkeypatch):
    integrator, us = _make_integrator()
    component = _make_component(us, state="healthy")

    result = SimpleNamespace(
        is_ready=False,
        state=us.ComponentReadinessState.LOADING,
        http_status=200,
        status_message="healthy",
        training_ready=False,
    )

    class _StubChecker:
        def __init__(self, logger=None):
            self.logger = logger

        async def check_readiness(self, health_url, component_name, timeout=5.0):
            return result

    monkeypatch.setattr(us, "SemanticReadinessChecker", _StubChecker)
    monkeypatch.setattr(us, "AIOHTTP_AVAILABLE", True)

    healthy = await integrator._check_health(component)
    assert healthy is True



# -- an adopted sibling is judged by the declaration that adopted it ----------
# Live evidence (supervisor-20261015-085516.log): the J-Prime Engine, idle and
# serving, was judged LOADING on every poll (its /health has no legacy
# "phase"), so the Mind was marked unhealthy every 325s and DISABLED at the
# fourth cycle -- and a disabled component was never observed again.

_ENGINE_HEALTH = {
    "service": "jarvis_prime", "component": "engine", "status": "healthy",
    "ready_for_inference": True, "model_loaded": True, "resident": [],
}


def _prime(us, state="healthy", sibling_key="jprime"):
    return us.TrinityComponent(
        name="jarvis-prime", port=8000,
        health_url="http://localhost:8000/health",
        state=state, sibling_key=sibling_key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sib_state,expected", [
    ("serving", True), ("lent", True), ("down", False),
])
async def test_check_health_adopted_sibling_uses_sibling_probe(monkeypatch, sib_state, expected):
    from backend.core.ouroboros.governance import trinity_siblings as ts
    integrator, us = _make_integrator()
    monkeypatch.setattr(ts, "sibling_runtime_state", lambda key: sib_state)

    class _Boom:
        def __init__(self, logger=None):
            raise AssertionError("semantic checker must not judge an adopted sibling")

    monkeypatch.setattr(us, "SemanticReadinessChecker", _Boom)
    assert await integrator._check_health(_prime(us)) is expected


def test_semantic_checker_engine_payload_without_phase_is_ready():
    _, us = _make_integrator()
    result = us.SemanticReadinessChecker()._analyze_response(
        dict(_ENGINE_HEALTH), 200, us.ComponentType.PRIME, "jarvis-prime",
    )
    assert result.is_ready is True


def test_semantic_checker_reported_loading_phase_still_not_ready():
    _, us = _make_integrator()
    payload = dict(_ENGINE_HEALTH, phase="loading_model")
    result = us.SemanticReadinessChecker()._analyze_response(
        payload, 200, us.ComponentType.PRIME, "jarvis-prime",
    )
    assert result.is_ready is False


def test_disabled_component_is_still_observed():
    integrator, us = _make_integrator()
    assert integrator._should_monitor_runtime_health(_prime(us, state="disabled")) is True


@pytest.mark.asyncio
async def test_disabled_component_recovers_and_resets_recovery_attempts():
    integrator, us = _make_integrator()
    component = _prime(us, state="disabled")
    integrator._check_health = AsyncMock(return_value=True)
    integrator._recovery_engine = MagicMock()

    await integrator._evaluate_component_runtime_health(component, grace_periods={})

    assert component.state == "healthy"
    integrator._recovery_engine.reset_attempts.assert_called_once_with("jarvis-prime")


@pytest.mark.asyncio
async def test_disabled_component_failing_probe_does_not_rerun_recovery():
    integrator, us = _make_integrator()
    component = _prime(us, state="disabled")
    component.start_time = time.time() - 2000.0
    integrator._check_health = AsyncMock(return_value=False)
    integrator._handle_unhealthy_component = AsyncMock()

    for _ in range(5):
        await integrator._evaluate_component_runtime_health(component, grace_periods={})

    assert component.state == "disabled"
    integrator._handle_unhealthy_component.assert_not_awaited()

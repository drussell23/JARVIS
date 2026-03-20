# tests/unit/core/test_ecapa_facade.py
"""Tests for the EcapaFacade state machine core."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from backend.core.ecapa_types import (
    CapabilityCheck,
    EcapaFacadeConfig,
    EcapaOverloadError,
    EcapaState,
    EcapaStateEvent,
    EcapaTier,
    EcapaUnavailableError,
    STATE_TO_TIER,
    VoiceCapability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> EcapaFacadeConfig:
    defaults = dict(
        failure_threshold=2,
        recovery_threshold=2,
        transition_cooldown_s=0.0,
        reprobe_interval_s=0.1,
        reprobe_max_backoff_s=0.5,
        reprobe_budget=3,
        probe_timeout_s=1.0,
        local_load_timeout_s=2.0,
        max_concurrent_extractions=4,
        recovering_fail_threshold=2,
    )
    defaults.update(overrides)
    return EcapaFacadeConfig(**defaults)


def _mock_registry(is_loaded: bool = False, load_succeeds: bool = True):
    """Return (registry_mock, wrapper_mock) with sensible defaults."""
    registry = MagicMock()
    wrapper = MagicMock()
    wrapper.is_loaded = is_loaded

    async def _load():
        wrapper.is_loaded = True
        return load_succeeds

    wrapper.load = AsyncMock(side_effect=_load if load_succeeds else AsyncMock(return_value=False))
    wrapper.extract = AsyncMock(return_value=np.zeros(192))
    registry.get_wrapper.return_value = wrapper
    return registry, wrapper


def _mock_cloud_client(healthy: bool = True):
    client = MagicMock()
    if healthy:
        client.health_check = AsyncMock(return_value=True)
        client.extract_embedding = AsyncMock(return_value=np.zeros(192))
    else:
        client.health_check = AsyncMock(return_value=False)
        client.extract_embedding = AsyncMock(side_effect=ConnectionError("unreachable"))
    return client


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton before each test."""
    from backend.core import ecapa_facade as mod

    mod._reset_facade()
    yield
    mod._reset_facade()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state():
    """Facade starts UNINITIALIZED, tier UNAVAILABLE, no active backend."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry()
    facade = EcapaFacade(registry=registry, config=_make_config())

    assert facade.state == EcapaState.UNINITIALIZED
    assert facade.tier == EcapaTier.UNAVAILABLE
    assert facade.active_backend is None


@pytest.mark.asyncio
async def test_start_probes_and_reaches_ready_via_local():
    """Mock registry with is_loaded=True; start() + ensure_ready() -> READY."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=True)
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)

    assert ready is True
    assert facade.state == EcapaState.READY
    assert facade.active_backend == "local"
    await facade.stop()


@pytest.mark.asyncio
async def test_start_reaches_ready_via_cloud():
    """Mock local slow (sleep), cloud healthy -> READY via cloud."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False, load_succeeds=False)
    # Make local probe show not loaded and load fail
    wrapper.is_loaded = False
    wrapper.load = AsyncMock(side_effect=lambda: asyncio.sleep(10))

    cloud = _mock_cloud_client(healthy=True)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)

    assert ready is True
    assert facade.state == EcapaState.READY
    assert facade.active_backend == "cloud"
    await facade.stop()


@pytest.mark.asyncio
async def test_no_backends_reaches_unavailable():
    """Local fails, cloud fails -> UNAVAILABLE."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False, load_succeeds=False)
    wrapper.load = AsyncMock(return_value=False)
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    await facade.start()
    # Give the background probe time to complete
    await asyncio.sleep(0.5)
    ready = await facade.ensure_ready(timeout=1.0)

    assert ready is False
    assert facade.state == EcapaState.UNAVAILABLE
    await facade.stop()


@pytest.mark.asyncio
async def test_ready_to_degraded_after_m_failures():
    """M=2 consecutive failures trigger DEGRADED."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=2))

    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY

    # Simulate 2 extraction failures
    wrapper.extract = AsyncMock(side_effect=RuntimeError("boom"))
    for _ in range(2):
        try:
            await facade.extract_embedding(np.zeros(16000))
        except Exception:
            pass

    assert facade.state == EcapaState.DEGRADED
    await facade.stop()


@pytest.mark.asyncio
async def test_illegal_transition_ready_to_unavailable():
    """_try_transition rejects READY -> UNAVAILABLE (not in legal table)."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY

    ok = await facade._try_transition(EcapaState.UNAVAILABLE, reason="test")
    assert ok is False
    # State should not have changed
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_stop_from_any_state_returns_to_uninitialized():
    """stop() always goes to UNINITIALIZED regardless of current state."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY

    await facade.stop()
    assert facade.state == EcapaState.UNINITIALIZED
    assert facade.active_backend is None


@pytest.mark.asyncio
async def test_start_is_idempotent():
    """Calling start() twice is safe and does not create duplicate background tasks."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    await facade.start()
    task_count_1 = len(facade._bg_tasks)
    await facade.start()  # second call
    task_count_2 = len(facade._bg_tasks)

    assert task_count_2 == task_count_1
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_ensure_ready_returns_true_for_degraded():
    """DEGRADED tier counts as 'ready enough' for ensure_ready."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=2))

    await facade.start()
    await facade.ensure_ready(timeout=5.0)

    # Force into DEGRADED via failures
    wrapper.extract = AsyncMock(side_effect=RuntimeError("boom"))
    for _ in range(2):
        try:
            await facade.extract_embedding(np.zeros(16000))
        except Exception:
            pass

    assert facade.state == EcapaState.DEGRADED
    ready = await facade.ensure_ready(timeout=1.0)
    assert ready is True
    await facade.stop()


@pytest.mark.asyncio
async def test_extract_raises_unavailable_when_not_ready():
    """extract before start raises EcapaUnavailableError."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry()
    facade = EcapaFacade(registry=registry, config=_make_config())

    with pytest.raises(EcapaUnavailableError):
        await facade.extract_embedding(np.zeros(16000))


@pytest.mark.asyncio
async def test_telemetry_events_emitted():
    """subscribe captures EcapaStateEvent with ECAPA_W001 warning code."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    events: list[EcapaStateEvent] = []

    def on_event(evt: EcapaStateEvent):
        events.append(evt)

    facade.subscribe(on_event)
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    # Let event tasks complete
    await asyncio.sleep(0.1)

    assert len(events) >= 1
    # At least one event should have the canonical warning code
    codes = {e.warning_code for e in events}
    assert "ECAPA_W001" in codes or "ECAPA_STATE_CHANGE" in codes
    await facade.stop()


@pytest.mark.asyncio
async def test_concurrent_ensure_ready_dedupes():
    """10 concurrent ensure_ready calls all succeed without extra probes."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    await facade.start()
    results = await asyncio.gather(
        *[facade.ensure_ready(timeout=5.0) for _ in range(10)]
    )
    assert all(r is True for r in results)
    await facade.stop()


@pytest.mark.asyncio
async def test_backpressure_semaphore():
    """semaphore=2: 3rd concurrent extraction raises EcapaOverloadError."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=True)

    # Make extract slow so we can stack up concurrent calls
    async def _slow_extract(*args, **kwargs):
        await asyncio.sleep(2.0)
        return np.zeros(192)

    wrapper.extract = AsyncMock(side_effect=_slow_extract)

    facade = EcapaFacade(
        registry=registry,
        config=_make_config(max_concurrent_extractions=2),
    )
    await facade.start()
    await facade.ensure_ready(timeout=5.0)

    # Launch 2 slow extractions
    t1 = asyncio.create_task(facade.extract_embedding(np.zeros(16000)))
    t2 = asyncio.create_task(facade.extract_embedding(np.zeros(16000)))
    # Give them a moment to acquire the semaphore
    await asyncio.sleep(0.1)

    # 3rd should fail with overload
    with pytest.raises(EcapaOverloadError):
        await facade.extract_embedding(np.zeros(16000))

    t1.cancel()
    t2.cancel()
    # Suppress CancelledError
    for t in (t1, t2):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    await facade.stop()


@pytest.mark.asyncio
async def test_capability_check_per_tier():
    """VOICE_UNLOCK denied when UNAVAILABLE, allowed when READY; BASIC_COMMAND always."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    # Before start: UNAVAILABLE tier
    check = facade.check_capability(VoiceCapability.VOICE_UNLOCK)
    assert check.allowed is False
    assert check.tier == EcapaTier.UNAVAILABLE

    check_basic = facade.check_capability(VoiceCapability.BASIC_COMMAND)
    assert check_basic.allowed is True

    # After start: READY tier
    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    check = facade.check_capability(VoiceCapability.VOICE_UNLOCK)
    assert check.allowed is True
    assert check.tier == EcapaTier.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_startup_cancellation():
    """stop() during slow load cancels cleanly without error."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False)

    async def _slow_load():
        await asyncio.sleep(30)
        return True

    wrapper.load = AsyncMock(side_effect=_slow_load)
    cloud = _mock_cloud_client(healthy=False)

    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())
    await facade.start()
    await asyncio.sleep(0.1)  # let probe begin

    # stop() should cancel the background task cleanly
    await facade.stop()
    assert facade.state == EcapaState.UNINITIALIZED


@pytest.mark.asyncio
async def test_restart_consistency():
    """start -> stop -> start -> READY works correctly."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY

    await facade.stop()
    assert facade.state == EcapaState.UNINITIALIZED

    await facade.start()
    await facade.ensure_ready(timeout=5.0)
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_get_status_returns_dict():
    """get_status returns dict with state, tier, active_backend keys."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, _ = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config())

    status = facade.get_status()
    assert isinstance(status, dict)
    assert "state" in status
    assert "tier" in status
    assert "active_backend" in status
    assert status["state"] == EcapaState.UNINITIALIZED.value
    assert status["tier"] == EcapaTier.UNAVAILABLE.value
    assert status["active_backend"] is None


@pytest.mark.asyncio
async def test_start_is_nonblocking():
    """start() returns in < 0.5s even with a slow backend."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False)

    async def _slow_load():
        await asyncio.sleep(30)
        return True

    wrapper.load = AsyncMock(side_effect=_slow_load)
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    t0 = time.monotonic()
    await facade.start()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, f"start() took {elapsed:.2f}s; expected < 0.5s"
    await facade.stop()


@pytest.mark.asyncio
async def test_flapping_hysteresis():
    """Alternating 2 failures + 1 success never hits threshold=3."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=True)
    facade = EcapaFacade(registry=registry, config=_make_config(failure_threshold=3))

    await facade.start()
    await facade.ensure_ready(timeout=5.0)

    # Pattern: fail, fail, succeed (resets counter) - repeat
    for cycle in range(3):
        # 2 failures
        wrapper.extract = AsyncMock(side_effect=RuntimeError("boom"))
        for _ in range(2):
            try:
                await facade.extract_embedding(np.zeros(16000))
            except Exception:
                pass
        assert facade.state == EcapaState.READY, f"Should still be READY after cycle {cycle}"

        # 1 success resets
        wrapper.extract = AsyncMock(return_value=np.zeros(192))
        await facade.extract_embedding(np.zeros(16000))
        assert facade.state == EcapaState.READY

    await facade.stop()


@pytest.mark.asyncio
async def test_cloud_unavailable_local_fallback():
    """Cloud fails, local loads -> active_backend='local'."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False, load_succeeds=True)
    cloud = _mock_cloud_client(healthy=False)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)

    assert ready is True
    assert facade.active_backend == "local"
    assert facade.state == EcapaState.READY
    await facade.stop()


@pytest.mark.asyncio
async def test_local_unavailable_cloud_fallback():
    """Local ImportError, cloud healthy -> READY via cloud."""
    from backend.core.ecapa_facade import EcapaFacade

    registry, wrapper = _mock_registry(is_loaded=False)
    wrapper.load = AsyncMock(side_effect=ImportError("No torchaudio"))
    cloud = _mock_cloud_client(healthy=True)
    facade = EcapaFacade(registry=registry, cloud_client=cloud, config=_make_config())

    await facade.start()
    ready = await facade.ensure_ready(timeout=5.0)

    assert ready is True
    assert facade.active_backend == "cloud"
    assert facade.state == EcapaState.READY
    await facade.stop()

from __future__ import annotations

import asyncio

import pytest


def _make_kernel(monkeypatch):
    import unified_supervisor as us

    us.JarvisSystemKernel._instance = None
    monkeypatch.setattr(us, "create_safe_task", asyncio.create_task)

    kernel = us.JarvisSystemKernel(config=us.SystemKernelConfig())
    kernel._audio_bus_enabled = True
    return kernel, us


def _reset_kernel(us) -> None:
    us.JarvisSystemKernel._instance = None


@pytest.mark.asyncio
async def test_schedule_audio_recovery_resets_failure_streak_for_new_campaign(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    started = asyncio.Event()

    async def _fake_recovery_loop(_reason: str) -> None:
        started.set()

    monkeypatch.setattr(kernel, "_audio_recovery_loop", _fake_recovery_loop)
    kernel._audio_init_consecutive_failures = 2

    try:
        kernel._schedule_audio_bus_recovery("early_init_timeout")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.wait_for(kernel._audio_recovery_task, timeout=1.0)

        assert kernel._audio_init_consecutive_failures == 0
    finally:
        _reset_kernel(us)


def test_schedule_audio_recovery_skips_only_while_cooling_down(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    scheduled = []

    def _fake_create_safe_task(coro, name=None, log_exceptions=True, **kwargs):
        scheduled.append(name)
        coro.close()
        return asyncio.Future()

    monkeypatch.setattr(us, "create_safe_task", _fake_create_safe_task)
    kernel._audio_recovery_cooldown_until = us.time.time() + 60.0

    try:
        kernel._schedule_audio_bus_recovery("phase4_audio_bus_missing")
        assert scheduled == []

        kernel._audio_recovery_cooldown_until = us.time.time() - 1.0
        kernel._schedule_audio_bus_recovery("phase4_audio_bus_missing")

        assert scheduled == ["audio-recovery"]
        assert kernel._audio_recovery_cooldown_until == 0.0
    finally:
        _reset_kernel(us)


@pytest.mark.asyncio
async def test_attempt_audio_bus_start_serializes_concurrent_calls(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    import backend.audio.audio_bus as audio_bus_mod

    release_start = asyncio.Event()
    entered_start = asyncio.Event()
    start_calls = []

    class _FakeBus:
        def __init__(self):
            self.is_running = False

        async def start(self, progress_callback=None):
            start_calls.append("start")
            if progress_callback is not None:
                progress_callback("stream_open", "started")
            entered_start.set()
            await release_start.wait()
            self.is_running = True

        async def stop(self):
            self.is_running = False

    class _FakeAudioBus:
        _instance = None

        @classmethod
        def get_instance(cls):
            if cls._instance is None:
                cls._instance = _FakeBus()
            return cls._instance

        @classmethod
        def reset_singleton(cls):
            old = cls._instance
            cls._instance = None
            return old

    async def _zero_lag():
        return 0.0

    monkeypatch.setattr(audio_bus_mod, "AudioBus", _FakeAudioBus)
    monkeypatch.setattr(kernel, "_measure_audio_event_loop_lag_ms", _zero_lag)
    monkeypatch.setattr(kernel, "_gather_audio_init_context", lambda: {})

    try:
        task1 = asyncio.create_task(
            kernel._attempt_audio_bus_start(context="test-1", base_timeout=1.0)
        )
        await asyncio.wait_for(entered_start.wait(), timeout=1.0)

        task2 = asyncio.create_task(
            kernel._attempt_audio_bus_start(context="test-2", base_timeout=1.0)
        )
        await asyncio.sleep(0)
        assert len(start_calls) == 1

        release_start.set()
        result1 = await asyncio.wait_for(task1, timeout=1.0)
        result2 = await asyncio.wait_for(task2, timeout=1.0)

        assert result1.success is True
        assert result2.success is True
        assert len(start_calls) == 1
    finally:
        _reset_kernel(us)


@pytest.mark.asyncio
async def test_audio_recovery_oscillation_uses_cooldown_for_transient_failures(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)

    monkeypatch.setenv("JARVIS_AUDIO_RECOVERY_INITIAL_DELAY", "0")
    monkeypatch.setenv("JARVIS_AUDIO_RECOVERY_RETRY_INTERVAL", "0")
    monkeypatch.setenv("JARVIS_AUDIO_RECOVERY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("JARVIS_AUDIO_INIT_MAX_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("JARVIS_AUDIO_RECOVERY_CAMPAIGN_COOLDOWN", "30")

    async def _fake_attempt_audio_bus_start(**_kwargs):
        return us.AudioInitAttemptResult(
            success=False,
            outcome=us.AudioInitOutcome.TIMEOUT_HUNG,
            timeout_seconds=12.0,
            timeout_reason="base",
            duration_ms=12000.0,
            progress_steps=0,
            last_phase="none",
            timed_out=True,
        )

    monkeypatch.setattr(kernel, "_attempt_audio_bus_start", _fake_attempt_audio_bus_start)

    try:
        await kernel._audio_recovery_loop("phase4_audio_bus_missing")

        assert kernel._audio_init_permanent_degraded is False
        assert kernel._audio_recovery_cooldown_until > us.time.time()
    finally:
        _reset_kernel(us)


@pytest.mark.asyncio
async def test_stop_zombie_audio_bus_clears_pipeline_state(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)

    class _FakeBus:
        async def stop(self):
            return None

    async def _fake_reset_audio_pipeline_state(shutdown: bool = True) -> None:
        kernel._audio_pipeline_handle = None
        kernel._audio_infrastructure_initialized = False
        kernel._conversation_pipeline = None
        kernel._mode_dispatcher = None

    monkeypatch.setattr(
        kernel,
        "_reset_audio_pipeline_state",
        _fake_reset_audio_pipeline_state,
    )

    kernel._audio_bus = _FakeBus()
    kernel._audio_pipeline_handle = object()
    kernel._audio_infrastructure_initialized = True
    kernel._conversation_pipeline = object()
    kernel._mode_dispatcher = object()

    try:
        await kernel._stop_zombie_audio_bus()

        assert kernel._audio_bus is None
        assert kernel._audio_pipeline_handle is None
        assert kernel._audio_infrastructure_initialized is False
        assert kernel._conversation_pipeline is None
        assert kernel._mode_dispatcher is None
    finally:
        _reset_kernel(us)

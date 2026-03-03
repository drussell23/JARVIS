from __future__ import annotations

import asyncio

import pytest


def _make_kernel(monkeypatch):
    import unified_supervisor as us

    us.JarvisSystemKernel._instance = None
    monkeypatch.setattr(us, "create_safe_task", asyncio.create_task)

    kernel = us.JarvisSystemKernel(config=us.SystemKernelConfig())
    return kernel, us


def _reset_kernel(us) -> None:
    us.JarvisSystemKernel._instance = None


@pytest.mark.asyncio
async def test_visual_pipeline_init_task_is_single_flight(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def _fake_run(*, outer_timeout: float) -> bool:
        calls.append(outer_timeout)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(
        kernel,
        "_run_visual_pipeline_initialization_with_timeout",
        _fake_run,
    )

    try:
        task1 = kernel._ensure_visual_pipeline_init_task(outer_timeout=12.0)
        await asyncio.wait_for(started.wait(), timeout=1.0)

        task2 = kernel._ensure_visual_pipeline_init_task(outer_timeout=99.0)

        assert task1 is task2
        assert calls == [12.0]

        release.set()
        assert await asyncio.wait_for(task1, timeout=1.0) is True
        await asyncio.sleep(0)
        assert kernel._visual_pipeline_deferred_task is None
    finally:
        _reset_kernel(us)


@pytest.mark.asyncio
async def test_start_deferred_visual_pipeline_initialization_reuses_active_task(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def _fake_run(*, outer_timeout: float) -> bool:
        calls.append(outer_timeout)
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(
        kernel,
        "_run_visual_pipeline_initialization_with_timeout",
        _fake_run,
    )
    monkeypatch.setattr(
        kernel,
        "_get_visual_pipeline_outer_timeout",
        lambda: 17.5,
    )

    try:
        kernel._start_deferred_visual_pipeline_initialization()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        kernel._start_deferred_visual_pipeline_initialization()

        assert calls == [17.5]

        release.set()
        await asyncio.wait_for(kernel._background_tasks[-1], timeout=1.0)
        await asyncio.sleep(0)
        assert kernel._visual_pipeline_deferred_task is None
    finally:
        _reset_kernel(us)


@pytest.mark.asyncio
async def test_visual_pipeline_outer_timeout_marks_error(monkeypatch):
    kernel, us = _make_kernel(monkeypatch)
    release = asyncio.Event()

    async def _never_finishes() -> bool:
        await release.wait()
        return True

    monkeypatch.setattr(kernel, "_initialize_visual_pipeline", _never_finishes)

    try:
        result = await kernel._run_visual_pipeline_initialization_with_timeout(
            outer_timeout=0.01,
        )

        assert result is False
        state = kernel._component_status.get("visual_pipeline", {})
        assert state.get("status") == "error"
        assert "Outer timeout" in state.get("message", "")
    finally:
        release.set()
        _reset_kernel(us)


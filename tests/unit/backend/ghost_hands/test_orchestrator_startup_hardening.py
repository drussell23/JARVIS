from __future__ import annotations

import asyncio

import pytest


def _make_orchestrator():
    import backend.ghost_hands.orchestrator as orchestrator_mod

    orchestrator_mod.GhostHandsOrchestrator._instance = None
    orchestrator = orchestrator_mod.GhostHandsOrchestrator(
        orchestrator_mod.GhostHandsConfig(
            vision_enabled=False,
            actuator_enabled=False,
            narration_enabled=True,
            yabai_enabled=False,
        )
    )
    return orchestrator, orchestrator_mod


def _reset(orchestrator_mod) -> None:
    orchestrator_mod.GhostHandsOrchestrator._instance = None


@pytest.mark.asyncio
async def test_start_returns_while_narration_initializes_in_background(monkeypatch):
    orchestrator, orchestrator_mod = _make_orchestrator()
    init_started = asyncio.Event()
    release_init = asyncio.Event()
    greeted = []

    class _Narration:
        async def narrate_greeting(self) -> None:
            greeted.append("greeted")

        async def stop(self) -> None:
            return None

    async def _fake_init_narration() -> None:
        init_started.set()
        await release_init.wait()
        orchestrator._narration = _Narration()

    monkeypatch.setattr(orchestrator, "_init_narration", _fake_init_narration)

    try:
        await asyncio.wait_for(orchestrator.start(), timeout=1.0)

        assert orchestrator._is_running is True
        assert "narration" in orchestrator._optional_init_tasks
        assert orchestrator._optional_init_tasks["narration"].done() is False

        await asyncio.wait_for(init_started.wait(), timeout=1.0)
        release_init.set()
        await asyncio.wait_for(
            orchestrator._optional_init_tasks["narration"],
            timeout=1.0,
        )

        assert greeted == ["greeted"]
    finally:
        await orchestrator.stop()
        _reset(orchestrator_mod)


@pytest.mark.asyncio
async def test_stop_cancels_optional_narration_init_task(monkeypatch):
    orchestrator, orchestrator_mod = _make_orchestrator()
    cancelled = asyncio.Event()

    async def _never_finish_narration() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(orchestrator, "_init_narration", _never_finish_narration)

    try:
        await asyncio.wait_for(orchestrator.start(), timeout=1.0)
        task = orchestrator._optional_init_tasks["narration"]

        await asyncio.wait_for(orchestrator.stop(), timeout=1.0)

        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        assert task.cancelled()
        assert orchestrator._optional_init_tasks == {}
    finally:
        _reset(orchestrator_mod)

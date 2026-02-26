import sys
import time
from types import SimpleNamespace

import pytest

from backend.core import parallel_initializer as pi


class _DummyApp:
    def __init__(self):
        self.state = SimpleNamespace()
        self.routes = []

    def include_router(self, *_args, **_kwargs):
        return None


class _DummyBroadcaster:
    async def broadcast_complete(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_bridge_import_unavailable_low_ram_forces_sequential(monkeypatch):
    monkeypatch.setattr(pi, "OOM_PREVENTION_AVAILABLE", False)
    monkeypatch.setattr(pi, "get_startup_broadcaster", lambda: _DummyBroadcaster())

    fake_psutil = SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(available=int(2.6 * (1024 ** 3)))
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    initializer = pi.ParallelInitializer(_DummyApp())
    initializer.started_at = time.time()

    monkeypatch.setattr(initializer, "_group_by_priority", lambda: {})
    monkeypatch.setattr(initializer, "_update_progress", lambda: None)
    monkeypatch.setattr(initializer, "_should_fast_forward_startup", lambda: False)

    async def _noop_fast_forward():
        return None

    monkeypatch.setattr(initializer, "_fast_forward_remaining_components", _noop_fast_forward)

    await initializer._background_initialization()

    assert initializer._force_sequential is True
    assert getattr(initializer.app.state, "oom_bridge_available", True) is False


@pytest.mark.asyncio
async def test_shared_effective_mode_gate_skips_heavy_components(monkeypatch):
    monkeypatch.setattr(pi, "OOM_PREVENTION_AVAILABLE", False)
    monkeypatch.setattr(pi, "get_startup_broadcaster", lambda: _DummyBroadcaster())
    monkeypatch.setenv("JARVIS_STARTUP_EFFECTIVE_MODE", "sequential")

    fake_psutil = SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(available=int(8.0 * (1024 ** 3)))
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    initializer = pi.ParallelInitializer(_DummyApp())
    initializer.started_at = time.time()

    heavy_comp = initializer.components["agentic_system"]
    monkeypatch.setattr(initializer, "_group_by_priority", lambda: {55: [heavy_comp]})
    monkeypatch.setattr(initializer, "_update_progress", lambda: None)
    monkeypatch.setattr(initializer, "_should_fast_forward_startup", lambda: False)

    async def _noop_fast_forward():
        return None

    monkeypatch.setattr(initializer, "_fast_forward_remaining_components", _noop_fast_forward)

    await initializer._background_initialization()

    assert getattr(initializer.app.state, "can_spawn_heavy", True) is False
    assert getattr(initializer.app.state, "startup_effective_mode", "") == "sequential"
    assert heavy_comp.phase == pi.InitPhase.SKIPPED
    assert "Heavy admission gate closed" in (heavy_comp.error or "")

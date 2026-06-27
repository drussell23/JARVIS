# tests/governance/test_resource_governor_stagger.py
from __future__ import annotations
import asyncio
import backend.core.ouroboros.governance.intake.intake_layer_service as ILS


class _FakeSensor:
    def __init__(self, name): self.name = name; self.started = False
    async def start(self): self.started = True


def test_off_path_starts_all_sequentially(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED", raising=False)
    svc = ILS.IntakeLayerService.__new__(ILS.IntakeLayerService)
    sensors = [_FakeSensor("a"), _FakeSensor("b")]
    asyncio.run(svc._gated_stagger_activate(sensors))
    assert all(s.started for s in sensors)


def test_high_pressure_holds_then_ignites(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_BASE_MS", "0")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_JITTER_MS", "0")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_STAGGER_HOLD_POLL_S", "0.01")
    from backend.core.ouroboros.governance import memory_pressure_gate as mpg
    levels = iter([mpg.PressureLevel.HIGH, mpg.PressureLevel.HIGH, mpg.PressureLevel.OK])
    monkeypatch.setattr(mpg.MemoryPressureGate, "pressure",
                        lambda self: next(levels, mpg.PressureLevel.OK))
    svc = ILS.IntakeLayerService.__new__(ILS.IntakeLayerService)
    s = _FakeSensor("x")
    asyncio.run(svc._gated_stagger_activate([s]))
    assert s.started   # held during HIGH, ignited once it subsided

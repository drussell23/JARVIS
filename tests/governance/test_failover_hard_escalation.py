"""Gap 2 -- Escalating Outage Promotion (sustained deep-probe drops -> AWAKEN).

Hybrid soak bt-2026-06-29-055555: the deep probe correctly degraded, but the
awaken never fired -- the early-prewarm path needs a HIGH-confidence slow-recovery
forecast, and the reactive path needs a 120s confirm window. A DEAD data plane
should not wait for a recovery forecast.

The fix: a mathematically-defined streak of N consecutive deep-probe drops (the
heartbeat's consecutive_failures) IS the outage confirmation -- it forcefully
promotes to AWAKENING, bypassing the forecast/confirm gate. Gated + injectable.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _low_forecast():
    class _F:
        confidence = "LOW_CONFIDENCE"
        p50_s = 0.0
        p90_s = 0.0
        velocity_hint = 1.0
        samples = 0
    return _F()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DW_HARD_OUTAGE_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_HARD_OUTAGE_STREAK", "3")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, awaken_calls, *, streak, flares=None, **kw):
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: streak >= 2,
        degrade_streak_fn=lambda: streak,
    )
    if flares is not None:
        defaults["flare_fn"] = lambda p: flares.append(p)
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


async def test_hard_streak_forces_awaken_bypassing_forecast(monkeypatch):
    """Streak >= threshold (3) -> forced AWAKEN even with a LOW_CONFIDENCE
    forecast and NO route at full outage (the forecast/confirm gate is bypassed)."""
    clock = FakeClock()
    awaken = []
    ctrl = _make_ctrl(clock, awaken, streak=3)
    monkeypatch.setattr(ctrl, "_get_forecast", _low_forecast)
    # No gradient outage -- the hard streak alone must carry the awaken.
    assert pq.get_provider_health_gradient().any_route_in_outage() is False

    await ctrl.tick()
    assert ctrl.state.name == "AWAKENING"
    assert awaken == [1]


async def test_below_hard_streak_no_forced_awaken(monkeypatch):
    """Streak 2 < threshold 3 -> NOT forced (degrade-only; stays DORMANT)."""
    clock = FakeClock()
    awaken = []
    ctrl = _make_ctrl(clock, awaken, streak=2)
    monkeypatch.setattr(ctrl, "_get_forecast", _low_forecast)
    await ctrl.tick()
    assert ctrl.state.name == "DORMANT"
    assert awaken == []


async def test_hard_escalation_gate_off_is_legacy(monkeypatch):
    """Escalation OFF -> a huge streak does NOT force awaken (byte-identical)."""
    monkeypatch.setenv("JARVIS_DW_HARD_OUTAGE_ESCALATION_ENABLED", "false")
    clock = FakeClock()
    awaken = []
    ctrl = _make_ctrl(clock, awaken, streak=9)
    monkeypatch.setattr(ctrl, "_get_forecast", _low_forecast)
    await ctrl.tick()
    assert ctrl.state.name == "DORMANT"
    assert awaken == []


async def test_hard_escalation_emits_flare(monkeypatch):
    clock = FakeClock()
    awaken = []
    flares = []
    ctrl = _make_ctrl(clock, awaken, streak=4, flares=flares)
    monkeypatch.setattr(ctrl, "_get_forecast", _low_forecast)
    await ctrl.tick()
    assert ctrl.state.name == "AWAKENING"
    assert len(flares) == 1
    assert flares[0]["trigger"] == "heartbeat_hard_outage"

"""Tests for the Immutable Trigger-Attribution Telemetry Flare (Hybrid Mesh).

Mandate (operator, 2026-06-28): the exact moment the FailoverLifecycleController
transitions DORMANT -> AWAKENING, a high-priority immutable payload must record
WHICH signal initiated the failover -- the Deep Inference Probe heartbeat
(early pre-warm) vs the unmasked route-exhaustion outage (reactive). Since GCS is
not wired locally, the flare routes to the local immutable WAL (debug.log).

The flare fires at the TRANSITION instant -- BEFORE the GCE boot is even
attempted -- so the trigger attribution survives even a fail-soft awaken (the
local-Mac no-GCP case). Injected sink -> ZERO real GCS / network.
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


def _fake_forecast(confidence: str, p50: float = 300.0, p90: float = 600.0):
    class _F:
        confidence = ""
        p50_s = 0.0
        p90_s = 0.0
        velocity_hint = 1.0
        samples = 0
    f = _F()
    f.confidence = confidence
    f.p50_s = p50
    f.p90_s = p90
    f.samples = 5 if confidence == "HIGH" else 0
    return f


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, flares, **kw):
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: flares.append(payload),
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


def _fill_outage(route, n=5):
    grad = pq.get_provider_health_gradient()
    for _ in range(n):
        grad.record_sweep(route, success=False)


# ---------------------------------------------------------------------------
# Reactive (unmasked exhaustion) -> flare attributes the outage route
# ---------------------------------------------------------------------------

async def test_reactive_outage_emits_flare_with_route(monkeypatch):
    clock = FakeClock()
    flares = []
    ctrl = _make_ctrl(clock, flares)
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    _fill_outage("background")

    await ctrl.tick()
    assert len(flares) == 1
    f = flares[0]
    assert f["trigger"] == "reactive_outage"
    assert f["route"] == "background"
    assert f["state_to"] == "AWAKENING"


# ---------------------------------------------------------------------------
# Early pre-warm (deep-probe heartbeat) -> flare attributes the heartbeat
# ---------------------------------------------------------------------------

async def test_heartbeat_prewarm_emits_flare(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    clock = FakeClock()
    flares = []
    ctrl = _make_ctrl(clock, flares, is_degrading_fn=lambda: True)
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    # No route at outage -> the heartbeat early-prewarm must carry the awaken.

    await ctrl.tick()
    assert len(flares) == 1
    assert flares[0]["trigger"] == "heartbeat_early_prewarm"


# ---------------------------------------------------------------------------
# The flare is IMMUTABLE w.r.t. the GCE boot outcome -- it fires at the
# transition, so even a fail-soft awaken (local no-GCP) records the trigger.
# ---------------------------------------------------------------------------

async def test_flare_emitted_even_when_awaken_failsoft(monkeypatch):
    clock = FakeClock()
    flares = []
    # vm_awaken_fn returns falsy -> awaken reverts to DORMANT, but the flare
    # must already be recorded (the trigger attribution is undeniable).
    ctrl = _make_ctrl(
        clock, flares, vm_awaken_fn=lambda *, startup_script: False
    )
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    _fill_outage("background")

    await ctrl.tick()
    assert ctrl.state.name == "DORMANT"  # awaken failed-soft
    assert len(flares) == 1  # ...but the trigger flare is immutable
    assert flares[0]["trigger"] == "reactive_outage"


# ---------------------------------------------------------------------------
# Default flare sink writes a high-priority [FailoverFlare] line to the WAL.
# ---------------------------------------------------------------------------

async def test_default_flare_writes_wal_line(monkeypatch, caplog):
    import logging
    clock = FakeClock()
    ctrl = FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        # no flare_fn -> default WAL sink
    )
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    _fill_outage("background")
    with caplog.at_level(logging.WARNING):
        await ctrl.tick()
    assert any("FailoverFlare" in r.message for r in caplog.records)

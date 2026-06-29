"""DETERMINISTIC FAILOVER HARNESS -- the 8-second proof of the layered awaken.

Mandate (operator, 2026-06-28): prove, in a deterministic sub-8-second harness,
that the two NEW layered defenses each independently drive the real
``FailoverLifecycleController`` to awaken J-Prime -- BEFORE we ever spend another
20-minute live soak. Defense in depth: either signal alone is sufficient.

Layer 1 -- Deep Inference Probe (data plane):
    A WEDGED DW inference queue (dispatch hangs) flips the REAL ``DWHeartbeat``
    ``is_degrading()`` in a fraction of a second (asyncio.wait_for kills the
    180s deadlock). Wired into the controller's early-prewarm gate -> AWAKEN.

Layer 2 -- Unmasked Terminal Exhaustion (gradient):
    The topology pre-block path now feeds ``record_terminal_exhaustion`` -> the
    REAL ``ProviderHealthGradient`` reaches ``is_global_outage("background")`` ->
    the controller's reactive path -> AWAKEN.

Everything is REAL except the GCE/network boundaries (vm_awaken_fn etc. are
fakes) and the DW inference dispatch (injected). ZERO real network. The whole
file must run in well under 8 seconds.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
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

    def advance(self, dt: float) -> None:
        self.t += dt


def _fake_forecast(confidence: str, p50: float = 300.0, p90: float = 600.0):
    class _F:
        def __init__(self):
            self.confidence = confidence
            self.p50_s = p50
            self.p90_s = p90
            self.velocity_hint = 1.0
            self.samples = 5 if confidence == "HIGH" else 0
    return _F()


@pytest.fixture(autouse=True)
def _harness_env(monkeypatch, tmp_path):
    # Real singletons, fresh per test.
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    ph._reset_singleton_for_tests()
    # Failover mesh armed (the layered-defense config the soak should run).
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    # Heartbeat + deep probe armed.
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", "0.02")
    monkeypatch.setenv(
        "JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "surface_health.json")
    )
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    ph._reset_singleton_for_tests()


def _make_ctrl(clock, awaken_calls, **kw):
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


def _wedged_heartbeat():
    """A REAL DWHeartbeat whose deep-probe inference dispatch is DEADLOCKED."""
    async def wedged_dispatch():
        await asyncio.sleep(30.0)  # DW inference queue is wedged
        return "1"

    return ph.DWHeartbeat(
        ledger=ph.SurfaceHealthLedger(autosave=False),
        inference_dispatch_fn=wedged_dispatch,
    )


# ---------------------------------------------------------------------------
# Layer 1: the deep probe flips the REAL is_degrading() fast (no 180s wait)
# ---------------------------------------------------------------------------

async def test_layer1_deep_probe_flips_real_is_degrading_fast():
    hb = _wedged_heartbeat()
    t0 = time.monotonic()
    await hb.beat()
    assert hb.is_degrading() is False  # streak 1
    await hb.beat()
    elapsed = time.monotonic() - t0
    assert hb.is_degrading() is True  # streak 2 -> degrading
    assert elapsed < 1.0  # two wedged beats, still a blink (not 2 x 30s)


# ---------------------------------------------------------------------------
# Layer 1 -> AWAKEN: wedged data plane drives the controller's early pre-warm
# ---------------------------------------------------------------------------

async def test_layer1_deep_probe_drives_controller_awaken(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    hb = _wedged_heartbeat()
    await hb.beat()
    await hb.beat()
    assert hb.is_degrading() is True

    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(clock, awaken_calls, is_degrading_fn=hb.is_degrading)
    # HIGH-confidence slow-recovery forecast -> cost gate says PRE-WARM.
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    # No route at full outage -- the deep-probe early-prewarm must carry it alone.
    assert pq.get_provider_health_gradient().any_route_in_outage() is False

    await ctrl.tick()
    assert ctrl.state.name == "AWAKENING"
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# Layer 2 -> AWAKEN: unmasked terminal exhaustion drives the reactive path
# ---------------------------------------------------------------------------

async def test_layer2_unmasked_exhaustion_drives_controller_awaken(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock, awaken_calls,
        is_degrading_fn=lambda: False,  # heartbeat sees nothing -- isolate layer 2
    )
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))

    # The masked path: 5 topology-block exhaustions feed the gradient the truth.
    for _ in range(5):
        pq.record_terminal_exhaustion("background", reason="dw_severed_queued")
    assert pq.get_provider_health_gradient().is_global_outage("background") is True

    await ctrl.tick()
    assert ctrl.state.name == "AWAKENING"
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# Defense in depth: BOTH signals present -> awaken (the indestructible mesh)
# ---------------------------------------------------------------------------

async def test_both_layers_together_awaken(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    hb = _wedged_heartbeat()
    await hb.beat()
    await hb.beat()

    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(clock, awaken_calls, is_degrading_fn=hb.is_degrading)
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    for _ in range(5):
        pq.record_terminal_exhaustion("background", reason="dw_severed_queued")

    await ctrl.tick()
    assert ctrl.state.name == "AWAKENING"
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# THE SAFETY LAW: an auth 401 must NEVER provision infrastructure.
# ---------------------------------------------------------------------------

async def test_auth_401_freezes_heartbeat_and_never_awakens(monkeypatch):
    """A probe that 401s (auth/config error, NOT a DW outage) FREEZES the
    heartbeat -> is_degrading=False + streak=0 -> neither the early-prewarm NOR
    the Gap-2 hard escalation fires -> J-Prime is NOT provisioned. (Reproduces +
    kills the spurious awaken of soak bt-2026-06-29-061928.)"""
    import urllib.error
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_HARD_OUTAGE_ESCALATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_HARD_OUTAGE_STREAK", "3")

    async def _auth_401():
        raise urllib.error.HTTPError(
            "http://aegis/v1/chat/completions", 401, "x", {}, None
        )

    hb = ph.DWHeartbeat(
        ledger=ph.SurfaceHealthLedger(autosave=False),
        inference_dispatch_fn=_auth_401,
    )
    for _ in range(5):  # a real outage would build streak >= 3 and awaken
        await hb.beat()
    assert hb.is_frozen() is True
    assert hb.is_degrading() is False
    assert hb.consecutive_failures() == 0  # auth NEVER counts as outage

    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock, awaken_calls,
        is_degrading_fn=hb.is_degrading, degrade_streak_fn=hb.consecutive_failures,
    )
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))

    await ctrl.tick()
    assert ctrl.state.name == "DORMANT"   # a misconfig provisioned NOTHING
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# Pre-fix regression guard: with BOTH new gates OFF, the run-#13 blindspot
# persists (no awaken) -- proves the fixes are load-bearing, not theatre.
# ---------------------------------------------------------------------------

async def test_prefix_blindspot_persists_with_gates_off(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DW_DEEP_PROBE_ENABLED", "false")

    # Legacy control-plane probe: GET /models is GREEN even while generation is
    # wedged (the exact soak condition). Model it as a healthy probe.
    hb = ph.DWHeartbeat(
        ledger=ph.SurfaceHealthLedger(autosave=False),
        probe_fn=lambda: True,  # control plane answers 200
    )
    await hb.beat()
    await hb.beat()
    assert hb.is_degrading() is False  # control plane looks fine

    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(clock, awaken_calls, is_degrading_fn=hb.is_degrading)
    monkeypatch.setattr(ctrl, "_get_forecast", lambda: _fake_forecast("HIGH"))
    # The topology-block exhaustion is MASKED (gate off) -> gradient never fills.
    for _ in range(5):
        pq.record_terminal_exhaustion("background", reason="dw_severed_queued")
    assert pq.get_provider_health_gradient().is_global_outage("background") is False

    await ctrl.tick()
    assert ctrl.state.name == "DORMANT"  # J-Prime never awoke (the soak)
    assert awaken_calls == []

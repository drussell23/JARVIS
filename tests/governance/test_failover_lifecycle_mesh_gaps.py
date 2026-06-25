"""Tests for the Sovereign Failover Mesh gaps (2026-06-24).

Gap 2 -- degradation -> early pre-warm trigger (wired into failover_lifecycle):
  when the heartbeat reports is_degrading() (DEGRADED but NOT yet a full
  is_global_outage) AND the forecast says recovery is slow (R > C*margin), the
  lifecycle PRE-WARMS J-Prime EARLY (calls vm_awaken_fn) so the node is warm by
  the time DW formally drops the op into the Cryo-DLQ. Fail-CLOSED: low forecast
  confidence / not degrading -> fall back to the reactive is_global_outage path
  (no behavior loss); a transient blip (streak below the degrade streak) does
  NOT pre-warm.

Gap 3a -- J-Prime endpoint wiring post-awaken: after awaken + node-ready, the
  controller RESOLVES the node's reachable endpoint and PUBLISHES it where
  PrimeClient/PrimeProvider reads it (JARVIS_PRIME_URL / JARVIS_PRIME_HOST env).

OFF (all flags false) -> byte-identical: no early pre-warm, no endpoint publish.

TDD with injected fakes -- ZERO real GCE / network.
"""
from __future__ import annotations

import os

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _reset_quarantine_singleton():
    """Reset the gradient singleton on whatever provider_quarantine module is
    currently live in sys.modules. A sibling test (test_provider_quarantine)
    deletes + reimports the module via _fresh_module(); failover_lifecycle
    resolves get_provider_health_gradient() lazily off sys.modules, so we must
    clear the singleton on the CURRENT module object, not a stale reference."""
    import importlib
    import sys as _sys
    mod = _sys.modules.get(
        "backend.core.ouroboros.governance.provider_quarantine"
    ) or importlib.import_module(
        "backend.core.ouroboros.governance.provider_quarantine"
    )
    mod._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    return mod


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    global pq
    pq = _reset_quarantine_singleton()
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    # Gap 2: early pre-warm armed by default in these tests (operator-gated in prod).
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    # Keep VRAM warmup out of the way for endpoint-wiring assertions.
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_ENABLED", "false")
    yield
    _reset_quarantine_singleton()
    fl._reset_singleton_for_tests()


def _fake_forecast(confidence: str, p50: float = 0.0, p90: float = 0.0):
    class _F:
        def __init__(self):
            self.confidence = confidence
            self.p50_s = p50
            self.p90_s = p90
            self.velocity_hint = 1.0
            self.samples = 5 if confidence == "HIGH" else 0
    return _F()


def _make_ctrl(clock, **kw) -> 'fl.FailoverLifecycleController':
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
    )
    defaults.update(kw)
    return fl.FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# Gap 2 -- degradation -> early pre-warm (BEFORE a full outage)
# ---------------------------------------------------------------------------

async def test_early_prewarm_on_degrading_plus_slow_forecast(monkeypatch):
    """is_degrading() True + HIGH-confidence slow forecast (R > C*margin) ->
    AWAKEN early, even though the quarantine window is NOT a full outage."""
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,  # heartbeat says DW degrading
    )
    # R=300 > C*margin=150 -> slow recovery -> worth pre-warming.
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    # NOTE: NO _fill_outage() -- the gradient is NOT a full outage. The ONLY
    # trigger is the early degradation signal.
    assert pq.get_provider_health_gradient().is_global_outage("dw") is False

    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.AWAKENING
    assert len(awaken_calls) == 1


async def test_no_early_prewarm_on_transient_blip(monkeypatch):
    """Degrading + FAST forecast (R < C*margin) -> blip-skip: do NOT pre-warm
    (the Cryo-DLQ would hold the op; DW likely back before J-Prime boots)."""
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,
    )
    # R=40 < C*margin=150 -> blip -> no pre-warm.
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=40.0, p90=80.0))
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.DORMANT
    assert awaken_calls == []


async def test_no_early_prewarm_when_not_degrading(monkeypatch):
    """Heartbeat NOT degrading + no full outage -> stays DORMANT (reactive path
    only); the early gate never fires."""
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: False,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.DORMANT
    assert awaken_calls == []


async def test_early_prewarm_low_confidence_falls_back_to_reactive(monkeypatch):
    """FAIL-CLOSED: degrading but LOW_CONFIDENCE forecast -> the early gate does
    NOT fire (R is unreliable); the controller falls back to the reactive
    is_global_outage path. With no full outage it stays DORMANT."""
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("LOW_CONFIDENCE"))
    await ctrl.tick()
    # Early gate declined (low confidence). No full outage -> DORMANT.
    assert ctrl.state == fl.FailoverState.DORMANT
    assert awaken_calls == []


async def test_reactive_outage_still_awakens_with_early_gate(monkeypatch):
    """The reactive is_global_outage path is preserved: a full outage awakens
    even when the heartbeat says NOT degrading (e.g. heartbeat disabled)."""
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: False,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    grad = pq.get_provider_health_gradient()
    for _ in range(5):
        grad.record_sweep("dw", success=False)
    assert grad.is_global_outage("dw") is True

    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.AWAKENING
    assert len(awaken_calls) == 1


async def test_early_prewarm_master_off_no_prewarm(monkeypatch):
    """Gap-2 sub-gate OFF -> degradation never pre-warms (reactive only)."""
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.DORMANT
    assert awaken_calls == []


async def test_lifecycle_off_no_early_prewarm(monkeypatch):
    """Master lifecycle OFF -> inert: even with degrading + early gate on, the
    controller never leaves DORMANT (byte-identical legacy)."""
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.DORMANT
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# Gap 3a -- J-Prime endpoint wired post-awaken so PrimeProvider uses it.
# ---------------------------------------------------------------------------

async def test_endpoint_published_to_env_on_serving(monkeypatch):
    """On SERVING, the resolved node endpoint is written to JARVIS_PRIME_URL /
    JARVIS_PRIME_HOST (where PrimeClient reads it)."""
    monkeypatch.delenv("JARVIS_PRIME_URL", raising=False)
    monkeypatch.delenv("JARVIS_PRIME_HOST", raising=False)
    # Resolve the node IP deterministically (mock the gcloud describe boundary).
    monkeypatch.setattr(fl, "_resolve_node_ip", lambda: "203.0.113.7")
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    grad = pq.get_provider_health_gradient()
    for _ in range(5):
        grad.record_sweep("dw", success=False)

    await ctrl.tick()  # DORMANT -> AWAKENING
    await ctrl.tick()  # AWAKENING -> SERVING (node ready)
    assert ctrl.state == fl.FailoverState.SERVING

    url = os.environ.get("JARVIS_PRIME_URL", "")
    host = os.environ.get("JARVIS_PRIME_HOST", "")
    assert "203.0.113.7" in url
    assert url.endswith(":11434")
    assert host == "203.0.113.7"
    # The exposed endpoint matches the published env target.
    assert ctrl.jprime_endpoint() == url


async def test_endpoint_publish_failsoft_still_serving(monkeypatch):
    """If endpoint resolution fails, SERVING still proceeds (fail-soft) -- the
    op is never lost; PrimeProvider falls back to its configured target."""
    monkeypatch.setattr(
        fl, "_resolve_node_ip",
        lambda: (_ for _ in ()).throw(RuntimeError("describe failed")),
    )
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    grad = pq.get_provider_health_gradient()
    for _ in range(5):
        grad.record_sweep("dw", success=False)
    await ctrl.tick()
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.SERVING


async def test_off_does_not_publish_endpoint(monkeypatch):
    """OFF -> no endpoint publish (byte-identical legacy): the env stays clean."""
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    monkeypatch.delenv("JARVIS_PRIME_URL", raising=False)
    publishes = []
    monkeypatch.setattr(
        fl, "_resolve_node_ip", lambda: publishes.append(1) or "203.0.113.7"
    )
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    await ctrl.tick()
    assert ctrl.state == fl.FailoverState.DORMANT
    assert os.environ.get("JARVIS_PRIME_URL", "") == ""
    assert publishes == []

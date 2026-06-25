"""Tests for the AUTHORITATIVE real-generation-failure awaken trigger.

Run-#11 blindspot (2026-06-24)
------------------------------
DW's cheap ``dw_heavy_probe`` HeavyProbe showed PARTIAL success (single-token
pings OK) so the probe-based heartbeat ``is_degrading()`` was False -- yet the
ACTUAL BACKGROUND *generation* collapsed (``dw_severed_queued`` ->
``fallback_tolerance=queue``), driving the per-route ProviderHealthGradient for
the ``"background"`` route to rate==0 across a FULL window
(``is_global_outage("background") == True``). The Failover FSM watched ONLY the
single configured ``JARVIS_FAILOVER_ROUTE`` key (default ``"dw"``) -- a key the
live ``candidate_generator.record_sweep`` path NEVER populates -- so the awaken
stayed False and J-Prime never awoke (0 awaken / 0 instances.insert).

The fix wires the FSM's reactive awaken to the AUTHORITATIVE any-route
``is_global_outage`` signal (the record_sweep-driven gradient), gated behind
``JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED`` (default-OFF byte-identical).

ZERO real GCE / network -- every boundary is a fake. The gradient is the real
in-process singleton (reset per test).
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverState,
    FailoverLifecycleController,
)
from backend.core.ouroboros.governance import provider_quarantine as pq


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    # The early pre-warm sub-gate stays OFF in these tests unless a test arms it
    # (we are exercising the REACTIVE authoritative path, not the pre-warm).
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _fill_outage(route: str, n: int = 5) -> None:
    """Push n failed sweeps so is_global_outage(route) -> True (FULL, rate 0)."""
    grad = pq.get_provider_health_gradient()
    for _ in range(n):
        grad.record_sweep(route, success=False)


def _fake_forecast(confidence: str, p50: float = 0.0, p90: float = 0.0):
    class _F:
        def __init__(self):
            self.confidence = confidence
            self.p50_s = p50
            self.p90_s = p90
            self.velocity_hint = 1.0
            self.samples = 5 if confidence == "HIGH" else 0
    return _F()


def _make_ctrl(clock, **kw) -> FailoverLifecycleController:
    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        # is_degrading() == False: the HeavyProbe-passed run-#11 mode. The
        # probe-based heartbeat sees NOTHING wrong; only real generation died.
        is_degrading_fn=lambda: False,
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# (a) Run-#11 mode: probe OK but a real generation route collapsed -> AWAKEN
# ---------------------------------------------------------------------------

async def test_run11_probe_ok_but_background_route_outage_awakens(monkeypatch):
    """The regression we fix: HeavyProbe passes (is_degrading False) but the
    BACKGROUND generation route hit rate==0 -> the FSM NOW awakens J-Prime."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    # HIGH-confidence slow forecast so the cost gate says AWAKEN (R>C*margin).
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))

    # The probe-based heartbeat sees nothing wrong.
    assert ctrl._is_degrading() is False
    # But the BACKGROUND *generation* route collapsed (rate==0, full window).
    _fill_outage("background")
    # And the configured "dw" route never recorded a sweep (the blindspot).
    assert pq.get_provider_health_gradient().is_global_outage("dw") is False

    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert awaken_calls == [1]


async def test_run11_blindspot_persists_when_subgate_off(monkeypatch):
    """OFF (sub-gate not armed) -> byte-identical: the BACKGROUND outage on a
    non-'dw' route does NOT awaken (the exact pre-fix run-#11 behavior)."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage("background")

    await ctrl.tick()
    # Legacy single-route ("dw") check -> no outage -> stays DORMANT.
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# (b) BACKGROUND route specifically triggers it (not just realtime / dw)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["background", "realtime", "standard", "complex"])
async def test_any_generation_route_triggers_awaken(monkeypatch, route):
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(route) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage(route)

    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert awaken_calls == [route]


# ---------------------------------------------------------------------------
# (c) No spurious awaken on a transient blip (window not full / rate > 0)
# ---------------------------------------------------------------------------

async def test_transient_blip_partial_window_no_awaken(monkeypatch):
    """Window not FULL (only 3 of 5 failures) -> NOT an outage -> no awaken."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage("background", n=3)  # window size 5 -> not full

    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []


async def test_full_window_but_one_success_no_awaken(monkeypatch):
    """Full window but rate > 0 (one success) -> NOT an outage -> no awaken.
    Fail-CLOSED: identical threshold to the quarantine seal."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    grad = pq.get_provider_health_gradient()
    for _ in range(4):
        grad.record_sweep("background", success=False)
    grad.record_sweep("background", success=True)  # one success -> rate > 0

    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# (d) The quarantine_op Cryo-DLQ seal connects to the awaken path
# ---------------------------------------------------------------------------

async def test_quarantine_seal_route_outage_drives_awaken(monkeypatch, tmp_path):
    """A quarantine_op seal only fires when is_global_outage(route) is True; that
    SAME gradient state is what the FSM reads. Prove: after a seal on the
    BACKGROUND route, the FSM awakens (the seal and the awaken share the
    gradient -- no new event bus needed)."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PROVIDER_QUARANTINE_ENABLED", "true")
    # Keep the DLQ append off-tree: stub append_dlq so the seal never touches
    # the repo .jarvis/ dir (the seal still returns True -- the gradient state
    # is what matters for the awaken wiring).
    import backend.core.ouroboros.governance.intake_dlq as _dlq
    monkeypatch.setattr(_dlq, "append_dlq", lambda *a, **k: None)
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))

    # Drive the BACKGROUND route into outage (what candidate_generator does on
    # the dw_severed_queued exhaustion), then perform the quarantine seal.
    _fill_outage("background")

    class _Ctx:
        op_id = "op-run11"
        dw_telemetry = None
        provider_override = ""

    assert pq.get_provider_health_gradient().is_global_outage("background") is True
    sealed = pq.quarantine_op(_Ctx(), route="background", telemetry={"x": 1})
    assert sealed is True  # the seal fired on the same gradient state

    # The FSM, reading that same gradient, now awakens.
    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# (e) The probe-based EARLY pre-warm still works (independent path retained)
# ---------------------------------------------------------------------------

async def test_probe_early_prewarm_still_works(monkeypatch):
    """The authoritative any-route signal does NOT replace the heartbeat early
    pre-warm: a DEGRADED probe (is_degrading True) + slow forecast still
    pre-warms even with NO route yet at full outage."""
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        is_degrading_fn=lambda: True,  # heartbeat says DEGRADED
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    # NO route at full outage -- the early pre-warm must carry the awaken.
    assert pq.get_provider_health_gradient().any_route_in_outage() is False

    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# (f) OFF (master) -> byte-identical: never awakens regardless of route outage
# ---------------------------------------------------------------------------

async def test_master_off_no_awaken_even_with_route_outage(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage("background")

    state = await ctrl.tick()
    assert state == FailoverState.DORMANT
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# (g) Explicit operator-pinned extra route folds in even pre-sweep
# ---------------------------------------------------------------------------

async def test_explicit_extra_route_folds_in(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_OUTAGE_ROUTES", "speculative")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage("speculative")

    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert awaken_calls == [1]


# ---------------------------------------------------------------------------
# Gradient unit tests: tracked_routes + any_route_in_outage
# ---------------------------------------------------------------------------

def test_gradient_tracked_routes_enumerates_seen_routes():
    grad = pq.ProviderHealthGradient()
    grad.record_sweep("background", success=False)
    grad.record_sweep("standard", success=True)
    assert set(grad.tracked_routes()) == {"background", "standard"}


def test_gradient_any_route_in_outage_true_only_on_full_zero_window(monkeypatch):
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    grad = pq.ProviderHealthGradient()
    # Partial window -> not outage.
    for _ in range(3):
        grad.record_sweep("background", success=False)
    assert grad.any_route_in_outage() is False
    # Fill it -> outage on that route -> any_route True.
    for _ in range(2):
        grad.record_sweep("background", success=False)
    assert grad.any_route_in_outage() is True


def test_gradient_any_route_in_outage_extra_routes_pre_sweep_noop():
    grad = pq.ProviderHealthGradient()
    # extra_routes for an unseen route reads not-outage (empty window).
    assert grad.any_route_in_outage(extra_routes=["dw", "background"]) is False
    assert grad.any_route_in_outage(extra_routes="dw") is False

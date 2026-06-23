"""Tests for failover_lifecycle.py -- Phase 3b keystone Failover FSM.

TDD with injected fakes -- ZERO real GCE / network. All boundaries
(vm_awaken_fn / vm_delete_fn / dw_probe_fn / node_ready_fn / clock_fn) are
fakes; the quarantine gradient + forecaster are driven via env + the real
in-process singletons (reset per test).

Covered (spec section 12):
  * cryo-trigger AWAKEN iff R > C*margin (HIGH confidence)
  * blip-skip (R < C -> no awaken)
  * LOW_CONFIDENCE -> reactive-floor confirm-window awaken
  * awaken injects the deadman startup-script + Spot-first
  * SERVING probe loop paced by probe_interval
  * HANDBACK requires full recovery + hysteresis + min-uptime
    (a single recovered cycle does NOT hand back)
  * cooldown blocks re-awaken
  * OFF -> inert (never awakens)
  * fail-soft (awaken_fn raising -> stays safe, op never lost)
"""
from __future__ import annotations

import importlib

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
    """Each test gets a fresh quarantine gradient + controller singleton, and
    the lifecycle defaults ON (most tests want it active; OFF test overrides)."""
    # Fresh quarantine gradient singleton.
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    # Lifecycle ON for the active-path tests; reactive defaults explicit.
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "5")
    # Forecaster: deterministic via env (we monkeypatch _get_forecast mostly).
    monkeypatch.setenv("JARVIS_JPRIME_COLDSTART_S", "100")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "1.5")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "120")
    monkeypatch.setenv("JARVIS_RECOVERY_THRESHOLD", "0.6")
    monkeypatch.setenv("JARVIS_RECOVERY_HYSTERESIS_CYCLES", "2")
    monkeypatch.setenv("JARVIS_JPRIME_MIN_UPTIME_S", "300")
    monkeypatch.setenv("JARVIS_HANDBACK_COOLDOWN_S", "300")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _fill_outage(route: str = "dw", n: int = 5) -> None:
    """Push n failed sweeps so is_global_outage(route) -> True (window FULL,
    rate 0.0)."""
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
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


# ---------------------------------------------------------------------------
# Cryo-trigger: AWAKEN iff R > C*margin (HIGH confidence)
# ---------------------------------------------------------------------------

async def test_cryo_awaken_high_confidence_R_above_threshold(monkeypatch):
    clock = FakeClock()
    awaken_calls = []

    def awaken(*, startup_script):
        awaken_calls.append(startup_script)
        return True

    ctrl = _make_ctrl(clock, vm_awaken_fn=awaken)
    # R = p50 = 300; C*margin = 100*1.5 = 150 -> 300 > 150 -> AWAKEN.
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0, p90=600.0))
    _fill_outage()

    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert len(awaken_calls) == 1


async def test_cryo_blip_skip_R_below_C(monkeypatch):
    clock = FakeClock()
    awaken_calls = []

    ctrl = _make_ctrl(
        clock, vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True
    )
    # R = p50 = 40 < C(100) -> blip-skip, stays DORMANT, no awaken call.
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=40.0, p90=80.0))
    _fill_outage()

    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []


async def test_cryo_high_conf_R_between_C_and_threshold_skips(monkeypatch):
    # R must exceed C*margin (150), not just C (100). R=120 -> still skip.
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=120.0, p90=200.0))
    _fill_outage()
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT


# ---------------------------------------------------------------------------
# LOW_CONFIDENCE -> reactive-floor confirm-window awaken
# ---------------------------------------------------------------------------

async def test_low_confidence_reactive_floor_confirm_window(monkeypatch):
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock, vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("LOW_CONFIDENCE"))
    _fill_outage()

    # First tick: anchors the confirm window, does NOT awaken yet.
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []

    # Advance < confirm window (120s) -> still no awaken.
    clock.advance(60.0)
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []

    # Advance past the confirm window -> AWAKEN (reactive floor).
    clock.advance(70.0)  # total 130 > 120
    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert len(awaken_calls) == 1


# ---------------------------------------------------------------------------
# Awaken injects the Dead-Man's Switch startup-script (real builder)
# ---------------------------------------------------------------------------

async def test_awaken_injects_deadman_startup_script(monkeypatch):
    clock = FakeClock()
    captured = {}

    def awaken(*, startup_script):
        captured["script"] = startup_script
        return True

    ctrl = _make_ctrl(clock, vm_awaken_fn=awaken)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    _fill_outage()
    await ctrl.tick()

    script = captured["script"]
    # The real deadman builder output -- a self-deleting watchdog bash script.
    assert "jprime-deadman" in script
    assert "self-DELETE" in script or "self-delete" in script
    assert "Metadata-Flavor: Google" in script


def test_default_awaken_is_spot_first(monkeypatch):
    """The default gcloud awaken wrapper tries Spot first, on-demand fallback,
    with the deadman startup-script + cloud-platform scope + DELETE."""
    cmds = []

    def fake_run(cmd, *, timeout_s=180.0):
        cmds.append(cmd)
        # Spot fails, on-demand succeeds -> exercise the fallback path.
        if "--provisioning-model=SPOT" in cmd:
            return 1, "spot capacity error"
        return 0, "ok"

    monkeypatch.setattr(fl, "_gcloud_run", fake_run)
    ok = fl._default_vm_awaken_fn(startup_script="#!/bin/bash\necho hi\n")
    assert ok is True
    # First attempt Spot.
    assert any("--provisioning-model=SPOT" in c for c in cmds)
    # Fallback attempt is on-demand (no SPOT).
    assert any("--provisioning-model=SPOT" not in c for c in cmds[1:])
    # All attempts carry the load-bearing flags.
    for c in cmds:
        assert "--instance-termination-action=DELETE" in c
        assert "--scopes=cloud-platform" in c
        assert "--image-family=jarvis-prime-coder" in c
        assert any(x.startswith("--metadata-from-file=startup-script=") for x in c)


# ---------------------------------------------------------------------------
# AWAKENING -> SERVING gated by observed ensure-ready probe
# ---------------------------------------------------------------------------

async def test_awakening_serving_gated_by_ready_probe(monkeypatch):
    clock = FakeClock()
    ready_state = {"ready": False}
    ctrl = _make_ctrl(clock, node_ready_fn=lambda endpoint: ready_state["ready"])
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    _fill_outage()

    await ctrl.tick()  # DORMANT -> AWAKENING
    assert ctrl.state == FailoverState.AWAKENING
    assert ctrl.is_jprime_serving() is False
    assert ctrl.jprime_endpoint() is None

    # Node not ready yet -> stays AWAKENING (observed gate).
    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING

    # Node observed ready -> SERVING + endpoint exposed.
    ready_state["ready"] = True
    await ctrl.tick()
    assert ctrl.state == FailoverState.SERVING
    assert ctrl.is_jprime_serving() is True
    ep = ctrl.jprime_endpoint()
    assert ep is not None and ep.endswith(":11434")


# ---------------------------------------------------------------------------
# SERVING probe loop paced by probe_interval
# ---------------------------------------------------------------------------

async def test_serving_probe_loop_paced(monkeypatch):
    clock = FakeClock()
    probe_calls = []
    ctrl = _make_ctrl(
        clock,
        dw_probe_fn=lambda: probe_calls.append(1) or False,
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    # Force a fixed 50s interval regardless of throttle internals.
    monkeypatch.setattr(ctrl, "_probe_interval", lambda *, now: 50.0)
    _fill_outage()

    await ctrl.tick()  # -> AWAKENING (and node_ready default True)
    await ctrl.tick()  # -> SERVING (transition tick; no probe yet)
    assert ctrl.state == FailoverState.SERVING
    assert len(probe_calls) == 0

    # First SERVING tick: _last_probe_at is None -> probes immediately.
    await ctrl.tick()
    assert len(probe_calls) == 1

    # Within the interval -> NO new probe.
    clock.advance(30.0)
    await ctrl.tick()
    assert len(probe_calls) == 1

    # Past the interval -> a new probe fires.
    clock.advance(25.0)  # total 55 > 50
    await ctrl.tick()
    assert len(probe_calls) == 2


# ---------------------------------------------------------------------------
# HANDBACK requires full recovery + hysteresis + min-uptime
# ---------------------------------------------------------------------------

async def _drive_to_serving(ctrl, clock, monkeypatch):
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    monkeypatch.setattr(ctrl, "_probe_interval", lambda *, now: 1.0)
    _fill_outage()
    await ctrl.tick()  # AWAKENING
    await ctrl.tick()  # SERVING
    assert ctrl.state == FailoverState.SERVING


async def test_handback_requires_full_recovery_hysteresis_and_uptime(monkeypatch):
    clock = FakeClock()
    delete_calls = []
    # dw_probe returns True (recovered) every probe -> fills window with True.
    ctrl = _make_ctrl(
        clock,
        dw_probe_fn=lambda: True,
        vm_delete_fn=lambda: delete_calls.append(1) or True,
    )
    await _drive_to_serving(ctrl, clock, monkeypatch)
    # The serving entry tick fired one probe (success). Need window FULL (5)
    # at >=0.6 success AND hysteresis (2 cycles) AND uptime (300s).
    # Drive more probe cycles. Keep uptime LOW first -> must NOT hand back.
    for _ in range(6):
        clock.advance(2.0)  # > 1s interval
        await ctrl.tick()
    # Uptime still ~ small (well under 300) -> NO handback despite recovery.
    assert ctrl.state == FailoverState.SERVING
    assert delete_calls == []

    # Now jump uptime past min_uptime and probe once more -> HANDBACK fires.
    clock.advance(400.0)
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert len(delete_calls) == 1


async def test_single_recovered_cycle_does_not_handback(monkeypatch):
    clock = FakeClock()
    delete_calls = []
    # Probe alternates: recovered then not -> hysteresis streak never reaches 2.
    seq = iter([True, False, True, False, True, False, True, False])

    ctrl = _make_ctrl(
        clock,
        dw_probe_fn=lambda: next(seq, False),
        vm_delete_fn=lambda: delete_calls.append(1) or True,
    )
    await _drive_to_serving(ctrl, clock, monkeypatch)
    # Even past uptime, alternating recovery never holds 2 consecutive cycles.
    clock.advance(400.0)
    for _ in range(6):
        clock.advance(2.0)
        await ctrl.tick()
    assert ctrl.state == FailoverState.SERVING
    assert delete_calls == []


# ---------------------------------------------------------------------------
# Cooldown blocks re-awaken
# ---------------------------------------------------------------------------

async def test_cooldown_blocks_reawaken(monkeypatch):
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock,
        dw_probe_fn=lambda: True,
        vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True,
        vm_delete_fn=lambda: True,
    )
    await _drive_to_serving(ctrl, clock, monkeypatch)
    assert len(awaken_calls) == 1
    # Drive to handback.
    clock.advance(400.0)
    for _ in range(8):
        clock.advance(2.0)
        await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT

    # New outage immediately -> cooldown (300s) blocks re-awaken.
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None  # reset window
    _fill_outage()
    clock.advance(60.0)  # < 300s cooldown
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert len(awaken_calls) == 1  # no re-awaken

    # Past the cooldown -> re-awaken allowed.
    clock.advance(300.0)
    await ctrl.tick()
    assert ctrl.state == FailoverState.AWAKENING
    assert len(awaken_calls) == 2


# ---------------------------------------------------------------------------
# OFF -> inert (never awakens)
# ---------------------------------------------------------------------------

async def test_off_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock, vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    _fill_outage()

    for _ in range(5):
        await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []
    assert ctrl.is_jprime_serving() is False
    assert ctrl.jprime_endpoint() is None

    # run() returns immediately when disabled.
    await ctrl.run()  # must not hang


async def test_off_note_outage_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    ctrl.note_outage()
    assert ctrl._outage_started_at is None


# ---------------------------------------------------------------------------
# Fail-soft: awaken raising -> stays safe, op never lost
# ---------------------------------------------------------------------------

async def test_failsoft_awaken_raises_stays_dormant(monkeypatch):
    clock = FakeClock()

    def boom(*, startup_script):
        raise RuntimeError("gce exploded")

    ctrl = _make_ctrl(clock, vm_awaken_fn=boom)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    _fill_outage()

    # Must not raise; reverts to DORMANT (op held in Cryo-DLQ backstop).
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert ctrl.is_jprime_serving() is False


async def test_failsoft_awaken_returns_false_stays_dormant(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock, vm_awaken_fn=lambda *, startup_script: False)
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    _fill_outage()
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT


async def test_failsoft_delete_raises_still_goes_dormant(monkeypatch):
    clock = FakeClock()

    def del_boom():
        raise RuntimeError("delete failed")

    ctrl = _make_ctrl(clock, dw_probe_fn=lambda: True, vm_delete_fn=del_boom)
    await _drive_to_serving(ctrl, clock, monkeypatch)
    clock.advance(400.0)
    # Drive to handback -- delete raises but we still reach DORMANT (the
    # Dead-Man's Switch is the cost backstop).
    for _ in range(8):
        clock.advance(2.0)
        await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT


# ---------------------------------------------------------------------------
# No-awaken without an observed global outage
# ---------------------------------------------------------------------------

async def test_no_awaken_without_global_outage(monkeypatch):
    clock = FakeClock()
    awaken_calls = []
    ctrl = _make_ctrl(
        clock, vm_awaken_fn=lambda *, startup_script: awaken_calls.append(1) or True
    )
    monkeypatch.setattr(ctrl, "_get_forecast",
                        lambda: _fake_forecast("HIGH", p50=300.0))
    # Do NOT fill the outage window -> is_global_outage False.
    await ctrl.tick()
    assert ctrl.state == FailoverState.DORMANT
    assert awaken_calls == []


# ---------------------------------------------------------------------------
# Singleton + public API surface
# ---------------------------------------------------------------------------

def test_get_failover_controller_singleton():
    fl._reset_singleton_for_tests()
    a = fl.get_failover_controller()
    b = fl.get_failover_controller()
    assert a is b
    assert a.state == FailoverState.DORMANT
    assert hasattr(a, "is_jprime_serving")
    assert hasattr(a, "jprime_endpoint")
    assert hasattr(a, "note_outage")
    assert hasattr(a, "note_dw_success")


def test_module_imports_clean():
    importlib.reload(fl)

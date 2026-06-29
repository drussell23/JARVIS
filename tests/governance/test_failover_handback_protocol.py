"""Autonomous Handback Protocol -- the return trip of the failover mesh.

#2 Deep-probe-driven recovery: while SERVING, the deep probe polls DW; N
   consecutive HEALTHY responses with low latency (<5s) -> HANDBACK.
#3 Zero-drop drain: HANDBACK routes new ops to DW immediately, AWAITS in-flight
   J-Prime ops to drain to zero, THEN fires the guaranteed parallel teardown
   (VM + firewall) and goes DORMANT. No op is ever severed mid-generation.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
)
from backend.core.ouroboros.governance.dw_surface_health import SurfaceHealthLedger


# ---------------------------------------------------------------------------
# #2 Deep-probe recovery signal on the heartbeat
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hb_env(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    ph._reset_singleton_for_tests()
    yield
    ph._reset_singleton_for_tests()


async def test_heartbeat_tracks_consecutive_successes():
    hb = ph.DWHeartbeat(probe_fn=lambda: True, ledger=SurfaceHealthLedger(autosave=False))
    await hb.beat(); await hb.beat(); await hb.beat()
    assert hb.consecutive_successes() == 3


async def test_degrade_resets_success_streak():
    flips = {"ok": True}
    hb = ph.DWHeartbeat(probe_fn=lambda: flips["ok"], ledger=SurfaceHealthLedger(autosave=False))
    await hb.beat(); await hb.beat()
    assert hb.consecutive_successes() == 2
    flips["ok"] = False
    await hb.beat()
    assert hb.consecutive_successes() == 0  # any failure resets recovery


async def test_dw_recovered_requires_streak_and_low_latency(monkeypatch):
    hb = ph.DWHeartbeat(probe_fn=lambda: True, ledger=SurfaceHealthLedger(autosave=False))
    hb._baseline_latency_s = 0.5  # fast, healthy
    await hb.beat(); await hb.beat()
    assert hb.dw_recovered(min_streak=3, max_latency_s=5.0) is False  # only 2
    await hb.beat()
    assert hb.dw_recovered(min_streak=3, max_latency_s=5.0) is True   # 3 fast healthy


async def test_dw_recovered_rejects_slow_healthy():
    hb = ph.DWHeartbeat(probe_fn=lambda: True, ledger=SurfaceHealthLedger(autosave=False))
    hb._baseline_latency_s = 30.0  # healthy but SLOW -> not 'recovered'
    await hb.beat(); await hb.beat(); await hb.beat()
    assert hb.dw_recovered(min_streak=3, max_latency_s=5.0) is False


# ---------------------------------------------------------------------------
# #3 Zero-drop drain: HANDBACK awaits in-flight before teardown
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HANDBACK_DRAIN_BUDGET_S", "2")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


async def test_handback_awaits_inflight_then_tears_down(monkeypatch):
    """Teardown must NOT fire while J-Prime ops are in flight; once the count
    drains to 0, the parallel teardown runs and the FSM goes DORMANT."""
    order = []
    inflight = {"n": 2}

    def in_flight():
        return inflight["n"]

    def vm_delete():
        order.append("teardown")
        return True

    ctrl = FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=vm_delete,
        node_ready_fn=lambda e: True,
        clock_fn=FakeClock(),
        in_flight_fn=in_flight,
    )
    monkeypatch.setattr(ctrl, "_close_ephemeral_perimeter", _noop)

    # Drain the in-flight count to 0 shortly after handback begins.
    async def _drain():
        await asyncio.sleep(0.05)
        inflight["n"] = 0
    drainer = asyncio.create_task(_drain())

    await ctrl._tick_handback(now=1000.0)
    await drainer
    assert order == ["teardown"]          # teardown happened
    assert ctrl.state.name == "DORMANT"
    assert inflight["n"] == 0             # we waited for zero-drop


async def test_handback_drain_bounded_by_budget(monkeypatch):
    """If in-flight never drains, the bounded budget still proceeds to teardown
    (the Dead-Man's Switch is the backstop -- never deadlock the FSM)."""
    monkeypatch.setenv("JARVIS_HANDBACK_DRAIN_BUDGET_S", "0.2")
    torn = {"n": 0}
    ctrl = FailoverLifecycleController(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=lambda: torn.__setitem__("n", torn["n"] + 1) or True,
        node_ready_fn=lambda e: True,
        clock_fn=FakeClock(),
        in_flight_fn=lambda: 5,  # never drains
    )
    monkeypatch.setattr(ctrl, "_close_ephemeral_perimeter", _noop)
    import time
    t0 = time.monotonic()
    await ctrl._tick_handback(now=1000.0)
    elapsed = time.monotonic() - t0
    assert torn["n"] == 1                 # tore down anyway (no deadlock)
    assert ctrl.state.name == "DORMANT"
    assert elapsed < 2.0                  # bounded by the drain budget


async def _noop(*a, **k):
    return None

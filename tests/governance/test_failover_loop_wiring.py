"""Tests for the FailoverController tick-loop wiring into the live boot.

THE BUG (Omni-Soak #3): the failover flags were ON and the
``FailoverLifecycleController`` FSM was fully built + armed, but NOTHING in
``governed_loop_service.py`` / ``harness.py`` / ``ouroboros_battle_test.py``
instantiated + ticked it during the soak -- so when DW collapsed J-Prime never
awoke. Same "built but no caller" pattern as the Swarm.

These tests prove the *wiring* only -- the FSM logic itself is exercised by
``test_failover_lifecycle.py``. We assert:

  * flag ON  -> GLS boot launches a background task that drives the controller
  * the loop ticks the controller on the interval (>1x over simulated time)
  * a tick raising is swallowed + the loop continues (fail-soft; a failover
    bug never crashes the main loop)
  * a simulated any-route DW outage drives the controller to AWAKENING
    (awaken_fn invoked -- the run-#3 fix)
  * DW-recovery drives handback (delete_fn invoked)
  * flag OFF -> NO failover task created (byte-identical)
  * shutdown cancels the loop cleanly

All boundaries are fakes -- ZERO real GCE / network / event-loop block.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
    FailoverState,
)
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance import governed_loop_service as gls


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Reset the failover + quarantine singletons and set deterministic env."""
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_QUARANTINE_WINDOW", "3")
    # Fast tick so the loop spins several times under a short sleep.
    monkeypatch.setenv("JARVIS_FAILOVER_TICK_INTERVAL_S", "0.01")
    monkeypatch.setenv("JARVIS_FAILOVER_TICK_S", "0.01")
    # Skip warmup + cost-gate friction so the FSM advances under the fakes.
    monkeypatch.setenv("JARVIS_FAILOVER_WARMUP_ENABLED", "false")
    monkeypatch.setenv("JARVIS_CRYO_AWAKEN_MARGIN", "0.0")
    monkeypatch.setenv("JARVIS_OUTAGE_CONFIRM_S", "0")
    monkeypatch.setenv("JARVIS_HANDBACK_COOLDOWN_S", "0")
    monkeypatch.setenv("JARVIS_JPRIME_MIN_UPTIME_S", "0")
    monkeypatch.setenv("JARVIS_RECOVERY_HYSTERESIS_CYCLES", "1")
    monkeypatch.setenv("JARVIS_RECOVERY_THRESHOLD", "0.6")
    yield
    fl._reset_singleton_for_tests()
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None


# ---------------------------------------------------------------------------
# _start_failover_loop / _stop_failover_loop -- the wiring under test
# ---------------------------------------------------------------------------

class _MinimalGLS:
    """A bare object exposing only the failover wiring methods of GLS so we
    can exercise them without booting the whole 102K kernel."""

    def __init__(self) -> None:
        self._failover_task: Optional[asyncio.Task] = None
        self._failover_controller = None

    # Bind the real GLS methods onto this stand-in.
    _start_failover_loop = gls.GovernedLoopService._start_failover_loop
    _failover_loop = gls.GovernedLoopService._failover_loop
    _stop_failover_loop = gls.GovernedLoopService._stop_failover_loop


@pytest.mark.asyncio
async def test_flag_on_starts_failover_task():
    """Flag ON -> a background failover task is created and scheduled."""
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        assert svc._failover_task is not None
        assert isinstance(svc._failover_task, asyncio.Task)
        assert not svc._failover_task.done()
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_flag_off_starts_no_task(monkeypatch):
    """Flag OFF -> NO failover task (byte-identical to today)."""
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "false")
    svc = _MinimalGLS()
    svc._start_failover_loop()
    assert svc._failover_task is None
    await svc._stop_failover_loop()  # idempotent / no-op


@pytest.mark.asyncio
async def test_loop_ticks_controller_more_than_once(monkeypatch):
    """The loop calls controller.tick() repeatedly on the interval."""
    calls: List[int] = []

    class _SpyController:
        async def tick(self):
            calls.append(1)
            return FailoverState.DORMANT

        async def run(self):
            # Mirror the real run(): tick on an interval until cancelled.
            while True:
                await self.tick()
                await asyncio.sleep(0.01)

        def stop(self):
            pass

    monkeypatch.setattr(fl, "get_failover_controller", lambda: _SpyController())
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        await asyncio.sleep(0.06)
        assert len(calls) > 1, f"expected multiple ticks, got {len(calls)}"
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_loop_is_fail_soft_on_tick_error(monkeypatch):
    """A tick raising is swallowed -> the loop continues (never crashes)."""
    ticks: List[int] = []

    class _BoomController:
        async def tick(self):
            ticks.append(1)
            raise RuntimeError("failover boom -- must not crash main loop")

        async def run(self):
            while True:
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 -- mirror real run() fail-soft
                    pass
                await asyncio.sleep(0.01)

        def stop(self):
            pass

    monkeypatch.setattr(fl, "get_failover_controller", lambda: _BoomController())
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        await asyncio.sleep(0.05)
        # The task survived the raising ticks (loop did not die).
        assert svc._failover_task is not None and not svc._failover_task.done()
        assert len(ticks) > 1
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_shutdown_cancels_loop_cleanly(monkeypatch):
    """_stop_failover_loop cancels the task without raising."""
    svc = _MinimalGLS()
    svc._start_failover_loop()
    task = svc._failover_task
    assert task is not None
    await svc._stop_failover_loop()
    assert task.done()
    # No CancelledError leaks to the caller.
    assert svc._failover_task is None


# ---------------------------------------------------------------------------
# Reactive path reachability -- the run-#3 fix (real controller + fakes)
# ---------------------------------------------------------------------------

class _ProbeState:
    """Controllable DW-recovery probe verdict (False until DW recovers)."""

    healthy = False


def _make_real_controller(
    clock: FakeClock, *, awakened: List, deleted: List, probe: _ProbeState
):
    """Build a REAL FailoverLifecycleController with fake boundaries so the
    actual FSM (not a mock) is driven by the wired loop."""

    async def fake_awaken(*, startup_script: str) -> bool:
        awakened.append(startup_script)
        return True

    def fake_delete() -> bool:
        deleted.append(1)
        return True

    def fake_ready(endpoint: str) -> bool:
        return True

    def fake_dw_probe() -> bool:
        return probe.healthy

    return FailoverLifecycleController(
        vm_awaken_fn=fake_awaken,
        vm_delete_fn=fake_delete,
        node_ready_fn=fake_ready,
        dw_probe_fn=fake_dw_probe,
        clock_fn=clock,
        warmup_fn=None,
    )


@pytest.mark.asyncio
async def test_any_route_outage_drives_awakening(monkeypatch):
    """A simulated any-route DW outage drives DORMANT -> AWAKENING through the
    WIRED loop (the controller is ticked, so it can transition) -- run-#3 fix."""
    clock = FakeClock()
    awakened: List = []
    deleted: List = []
    probe = _ProbeState()
    ctrl = _make_real_controller(
        clock, awakened=awakened, deleted=deleted, probe=probe
    )
    monkeypatch.setattr(fl, "get_failover_controller", lambda: ctrl)

    # Drive the real quarantine gradient to a full-window rate==0 outage on the
    # urgency-routing key the live record_sweep populates (NOT "dw").
    grad = pq.get_provider_health_gradient()
    for _ in range(3):
        grad.record_sweep("background", success=False)
    assert grad.is_global_outage("background") is True

    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        # Pump the wired loop until the FSM leaves DORMANT (bounded).
        for _ in range(200):
            await asyncio.sleep(0.01)
            if ctrl.state != FailoverState.DORMANT:
                break
        assert ctrl.state in (FailoverState.AWAKENING, FailoverState.SERVING)
        assert awakened, "awaken_fn must have been invoked (J-Prime awakens)"
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_recovery_drives_handback(monkeypatch):
    """Once SERVING, DW-recovery drives SERVING -> HANDBACK -> delete_fn."""
    clock = FakeClock()
    awakened: List = []
    deleted: List = []
    probe = _ProbeState()  # DW unhealthy at first
    ctrl = _make_real_controller(
        clock, awakened=awakened, deleted=deleted, probe=probe
    )
    monkeypatch.setattr(fl, "get_failover_controller", lambda: ctrl)

    grad = pq.get_provider_health_gradient()
    for _ in range(3):
        grad.record_sweep("background", success=False)

    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        # Reach SERVING.
        for _ in range(300):
            await asyncio.sleep(0.01)
            clock.advance(1.0)  # advance the probe-interval clock
            if ctrl.state == FailoverState.SERVING:
                break
        assert ctrl.state == FailoverState.SERVING, ctrl.state

        # Now DW recovers: the serving loop's recovery probe reports healthy,
        # so it records dw-success sweeps that fill the window above threshold
        # and trip the recovery hysteresis -> handback.
        probe.healthy = True
        clock.advance(10_000.0)  # blow past min-uptime

        for _ in range(600):
            await asyncio.sleep(0.01)
            clock.advance(1000.0)  # keep probe-interval open every tick
            if deleted:
                break
        assert deleted, "delete_fn must be invoked on handback (J-Prime torn down)"
    finally:
        await svc._stop_failover_loop()

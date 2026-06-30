"""Tests for the Violent Ephemeral Teardown (Task CR4).

The FailoverLifecycleController must reap the GPU J-Prime node the INSTANT an A1
DAG op reaches a terminal state (PR opened OR fail-closed at any gate) so a g2 GPU
never idles waiting for human review. It reuses the proven node + /32-firewall
parallel-teardown idiom and does NOT rely only on the passive dead-man.

All boundaries are injected fakes / monkeypatched -> ZERO real GCP / network.
Default-OFF byte-identical: ``violent_teardown_enabled()`` is False by default and
the seam call is skipped, so ``force_teardown`` is never reached.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
    FailoverState,
    violent_teardown_enabled,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.delenv("JARVIS_FAILOVER_VIOLENT_TEARDOWN_ENABLED", raising=False)
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, vm_deletes=None, **kw):
    vm_deletes = vm_deletes if vm_deletes is not None else []

    def _vm_delete():
        vm_deletes.append(True)
        return True

    defaults = dict(
        vm_awaken_fn=lambda *, startup_script: True,
        vm_delete_fn=_vm_delete,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: None,
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


def _stub_reaps(ctrl, monkeypatch, calls):
    """Replace the GPU-reap + ephemeral-perimeter boundaries with recorders."""

    async def _reap_gpu():
        calls.append("reap_gpu")

    async def _close_perimeter():
        calls.append("close_perimeter")

    monkeypatch.setattr(ctrl, "_reap_gpu_node", _reap_gpu)
    monkeypatch.setattr(ctrl, "_close_ephemeral_perimeter", _close_perimeter)


# ---------------------------------------------------------------------------
# force_teardown when SERVING reaps everything and drops to DORMANT
# ---------------------------------------------------------------------------

async def test_force_teardown_when_serving_reaps_and_dormant(monkeypatch):
    clock = FakeClock()
    vm_deletes = []
    calls = []
    ctrl = _make_ctrl(clock, vm_deletes=vm_deletes)
    _stub_reaps(ctrl, monkeypatch, calls)
    ctrl._state = FailoverState.SERVING
    ctrl._endpoint = "http://10.0.0.1:8000"

    await ctrl.force_teardown(reason="a1_terminal:COMPLETE")

    # GPU node + node-delete + ephemeral /32 firewall all vacated.
    assert "reap_gpu" in calls
    assert "close_perimeter" in calls
    assert vm_deletes == [True]
    # State dropped to DORMANT, cooldown anchor armed, endpoint cleared.
    assert ctrl._state == FailoverState.DORMANT
    assert ctrl._last_handback_at == clock()
    assert ctrl._endpoint is None


# ---------------------------------------------------------------------------
# force_teardown is a no-op when already DORMANT (nothing to reap)
# ---------------------------------------------------------------------------

async def test_force_teardown_dormant_is_noop(monkeypatch):
    clock = FakeClock()
    vm_deletes = []
    calls = []
    ctrl = _make_ctrl(clock, vm_deletes=vm_deletes)
    _stub_reaps(ctrl, monkeypatch, calls)
    assert ctrl._state == FailoverState.DORMANT

    await ctrl.force_teardown()

    assert calls == []
    assert vm_deletes == []
    assert ctrl._state == FailoverState.DORMANT
    assert ctrl._last_handback_at is None  # cooldown not armed -- nothing happened


# ---------------------------------------------------------------------------
# Fail-soft: a reap that raises still completes teardown + reaches DORMANT
# ---------------------------------------------------------------------------

async def test_force_teardown_never_raises(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    ctrl._state = FailoverState.SERVING

    async def _boom():
        raise RuntimeError("gpu reap exploded")

    monkeypatch.setattr(ctrl, "_reap_gpu_node", _boom)

    async def _close_perimeter():
        return None

    monkeypatch.setattr(ctrl, "_close_ephemeral_perimeter", _close_perimeter)

    # Must NOT raise despite the exploding reap boundary.
    await ctrl.force_teardown(reason="a1_terminal:POSTMORTEM")

    assert ctrl._state == FailoverState.DORMANT
    assert ctrl._last_handback_at == clock()
    assert ctrl._endpoint is None


# ---------------------------------------------------------------------------
# Default-OFF byte-identical: the master gate is False by default
# ---------------------------------------------------------------------------

def test_violent_teardown_flag_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_VIOLENT_TEARDOWN_ENABLED", raising=False)
    assert violent_teardown_enabled() is False

    monkeypatch.setenv("JARVIS_FAILOVER_VIOLENT_TEARDOWN_ENABLED", "true")
    assert violent_teardown_enabled() is True


# ---------------------------------------------------------------------------
# Serialization: force_teardown waits for the lock held by a concurrent tick()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_teardown_serializes_under_lock(monkeypatch):
    """force_teardown MUST acquire self._lock before mutating state, so it
    cannot race an in-flight tick()/_do_awaken() that already holds the lock.

    Proof: we hold the lock externally (simulating tick()) and confirm that
    force_teardown blocks -- no reap, no state mutation -- until the lock is
    released.
    """
    clock = FakeClock()
    calls = []
    ctrl = _make_ctrl(clock)
    _stub_reaps(ctrl, monkeypatch, calls)

    ctrl._state = FailoverState.SERVING
    ctrl._endpoint = "http://10.0.0.2:8000"

    # Simulate tick() holding the FSM lock mid-transition.
    await ctrl._lock.acquire()
    try:
        task = asyncio.create_task(ctrl.force_teardown(reason="a1_terminal:COMPLETE"))

        # Give the task a chance to run up to the lock acquisition point.
        await asyncio.sleep(0.05)

        # Must still be blocked -- lock is held, reap must NOT have fired yet.
        assert not task.done(), "force_teardown ran without acquiring the lock"
        assert calls == [], f"reap was called before lock was released: {calls}"
        assert ctrl._state == FailoverState.SERVING, "state mutated before lock acquired"
    finally:
        ctrl._lock.release()

    # Now the lock is free -- force_teardown should complete quickly.
    await asyncio.wait_for(task, timeout=1.0)

    assert task.done()
    assert "reap_gpu" in calls, "reap_gpu not called after lock release"
    assert "close_perimeter" in calls, "close_perimeter not called after lock release"
    assert ctrl._state == FailoverState.DORMANT
    assert ctrl._endpoint is None

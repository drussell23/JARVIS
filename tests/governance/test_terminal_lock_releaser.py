"""Universal Terminal-State Lock Releaser — ingress-lock self-heal.

Mandated bulletproof: an op acquires a TestFailureSensor target lock, then
terminates / is re-queued by the LeaseReaper, and the Universal Lock Releaser
must free the target within 100ms — immediately permitting a re-dispatch of
the (previously wedged) saga op.
"""

from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.in_flight_registry import (
    get_default_registry,
    reset_default_registry,
)
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.op_lease import LeaseReaper
from backend.core.ouroboros.governance.terminal_lock_releaser import (
    get_terminal_lock_releaser,
    release_locks_for_op,
    reset_terminal_lock_releaser,
)


class _Router:
    async def ingest(self, envelope):
        return "enqueued"


def _signal(target: str) -> IntentSignal:
    return IntentSignal(
        source="intent:test_failure",
        target_files=(target,),
        repo="r",
        description=f"Stable test failure: {target}",
        evidence={"signature": f"boom:{target}"},
        confidence=0.9,
        stable=True,
    )


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("JARVIS_TERMINAL_LOCK_RELEASER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OP_LEASE_ENABLED", "true")
    reset_terminal_lock_releaser()
    reset_default_registry()
    yield
    reset_terminal_lock_releaser()
    reset_default_registry()


async def test_terminal_release_frees_sensor_target_within_100ms() -> None:
    """Direct terminal bridge: an op's terminal state revokes the sensor's
    target lock within 100ms, re-enabling re-dispatch."""
    target = "backend/core/ouroboros/governance/saga/saga_apply_strategy.py"
    sensor = TestFailureSensor(repo="r", router=_Router(), test_watcher=None)
    # The sensor registered itself as a lock surface in __init__.

    sig = _signal(target)
    sensor._mark_targets_in_flight(sig)              # op acquires the lock
    assert sensor._in_flight_target(sig) == target   # re-emission is suppressed

    t0 = time.monotonic()
    released = release_locks_for_op("op-saga-0001", [target])  # op terminates
    dt_ms = (time.monotonic() - t0) * 1000.0

    assert released >= 1
    assert dt_ms < 100.0, f"release took {dt_ms:.1f}ms"
    # Lock revoked → the saga op can re-dispatch.
    assert sensor._in_flight_target(sig) is None


async def test_lease_reaper_requeue_releases_sensor_lock() -> None:
    """End-to-end: worker crashes → lease expires → LeaseReaper re-queues AND
    releases the sensor lock, so the re-dispatch is not suppressed."""
    target = "backend/core/ouroboros/governance/saga/saga_apply_strategy.py"
    op_id = "op-saga-lease-0001"
    sensor = TestFailureSensor(repo="r", router=_Router(), test_watcher=None)
    sig = _signal(target)
    sensor._mark_targets_in_flight(sig)
    assert sensor._in_flight_target(sig) == target

    # A worker registered this op with a lease + target_files ctx, then crashed.
    class _Ctx:
        op_id = "op-saga-lease-0001"
        target_files = (target,)

    reg = get_default_registry()
    now = time.monotonic()
    reg.register(op_id, ctx_ref=_Ctx(), deadline_monotonic=now - 1.0)  # expired

    reclaimed = []

    async def _requeue(rec):
        reclaimed.append(rec.op_id)

    reaper = LeaseReaper(_requeue, max_requeue_override=3)
    t0 = time.monotonic()
    n = await reaper.tick_once(now_monotonic=now + 1.0)
    dt_ms = (time.monotonic() - t0) * 1000.0

    assert n == 1
    assert reclaimed == [op_id]                       # op re-claimed
    assert dt_ms < 100.0
    # AND the sensor lock was revoked by the releaser bridge → not suppressed.
    assert sensor._in_flight_target(sig) is None


async def test_releaser_disabled_is_inert(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TERMINAL_LOCK_RELEASER_ENABLED", "false")
    reset_terminal_lock_releaser()
    target = "x/y.py"
    sensor = TestFailureSensor(repo="r", router=_Router(), test_watcher=None)
    sig = _signal(target)
    sensor._mark_targets_in_flight(sig)
    assert sensor._in_flight_target(sig) == target
    # Master-off → the lock is NOT released (legacy TTL-prune behavior).
    assert release_locks_for_op("op-x", [target]) == 0
    assert sensor._in_flight_target(sig) == target


async def test_multiple_surfaces_all_swept() -> None:
    """The registry sweep hits every registered surface deterministically."""
    releaser = get_terminal_lock_releaser()
    freed = {"a": [], "b": []}

    class _Surface:
        def __init__(self, name):
            self.name = name

        def release_target(self, t):
            freed[self.name].append(t)

    sa, sb = _Surface("a"), _Surface("b")
    releaser.register_surface(sa)
    releaser.register_surface(sb)
    releaser.register_surface(sa)  # idempotent

    released = releaser.release_for_op("op-1", ["f1.py", "f2.py"])
    assert released == 4  # 2 targets × 2 surfaces
    assert freed["a"] == ["f1.py", "f2.py"]
    assert freed["b"] == ["f1.py", "f2.py"]

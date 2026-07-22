"""Lease-Based In-Flight Locks — TTL heartbeat + auto-re-queue reaper.

Mandated bulletproof #2: a worker that hard-crashes mid-op must have its TTL
lease expire, and the reaper must RE-QUEUE (re-claim) the op for another
worker — permanently preventing the 33-minute wedge from soak
bt-2026-07-22-163424.
"""

from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.in_flight_registry import (
    get_default_registry,
    renew_op_safely,
    reset_default_registry,
)
from backend.core.ouroboros.governance.op_lease import (
    LeaseReaper,
    cancel_lease_heartbeat,
    initial_lease_deadline,
    lease_enabled,
    spawn_lease_heartbeat,
)


@pytest.fixture(autouse=True)
def _enable_lease(monkeypatch):
    monkeypatch.setenv("JARVIS_IN_FLIGHT_REGISTRY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OP_LEASE_ENABLED", "true")
    reset_default_registry()
    yield
    reset_default_registry()


async def test_worker_crash_lease_expires_and_reaper_reclaims() -> None:
    """Worker claims op with a lease, then hard-crashes (no more heartbeat
    renewals). The reaper must reap the expired lease and re-claim the op."""
    reg = get_default_registry()
    op_id = "op-crash-0001"
    now = time.monotonic()

    # Worker claims the op with a 5s lease.
    reg.register(op_id, deadline_monotonic=now + 5.0, last_phase_name="GENERATE")
    assert reg.lookup(op_id) is not None

    reclaimed = []

    async def requeue_fn(rec):
        reclaimed.append(rec.op_id)

    reaper = LeaseReaper(requeue_fn, max_requeue_override=3)

    # BEFORE the lease deadline: the worker looks alive → nothing reaped.
    n0 = await reaper.tick_once(now_monotonic=now + 1.0)
    assert n0 == 0
    assert reg.lookup(op_id) is not None
    assert reclaimed == []

    # Worker HARD-CRASHED → no renewals → past the deadline the lease lapses.
    n1 = await reaper.tick_once(now_monotonic=now + 6.0)
    assert n1 == 1
    assert reclaimed == [op_id]          # orchestrator re-claimed the op
    assert reg.lookup(op_id) is None     # expired lease slot freed


async def test_heartbeat_renewal_keeps_a_live_lease_from_expiring() -> None:
    """A live worker's heartbeat renewal pushes the deadline forward, so the
    reaper does NOT reap a healthy op."""
    reg = get_default_registry()
    op_id = "op-alive-0001"
    now = time.monotonic()
    reg.register(op_id, deadline_monotonic=now + 2.0)
    d0 = reg.lookup(op_id).deadline_monotonic

    # Heartbeat renews the lease well into the future (what the loop does each
    # interval via renew_op_safely).
    ok = renew_op_safely(op_id, new_deadline_monotonic=now + 60.0)
    assert ok is True
    d1 = reg.lookup(op_id).deadline_monotonic
    assert d1 > d0  # deadline extended

    reclaimed = []

    async def requeue_fn(rec):
        reclaimed.append(rec.op_id)

    reaper = LeaseReaper(requeue_fn)
    # At a time that was PAST the original deadline but before the renewed one:
    n = await reaper.tick_once(now_monotonic=now + 5.0)
    assert n == 0                        # renewed lease is NOT reaped
    assert reclaimed == []
    assert reg.lookup(op_id) is not None


async def test_reaper_respects_max_requeue_then_drops() -> None:
    """A genuinely-stuck op is re-queued up to the bound, then dropped — never
    cycles forever."""
    reg = get_default_registry()
    op_id = "op-stuck-0001"

    reclaims = []

    async def requeue_fn(rec):
        reclaims.append(rec.op_id)

    reaper = LeaseReaper(requeue_fn, max_requeue_override=2)

    # The op keeps re-appearing expired (re-registered each round to mimic the
    # re-claim placing it back, then crashing again).
    for _ in range(4):
        now = time.monotonic()
        reg.register(op_id, deadline_monotonic=now - 1.0)  # already expired
        await reaper.tick_once(now_monotonic=now + 1.0)

    # Re-queued at most max_requeue (2) times, then dropped.
    assert len(reclaims) == 2


async def test_heartbeat_spawn_and_cancel_are_clean() -> None:
    """The heartbeat task spawns and cancels without raising."""
    reg = get_default_registry()
    op_id = "op-hb-0001"
    reg.register(op_id, deadline_monotonic=initial_lease_deadline())
    assert lease_enabled() is True

    task = spawn_lease_heartbeat(op_id)
    assert task is not None
    assert not task.done()

    await cancel_lease_heartbeat(task)
    assert task.done()


async def test_lease_disabled_is_inert(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_OP_LEASE_ENABLED", "false")
    reg = get_default_registry()
    now = time.monotonic()
    reg.register("op-off", deadline_monotonic=now - 1.0)

    reclaimed = []

    async def requeue_fn(rec):
        reclaimed.append(rec.op_id)

    reaper = LeaseReaper(requeue_fn)
    n = await reaper.tick_once(now_monotonic=now + 10.0)
    assert n == 0                        # master-off → reaper is a no-op
    assert reclaimed == []
    assert spawn_lease_heartbeat("op-off") is None

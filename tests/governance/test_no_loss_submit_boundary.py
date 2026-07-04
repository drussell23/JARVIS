"""No-loss submit boundary — capacity rejections park durably in the intake WAL.

THE EVIDENCED DEFECT (live session bt-iso-1783144982, 45 occurrences):
    ``[GovernedLoop] Background submit failed, falling back to sync:
      Background queue is full (16 items)...``
``GovernedLoopService.submit_background`` caught the ``QueueFullError`` from
``BackgroundAgentPool.submit`` and ran ``await self.submit(ctx)`` — a
SYNCHRONOUS INLINE ~2-minute op ON THE DISPATCH PATH. The dispatcher
serialized, the intake queue backed up behind it, and low-priority
write-intent signals (TodoScanner, priority 5) never dequeued for 60+ min.
Effective loss.

THE FIX (no-loss submit boundary):
    A capacity rejection (``QueueFullError``) is NOT a broken pool. It must
    park DURABLY in the EXISTING intake WAL (the envelope is already a
    ``status="pending"`` WAL row — parking = simply never acking it) and
    drain via the EXISTING replay machinery (``_replay_wal``) once the pool
    reports free capacity. The dispatcher stays fully async. Genuinely broken
    (non-capacity) submit exceptions still take the legacy sync fallback.

THE GATE TEST (deterministic): burst 100 envelopes into a simulated
16-slot / 1-worker pool with the worker GATED (zero consumption during the
burst) → EXACTLY 84 durably parked in the WAL (84 pending rows), 16 accepted
(acked / pool-owned), ZERO lost, ZERO sync-inline fallbacks; then release the
worker → ALL 100 reach accepted/terminal states (bounded drain, no loss).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance.background_agent_pool import QueueFullError
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopService,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)
from backend.core.ouroboros.governance.intake.wal import WALEntry
import time


# ---------------------------------------------------------------------------
# Test doubles — a faithful 16-slot / 1-worker gated pool + a stub GLS that
# binds the REAL submit_background / has_background_capacity boundary methods.
# ---------------------------------------------------------------------------


class _GatedPool:
    """Faithful 16-slot / 1-worker BackgroundAgentPool.

    ``submit`` raises the REAL ``QueueFullError`` once ``capacity`` slots are
    occupied. The single worker is GATED: slots free only via ``release_one``
    (simulating a worker consuming one op), so the test controls consumption
    deterministically. Records every accepted ctx.op_id for loss accounting.
    """

    def __init__(self, capacity: int = 16) -> None:
        self._cap = capacity
        self._queued: list = []  # accepted, not yet consumed by the worker
        self.accepted_ctx_ids: list = []  # ctx.op_id of every accepted op
        self._counter = 0

    async def submit(self, ctx) -> str:
        if len(self._queued) >= self._cap:
            raise QueueFullError(
                f"Background queue is full ({self._cap} items). "
                f"Operation rejected."
            )
        self._counter += 1
        op_id = f"bgop-{self._counter:05d}"
        self._queued.append((op_id, ctx))
        self.accepted_ctx_ids.append(str(getattr(ctx, "op_id", "") or op_id))
        return op_id

    def has_capacity(self) -> bool:
        return len(self._queued) < self._cap

    def queue_depth(self) -> int:
        return len(self._queued)

    def release_one(self) -> None:
        """Simulate the single worker draining one occupied slot."""
        if self._queued:
            self._queued.pop(0)


class _StubGLS:
    """Minimal GLS that binds the REAL boundary methods under test.

    ``submit_background`` + ``has_background_capacity`` are the production
    methods bound onto a lightweight object carrying only ``_bg_pool`` and the
    sync fallback ``submit``. Using ``getattr`` fallback for
    ``has_background_capacity`` keeps the test importable BEFORE the method is
    implemented (fail-first cleanliness).
    """

    submit_background = GovernedLoopService.submit_background
    has_background_capacity = getattr(
        GovernedLoopService, "has_background_capacity", lambda self: True
    )

    def __init__(self, pool: _GatedPool) -> None:
        self._bg_pool = pool
        self.sync_fallback_calls = 0
        self.sync_fallback_ctx_ids: list = []

    async def submit(self, ctx, trigger_source: str = "unknown"):
        # THE sync-inline fallback. For a CAPACITY rejection this MUST NOT run
        # (that is the exact defect: a ~2-min op serialized on the dispatch
        # path). Only a genuinely-broken (non-capacity) pool may reach here.
        self.sync_fallback_calls += 1
        self.sync_fallback_ctx_ids.append(str(getattr(ctx, "op_id", "") or ""))

        class _R:
            op_id = str(getattr(ctx, "op_id", "") or "sync")

        return _R()


def _make_router(tmp_path, pool, monkeypatch, f1_master: bool = False):
    # Keep intake side-channels quiet + deterministic.
    monkeypatch.setenv("JARVIS_SEMANTIC_INFERENCE_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED",
        "true" if f1_master else "false",
    )
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", "false")
    cfg = IntakeRouterConfig(
        project_root=tmp_path,
        wal_path=tmp_path / "intake_wal.jsonl",
        max_queue_size=512,
        dispatch_timeout_s=30.0,
    )
    gls = _StubGLS(pool)
    router = UnifiedIntakeRouter(gls, cfg)
    return router, gls


def _envelopes(n: int):
    out = []
    for i in range(n):
        out.append(
            make_envelope(
                source="todo_scanner",
                description=f"fix bug in module_{i}",
                target_files=(f"pkg/mod_{i}.py",),
                repo="jarvis",
                confidence=0.9,
                urgency="high",  # bypass coalescing → 1:1 dispatch
                evidence={"signature": f"sig-{i}"},
                requires_human_ack=False,
            )
        )
    return out


def _lease_and_wal(router, env):
    """Mimic ingest step 4 — durable WAL append (status=pending) before
    the envelope is handed to dispatch. Returns the leased envelope."""
    lease_id = generate_operation_id("lse")
    env = env.with_lease(lease_id)
    router._wal.append(
        WALEntry(
            lease_id=lease_id,
            envelope_dict=env.to_dict(),
            status="pending",
            ts_monotonic=time.monotonic(),
            ts_utc=datetime.now(timezone.utc).isoformat(),
        )
    )
    return env


# ---------------------------------------------------------------------------
# THE GATE TEST — 84 parked / 16 accepted / 0 lost / 0 sync-inline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_burst_100_parks_84_in_wal_zero_loss_zero_sync(
    tmp_path, monkeypatch, caplog
):
    pool = _GatedPool(capacity=16)
    router, gls = _make_router(tmp_path, pool, monkeypatch)

    envs = [_lease_and_wal(router, e) for e in _envelopes(100)]

    caplog.set_level(logging.INFO)
    # Burst: dispatch all 100 with the worker GATED (zero consumption).
    for env in envs:
        await router._dispatch_one(env)

    # --- 16 accepted (pool-owned / acked), 84 durably parked (pending) ---
    pending = router._wal.pending_entries()
    assert len(pending) == 84, f"expected 84 parked, got {len(pending)}"
    assert len(pool.accepted_ctx_ids) == 16, (
        f"expected 16 accepted, got {len(pool.accepted_ctx_ids)}"
    )

    # --- ZERO sync-inline fallback for capacity rejections ---
    assert gls.sync_fallback_calls == 0, (
        f"capacity rejection must NOT serialize on the dispatch path; "
        f"sync fallback ran {gls.sync_fallback_calls}x"
    )

    # --- ZERO loss: 16 accepted + 84 pending == 100, disjoint sets ---
    parked_ids = {e.envelope_dict["causal_id"] for e in pending}
    accepted_ids = set(pool.accepted_ctx_ids)
    assert len(parked_ids) == 84
    assert len(accepted_ids) == 16
    assert parked_ids.isdisjoint(accepted_ids)
    assert len(parked_ids | accepted_ids) == 100, "envelope loss detected"

    # --- structured telemetry fires once per parked envelope ---
    deferred_logs = [
        r for r in caplog.records if "bg_submit_deferred" in r.getMessage()
    ]
    assert len(deferred_logs) == 84, (
        f"expected 84 bg_submit_deferred telemetry lines, got {len(deferred_logs)}"
    )


# ---------------------------------------------------------------------------
# Byte-fidelity — parked envelopes round-trip through the WAL exactly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wal_parking_preserves_envelope_byte_fidelity(
    tmp_path, monkeypatch
):
    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch)

    originals = _envelopes(40)
    leased = [_lease_and_wal(router, e) for e in originals]
    for env in leased:
        await router._dispatch_one(env)

    pending = router._wal.pending_entries()
    assert len(pending) == 24  # 40 - 16

    by_cid = {e.envelope_dict["causal_id"]: e for e in pending}
    for env in leased:
        entry = by_cid.get(env.causal_id)
        if entry is None:
            continue  # this one was accepted, not parked
        # Round-trip: from_dict(to_dict) == to_dict, and matches the leased env.
        rehydrated = IntentEnvelope.from_dict(entry.envelope_dict)
        assert rehydrated.to_dict() == entry.envelope_dict
        assert rehydrated.to_dict() == env.to_dict()


# ---------------------------------------------------------------------------
# Drain proof — release the worker, ALL 100 reach accepted, bounded, no loss.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_released_worker_drains_all_parked(tmp_path, monkeypatch):
    pool = _GatedPool(capacity=16)
    router, gls = _make_router(tmp_path, pool, monkeypatch)

    envs = [_lease_and_wal(router, e) for e in _envelopes(100)]
    for env in envs:
        await router._dispatch_one(env)

    assert len(router._wal.pending_entries()) == 84

    # Release the worker: drain everything via the EXISTING replay machinery.
    # Bounded loop — each round consumes the pool then re-drains parked WAL rows.
    rounds = 0
    while router._wal.pending_entries():
        rounds += 1
        assert rounds < 50, "drain did not converge (unbounded)"
        # Worker consumes all currently-occupied slots.
        while pool.queue_depth() > 0:
            pool.release_one()
        # Capacity-gated replay of the parked WAL rows onto the queue.
        await router._drain_capacity_deferred()
        # Dispatch whatever the replay re-enqueued.
        while not router._queue.empty():
            item = router._queue.get_nowait()
            await router._dispatch_one(item[-1])

    # ALL 100 accepted exactly once, zero lost, zero sync-inline.
    assert len(pool.accepted_ctx_ids) == 100
    assert len(set(pool.accepted_ctx_ids)) == 100, "double-dispatch detected"
    assert gls.sync_fallback_calls == 0
    assert router._wal.pending_entries() == []


# ---------------------------------------------------------------------------
# Idempotency — replay after full drain dispatches nothing (no double-dispatch).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_is_idempotent_after_drain(tmp_path, monkeypatch):
    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch)

    envs = [_lease_and_wal(router, e) for e in _envelopes(20)]
    for env in envs:
        await router._dispatch_one(env)

    # Drain fully (pool big enough after release).
    while router._wal.pending_entries():
        while pool.queue_depth() > 0:
            pool.release_one()
        await router._drain_capacity_deferred()
        while not router._queue.empty():
            item = router._queue.get_nowait()
            await router._dispatch_one(item[-1])

    accepted_after_drain = len(pool.accepted_ctx_ids)
    assert accepted_after_drain == 20

    # Re-run replay: no pending rows remain → nothing re-enqueued, no re-dispatch.
    await router._replay_wal()
    dispatched_extra = 0
    while not router._queue.empty():
        item = router._queue.get_nowait()
        await router._dispatch_one(item[-1])
        dispatched_extra += 1
    assert dispatched_extra == 0
    assert len(pool.accepted_ctx_ids) == accepted_after_drain  # no growth


# ---------------------------------------------------------------------------
# Distinguish — a genuinely broken (non-capacity) pool STILL uses sync fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_capacity_exception_still_takes_sync_fallback(
    tmp_path, monkeypatch, caplog
):
    class _BrokenPool(_GatedPool):
        async def submit(self, ctx):
            raise RuntimeError("pool worker crashed — not a capacity condition")

    pool = _BrokenPool(capacity=16)
    router, gls = _make_router(tmp_path, pool, monkeypatch)

    env = _lease_and_wal(router, _envelopes(1)[0])
    caplog.set_level(logging.INFO)
    await router._dispatch_one(env)

    # Non-capacity exception → legacy sync fallback preserved (broken pool).
    assert gls.sync_fallback_calls == 1
    # Non-capacity failures are NOT capacity deferrals.
    deferred_logs = [
        r for r in caplog.records if "bg_submit_deferred" in r.getMessage()
    ]
    assert deferred_logs == []
    # The op ran (sync) → acked, not parked.
    assert router._wal.pending_entries() == []


# ---------------------------------------------------------------------------
# Sync-FS-on-loop guard — the drain's WAL read runs via cooperative_fs_io
# offload (off the event-loop thread), never synchronously on the loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_wal_read_routed_through_offload_substrate(
    tmp_path, monkeypatch
):
    import threading

    import backend.core.ouroboros.governance.cooperative_fs_io as cfs

    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch)

    envs = [_lease_and_wal(router, e) for e in _envelopes(20)]
    for env in envs:
        await router._dispatch_one(env)
    assert router._capacity_deferred_pending

    # Spy 1 — every offload() call records the fn it was handed.
    real_offload = cfs.offload
    offloaded_fns: list = []

    async def spy_offload(fn, *args, **kwargs):
        offloaded_fns.append(fn)
        return await real_offload(fn, *args, **kwargs)

    monkeypatch.setattr(cfs, "offload", spy_offload)

    # Spy 2 — pending_entries records the thread it executes on, and RAISES
    # if it ever runs on the event-loop thread during the drain.
    loop_thread = threading.current_thread()
    real_pending = router._wal.pending_entries
    read_threads: list = []

    def spy_pending():
        read_threads.append(threading.current_thread())
        if threading.current_thread() is loop_thread:
            raise AssertionError(
                "sync-FS-on-loop: pending_entries ran ON the event-loop "
                "thread during the drain path"
            )
        return real_pending()

    monkeypatch.setattr(router._wal, "pending_entries", spy_pending)

    while pool.queue_depth() > 0:
        pool.release_one()
    await router._drain_capacity_deferred()

    # The WAL read went through the offload substrate...
    assert spy_pending in offloaded_fns, (
        "drain did not route the WAL read through cooperative_fs_io.offload"
    )
    # ...and actually executed off the loop thread.
    assert read_threads, "pending_entries never ran"
    assert all(t is not loop_thread for t in read_threads)
    # And the drain still worked: parked rows were re-enqueued.
    assert router._queue.qsize() == 4  # 20 - 16 accepted


# ---------------------------------------------------------------------------
# Single-flight — overlapping drain calls perform exactly ONE WAL read.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_drains_are_single_flight(tmp_path, monkeypatch):
    import asyncio as _asyncio

    import backend.core.ouroboros.governance.cooperative_fs_io as cfs

    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch)

    envs = [_lease_and_wal(router, e) for e in _envelopes(20)]
    for env in envs:
        await router._dispatch_one(env)
    assert router._capacity_deferred_pending
    while pool.queue_depth() > 0:
        pool.release_one()

    # Slow offload: first drain parks on `release`; reads are counted.
    read_count = 0
    first_read_started = _asyncio.Event()
    release = _asyncio.Event()

    async def slow_offload(fn, *args, **kwargs):
        nonlocal read_count
        kwargs.pop("cpu_bound", None)  # offload-only kwarg, not fn's
        read_count += 1
        first_read_started.set()
        await release.wait()
        return fn(*args, **kwargs)

    monkeypatch.setattr(cfs, "offload", slow_offload)

    t1 = _asyncio.create_task(router._drain_capacity_deferred())
    await first_read_started.wait()
    # Second drain while the first is suspended mid-offload: must return
    # immediately WITHOUT a second WAL read (single-flight guard).
    await router._drain_capacity_deferred()
    assert read_count == 1, "overlapping drain performed a second WAL read"

    release.set()
    await t1
    assert read_count == 1
    # The single in-flight drain completed the re-enqueue.
    assert router._queue.qsize() == 4  # 20 - 16 accepted
    assert router._capacity_drain_in_flight is False


# ---------------------------------------------------------------------------
# F1 mirror — replayed rows land in the IntakePriorityQueue when the
# scheduler master flag is on; default-off behavior is byte-identical.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_mirror_enqueues_priority_queue_when_f1_on(
    tmp_path, monkeypatch
):
    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch, f1_master=True)
    assert router._priority_queue is not None  # F1 primary mode wired

    # Park 5 rows durably (pending WAL rows, nothing in the queues).
    for env in _envelopes(5):
        _lease_and_wal(router, env)
    router._capacity_deferred_pending = True

    await router._drain_capacity_deferred()

    # Replayed rows land on BOTH queues — the priority queue is the dispatch
    # source of truth in F1 primary mode, so missing the mirror means the
    # replayed rows would never dequeue.
    assert router._queue.qsize() == 5
    assert len(router._priority_queue) == 5


@pytest.mark.asyncio
async def test_replay_no_priority_queue_when_f1_off(tmp_path, monkeypatch):
    pool = _GatedPool(capacity=16)
    router, _ = _make_router(tmp_path, pool, monkeypatch, f1_master=False)
    assert router._priority_queue is None  # default-off: no mirror target

    for env in _envelopes(5):
        _lease_and_wal(router, env)
    router._capacity_deferred_pending = True

    await router._drain_capacity_deferred()

    # Byte-identical default-off behavior: legacy queue only, no errors.
    assert router._queue.qsize() == 5

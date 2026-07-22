"""Lease-Based In-Flight Locks — TTL heartbeat + auto-re-queue reaper.

Empirical foil — soak bt-2026-07-22-163424 (the 33-minute wedge):

    A heavy op (the 932-line Saga source repair) claimed the in-flight lock,
    its worker hung on a flapping DoubleWord stream, and NOTHING released the
    lock or re-queued the op. The static in-flight lock carried no liveness
    signal, so the sensor suppressed every re-emission ("target already
    in-flight") for the entire session while zero work happened.

This converts the in-flight state from a STATIC lock into a TTL LEASE:

  * On claim, a worker registers its op with a bounded lease deadline and
    spawns a lightweight heartbeat coroutine (``spawn_lease_heartbeat``) that
    RENEWS the lease each interval — but only while the worker's own coroutine
    is alive and scheduling. If the worker's task is cancelled/crashes, the
    child heartbeat task dies with it (structured-concurrency parent→child),
    renewals stop, and the lease expires.
  * A periodic ``LeaseReaper`` sweeps expired leases and RE-QUEUES the op via
    an injected re-claim callback — unlike ``ConvergenceReaper`` (which
    force-FAILs a wedged op). Another worker then reclaims it. Wedged sessions
    become structurally impossible.

Composes the EXISTING ``in_flight_registry`` lease substrate
(``OpInFlight.deadline_monotonic`` + ``reap_past_deadline`` +
``renew_op_safely`` + ``unregister_op_safely``) — no parallel state store
(DRY). Env-driven (no hardcoded timeouts), master-gated, NEVER raises.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Optional

from backend.core.ouroboros.governance.in_flight_registry import (
    OpInFlight,
    get_default_registry,
    registration_active,
    renew_op_safely,
    unregister_op_safely,
)

logger = logging.getLogger("Ouroboros.OpLease")


# ---------------------------------------------------------------------------
# Env knobs (clamped-getter pattern — mirrors convergence_reaper / stream_rupture)
# ---------------------------------------------------------------------------

_MASTER_FLAG = "JARVIS_OP_LEASE_ENABLED"
_TTL_ENV = "JARVIS_OP_LEASE_TTL_S"
_HEARTBEAT_ENV = "JARVIS_OP_LEASE_HEARTBEAT_INTERVAL_S"
_REAPER_TICK_ENV = "JARVIS_OP_LEASE_REAPER_TICK_S"
_MAX_REQUEUE_ENV = "JARVIS_OP_LEASE_MAX_REQUEUE"

_DEFAULT_TTL_S = 90.0
_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_REAPER_TICK_S = 15.0
_DEFAULT_MAX_REQUEUE = 3


def _clamped_float(env: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(env, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _clamped_int(env: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(env, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def lease_enabled() -> bool:
    """Master gate (default TRUE). Also honors the in-flight registry's own
    activation — a lease is meaningless if the registry is inert."""
    on = os.environ.get(_MASTER_FLAG, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )
    return on and registration_active()


def lease_ttl_s() -> float:
    """Lease window (default 90s). The op holds its slot for at most this long
    without a renewal. Clamped to [10, 900]."""
    return _clamped_float(_TTL_ENV, _DEFAULT_TTL_S, 10.0, 900.0)


def heartbeat_interval_s() -> float:
    """Heartbeat renewal cadence (default 30s). Must be well under the TTL so
    a healthy worker never lets the lease lapse. Clamped to [5, 300], and
    additionally to at most half the TTL so a single missed tick can't expire
    a live lease."""
    raw = _clamped_float(_HEARTBEAT_ENV, _DEFAULT_HEARTBEAT_S, 5.0, 300.0)
    return min(raw, max(5.0, lease_ttl_s() / 2.0))


def reaper_tick_s() -> float:
    """Reaper sweep cadence (default 15s). Clamped to [5, 120]."""
    return _clamped_float(_REAPER_TICK_ENV, _DEFAULT_REAPER_TICK_S, 5.0, 120.0)


def max_requeue() -> int:
    """Max autonomous re-queues per op before the reaper gives up (default 3).
    Prevents an infinitely-wedging op from cycling forever. Clamped [1, 20]."""
    return _clamped_int(_MAX_REQUEUE_ENV, _DEFAULT_MAX_REQUEUE, 1, 20)


def initial_lease_deadline(now: Optional[float] = None) -> float:
    """The absolute monotonic deadline a fresh lease should carry."""
    return (now if now is not None else time.monotonic()) + lease_ttl_s()


# ---------------------------------------------------------------------------
# Worker heartbeat — renews the lease while the worker coroutine is alive
# ---------------------------------------------------------------------------


async def _heartbeat_loop(op_id: str, interval_s: float, ttl_s: float) -> None:
    """Renew ``op_id``'s lease every ``interval_s`` until cancelled or the op
    is gone. Cancelled by the worker's ``finally`` (normal completion) or dies
    with the worker task (crash) — either way renewals stop and the lease
    lapses. Pure async sleep; never blocks the loop between ticks."""
    try:
        while True:
            await asyncio.sleep(interval_s)
            ok = renew_op_safely(
                op_id, new_deadline_monotonic=time.monotonic() + ttl_s,
            )
            if not ok:
                # Op already released / reaped — nothing left to renew.
                return
    except asyncio.CancelledError:
        # Worker finished or died — stop renewing so the lease can expire if
        # the op was NOT cleanly released.
        raise


def spawn_lease_heartbeat(op_id: str) -> Optional[asyncio.Task]:
    """Arm a per-op lease heartbeat task. Returns the task (cancel it in the
    worker's ``finally``), or ``None`` when leasing is disabled / no running
    loop. Never raises."""
    if not lease_enabled() or not isinstance(op_id, str) or not op_id:
        return None
    try:
        return asyncio.ensure_future(
            _heartbeat_loop(op_id, heartbeat_interval_s(), lease_ttl_s())
        )
    except Exception:  # noqa: BLE001 — no running loop / shutdown
        return None


async def cancel_lease_heartbeat(task: Optional[asyncio.Task]) -> None:
    """Cancel a heartbeat task and await its teardown. Never raises."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Lease reaper — re-queues (does NOT fail) ops whose lease has lapsed
# ---------------------------------------------------------------------------

# Re-claim callback: given the expired OpInFlight record, hand the op back to
# intake for a fresh attempt. Injected so this module carries no intake import
# (no cycle). May be sync or async.
RequeueFn = Callable[[OpInFlight], Awaitable[None]]


class LeaseReaper:
    """Periodic sweep that RE-QUEUES ops whose lease expired (worker died /
    hung past its lease) instead of force-failing them.

    The re-claim action is injected (``requeue_fn``) so the reaper stays free
    of intake/orchestrator imports. Bounded by ``max_requeue`` per op so a
    genuinely-broken op cannot cycle forever."""

    def __init__(
        self,
        requeue_fn: RequeueFn,
        *,
        tick_s: Optional[float] = None,
        max_requeue_override: Optional[int] = None,
    ) -> None:
        self._requeue_fn = requeue_fn
        self._tick_s = tick_s if tick_s is not None else reaper_tick_s()
        self._max_requeue = (
            max_requeue_override
            if max_requeue_override is not None
            else max_requeue()
        )
        self._requeue_counts: Dict[str, int] = {}
        self._task: Optional[asyncio.Task] = None

    async def tick_once(self, *, now_monotonic: Optional[float] = None) -> int:
        """One reaper sweep. Reaps every past-deadline lease, unregisters it
        (frees the slot), and re-queues the op (bounded). Returns the number
        re-queued. NEVER raises."""
        if not lease_enabled():
            return 0
        try:
            expired = get_default_registry().reap_past_deadline(
                now_monotonic=now_monotonic,
            )
        except Exception:  # noqa: BLE001
            return 0
        requeued = 0
        for rec in expired:
            op_id = rec.op_id
            # Always free the expired lease slot first.
            unregister_op_safely(op_id)
            # Universal Lock Release: a re-queued op must NOT stay locked out of
            # ingress. Revoke its target locks / in-flight flags BEFORE re-queue
            # so the sensor doesn't suppress the re-dispatch (the sensor-side
            # wedge, soak bt-2026-07-22-174240). DRY — the same central bridge
            # the TrinityEventBus terminal observer uses.
            try:
                from backend.core.ouroboros.governance.terminal_lock_releaser import (  # noqa: E501
                    release_locks_for_op as _release_locks,
                )
                _tf = getattr(getattr(rec, "ctx_ref", None), "target_files", None)
                _release_locks(op_id, _tf)
            except Exception:  # noqa: BLE001 — cleanup never blocks the sweep
                pass
            count = self._requeue_counts.get(op_id, 0)
            if count >= self._max_requeue:
                logger.warning(
                    "[LeaseReaper] op=%s exceeded max_requeue=%d — dropping "
                    "(genuinely stuck; NOT re-queued)",
                    op_id, self._max_requeue,
                )
                continue
            self._requeue_counts[op_id] = count + 1
            try:
                res = self._requeue_fn(rec)
                if asyncio.iscoroutine(res):
                    await res
                requeued += 1
                logger.warning(
                    "[LeaseReaper] lease EXPIRED op=%s (in_flight=%.0fs, "
                    "phase=%s) — RE-QUEUED (attempt %d/%d) — no wedge",
                    op_id, rec.time_in_flight_s(), rec.coarse_phase(),
                    count + 1, self._max_requeue,
                )
            except Exception:  # noqa: BLE001 — a bad re-queue never crashes the sweep
                logger.debug(
                    "[LeaseReaper] requeue_fn failed for %s", op_id,
                    exc_info=True,
                )
        return requeued

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._tick_s)
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop is immortal
                logger.debug("[LeaseReaper] tick error", exc_info=True)

    def start(self) -> Optional[asyncio.Task]:
        """Start the background reaper loop. Returns the task or ``None`` when
        leasing is disabled / no loop. Idempotent."""
        if not lease_enabled():
            return None
        if self._task is not None and not self._task.done():
            return self._task
        try:
            self._task = asyncio.ensure_future(self._run_loop())
            logger.info(
                "[LeaseReaper] armed: tick=%.0fs ttl=%.0fs heartbeat=%.0fs "
                "max_requeue=%d",
                self._tick_s, lease_ttl_s(), heartbeat_interval_s(),
                self._max_requeue,
            )
            return self._task
        except Exception:  # noqa: BLE001
            return None

    async def stop(self) -> None:
        await cancel_lease_heartbeat(self._task)
        self._task = None


__all__ = [
    "LeaseReaper",
    "RequeueFn",
    "cancel_lease_heartbeat",
    "heartbeat_interval_s",
    "initial_lease_deadline",
    "lease_enabled",
    "lease_ttl_s",
    "max_requeue",
    "reaper_tick_s",
    "spawn_lease_heartbeat",
]

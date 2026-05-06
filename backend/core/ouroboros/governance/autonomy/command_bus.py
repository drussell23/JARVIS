"""backend/core/ouroboros/governance/autonomy/command_bus.py

Command Bus — Priority Queue with Dedup (Task 2, C+ Autonomous Loop).

Sits in L1 (GLS) and receives commands from L2/L3/L4 advisory layers.
Commands are priority-ordered so safety commands (L3) preempt optimization
commands (L2/L4).

Design:
    - Priority queue via heapq (lower number = higher priority).
    - FIFO tiebreaker via monotonic sequence counter.
    - Deduplication via IdempotencyLRU (bounded LRU cache).
    - TTL expiry: expired commands are silently discarded on dequeue.
    - Backpressure: bounded maxsize; put()/try_put() return False when full.
    - Async-friendly: get() blocks via asyncio.Event when queue is empty.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import weakref
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    IdempotencyLRU,
)

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.autonomy.rate_limiter import (
        TokenBucketRateLimiter,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heap entry type: (priority, sequence, envelope)
# The sequence counter ensures FIFO ordering among equal-priority commands
# and avoids comparing CommandEnvelope objects (which are not orderable).
# ---------------------------------------------------------------------------
_HeapEntry = Tuple[int, int, CommandEnvelope]


class CommandBus:
    """Priority queue with dedup, TTL expiry, and backpressure.

    Parameters
    ----------
    maxsize:
        Maximum number of commands the bus can hold.  ``put()`` and
        ``try_put()`` return ``False`` when this limit is reached.
    dedup_capacity:
        Maximum number of idempotency keys retained in the LRU cache.
        Defaults to 4x *maxsize* so that keys outlive their commands
        in the heap, preventing resubmission of recently-consumed
        commands.
    """

    # Path D.4 (PRD §36.6, 2026-05-05) — class-level weak-ref
    # registry of live CommandBus instances. CommandBus has no
    # global singleton (multiple callers construct their own:
    # governed_loop_service, subagent_scheduler, advanced_
    # coordination, safety_net, feedback_engine). The Class-
    # level Instance Registry pattern lets operator surfaces
    # (`/bus` + `/observability/command-bus`) aggregate metrics
    # across ALL live buses without forcing a single-instance
    # contract.
    #
    # WeakSet keeps refs without preventing GC — orphaned buses
    # drop out automatically.
    _INSTANCES: "weakref.WeakSet[CommandBus]" = weakref.WeakSet()

    def __init__(
        self,
        maxsize: int = 256,
        dedup_capacity: int | None = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self._maxsize = maxsize
        self._heap: List[_HeapEntry] = []
        self._seq = itertools.count()  # monotonic FIFO tiebreaker
        self._dedup = IdempotencyLRU(
            capacity=dedup_capacity if dedup_capacity is not None else maxsize * 4
        )
        # Optional rate limiter — when set, async put() consults it before
        # enqueuing.
        self._rate_limiter = rate_limiter
        # Signalled whenever a new item is pushed so that a blocked get()
        # can wake up and try to dequeue.
        self._not_empty = asyncio.Event()
        # Path D.4 — per-command-type dispatch counter.
        # Lightweight Dict[str, int] incremented inside _enqueue
        # AFTER all gates pass (so the count reflects "accepted"
        # rather than "attempted"). Defaultdict + int — never
        # raises.
        self._dispatch_counts: Dict[str, int] = defaultdict(int)
        # Total commands ever rejected (dedup OR backpressure)
        # — useful operator signal for back-pressure alerts.
        self._rejected_dedup: int = 0
        self._rejected_backpressure: int = 0
        # Register self for cross-instance aggregation.
        CommandBus._INSTANCES.add(self)

    # ------------------------------------------------------------------
    # put (async, non-blocking on full — returns False)
    # ------------------------------------------------------------------

    async def put(self, cmd: CommandEnvelope) -> bool:
        """Enqueue *cmd* if it is neither a duplicate nor over capacity.

        When a rate limiter is configured, a token must be acquired before
        enqueuing.  The limiter is checked with ``timeout=0.0`` (non-blocking)
        so that callers are never stalled — they simply receive ``False``
        when the rate limit is exceeded.

        Returns ``True`` on success, ``False`` if the command was
        rejected (rate-limited, duplicate idempotency key, **or** bus at
        capacity).
        """
        if self._rate_limiter is not None:
            acquired = await self._rate_limiter.acquire(timeout=0.0)
            if not acquired:
                logger.debug(
                    "CommandBus: rate-limited command %s (type=%s)",
                    cmd.command_id,
                    cmd.command_type.value,
                )
                return False
        return self._enqueue(cmd)

    # ------------------------------------------------------------------
    # try_put (sync, non-blocking)
    # ------------------------------------------------------------------

    def try_put(self, cmd: CommandEnvelope) -> bool:
        """Synchronous, non-blocking variant of :meth:`put`."""
        return self._enqueue(cmd)

    # ------------------------------------------------------------------
    # get (async, blocks when empty, skips expired)
    # ------------------------------------------------------------------

    async def get(self) -> CommandEnvelope:
        """Block until a non-expired command is available, then return it.

        Expired commands encountered at the head of the heap are
        silently discarded.  If the heap becomes empty after discarding,
        the method waits for new arrivals.
        """
        while True:
            # Wait until something is in the heap
            while not self._heap:
                self._not_empty.clear()
                await self._not_empty.wait()

            # Pop the highest-priority (lowest number) entry
            _priority, _seq, cmd = heapq.heappop(self._heap)

            if cmd.is_expired():
                logger.debug(
                    "CommandBus: discarded expired command %s (type=%s)",
                    cmd.command_id,
                    cmd.command_type.value,
                )
                continue  # try next entry

            return cmd

    # ------------------------------------------------------------------
    # qsize
    # ------------------------------------------------------------------

    def qsize(self) -> int:
        """Return the number of pending commands (including possibly expired ones)."""
        return len(self._heap)

    # ------------------------------------------------------------------
    # rate limiter status
    # ------------------------------------------------------------------

    def get_rate_limiter_status(self) -> Optional[Dict[str, Any]]:
        """Return the rate limiter's status dict, or ``None`` if no limiter is configured."""
        if self._rate_limiter is not None:
            return self._rate_limiter.get_status()
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _is_duplicate(self, key: str) -> bool:
        """Read-only duplicate check (does NOT record the key)."""
        # Access the LRU's internal cache for a non-mutating lookup.
        # This avoids poisoning the dedup cache when a command will be
        # rejected by a later gate (e.g. backpressure).
        return key in self._dedup._cache  # noqa: SLF001

    def _enqueue(self, cmd: CommandEnvelope) -> bool:
        """Shared enqueue logic for put() and try_put().

        Order of checks:
        1. Dedup — reject if idempotency key was already seen.
        2. Backpressure — reject if at capacity.
        3. Record key in LRU and push onto heap.

        The key is recorded in the LRU only after all gates pass so
        that a command rejected by backpressure can be retried later.
        """
        # 1. Dedup check (read-only — does NOT record the key yet)
        if self._is_duplicate(cmd.idempotency_key):
            logger.debug(
                "CommandBus: duplicate idempotency_key=%s dropped",
                cmd.idempotency_key[:12],
            )
            self._rejected_dedup += 1
            return False

        # 2. Backpressure check
        if len(self._heap) >= self._maxsize:
            logger.debug(
                "CommandBus: at capacity (%d), command %s rejected",
                self._maxsize,
                cmd.command_id,
            )
            self._rejected_backpressure += 1
            return False

        # 3. Record key in LRU (now committed to accepting this command)
        self._dedup.seen(cmd.idempotency_key)

        # 4. Enqueue with (priority, sequence) for stable ordering
        entry: _HeapEntry = (cmd.priority, next(self._seq), cmd)
        heapq.heappush(self._heap, entry)
        self._not_empty.set()

        # Path D.4 — increment per-command-type dispatch counter
        # after successful enqueue. Counter is "accepted" not
        # "attempted" — rejections fall in dedicated counters
        # above. NEVER raises (defaultdict + int).
        try:
            ct = cmd.command_type.value
        except Exception:  # noqa: BLE001 — defensive
            ct = str(getattr(cmd, "command_type", "unknown"))
        self._dispatch_counts[ct] += 1

        logger.debug(
            "CommandBus: enqueued %s (type=%s, priority=%d, qsize=%d)",
            cmd.command_id,
            cmd.command_type.value,
            cmd.priority,
            len(self._heap),
        )
        return True

    # ------------------------------------------------------------------
    # Path D.4 — operator-visibility read API
    # ------------------------------------------------------------------

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Per-bus metrics snapshot. Pure-read; deep-copies
        counters so the caller cannot mutate instance state.
        NEVER raises.

        Composes existing :meth:`qsize` + :meth:`get_rate_limiter_status`
        + dedup-cache size with the new per-command-type
        dispatch counters + rejection counters.

        Shape::

            {
                "qsize": <int>,
                "maxsize": <int>,
                "dedup_cache_size": <int>,
                "rate_limiter": {...} | None,
                "total_dispatched": <int>,
                "rejected_dedup": <int>,
                "rejected_backpressure": <int>,
                "by_command_type": {
                    "REQUEST_MODE_SWITCH": <int>,
                    ...
                },
            }
        """
        try:
            by_type = {
                k: int(v)
                for k, v in self._dispatch_counts.items()
            }
            total_dispatched = sum(by_type.values())
            try:
                # IdempotencyLRU.size() if exposed; fall back to
                # private cache len.
                if hasattr(self._dedup, "size"):
                    dedup_size = int(self._dedup.size())  # type: ignore[arg-type]
                else:
                    dedup_size = len(
                        self._dedup._cache,  # noqa: SLF001
                    )
            except Exception:  # noqa: BLE001 — defensive
                dedup_size = 0
            try:
                rl_status = self.get_rate_limiter_status()
            except Exception:  # noqa: BLE001 — defensive
                rl_status = None
            return {
                "qsize": int(self.qsize()),
                "maxsize": int(self._maxsize),
                "dedup_cache_size": dedup_size,
                "rate_limiter": rl_status,
                "total_dispatched": total_dispatched,
                "rejected_dedup": int(self._rejected_dedup),
                "rejected_backpressure": int(
                    self._rejected_backpressure,
                ),
                "by_command_type": by_type,
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "qsize": 0,
                "maxsize": 0,
                "dedup_cache_size": 0,
                "rate_limiter": None,
                "total_dispatched": 0,
                "rejected_dedup": 0,
                "rejected_backpressure": 0,
                "by_command_type": {},
            }

    @classmethod
    def snapshot_all(cls) -> Dict[str, Any]:
        """Aggregate metrics across ALL live CommandBus
        instances. Composes :meth:`metrics_snapshot` per
        instance + merges. NEVER raises.

        CommandBus has no global singleton (5 internal
        consumers each construct their own); this aggregate
        snapshot is the operator-surface alternative. Live
        instances are tracked via the class-level
        :data:`_INSTANCES` WeakSet — orphaned buses drop out
        automatically.
        """
        try:
            instances = list(cls._INSTANCES)
            if not instances:
                return {
                    "instance_count": 0,
                    "total_qsize": 0,
                    "total_dispatched": 0,
                    "total_rejected_dedup": 0,
                    "total_rejected_backpressure": 0,
                    "by_command_type": {},
                }
            total_qsize = 0
            total_dispatched = 0
            total_rd = 0
            total_rb = 0
            agg_by_type: Dict[str, int] = defaultdict(int)
            for inst in instances:
                try:
                    snap = inst.metrics_snapshot()
                except Exception:  # noqa: BLE001 — defensive
                    continue
                total_qsize += int(snap.get("qsize", 0))
                total_dispatched += int(
                    snap.get("total_dispatched", 0),
                )
                total_rd += int(
                    snap.get("rejected_dedup", 0),
                )
                total_rb += int(
                    snap.get("rejected_backpressure", 0),
                )
                for ct, count in snap.get(
                    "by_command_type", {},
                ).items():
                    agg_by_type[ct] += int(count)
            return {
                "instance_count": len(instances),
                "total_qsize": total_qsize,
                "total_dispatched": total_dispatched,
                "total_rejected_dedup": total_rd,
                "total_rejected_backpressure": total_rb,
                "by_command_type": dict(agg_by_type),
            }
        except Exception:  # noqa: BLE001 — defensive
            return {
                "instance_count": 0,
                "total_qsize": 0,
                "total_dispatched": 0,
                "total_rejected_dedup": 0,
                "total_rejected_backpressure": 0,
                "by_command_type": {},
            }

    @classmethod
    def reset_instance_registry_for_tests(cls) -> None:
        """Test-only — clear the WeakSet of live instances.
        Mirrors the equivalent EventEmitter helper."""
        cls._INSTANCES = weakref.WeakSet()

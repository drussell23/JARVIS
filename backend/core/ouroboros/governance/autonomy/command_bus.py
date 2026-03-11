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
from typing import List, Tuple

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    IdempotencyLRU,
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

    def __init__(
        self,
        maxsize: int = 256,
        dedup_capacity: int | None = None,
    ) -> None:
        self._maxsize = maxsize
        self._heap: List[_HeapEntry] = []
        self._seq = itertools.count()  # monotonic FIFO tiebreaker
        self._dedup = IdempotencyLRU(
            capacity=dedup_capacity if dedup_capacity is not None else maxsize * 4
        )
        # Signalled whenever a new item is pushed so that a blocked get()
        # can wake up and try to dequeue.
        self._not_empty = asyncio.Event()

    # ------------------------------------------------------------------
    # put (async, non-blocking on full — returns False)
    # ------------------------------------------------------------------

    async def put(self, cmd: CommandEnvelope) -> bool:
        """Enqueue *cmd* if it is neither a duplicate nor over capacity.

        Returns ``True`` on success, ``False`` if the command was
        rejected (duplicate idempotency key **or** bus at capacity).
        """
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
            return False

        # 2. Backpressure check
        if len(self._heap) >= self._maxsize:
            logger.debug(
                "CommandBus: at capacity (%d), command %s rejected",
                self._maxsize,
                cmd.command_id,
            )
            return False

        # 3. Record key in LRU (now committed to accepting this command)
        self._dedup.seen(cmd.idempotency_key)

        # 4. Enqueue with (priority, sequence) for stable ordering
        entry: _HeapEntry = (cmd.priority, next(self._seq), cmd)
        heapq.heappush(self._heap, entry)
        self._not_empty.set()

        logger.debug(
            "CommandBus: enqueued %s (type=%s, priority=%d, qsize=%d)",
            cmd.command_id,
            cmd.command_type.value,
            cmd.priority,
            len(self._heap),
        )
        return True

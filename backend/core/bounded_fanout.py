"""BoundedFanout — P2-1 backpressure and shedding policy for event queues.

Provides a reusable mix-in / wrapper that turns any unlimited async event
dispatch into a bounded, shedding-capable fanout with configurable policy.

Shedding policies
-----------------
DROP_NEWEST  — When the queue is full, the incoming event is dropped.
DROP_OLDEST  — When the queue is full, the oldest unprocessed event is
               evicted and the new one is accepted.

The module also exports helpers for the CrossRepoEventBus filesystem-backed
queue (which cannot use asyncio.Queue directly because it persists to disk).

Usage
-----
    # As an async context manager around a fire-and-forget transport:
    fanout = BoundedFanout(maxsize=500, policy=SheddingPolicy.DROP_OLDEST)
    async with fanout:
        await fanout.enqueue(msg)   # non-blocking; sheds if full
"""
from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any, Callable, Coroutine, Optional

__all__ = [
    "SheddingPolicy",
    "BoundedFanout",
    "FanoutStats",
    "check_fs_queue_maxsize",
]

logger = logging.getLogger(__name__)

# Default sizes
_DEFAULT_MAXSIZE = 500
_FS_DEFAULT_MAXSIZE = 1000


# ---------------------------------------------------------------------------
# Shedding policy
# ---------------------------------------------------------------------------


class SheddingPolicy(str, enum.Enum):
    """What to do when the bounded queue is at capacity."""

    DROP_NEWEST = "drop_newest"   # incoming message is discarded
    DROP_OLDEST = "drop_oldest"   # oldest enqueued message is evicted first


# ---------------------------------------------------------------------------
# Stats (read-only snapshot)
# ---------------------------------------------------------------------------


class FanoutStats:
    """Mutable counters; read is lock-free (CPython int assignment is atomic)."""

    def __init__(self) -> None:
        self.total_enqueued: int = 0
        self.total_shed: int = 0
        self.total_processed: int = 0

    def snapshot(self) -> dict:
        return {
            "total_enqueued": self.total_enqueued,
            "total_shed": self.total_shed,
            "total_processed": self.total_processed,
        }


# ---------------------------------------------------------------------------
# BoundedFanout
# ---------------------------------------------------------------------------


class BoundedFanout:
    """Bounded async queue wrapper with configurable shedding policy.

    Parameters
    ----------
    maxsize:
        Maximum number of in-flight messages.
    policy:
        What to do when the queue is full.
    name:
        Human-readable name for log messages.
    """

    def __init__(
        self,
        maxsize: int = _DEFAULT_MAXSIZE,
        policy: SheddingPolicy = SheddingPolicy.DROP_OLDEST,
        name: str = "fanout",
    ) -> None:
        self._maxsize = maxsize
        self._policy = policy
        self._name = name
        self._queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=maxsize)
        self._drain_task: Optional["asyncio.Task[None]"] = None
        self.stats = FanoutStats()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BoundedFanout":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    def start(self, handler: Callable[[Any], Coroutine[Any, Any, None]]) -> None:
        """Start the background drain loop.

        Parameters
        ----------
        handler:
            Async callable that processes one message at a time.
        """
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = asyncio.ensure_future(self._drain_loop(handler))

    async def stop(self) -> None:
        """Cancel the drain loop and wait for it to finish."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    # ------------------------------------------------------------------
    # Enqueue (non-blocking)
    # ------------------------------------------------------------------

    def enqueue_nowait(self, item: Any) -> bool:
        """Enqueue *item* without blocking.

        Returns True if accepted, False if shed.
        """
        if not self._queue.full():
            self._queue.put_nowait(item)
            self.stats.total_enqueued += 1
            return True

        # Queue full — apply shedding policy
        if self._policy == SheddingPolicy.DROP_NEWEST:
            logger.debug(
                "[BoundedFanout:%s] queue full (maxsize=%d), dropping newest",
                self._name, self._maxsize,
            )
            self.stats.total_shed += 1
            return False

        # DROP_OLDEST: evict oldest then accept
        try:
            self._queue.get_nowait()
            self.stats.total_shed += 1
            logger.debug(
                "[BoundedFanout:%s] queue full, evicting oldest (shed_total=%d)",
                self._name, self.stats.total_shed,
            )
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(item)
            self.stats.total_enqueued += 1
            return True
        except asyncio.QueueFull:
            self.stats.total_shed += 1
            return False

    # ------------------------------------------------------------------
    # Drain loop
    # ------------------------------------------------------------------

    async def _drain_loop(
        self, handler: Callable[[Any], Coroutine[Any, Any, None]]
    ) -> None:
        """Drain the queue serially, calling *handler* for each item."""
        while True:
            try:
                item = await self._queue.get()
                try:
                    await handler(item)
                except Exception as exc:
                    logger.warning(
                        "[BoundedFanout:%s] handler raised: %s", self._name, exc
                    )
                finally:
                    self._queue.task_done()
                    self.stats.total_processed += 1
            except asyncio.CancelledError:
                break


# ---------------------------------------------------------------------------
# Filesystem queue guard — CrossRepoEventBus helper
# ---------------------------------------------------------------------------


def check_fs_queue_maxsize(
    pending_dir: Any,  # pathlib.Path
    maxsize: int = _FS_DEFAULT_MAXSIZE,
) -> bool:
    """Return True if the filesystem pending queue is under maxsize.

    Counts ``*.json`` files in *pending_dir*.  Callers should call this
    before writing a new event file and shed if it returns False.
    """
    try:
        count = sum(1 for _ in pending_dir.glob("*.json"))
        if count >= maxsize:
            logger.warning(
                "[CrossRepoEventBus] fs pending queue at maxsize=%d (count=%d) — "
                "shedding event",
                maxsize, count,
            )
            return False
        return True
    except Exception:
        return True  # on error, allow (fail-open for reliability)

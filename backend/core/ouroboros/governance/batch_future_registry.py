"""BatchFutureRegistry — zero-poll webhook-driven batch completion.

Maps ``batch_id`` to ``asyncio.Future`` so callers can ``await`` a result
that is resolved by an incoming DW webhook (``batch.completed`` /
``batch.failed``).  Falls back to adaptive polling when the webhook is
not configured.

Manifesto §3: Zero polling. Pure reflex.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


class BatchFutureRegistry:
    """In-memory registry of pending batch futures.

    Thread-safe via asyncio primitives (single event loop).  Stale
    entries are pruned automatically on ``register()``.

    Parameters
    ----------
    ttl_s:
        Time-to-live for registered futures (seconds).  Futures older
        than this are pruned to prevent memory leaks from batches that
        never receive a webhook.
    """

    def __init__(self, ttl_s: float = 3600.0) -> None:
        self._futures: Dict[str, asyncio.Future] = {}
        self._created_at: Dict[str, float] = {}
        self._ttl_s = ttl_s

    # ── Public API ────────────────────────────────────────────

    def register(self, batch_id: str) -> asyncio.Future:
        """Register a future for *batch_id*.  Returns the Future.

        Called at batch submission time so the webhook handler can
        resolve it later.  Prunes stale entries on each call.
        """
        self._prune_stale()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._futures[batch_id] = future
        self._created_at[batch_id] = time.monotonic()
        logger.debug("[BatchFutureRegistry] Registered %s", batch_id)
        return future

    def resolve(self, batch_id: str, output_file_id: str) -> bool:
        """Resolve a pending future with the output file ID.

        Called by the webhook handler on ``batch.completed``.
        Returns ``True`` if a future was resolved.
        """
        future = self._futures.pop(batch_id, None)
        self._created_at.pop(batch_id, None)
        if future is not None and not future.done():
            future.set_result(output_file_id)
            logger.info("[BatchFutureRegistry] Resolved %s", batch_id)
            return True
        return False

    def reject(self, batch_id: str, reason: str) -> bool:
        """Reject a pending future with an error.

        Called by the webhook handler on ``batch.failed``.
        Returns ``True`` if a future was rejected.
        """
        future = self._futures.pop(batch_id, None)
        self._created_at.pop(batch_id, None)
        if future is not None and not future.done():
            from backend.core.ouroboros.governance.doubleword_provider import DoublewordInfraError
            future.set_exception(DoublewordInfraError(f"batch_failed: {reason}"))
            logger.warning("[BatchFutureRegistry] Rejected %s: %s", batch_id, reason)
            return True
        return False

    async def wait(self, batch_id: str, timeout: float) -> str:
        """Await the future for *batch_id* with a timeout.

        Returns the ``output_file_id`` on success.
        Raises ``asyncio.TimeoutError`` on timeout.
        Raises ``DoublewordInfraError`` if the batch failed.
        Raises ``KeyError`` if no future is registered.
        """
        future = self._futures.get(batch_id)
        if future is None:
            raise KeyError(f"No future registered for batch {batch_id}")
        return await asyncio.wait_for(future, timeout=timeout)

    @property
    def pending_count(self) -> int:
        """Number of pending (unresolved) futures."""
        return len(self._futures)

    # ── Internal ──────────────────────────────────────────────

    def _prune_stale(self) -> None:
        """Remove futures older than TTL."""
        now = time.monotonic()
        stale = [
            bid for bid, ts in self._created_at.items()
            if now - ts > self._ttl_s
        ]
        for bid in stale:
            future = self._futures.pop(bid, None)
            self._created_at.pop(bid, None)
            if future is not None and not future.done():
                future.cancel()
            logger.debug("[BatchFutureRegistry] Pruned stale %s", bid)

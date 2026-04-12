"""
Background Agent Pool -- Non-Blocking Agent Execution
======================================================

Provides a bounded worker pool for executing Ouroboros governance
operations concurrently without blocking the caller.  Operations are
submitted via :meth:`BackgroundAgentPool.submit`, which returns an
``op_id`` immediately.  Callers retrieve results later via
:meth:`get_result`.

Architecture
------------

.. code-block:: text

    submit(op_context)
        |
        v
    +----------+    _worker_loop()    +---------------------+
    |  Queue   | ---- dequeue ------> | GovernedOrchestrator |
    | (bounded)|                      |       .run(ctx)      |
    +----------+                      +---------------------+
        ^                                     |
        |                                     v
    QueueFullError                      BackgroundOp.result
    (if at capacity)                   (OperationContext)

Workers are plain ``asyncio.Task`` coroutines (not threads), so they
share the event loop with the rest of the supervisor.  Pool size and
queue depth are tunable via environment variables to match available
system resources.

Boundary Principle
------------------
Queue management is **deterministic** -- bounded size, FIFO ordering,
back-pressure via ``QueueFullError``.  The actual operation execution
inside each worker is **agentic** -- delegated entirely to the
``GovernedOrchestrator.run()`` pipeline.

Environment Variables
---------------------
``JARVIS_BG_POOL_SIZE``
    Number of concurrent worker coroutines (default 2).
``JARVIS_BG_QUEUE_SIZE``
    Maximum number of queued operations before back-pressure (default 10).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger("Ouroboros.BackgroundAgentPool")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QueueFullError(Exception):
    """Raised when the background operation queue is at capacity.

    Callers should either wait and retry, or drop the operation with
    appropriate logging.
    """


# ---------------------------------------------------------------------------
# BackgroundOp
# ---------------------------------------------------------------------------


_VALID_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})


@dataclass
class BackgroundOp:
    """Tracks the lifecycle of a single background operation.

    Attributes
    ----------
    op_id:
        Unique identifier for this background operation slot.  This is
        **not** the same as the governance ``OperationContext.op_id`` --
        it is a pool-internal tracking ID.
    goal:
        Human-readable goal description extracted from the operation context.
    status:
        One of ``"queued"``, ``"running"``, ``"completed"``, ``"failed"``,
        ``"cancelled"``.
    submitted_at:
        Monotonic timestamp when the operation was submitted.
    started_at:
        Monotonic timestamp when a worker began processing (None until started).
    completed_at:
        Monotonic timestamp when the operation reached a terminal state.
    result:
        The terminal ``OperationContext`` returned by the orchestrator.
    error:
        String representation of the exception if the operation failed.
    context:
        The original ``OperationContext`` submitted for processing.
    task:
        Internal reference to the ``asyncio.Task`` running this operation.
        Not intended for external use.
    """

    op_id: str
    goal: str
    status: str = "queued"
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    context: Optional[Any] = None  # OperationContext (typed loosely to avoid import)
    task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {self.status!r}; must be one of {_VALID_STATUSES}"
            )

    @property
    def elapsed_s(self) -> Optional[float]:
        """Wall-clock seconds from start to completion, or None if not started."""
        if self.started_at is None:
            return None
        end = self.completed_at if self.completed_at is not None else time.monotonic()
        return end - self.started_at

    @property
    def is_terminal(self) -> bool:
        """True if the operation has reached a terminal status."""
        return self.status in ("completed", "failed", "cancelled")


# ---------------------------------------------------------------------------
# BackgroundAgentPool
# ---------------------------------------------------------------------------


def _read_env_int(key: str, default: int) -> int:
    """Read an integer from the environment with a safe fallback."""
    raw = os.environ.get(key, "")
    if not raw:
        return default
    try:
        value = int(raw)
        if value < 1:
            logger.warning("%s=%d is < 1, using default %d", key, value, default)
            return default
        return value
    except ValueError:
        logger.warning("%s=%r is not a valid integer, using default %d", key, raw, default)
        return default


class BackgroundAgentPool:
    """Bounded async worker pool for non-blocking governance operations.

    Parameters
    ----------
    orchestrator:
        The ``GovernedOrchestrator`` instance whose ``.run()`` method will
        be called for each queued operation.
    pool_size:
        Number of concurrent worker coroutines.  Defaults to the value of
        ``JARVIS_BG_POOL_SIZE`` (or 2).
    queue_size:
        Maximum number of operations that can be queued before
        :class:`QueueFullError` is raised.  Defaults to the value of
        ``JARVIS_BG_QUEUE_SIZE`` (or 10).
    """

    def __init__(
        self,
        orchestrator: GovernedOrchestrator,
        pool_size: Optional[int] = None,
        queue_size: Optional[int] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._pool_size: int = (
            pool_size if pool_size is not None
            else _read_env_int("JARVIS_BG_POOL_SIZE", 3)
        )
        self._queue_size: int = (
            queue_size if queue_size is not None
            else _read_env_int("JARVIS_BG_QUEUE_SIZE", 16)
        )
        # PriorityQueue: items are (priority, submission_order, op).
        # Lower priority number = runs first.  Ensures IMMEDIATE ops
        # don't starve BACKGROUND ops when workers free up.
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=self._queue_size,
        )
        self._submit_counter: int = 0
        self._ops: Dict[str, BackgroundOp] = {}
        self._workers: List[asyncio.Task] = []  # type: ignore[type-arg]
        self._running: bool = False

        # Pause gate for HIBERNATION_MODE. Default set == "not paused".
        # Workers await this before dequeuing; pause() clears it, resume()
        # sets it. In-flight ops are NOT interrupted — they finish naturally.
        # Queued items stay in the PriorityQueue untouched, so state is
        # preserved across the outage window.
        self._unpaused_event: asyncio.Event = asyncio.Event()
        self._unpaused_event.set()
        self._paused: bool = False
        self._paused_at: Optional[float] = None
        self._pause_count: int = 0

        # Counters for health reporting
        self._completed_count: int = 0
        self._failed_count: int = 0
        self._cancelled_count: int = 0

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Create the worker coroutines and begin processing the queue.

        Safe to call multiple times -- subsequent calls are no-ops if
        the pool is already running.
        """
        if self._running:
            logger.debug("Pool already running, start() is a no-op")
            return

        self._running = True
        self._workers = [
            asyncio.create_task(
                self._worker_loop(worker_id=i),
                name=f"bg-agent-worker-{i}",
            )
            for i in range(self._pool_size)
        ]
        logger.info(
            "Background agent pool started: pool_size=%d, queue_size=%d",
            self._pool_size,
            self._queue_size,
        )

    async def stop(self) -> None:
        """Cancel all workers, drain the queue, and mark pending ops as cancelled.

        Idempotent -- safe to call even if the pool was never started.
        """
        if not self._running:
            return

        self._running = False
        logger.info("Stopping background agent pool (%d workers)", len(self._workers))

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        # Await worker shutdown with a generous timeout
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Drain remaining queued ops (handle PriorityQueue tuples)
        drained = 0
        while not self._queue.empty():
            try:
                _item = self._queue.get_nowait()
                op = _item[2] if isinstance(_item, tuple) else _item
                op.status = "cancelled"
                op.completed_at = time.monotonic()
                self._cancelled_count += 1
                drained += 1
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        if drained:
            logger.info("Drained %d queued operations during shutdown", drained)

        logger.info("Background agent pool stopped")

    # -- Pause / Resume (HIBERNATION_MODE) -----------------------------------

    def pause(self, *, reason: str = "") -> bool:
        """Pause dequeuing without draining the queue.

        Workers finish their in-flight op naturally, then block on
        ``_unpaused_event`` before picking up the next one. The queue and
        all tracked ops are preserved — ``resume()`` restores throughput
        exactly where it left off.

        Idempotent. Safe to call while the pool is stopped (no-op).

        Returns True if a state transition occurred, False if already paused
        or not running.
        """
        if not self._running:
            logger.debug("pause() called on stopped pool — no-op")
            return False
        if self._paused:
            return False
        self._unpaused_event.clear()
        self._paused = True
        self._paused_at = time.monotonic()
        self._pause_count += 1
        logger.info(
            "Background agent pool PAUSED (queue_depth=%d, reason=%r)",
            self._queue.qsize(),
            reason or "unspecified",
        )
        return True

    def resume(self, *, reason: str = "") -> bool:
        """Release workers to dequeue again.

        Idempotent. Returns True on transition, False if already running
        (not paused) or pool is stopped.
        """
        if not self._running:
            logger.debug("resume() called on stopped pool — no-op")
            return False
        if not self._paused:
            return False
        paused_for = (
            time.monotonic() - self._paused_at if self._paused_at is not None else 0.0
        )
        self._unpaused_event.set()
        self._paused = False
        self._paused_at = None
        logger.info(
            "Background agent pool RESUMED after %.1fs (queue_depth=%d, reason=%r)",
            paused_for,
            self._queue.qsize(),
            reason or "unspecified",
        )
        return True

    @property
    def is_paused(self) -> bool:
        """True if pause() has been called and resume() has not yet fired."""
        return self._paused

    # -- Submit / Query ------------------------------------------------------

    async def submit(self, op_context: OperationContext) -> str:
        """Submit an operation for background processing.

        Returns the pool-internal ``op_id`` immediately.  The operation
        will be picked up by the next available worker.

        Parameters
        ----------
        op_context:
            The ``OperationContext`` to execute through the governance
            pipeline.

        Returns
        -------
        str
            The unique background operation ID (use with :meth:`get_result`).

        Raises
        ------
        QueueFullError
            If the queue is already at capacity.
        RuntimeError
            If the pool has not been started.
        """
        if not self._running:
            raise RuntimeError(
                "BackgroundAgentPool is not running -- call start() first"
            )

        op_id = f"bgop-{uuid.uuid4().hex[:12]}"
        goal = getattr(op_context, "goal", "") or getattr(op_context, "op_id", op_id)
        op = BackgroundOp(
            op_id=op_id,
            goal=str(goal),
            context=op_context,
        )

        # Route-based priority for PriorityQueue (lower = runs first).
        _route = getattr(op_context, "provider_route", "") or "standard"
        _route_priority = {
            "immediate": 1, "standard": 3, "complex": 3,
            "background": 5, "speculative": 7,
        }
        _priority = _route_priority.get(_route, 3)
        self._submit_counter += 1

        try:
            self._queue.put_nowait((_priority, self._submit_counter, op))
        except asyncio.QueueFull:
            raise QueueFullError(
                f"Background queue is full ({self._queue_size} items). "
                f"Operation {op_id} rejected. Either wait for capacity or "
                f"increase JARVIS_BG_QUEUE_SIZE."
            )

        self._ops[op_id] = op
        logger.info(
            "Submitted background operation %s (goal=%r, route=%s, "
            "priority=%d, queue_depth=%d/%d)",
            op_id,
            op.goal[:80],
            _route,
            _priority,
            self._queue.qsize(),
            self._queue_size,
        )
        return op_id

    def get_result(self, op_id: str) -> Optional[BackgroundOp]:
        """Return the :class:`BackgroundOp` for the given ID, or None.

        Callers should check ``.status`` and ``.result`` on the returned
        object to determine whether the operation is still in progress.
        """
        return self._ops.get(op_id)

    async def cancel(self, op_id: str) -> bool:
        """Cancel a queued or running operation.

        Parameters
        ----------
        op_id:
            The background operation ID returned by :meth:`submit`.

        Returns
        -------
        bool
            True if the operation was successfully cancelled.
        """
        op = self._ops.get(op_id)
        if op is None:
            logger.warning("Cannot cancel unknown operation %s", op_id)
            return False

        if op.is_terminal:
            logger.debug("Operation %s already terminal (%s)", op_id, op.status)
            return False

        if op.status == "queued":
            # Mark as cancelled -- the worker will skip it when dequeued
            op.status = "cancelled"
            op.completed_at = time.monotonic()
            self._cancelled_count += 1
            logger.info("Cancelled queued operation %s", op_id)
            return True

        if op.status == "running" and op.task is not None:
            op.task.cancel()
            # The worker_loop will catch CancelledError and set status
            logger.info("Sent cancel signal to running operation %s", op_id)
            return True

        logger.warning(
            "Operation %s in unexpected state for cancel: status=%s, task=%s",
            op_id,
            op.status,
            op.task,
        )
        return False

    def list_active(self) -> List[BackgroundOp]:
        """Return all non-terminal operations (queued + running)."""
        return [op for op in self._ops.values() if not op.is_terminal]

    def list_all(self) -> List[BackgroundOp]:
        """Return all tracked operations, in submission order."""
        return list(self._ops.values())

    # -- Health --------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return pool health statistics for monitoring and dashboards.

        Returns a dict with keys:

        - ``running``: whether the pool is active
        - ``pool_size``: configured worker count
        - ``queue_depth``: current queue occupancy
        - ``queue_capacity``: maximum queue size
        - ``active_workers``: number of workers currently processing an op
        - ``total_tracked``: total operations ever submitted
        - ``completed_count``: successful completions
        - ``failed_count``: operations that raised exceptions
        - ``cancelled_count``: operations cancelled before completion
        - ``active_ops``: list of active operation summaries
        """
        active_ops = self.list_active()
        paused_for = (
            time.monotonic() - self._paused_at
            if self._paused and self._paused_at is not None
            else None
        )
        return {
            "running": self._running,
            "paused": self._paused,
            "paused_for_s": round(paused_for, 2) if paused_for is not None else None,
            "pause_count": self._pause_count,
            "pool_size": self._pool_size,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue_size,
            "active_workers": sum(
                1 for op in self._ops.values() if op.status == "running"
            ),
            "total_tracked": len(self._ops),
            "completed_count": self._completed_count,
            "failed_count": self._failed_count,
            "cancelled_count": self._cancelled_count,
            "active_ops": [
                {
                    "op_id": op.op_id,
                    "goal": op.goal[:120],
                    "status": op.status,
                    "elapsed_s": round(op.elapsed_s, 2) if op.elapsed_s else None,
                }
                for op in active_ops
            ],
        }

    # -- Internal worker -----------------------------------------------------

    async def _worker_loop(self, worker_id: int) -> None:
        """Core worker coroutine -- dequeues and executes operations.

        Runs until the pool is stopped or the task is cancelled.  Each
        iteration dequeues one :class:`BackgroundOp`, delegates to
        ``GovernedOrchestrator.run()``, and records the outcome.

        Parameters
        ----------
        worker_id:
            Numeric identifier for log correlation.
        """
        logger.debug("Worker %d started", worker_id)
        try:
            while self._running:
                # HIBERNATION_MODE: block here while paused. Bounded wait so
                # stop() still reacts within ~2s even if resume() never fires.
                if not self._unpaused_event.is_set():
                    try:
                        await asyncio.wait_for(
                            self._unpaused_event.wait(),
                            timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        continue

                try:
                    _item = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=2.0,  # Periodic check of self._running
                    )
                except asyncio.TimeoutError:
                    continue

                # Race guard: pause() may have fired while we were blocked
                # inside get(). If so, re-enqueue the item and loop back to
                # the pause-wait gate. This preserves queue ordering (the
                # tuple carries its original priority + submission counter)
                # and keeps unfinished_tasks balanced (task_done + put).
                if not self._unpaused_event.is_set():
                    self._queue.task_done()
                    self._queue.put_nowait(_item)
                    continue

                # Unpack PriorityQueue tuple: (priority, counter, op)
                if isinstance(_item, tuple):
                    _, _, op = _item
                else:
                    op = _item  # backward compat

                # Skip already-cancelled ops (cancelled while queued)
                if op.status == "cancelled":
                    self._queue.task_done()
                    continue

                op.status = "running"
                op.started_at = time.monotonic()
                op.task = asyncio.current_task()

                logger.info(
                    "Worker %d picked up operation %s (goal=%r)",
                    worker_id,
                    op.op_id,
                    op.goal[:80],
                )

                try:
                    result = await self._orchestrator.run(op.context)
                    op.result = result
                    op.status = "completed"
                    self._completed_count += 1
                    logger.info(
                        "Worker %d completed operation %s in %.2fs",
                        worker_id,
                        op.op_id,
                        op.elapsed_s or 0.0,
                    )
                except asyncio.CancelledError:
                    op.status = "cancelled"
                    self._cancelled_count += 1
                    logger.info(
                        "Worker %d: operation %s was cancelled",
                        worker_id,
                        op.op_id,
                    )
                    raise  # Re-raise to let asyncio handle task cancellation
                except Exception as exc:
                    op.error = f"{type(exc).__name__}: {exc}"
                    op.status = "failed"
                    self._failed_count += 1
                    logger.error(
                        "Worker %d: operation %s failed: %s",
                        worker_id,
                        op.op_id,
                        op.error,
                        exc_info=True,
                    )
                finally:
                    op.completed_at = time.monotonic()
                    op.task = None
                    self._queue.task_done()

        except asyncio.CancelledError:
            logger.debug("Worker %d shutting down (cancelled)", worker_id)
        except Exception:
            logger.exception("Worker %d crashed unexpectedly", worker_id)
        finally:
            logger.debug("Worker %d exited", worker_id)

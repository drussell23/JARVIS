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
Queue management is **deterministic** -- bounded size, route-based
**priority ordering** (``asyncio.PriorityQueue``: lower route-priority number
runs first -- immediate=1, standard/complex=3, background=5, speculative=7, with
``submission_order`` as the FIFO tie-break within a priority), back-pressure via
``QueueFullError``.  The actual operation execution
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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type

# Slice 246 — leaf imports (no cycle: intent_envelope + preemption import neither
# this module nor the orchestrator).
from backend.core.ouroboros.governance.intake.intent_envelope import (
    SOVEREIGN_SOURCES as _SOVEREIGN_SOURCES,
)
from backend.core.ouroboros.governance import preemption as _preemption
from backend.core.ouroboros.governance.preemption import OperationPreemptedError

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.park_signal import ParkRequested

logger = logging.getLogger("Ouroboros.BackgroundAgentPool")


# ---------------------------------------------------------------------------
# Stage 1.6 — lazy import of ParkRequested
# ---------------------------------------------------------------------------
# Lazy-loaded so the BG pool stays module-import-cheap and the park
# substrate can be unit-tested without forcing this module to load.
# Cached on first call (typically the FIRST park event in the process
# lifetime — there is no resolution on the legacy non-park path).
#
# Cannot use ``TYPE_CHECKING`` here because the worker loop must actually
# catch the class at runtime — TYPE_CHECKING gates compile-time-only
# imports.  A cached lazy-import is the cleanest pattern that keeps
# import order acyclic and one-shot.

_PARK_REQUESTED_CLS: Optional[Type[BaseException]] = None


def _ParkRequested_t() -> "Type[ParkRequested]":
    """Resolve and cache the :class:`ParkRequested` class.

    Returns the bare ``BaseException`` subclass that the worker
    ``except`` clause can use as a class-reference.  Cached for the
    lifetime of the process — the import is idempotent and the class
    object never changes mid-process.

    On import failure (substrate not yet merged, broken install) the
    function returns a sentinel ``_ParkRequestedUnavailable`` class
    so the ``except`` clause is well-formed but unreachable.  This
    keeps the worker resilient to substrate breakage — the legacy
    paths (completed / failed / cancelled / timeout) remain valid.
    """
    global _PARK_REQUESTED_CLS
    if _PARK_REQUESTED_CLS is not None:
        return _PARK_REQUESTED_CLS  # type: ignore[return-value]
    try:
        from backend.core.ouroboros.governance.park_signal import (
            ParkRequested as _PR,
        )
        _PARK_REQUESTED_CLS = _PR
    except Exception:  # noqa: BLE001 — defensive substrate-load
        logger.warning(
            "ParkRequested import failed; park-aware worker loop is a "
            "no-op until substrate is reachable",
            exc_info=True,
        )

        class _ParkRequestedUnavailable(Exception):
            """Sentinel — never raised, makes the except clause well-formed."""

        _PARK_REQUESTED_CLS = _ParkRequestedUnavailable
    return _PARK_REQUESTED_CLS  # type: ignore[return-value]


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


_VALID_STATUSES = frozenset({
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    # Stage 1.6 — terminal-for-this-dispatch but non-terminal for the
    # op: the slot is freed and a resume dispatch will fire when the
    # out-of-pool continuation completes.  ``is_terminal`` deliberately
    # returns False for "parked" so existing watchers (e.g. waiters
    # awaiting result) keep waiting until the resume completes.
    "parked",
})


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
    # Stage 1.6 — resume-after-park indicator.  Set to True by the
    # out-of-pool continuation when it re-submits ctx after the parked
    # provider call completes.  The GENERATE wrapper (Slice 2b) reads
    # this (via a pool side-channel) to materialize the parked result
    # from ParkedOpStore instead of re-issuing the provider call.
    # Default False = fresh dispatch — behavior identical to pre-1.6.
    resumed: bool = False
    # Stage 1.6 — monotonic park-attempt sequence for this op.  Bumped
    # by the GENERATE wrapper each time it parks, so a GENERATE_RETRY
    # produces a fresh ledger entry_id under the same op_id.  Default 0
    # = never parked.
    park_attempt_seq: int = 0

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


# Slice 245 — route priorities the pool ranks by (mirrors submit()). Lower runs
# first: immediate=1, standard/complex=3, background=5, speculative=7.
_ROUTE_PRIORITY: Dict[str, int] = {
    "immediate": 1, "standard": 3, "complex": 3, "background": 5, "speculative": 7,
}
# Error substrings that mark an op as a hibernation SURVIVOR — it died because
# the grid went dark, not because the work was bad. These get re-ingested with
# Absolute-Max Primacy on wake.
_RESURRECTABLE_ERROR_MARKERS: Tuple[str, ...] = (
    "all_providers_exhausted", "providers_exhausted",
    "deadline_exhausted", "live_transport",
)


def _resurrection_primacy_margin() -> int:
    """Margin below the highest normal pool priority for a resurrected op.
    Shares the JARVIS_RESURRECTION_PRIMACY_MARGIN knob with the intake layer.
    NEVER raises (floors at 1)."""
    try:
        v = int(float(os.environ.get("JARVIS_RESURRECTION_PRIMACY_MARGIN", "").strip() or 100))
        return v if v >= 1 else 100
    except (TypeError, ValueError):
        return 100


def _resurrection_pool_priority() -> int:
    """Absolute-max pool priority — dynamically below the highest normal route
    priority (NOT a hardcoded 0/1). Derived from _ROUTE_PRIORITY so it stays
    correct if the route tiers change."""
    return min(_ROUTE_PRIORITY.values()) - _resurrection_primacy_margin()


def _sovereign_primacy_margin() -> int:
    """Slice 246 — margin by which a human-origin intent outranks resurrection in
    the pool. Shares the JARVIS_SOVEREIGN_PRIMACY_MARGIN knob with the intake
    layer. NEVER raises (floors at 1)."""
    try:
        v = int(float(os.environ.get("JARVIS_SOVEREIGN_PRIMACY_MARGIN", "").strip() or 100))
        return v if v >= 1 else 100
    except (TypeError, ValueError):
        return 100


def _sovereign_pool_priority() -> int:
    """Sovereign human pool priority — strictly below resurrection (Human >
    Resurrected > Normal). The host always wins the worker lane."""
    return _resurrection_pool_priority() - _sovereign_primacy_margin()


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
        on_op_active_register: Optional[Callable[[str], None]] = None,
        on_op_active_unregister: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._orchestrator = orchestrator
        # Move 2 v5 — Unified Observability. The harness ActivityMonitor
        # tracks ``GovernedLoopService._active_ops``; the original BG path
        # bypassed that tracker, so ops streaming tokens for minutes were
        # invisible to the staleness check and the idle watchdog fired
        # prematurely. The hooks let GLS register/unregister BG ops in
        # the same central registry foreground ops live in. Both default
        # to no-ops so older callers (tests) don't break.
        self._on_op_active_register = on_op_active_register
        self._on_op_active_unregister = on_op_active_unregister
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
        # Stage 1.6 — count of GENERATE-parked dispatches.  Distinct
        # from completed/failed/cancelled because "parked" is non-
        # terminal for the op: a separate resumed dispatch will land
        # one of the other three counters when the out-of-pool
        # continuation completes.  Surfaced in get_status() for
        # observability.
        self._parked_count: int = 0
        # Stage 1.6 — out-of-pool continuation tasks (Slice 2b).  These
        # are the asyncio.Tasks that run the actual provider call
        # OUTSIDE the worker slot (where the slot has been released).
        # Tracked so shutdown can cancel them gracefully + GC them on
        # completion via add_done_callback.  Set semantics — a task
        # cannot be in the set twice.
        self._park_continuation_tasks: set = set()
        # Stage 1.6 — resume-dispatch side-channel.  Maps
        # ctx.op_id -> park_attempt_seq for ops currently in a resume
        # dispatch.  The GENERATE-park wrapper reads this to know
        # whether to materialize from the store (resume) or take the
        # park-emit / legacy path.  Cleared after the resumed
        # dispatch reaches terminal in the worker loop's finally
        # block (see _clear_resume_mark below).
        self._resumed_ops: Dict[str, int] = {}

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
        # Stage 1.6 — register self in the process-wide bind so the
        # GENERATE-park wrapper can re-submit resumed dispatches
        # without taking a hard module dependency on this class.
        # Mirrors the orchestrator bind shape; best-effort (test
        # contexts that import _governance_state lazily are fine).
        try:
            from backend.core.ouroboros.governance._governance_state import (
                bind_bg_pool as _bind,
            )
            _bind(self)
        except Exception:  # noqa: BLE001
            logger.debug(
                "bind_bg_pool unavailable at pool.start() — park "
                "substrate will fall back to legacy direct-await",
                exc_info=True,
            )
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

        # Stage 1.6 — clear the process-wide bind BEFORE cancelling
        # workers so no new park-emit can racing the shutdown can
        # re-submit a resumed dispatch into a stopped pool.
        try:
            from backend.core.ouroboros.governance._governance_state import (
                bind_bg_pool as _bind,
            )
            _bind(None)
        except Exception:  # noqa: BLE001
            logger.debug(
                "bind_bg_pool(None) failed during pool.stop()",
                exc_info=True,
            )

        # Stage 1.6 — cancel any in-flight out-of-pool continuations.
        # These are the tasks that hold the slot-freed provider call;
        # cancelling them propagates CancelledError up through the
        # continuation handler which calls store.cancel(token).
        if self._park_continuation_tasks:
            n_cont = len(self._park_continuation_tasks)
            for cont in list(self._park_continuation_tasks):
                cont.cancel()
            await asyncio.gather(
                *self._park_continuation_tasks, return_exceptions=True,
            )
            self._park_continuation_tasks.clear()
            logger.info(
                "Cancelled %d park continuation tasks during shutdown",
                n_cont,
            )

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
                # Sovereign Exec Engine (2026-06-19) — release any budget
                # reservation this queued op held. These ops never reach the
                # orchestrator terminal release hook (they're cancelled here,
                # not run()), so without this they LEAK their reservation and
                # permanently shrink effective_remaining. The TTL sweep is the
                # backstop; this is the precise, immediate release.
                try:
                    _ctx = getattr(op, "context", None)
                    _oid = str(getattr(_ctx, "op_id", "") or "")
                    if _oid:
                        from backend.core.ouroboros.governance.session_budget_authority import (  # noqa: E501
                            release_reservation as _sba_release,
                        )
                        _sba_release(_oid)
                except Exception:  # noqa: BLE001 — release is best-effort
                    pass
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
        _signal_src = getattr(op_context, "signal_source", "") or ""
        # Slice 246 — sovereign human primacy: a direct human-origin intent
        # outranks EVERYTHING (including a resurrected survivor). Checked first.
        # Slice 245 — a hibernation survivor jumps ahead of EVERY normal route
        # (dynamic absolute-max), so dark-window backlog can't starve it.
        if _signal_src in _SOVEREIGN_SOURCES:
            _priority = _sovereign_pool_priority()
        elif getattr(op_context, "resurrected_from_hibernation", False):
            _priority = _resurrection_pool_priority()
        else:
            _priority = _ROUTE_PRIORITY.get(_route, 3)
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
        # Slice 246 — Preemption Sentinel. A live human intent just landed; if a
        # resurrected survivor is actively occupying a worker, fire a non-blocking
        # preemption so it gracefully yields at its next round boundary (the
        # worker re-ingests it — micro-hibernation). Queue ordering alone can't
        # preempt an already-running op. Gated + fail-soft.
        if _signal_src in _SOVEREIGN_SOURCES and _preemption.human_preemption_enabled():
            try:
                for _running_id in self.running_resurrected_op_ids():
                    _preemption.request_preemption(_running_id)
                    logger.info(
                        "[BGPool] PREEMPTION requested for running resurrected "
                        "op=%s — yielding to human override (source=%s)",
                        _running_id, _signal_src,
                    )
            except Exception:  # noqa: BLE001 — sentinel must never block submit
                logger.exception("[BGPool] preemption sentinel failed")
        return op_id

    def running_resurrected_op_ids(self) -> List[str]:
        """Slice 246 — ctx.op_id of every currently-RUNNING resurrected survivor.
        These are the only ops a human override needs to preempt (queued ones are
        already out-ranked by sovereign priority). NEVER raises."""
        out: List[str] = []
        try:
            for op in self._ops.values():
                if op.status != "running":
                    continue
                ctx = getattr(op, "context", None)
                if ctx is not None and getattr(ctx, "resurrected_from_hibernation", False):
                    cid = str(getattr(ctx, "op_id", "") or "")
                    if cid:
                        out.append(cid)
        except Exception:  # noqa: BLE001
            logger.exception("[BGPool] running_resurrected_op_ids failed")
        return out

    def get_result(self, op_id: str) -> Optional[BackgroundOp]:
        """Return the :class:`BackgroundOp` for the given ID, or None.

        Callers should check ``.status`` and ``.result`` on the returned
        object to determine whether the operation is still in progress.
        """
        return self._ops.get(op_id)

    # ------------------------------------------------------------------
    # Stage 1.6 — park / resume side-channel (Slice 2b)
    # ------------------------------------------------------------------

    async def submit_for_resume(
        self, op_context: "OperationContext", *, attempt_seq: int,
    ) -> str:
        """Re-submit a parked op as a resume dispatch.

        Called by the out-of-pool continuation task after the parked
        provider call completes (or fails).  The next worker that picks
        up this ctx will read ``BackgroundOp.resumed=True`` and
        ``park_attempt_seq=attempt_seq``; the GENERATE-park wrapper
        materializes the parked result from :class:`ParkedOpStore`
        instead of re-issuing the provider call.

        Parameters
        ----------
        op_context:
            The same OperationContext the original park-emit dispatch
            ran under.  Identity preservation invariant — ctx.op_id
            stable across park→resume.
        attempt_seq:
            The park attempt sequence (1 for first GENERATE, 2+ for
            GENERATE_RETRY).  Mirrored into the ledger entry_id.

        Returns
        -------
        str
            The pool-internal op_id of the resumed BackgroundOp.

        Raises
        ------
        QueueFullError
            If the queue is at capacity.  The out-of-pool continuation
            should catch this and route the op to a failure ledger
            entry — the parked op cannot resume without a slot.
        """
        # Submit via the canonical path so all the priority + tracking
        # invariants compose; THEN mark the BackgroundOp as resumed.
        # The marking happens BEFORE any worker can pick it up because
        # we hold the loop until both ops complete in this coroutine.
        if attempt_seq < 1:
            raise ValueError(
                f"attempt_seq must be >= 1, got {attempt_seq}"
            )
        op_id = await self.submit(op_context)
        bg_op = self._ops[op_id]
        bg_op.resumed = True
        bg_op.park_attempt_seq = attempt_seq
        # Side-channel for the GENERATE wrapper.  Keyed by ctx.op_id
        # (the orchestrator-visible id), not the pool-internal slot id,
        # because the wrapper sees only ctx.
        ctx_op_id = str(getattr(op_context, "op_id", "") or "")
        if ctx_op_id:
            self._resumed_ops[ctx_op_id] = attempt_seq
        logger.info(
            "submit_for_resume: ctx_op_id=%s pool_op_id=%s attempt=%d "
            "(resumed=True)",
            ctx_op_id, op_id, attempt_seq,
        )
        return op_id

    async def resubmit_resurrected(self, op_context: "OperationContext") -> str:
        """Slice 245 — re-ingest a hibernation survivor with Absolute-Max Primacy.

        Re-submits the op's EXACT OperationContext (preserving durable partial
        state — phase, already-generated candidates, plan; no completed work is
        re-computed) flagged via ``with_resurrection()``. ``submit()`` then reads
        the flag and assigns ``_resurrection_pool_priority()`` so the survivor
        dequeues ahead of everything that accumulated during the dark window.
        Reuses the canonical submit path — no parallel enqueue logic."""
        resurrected = op_context
        with_fn = getattr(op_context, "with_resurrection", None)
        if callable(with_fn):
            resurrected = with_fn()
        op_id = await self.submit(resurrected)
        logger.info(
            "resubmit_resurrected: ctx_op_id=%s pool_op_id=%s — ABSOLUTE PRIMACY",
            str(getattr(op_context, "op_id", "") or ""), op_id,
        )
        return op_id

    def drain_exhaustion_failures(self) -> List["OperationContext"]:
        """Slice 245 — collect + clear ops that FAILED due to provider
        exhaustion during a dark window (the survivors). Returns their preserved
        OperationContexts for re-ingest on wake. Cleared from ``_ops`` so a
        subsequent wake cannot double-resurrect the same op. NEVER raises."""
        survivors: List["OperationContext"] = []
        try:
            dead_ids = []
            for op_id, op in list(self._ops.items()):
                if op.status != "failed":
                    continue
                err = str(op.error or "")
                if not any(m in err for m in _RESURRECTABLE_ERROR_MARKERS):
                    continue
                ctx = getattr(op, "context", None)
                if ctx is not None:
                    survivors.append(ctx)
                    dead_ids.append(op_id)
            for op_id in dead_ids:
                self._ops.pop(op_id, None)
        except Exception:  # noqa: BLE001 — wake path, never raise
            logger.exception("[BGPool] drain_exhaustion_failures failed")
        return survivors

    def is_resumed_dispatch(self, ctx_op_id: str) -> bool:
        """Return True iff a resumed dispatch is pending or in-flight
        for this ctx.op_id.

        The GENERATE-park wrapper queries this to know whether to
        take the resume path (materialize from store) or the
        park-emit / legacy path.
        """
        return ctx_op_id in self._resumed_ops

    def get_park_attempt_seq(self, ctx_op_id: str) -> int:
        """Return the park attempt sequence for a resumed dispatch.

        Returns 0 if the op is not currently in a resume dispatch.
        Callers should treat 0 as "not resumed."
        """
        return self._resumed_ops.get(ctx_op_id, 0)

    def queue_depth(self) -> int:
        """Number of ops waiting for a worker slot (queue pressure).

        The GENERATE-park wrapper uses this to decide whether parking
        the current op would benefit throughput — if no one is waiting,
        parking just adds bookkeeping with no slot-utilization gain.
        """
        return self._queue.qsize()

    def _clear_resume_mark(self, ctx_op_id: str) -> None:
        """Drop the resume mark.  Called by the worker loop's finally
        block AFTER a resumed dispatch reaches terminal.

        Idempotent — clearing a never-marked op is a no-op.
        """
        self._resumed_ops.pop(ctx_op_id, None)

    def register_park_continuation(self, task: asyncio.Task) -> None:
        """Track an out-of-pool continuation task.

        Called by the GENERATE-park wrapper after spawning the
        continuation.  The task is GC'd via add_done_callback when it
        completes; pool.stop() cancels all remaining tasks.
        """
        self._park_continuation_tasks.add(task)
        task.add_done_callback(self._park_continuation_tasks.discard)

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
            "parked_count": self._parked_count,
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

                # Move 2 v5 — Unified Observability: register this BG op
                # into the central active-op tracker so the harness
                # ActivityMonitor sees it during staleness checks. The
                # underlying op_id used by the orchestrator is the
                # context's op_id (not the pool-internal slot id).
                _ctx_op_id = str(getattr(op.context, "op_id", "") or "")
                _registered_ctx_op_id: Optional[str] = None
                if (
                    self._on_op_active_register is not None
                    and _ctx_op_id
                ):
                    try:
                        self._on_op_active_register(_ctx_op_id)
                        _registered_ctx_op_id = _ctx_op_id
                    except Exception as _exc:  # noqa: BLE001
                        logger.warning(
                            "Worker %d: on_op_active_register failed for "
                            "%s: %s",
                            worker_id, _ctx_op_id, _exc,
                        )

                # Suspend gap 6b -- mirror the op into the typed in-flight
                # registry (master-gated, silent no-op when off) so a graceful
                # shutdown's capture_inflight() can checkpoint POOL ops too.
                # Registration previously existed only on the direct
                # GovernedLoopService.submit() path; a SIGTERM mid-soak saw an
                # EMPTY registry and wrote 0 checkpoints despite 3 in-flight
                # pool ops (bt-iso-1782942507). Symmetric unregister in the
                # finally below.
                _inflight_registered = False
                try:
                    from backend.core.ouroboros.governance.in_flight_registry import (  # noqa: PLC0415,E501
                        register_op_safely as _reg_op_safely,
                    )
                    _inflight_registered = _reg_op_safely(
                        _ctx_op_id or op.op_id,
                        ctx_ref=op.context,
                        last_phase_name=getattr(
                            getattr(op.context, "phase", None), "name", "",
                        ),
                        metadata={
                            "pool_worker": worker_id,
                            "pool_op_id": str(op.op_id),
                        },
                    )
                except Exception:  # noqa: BLE001 -- registry is observability, never blocks pickup
                    _inflight_registered = False

                try:
                    # Phase 1 Step 3C: § 4 bind contract dispatch. Read
                    # the live orchestrator from the process-wide bind
                    # (set at ``GovernedLoopService._attach_to_stack``
                    # time via ``stack.bind_orchestrator``) so an
                    # ``importlib.reload(orchestrator)`` that swapped
                    # the class out from under this long-lived worker
                    # flips to the new instance on its very next
                    # dispatch. Fallback chain: live bind → captured
                    # constructor ref (for tests and pre-3C
                    # deployments that never engaged the bind).
                    from backend.core.ouroboros.governance._governance_state import (
                        get_bound_orchestrator as _get_bound_orch,
                    )
                    _orch = _get_bound_orch() or self._orchestrator
                    # Per-op watchdog: last-resort pool-level ceiling so
                    # a wedged worker (subprocess hang, blocking I/O,
                    # deadlock that escapes orchestrator phase timers)
                    # doesn't monopolize a slot indefinitely. First
                    # surfaced by bt-2026-04-13-031119 (two workers
                    # wedged 424s/451s on workspace_checkpoint hang).
                    #
                    # Invariant this ceiling MUST respect:
                    #
                    #   worker_op_timeout
                    #       >= max(route_generation_budget)
                    #        +  tool_loop_overhead
                    #        +  candidate_assembly
                    #        +  verify_phase
                    #        +  slack
                    #
                    # Concretely, with current route budgets:
                    #   BACKGROUND DW:  180s generation + 15s tool-loop
                    #                   overhead + 30s assembly + 60s
                    #                   verify + 75s slack ~= 360s.
                    # A 240s ceiling force-reaps every BACKGROUND op
                    # before generation returns (witnessed in session
                    # bt-2026-04-14-005028: 3 simple/BACKGROUND ops
                    # killed at 240s, cost=$0, tool_execution_records=0,
                    # shadow-log gate unreached). Raising the default
                    # to 360s keeps the watchdog's anti-hang purpose
                    # while letting the slowest legitimate path — DW
                    # Venom on BACKGROUND sensor traffic — complete.
                    # Env-tunable so battle tests can go higher without
                    # a code change.
                    _op_timeout_base_s = float(
                        os.environ.get("JARVIS_BG_WORKER_OP_TIMEOUT_S", "360")
                    )
                    # Route-aware ceiling: COMPLEX ops (≥4 target files in
                    # the intake envelope) need longer wall-time budget than
                    # the 360s anti-hang watchdog provides, because their
                    # full lifecycle (CLASSIFY + PLAN + attempt 1 GENERATE
                    # + Iron Gate rejection + GENERATE_RETRY with tool-round
                    # exploration + synthesis) can legitimately consume
                    # 400–600s. Session `bt-2026-04-15-044627` (2026-04-14)
                    # proved this empirically under `BG_POOL_SIZE=1`
                    # isolation: a 4-file probe hit the 360s ceiling mid
                    # retry-2 synthesis with 131s of nominal generation
                    # budget still remaining, with zero semaphore contention.
                    # The cancel propagated from this pool up through the
                    # fallback `asyncio.wait_for` and surfaced as
                    # `fallback_err_class=CancelledError`, which earlier
                    # diagnosis had mistaken for semaphore starvation.
                    #
                    # We use target_files count (not task_complexity) because
                    # CLASSIFY/ROUTE haven't run yet at worker pickup — but
                    # the complexity classifier is deterministic on file
                    # count (`complexity_classifier.py:161-165`): ≥4 files
                    # → `complex`, which in turn routes to COMPLEX provider
                    # path per `urgency_router.py:256-263`. File-count-based
                    # prediction is therefore equivalent to complexity-
                    # based without the phase-ordering hazard.
                    try:
                        _target_file_count = len(
                            getattr(op.context, "target_files", None) or ()
                        )
                    except Exception:
                        _target_file_count = 0
                    # Read-only subagent fan-out override (Session 7,
                    # Derek 2026-04-17). Read-only cartography ops
                    # dispatch MAX_PARALLEL_SCOPES subagents each
                    # consuming PRIMARY_PROVIDER_TIMEOUT_S wall-clock,
                    # then the parent Claude synthesizes findings. This
                    # burns well past the 360s base ceiling even with
                    # only 3 target files. Without this branch the
                    # pool worker cancels the op mid-synthesis and
                    # surfaces as `fallback_err_class=CancelledError`
                    # with 342s of nominal budget still remaining
                    # (Session 7 bt-2026-04-18-043443). Precedence is
                    # read-only > complex > base so file-count-based
                    # complex heuristic doesn't mask a read-only
                    # fan-out profile.
                    _is_read_only = bool(
                        getattr(op.context, "is_read_only", False)
                    )
                    _signal_source = (
                        getattr(op.context, "signal_source", "") or ""
                    )

                    # Source-aware + shape-aware timebox table — operator
                    # binding 2026-05-13 ("per-route / per-source timeout
                    # table; default conservative for sensors, explicitly
                    # higher for swe_bench_pro").  Same discipline as the
                    # CLAUDE.md route-timeout table — env-tunable, single
                    # source of truth per category, MAX-aggregated so
                    # multiple applicable categories compose (e.g.
                    # read-only swe_bench_pro takes whichever is longer)
                    # rather than letting the precedence chain accidentally
                    # mask a longer legitimate budget.
                    #
                    # Stage-1 wiring soak v12 (session
                    # bt-2026-05-13-201526) caught the gap: SWE-Bench-Pro
                    # envelopes with 1 target file + is_read_only=False
                    # inherited the 360s sensor base, which is too tight
                    # for the full CLASSIFY → ROUTE → CTX → PLAN →
                    # GENERATE-with-LLM → VALIDATE → APPLY → VERIFY
                    # pipeline.  Source-aware budget lets benchmark eval
                    # work get a longer lease without raising the global
                    # default and weakening the anti-hang watchdog for
                    # sensor traffic.
                    _candidates: "List[Tuple[float, str]]" = [
                        (_op_timeout_base_s, "base"),
                    ]
                    if _is_read_only:
                        _candidates.append((
                            float(os.environ.get(
                                "JARVIS_BG_WORKER_OP_TIMEOUT_READONLY_S",
                                "900",
                            )),
                            "read_only",
                        ))
                    if _target_file_count >= 4:
                        _candidates.append((
                            float(os.environ.get(
                                "JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S",
                                "900",
                            )),
                            "complex",
                        ))
                    if _signal_source == "swe_bench_pro":
                        _candidates.append((
                            float(os.environ.get(
                                "JARVIS_BG_WORKER_OP_TIMEOUT_SWE_BENCH_PRO_S",
                                "900",
                            )),
                            "swe_bench_pro",
                        ))
                    # Max-aggregation: the longest applicable ceiling wins.
                    # Tie-break by category-name lexicographic order so the
                    # log line is deterministic across runs.
                    _op_timeout_s, _ceiling_reason = max(
                        _candidates, key=lambda p: (p[0], p[1]),
                    )
                    # Slice 195 — Adaptive Horizon Governor. Derive the ceiling
                    # from the op's STATIC shape (context size, continuous
                    # file-count vector, model catalog profile) instead of the
                    # magic-number table. Raise-only above the legacy floor +
                    # hard-clamped (JARVIS_HORIZON_MAX_S) — computed ONCE here
                    # at pickup, never extended mid-run (Slice 47 watchdog
                    # doctrine: no ledger/liveness coupling). OFF → the legacy
                    # pair above passes through byte-identical.
                    try:
                        from backend.core.ouroboros.governance.adaptive_horizon import (
                            compute_horizon as _s195_compute_horizon,
                        )
                        _s195_model_id = None
                        try:
                            from backend.core.ouroboros.governance.provider_topology import (
                                get_topology as _s195_get_topology,
                            )
                            _s195_route = str(
                                getattr(
                                    getattr(op.context, "routing", None),
                                    "provider_route", "",
                                ) or "background"
                            )
                            _s195_model_id = _s195_get_topology().model_for_route(
                                _s195_route,
                            )
                        except Exception:  # noqa: BLE001
                            _s195_model_id = None
                        _op_timeout_s, _ceiling_reason = _s195_compute_horizon(
                            legacy_floor_s=_op_timeout_s,
                            legacy_reason=_ceiling_reason,
                            context_chars=len(
                                getattr(op.context, "description", "") or ""
                            ),
                            target_file_count=_target_file_count,
                            model_id=_s195_model_id,
                        )
                    except Exception:  # noqa: BLE001 — governor is enhancement,
                        pass            # never blocks pickup; legacy pair stands
                    if _ceiling_reason != "base":
                        logger.info(
                            "Worker %d: %s ceiling=%.0fs reason=%s "
                            "(source=%r, file_count=%d, read_only=%s, "
                            "base=%.0fs)",
                            worker_id, op.op_id, _op_timeout_s,
                            _ceiling_reason, _signal_source,
                            _target_file_count, _is_read_only,
                            _op_timeout_base_s,
                        )
                    # Dynamic FSM-Aware Timeboxing: the ceiling is enforced in
                    # slices around a shielded task. At each expiry the LIVE
                    # failover FSM is consulted (_fsm_timebox_extension_s) --
                    # engaged lifecycle (zone hunt / heavy streaming) grants a
                    # bounded extension; DORMANT kills exactly like the legacy
                    # single wait_for (cancel + TimeoutError to the handler).
                    _run_task = asyncio.ensure_future(_orch.run(op.context))
                    _fsm_granted_s = 0.0
                    _next_timeout_s = _op_timeout_s
                    try:
                        while True:
                            try:
                                result = await asyncio.wait_for(
                                    asyncio.shield(_run_task),
                                    timeout=_next_timeout_s,
                                )
                                break
                            except asyncio.TimeoutError:
                                _ext_s = _fsm_timebox_extension_s(_fsm_granted_s)
                                if _ext_s <= 0.0:
                                    _run_task.cancel()
                                    try:
                                        await _run_task
                                    except BaseException:  # noqa: BLE001 -- drain the cancel
                                        pass
                                    raise
                                _fsm_granted_s += _ext_s
                                _next_timeout_s = _ext_s
                                logger.info(
                                    "Worker %d: %s bg_timebox reached but failover "
                                    "FSM engaged (cold-start/heavy path) -- extending "
                                    "%.0fs (granted %.0fs total)",
                                    worker_id, op.op_id, _ext_s, _fsm_granted_s,
                                )
                    finally:
                        if not _run_task.done():
                            _run_task.cancel()
                    op.result = result
                    op.status = "completed"
                    self._completed_count += 1
                    logger.info(
                        "Worker %d completed operation %s in %.2fs",
                        worker_id,
                        op.op_id,
                        op.elapsed_s or 0.0,
                    )
                except asyncio.TimeoutError:
                    # Structured reason_code per operator binding 2026-05-13:
                    # "orchestrator cooperative checkpoints so kills are
                    # observable (reason_code=bg_timebox) not silent
                    # starvation".  The structured payload carries the
                    # applied category + source so downstream observability
                    # (op-lifecycle SSE, audit ledger, postmortem) can
                    # distinguish ceiling-bound kills from upstream
                    # cancellations.
                    op.error = (
                        f"bg_timebox:{_ceiling_reason}:"
                        f"source={_signal_source or '-'}:"
                        f"timeout={_op_timeout_s:.0f}s"
                    )
                    op.status = "failed"
                    self._failed_count += 1
                    logger.warning(
                        "Worker %d: operation %s exceeded pool ceiling "
                        "reason=bg_timebox category=%s source=%r "
                        "timeout=%.0fs — freeing slot",
                        worker_id, op.op_id, _ceiling_reason,
                        _signal_source, _op_timeout_s,
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
                except _ParkRequested_t() as park_exc:
                    # Stage 1.6 — the op released its slot for the duration
                    # of provider I/O.  The PARKED_GENERATE ledger entry was
                    # written by the GENERATE wrapper BEFORE raising
                    # (canonical authority: orchestrator owns the ledger,
                    # worker owns the slot).  The out-of-pool continuation
                    # is already scheduled — on completion it re-submits
                    # ctx with resumed=True.  Here we just observe + free.
                    #
                    # ``op.result`` carries the ParkSignal so observers
                    # tracing through the BackgroundOp can find the token.
                    # ``op.status="parked"`` is non-terminal (see
                    # BackgroundOp.is_terminal — "parked" is deliberately
                    # NOT in the terminal tuple).
                    op.result = park_exc.signal
                    op.status = "parked"
                    self._parked_count += 1
                    logger.info(
                        "Worker %d: operation %s parked at GENERATE "
                        "token=%s attempt=%d kind=%s — freeing slot "
                        "(slot held for %.2fs)",
                        worker_id, op.op_id, park_exc.signal.token,
                        park_exc.signal.attempt_seq,
                        park_exc.signal.descriptor.kind,
                        op.elapsed_s or 0.0,
                    )
                except OperationPreemptedError:
                    # Slice 246 — graceful human-override preemption (NOT a hard
                    # kill, NOT terminal). The op yielded at a round boundary; its
                    # OperationContext (completed phases) is intact. Re-ingest it
                    # via Slice 245's resurrection path (micro-hibernation) so it
                    # re-enters the VIP lane BELOW the human and resumes from its
                    # last durable phase. "preempted" is non-terminal.
                    _ctx_op_id = str(getattr(op.context, "op_id", "") or "")
                    _preemption.clear_preemption(_ctx_op_id)
                    op.status = "preempted"
                    try:
                        await self.resubmit_resurrected(op.context)
                        logger.info(
                            "Worker %d: operation %s PREEMPTED by human override "
                            "— re-ingested with VIP primacy (micro-hibernation)",
                            worker_id, op.op_id,
                        )
                    except Exception:  # noqa: BLE001 — never lose the payload
                        logger.exception(
                            "Worker %d: preempt re-ingest failed for %s",
                            worker_id, op.op_id,
                        )
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
                    # Stage 1.6 — clear the resume mark IF this was a
                    # resumed dispatch that reached a real terminal
                    # state (completed/failed/cancelled). If status is
                    # "parked" we are NOT terminal — the out-of-pool
                    # continuation will re-submit and that resumed
                    # dispatch's finally will clear the mark.
                    if op.resumed and op.status in ("completed", "failed", "cancelled"):
                        self._clear_resume_mark(_ctx_op_id)
                    # Move 2 v5 — Unified Observability cleanup. Always
                    # unregister, even on cancellation / failure / rupture
                    # so no dangling state accumulates in the central
                    # tracker. Symmetric with the register call above:
                    # we only unregister an op_id we successfully
                    # registered. Best-effort: a failing unregister
                    # cannot leak the worker.
                    if (
                        _registered_ctx_op_id is not None
                        and self._on_op_active_unregister is not None
                    ):
                        try:
                            self._on_op_active_unregister(
                                _registered_ctx_op_id,
                            )
                        except Exception as _exc:  # noqa: BLE001
                            logger.warning(
                                "Worker %d: on_op_active_unregister "
                                "failed for %s: %s",
                                worker_id, _registered_ctx_op_id, _exc,
                            )
                    # Suspend gap 6b cleanup -- symmetric with the in-flight
                    # registry registration above. Unconditional on terminal
                    # AND parked/preempted exits (a resumed dispatch
                    # re-registers on its next worker pickup).
                    if _inflight_registered:
                        try:
                            from backend.core.ouroboros.governance.in_flight_registry import (  # noqa: PLC0415,E501
                                unregister_op_safely as _unreg_op_safely,
                            )
                            _unreg_op_safely(_ctx_op_id or op.op_id)
                        except Exception:  # noqa: BLE001 -- never leak the worker
                            pass

        except asyncio.CancelledError:
            logger.debug("Worker %d shutting down (cancelled)", worker_id)
        except Exception:
            logger.exception("Worker %d crashed unexpectedly", worker_id)
        finally:
            logger.debug("Worker %d exited", worker_id)


def _fsm_timebox_extension_s(already_granted_s: float) -> float:
    """Dynamic FSM-Aware Timeboxing: at bg_timebox expiry, consult the LIVE
    failover FSM before killing the op.

    When the lifecycle is engaged (AWAKENING zone-hunt / SERVING slow heavy
    streaming), the elapsed wall is infrastructure cold-start or heavy-tier
    inference -- not a wedged op -- so grant a bounded extension slice (env
    ``JARVIS_BG_WORKER_FSM_EXTENSION_SLICE_S``, default 120) up to a total
    budget (``JARVIS_BG_WORKER_FSM_EXTENSION_MAX_S``, default 900). DORMANT
    (normal DW ops) returns 0.0 => byte-identical legacy anti-hang watchdog.
    Live evidence: bt-iso-1782944904 killed a resumed op at its static 415s
    ceiling while the FSM was 4 minutes into an AWAKENING zone hunt.
    Master ``JARVIS_BG_FSM_TIMEBOX_ENABLED`` (default true). NEVER raises."""
    try:
        if (os.environ.get("JARVIS_BG_FSM_TIMEBOX_ENABLED", "true") or "").strip().lower() \
                in ("0", "false", "no", "off"):
            return 0.0
        max_total = float(os.environ.get("JARVIS_BG_WORKER_FSM_EXTENSION_MAX_S", "900") or 900)
        if already_granted_s >= max_total:
            return 0.0
        from backend.core.ouroboros.governance import failover_lifecycle as _fl  # noqa: PLC0415
        if not _fl.lifecycle_enabled():
            return 0.0
        if _fl.get_failover_controller().state == _fl.FailoverState.DORMANT:
            return 0.0
        slice_s = float(os.environ.get("JARVIS_BG_WORKER_FSM_EXTENSION_SLICE_S", "120") or 120)
        return max(0.0, min(slice_s, max_total - already_granted_s))
    except Exception:  # noqa: BLE001 -- the watchdog must never break on a probe
        return 0.0

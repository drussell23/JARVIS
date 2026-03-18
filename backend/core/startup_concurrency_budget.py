"""StartupConcurrencyBudget — named-slot semaphore for bounding heavy startup tasks.

Disease 10 — Startup Sequencing, Task 3.

Provides a concurrency budget that limits how many heavyweight operations
(model loads, GCP provisioning, reactor launches, etc.) can run simultaneously
during the startup sequence.  Each acquisition yields a frozen ``TaskSlot``
describing the holder; on release, a ``CompletedTask`` record is appended to
an observable history list.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum: categories of heavyweight startup work
# ---------------------------------------------------------------------------


@enum.unique
class HeavyTaskCategory(enum.Enum):
    """Categories of heavy startup tasks, each carrying a weight.

    Values encode ``(ordinal, weight)`` to ensure uniqueness while
    keeping the ``weight`` property stable across all members.
    """

    MODEL_LOAD = (1, 1)
    GCP_PROVISION = (2, 1)
    REACTOR_LAUNCH = (3, 1)
    ML_INIT = (4, 1)
    SUBPROCESS_SPAWN = (5, 1)
    ML_WEIGHT_LOAD = (6, 1)
    """Separate gate for loading raw model-weight files (gguf, safetensors, etc.).

    Unlike ML_INIT (which covers Python-level model bootstrapping), this
    category covers the actual weight-tensor reads which are memory-bandwidth-
    bound and can spike RAM by 2–4 GiB per model.  Keeping this at weight=1
    with a separate semaphore (max 1 concurrent) prevents two models from
    loading their weights simultaneously — the primary cause of OOM kills on
    16 GiB hardware.
    """

    @property
    def weight(self) -> int:
        """Return the concurrency weight for this category."""
        return self.value[1]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSlot:
    """Represents an active slot held by a heavy task."""

    category: HeavyTaskCategory
    name: str
    acquired_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CompletedTask:
    """Record of a completed heavy task for observability."""

    category: HeavyTaskCategory
    name: str
    duration_s: float
    started_at: float
    ended_at: float


# ---------------------------------------------------------------------------
# StartupConcurrencyBudget
# ---------------------------------------------------------------------------


class StartupConcurrencyBudget:
    """Named-slot semaphore that bounds concurrent heavy startup tasks.

    Parameters
    ----------
    max_concurrent:
        Maximum number of heavy tasks that may execute simultaneously.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._max_concurrent = max_concurrent
        self._active: List[TaskSlot] = []
        self._history: List[CompletedTask] = []
        self._peak_concurrent: int = 0
        # Lazily initialised on first use so the Budget can be constructed
        # outside an async context (asyncio.Semaphore / Lock bind to the
        # running loop and raise on Python 3.9 if none is active yet).
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_primitives(self) -> None:
        """Create asyncio primitives on first use (bound to current loop)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent)
        if self._lock is None:
            self._lock = asyncio.Lock()

    # -- Properties ----------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Number of currently active (held) slots."""
        return len(self._active)

    @property
    def peak_concurrent(self) -> int:
        """Highest number of simultaneously active slots observed."""
        return self._peak_concurrent

    @property
    def history(self) -> List[CompletedTask]:
        """Copy of completed-task history (safe to mutate)."""
        return list(self._history)

    # -- Core API ------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        category: HeavyTaskCategory,
        name: str,
        timeout: float | None = None,
    ) -> AsyncIterator[TaskSlot]:
        """Acquire a concurrency slot, blocking until one is available.

        Parameters
        ----------
        category:
            The kind of heavy work being performed.
        name:
            A human-readable label for this task.
        timeout:
            Optional maximum seconds to wait for a slot.  Raises
            ``TimeoutError`` if the slot cannot be acquired in time.

        Yields
        ------
        TaskSlot
            Frozen descriptor of the acquired slot.
        """
        self._ensure_primitives()
        assert self._semaphore is not None  # for type-checker
        assert self._lock is not None

        # Acquire the underlying semaphore, with optional timeout.
        if timeout is not None:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for concurrency slot "
                    f"[{category.name}:{name}]"
                )
        else:
            await self._semaphore.acquire()

        slot = TaskSlot(category=category, name=name)

        async with self._lock:
            self._active.append(slot)
            current = len(self._active)
            if current > self._peak_concurrent:
                self._peak_concurrent = current

        logger.info(
            "Acquired concurrency slot [%s:%s] — active=%d/%d",
            category.name,
            name,
            current,
            self._max_concurrent,
        )

        try:
            yield slot
        finally:
            ended_at = time.monotonic()
            async with self._lock:
                self._active.remove(slot)
                remaining = len(self._active)

            completed = CompletedTask(
                category=slot.category,
                name=slot.name,
                duration_s=ended_at - slot.acquired_at,
                started_at=slot.acquired_at,
                ended_at=ended_at,
            )
            self._history.append(completed)
            self._semaphore.release()

            logger.info(
                "Released concurrency slot [%s:%s] after %.3fs — active=%d/%d",
                category.name,
                name,
                completed.duration_s,
                remaining,
                self._max_concurrent,
            )

    @asynccontextmanager
    async def io_phase(self, slot: TaskSlot) -> AsyncIterator[None]:
        """Temporarily release the semaphore slot for I/O-bound GCP API calls.

        The slot remains in ``_active`` (the work is still logically "owned").
        Only the semaphore count is relaxed so that other CPU-bound components
        waiting for a slot can proceed during the I/O wait.

        Prevents ``BudgetStarvationError`` cascades when slow GCP API calls
        (10–30 s) hold the hard-category semaphore while downstream components
        queue behind it (Nuance 6).

        Usage::

            async with budget.acquire(HeavyTaskCategory.GCP_PROVISION, "vm") as slot:
                await cpu_bound_init()
                async with budget.io_phase(slot):
                    result = await gcp_client.wait_for_vm_ready()  # I/O, not CPU
                await process(result)
        """
        self._ensure_primitives()
        assert self._semaphore is not None
        if slot not in self._active:
            raise RuntimeError(
                f"io_phase() called with a slot not currently active: {slot}"
            )
        self._semaphore.release()
        logger.debug(
            "io_phase: released slot [%s:%s] for I/O",
            slot.category.name, slot.name,
        )
        try:
            yield
        finally:
            await self._semaphore.acquire()
            logger.debug(
                "io_phase: reacquired slot [%s:%s] after I/O",
                slot.category.name, slot.name,
            )

    def reset(self) -> None:
        """Reset the budget to a clean state for DMS restart cycles.

        Recreates the asyncio semaphore (binding it to the current event loop)
        and clears the active-slot list.  Call this at the top of each DMS
        restart cycle to recover from leaked semaphore slots that may have been
        caused by ``CancelledError`` bypassing ``async with`` cleanup on Python
        ≤ 3.11 (Nuance 8).

        Must only be called from an async context (event loop must be running).
        """
        self._ensure_primitives()
        leaked = len(self._active)
        if leaked:
            logger.warning(
                "StartupConcurrencyBudget.reset(): discarding %d leaked active slot(s): %s",
                leaked,
                [s.name for s in self._active],
            )
        self._active.clear()
        # Recreate primitives bound to the current (possibly new) event loop.
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._lock = asyncio.Lock()
        logger.info(
            "StartupConcurrencyBudget.reset(): semaphore recreated (max=%d)",
            self._max_concurrent,
        )

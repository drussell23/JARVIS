"""StartupBudgetPolicy — tiered concurrency budget enforcement.

Disease 10 — Startup Sequencing, Task 4.

Wraps :class:`StartupConcurrencyBudget` with **tiered** enforcement:

* **Hard semaphore** (default max 1) — serialises RAM-killer categories
  (MODEL_LOAD, REACTOR_LAUNCH, SUBPROCESS_SPAWN).
* **Total semaphore** (default max 3) — global cap across all categories.
* **Soft gate preconditions** — category-level phase requirements that must
  be signalled before acquisition is allowed.
* **Starvation protection** — configurable timeout on all semaphore waits.

All asyncio primitives are lazily initialised (same pattern as
:class:`StartupConcurrencyBudget`) so the policy can be constructed outside
an active event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional, Set

from backend.core.startup_concurrency_budget import (
    CompletedTask,
    HeavyTaskCategory,
    TaskSlot,
)
from backend.core.startup_config import BudgetConfig, SoftGatePrecondition

__all__ = [
    "StartupBudgetPolicy",
    "BudgetAcquisitionError",
    "PreconditionNotMetError",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BudgetAcquisitionError(Exception):
    """Raised when a budget slot cannot be acquired within the timeout."""


class PreconditionNotMetError(Exception):
    """Raised when a soft-gate precondition has not been satisfied."""


# ---------------------------------------------------------------------------
# StartupBudgetPolicy
# ---------------------------------------------------------------------------


class StartupBudgetPolicy:
    """Tiered concurrency budget with hard/soft gates and starvation protection.

    Parameters
    ----------
    config:
        Budget configuration specifying concurrency limits, hard/soft
        category lists, preconditions, and timeout.
    """

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self._reached_phases: Set[str] = set()
        self._active_slots: List[TaskSlot] = []
        self._history: List[CompletedTask] = []
        self._peak_concurrent: int = 0

        # Lazily initialised — must not create asyncio primitives outside
        # a running event loop (Python 3.9 raises).
        self._hard_sem: Optional[asyncio.Semaphore] = None
        self._total_sem: Optional[asyncio.Semaphore] = None
        self._lock: Optional[asyncio.Lock] = None

    # -- Lazy init -----------------------------------------------------------

    def _ensure_primitives(self) -> None:
        """Create asyncio primitives on first use (bound to current loop)."""
        if self._hard_sem is None:
            self._hard_sem = asyncio.Semaphore(self._config.max_hard_concurrent)
        if self._total_sem is None:
            self._total_sem = asyncio.Semaphore(self._config.max_total_concurrent)
        if self._lock is None:
            self._lock = asyncio.Lock()

    # -- Category classification ---------------------------------------------

    def _is_hard_category(self, category: HeavyTaskCategory) -> bool:
        """Return True if *category* belongs to the hard-gate set."""
        return category.name in self._config.hard_gate_categories

    # -- Precondition checking -----------------------------------------------

    def _check_preconditions(self, category: HeavyTaskCategory) -> None:
        """Raise :class:`PreconditionNotMetError` if preconditions unmet.

        Only categories listed in ``config.soft_gate_preconditions`` are
        checked; categories without preconditions pass unconditionally.
        """
        preconditions = self._config.soft_gate_preconditions
        if category.name not in preconditions:
            return

        precondition: SoftGatePrecondition = preconditions[category.name]
        required_phase = precondition.require_phase

        if required_phase not in self._reached_phases:
            raise PreconditionNotMetError(
                f"Category {category.name} requires phase "
                f"{required_phase!r} which has not been reached yet"
            )

    # -- Phase signalling ----------------------------------------------------

    def signal_phase_reached(self, phase: str) -> None:
        """Record that a startup phase has been reached.

        This unblocks any soft-gate categories whose precondition
        requires *phase*.
        """
        self._reached_phases.add(phase)
        logger.info("Phase %r reached — %d phases now signalled", phase, len(self._reached_phases))

    # -- Core API ------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        category: HeavyTaskCategory,
        name: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[TaskSlot]:
        """Acquire a tiered concurrency slot.

        Parameters
        ----------
        category:
            The kind of heavy work being performed.
        name:
            A human-readable label for this task.
        timeout:
            Optional per-call timeout override; falls back to
            ``config.max_wait_s``.

        Yields
        ------
        TaskSlot
            Frozen descriptor of the acquired slot.

        Raises
        ------
        PreconditionNotMetError
            If the category has an unsatisfied phase precondition.
        BudgetAcquisitionError
            If the slot cannot be acquired within the timeout.
        """
        self._ensure_primitives()
        assert self._total_sem is not None  # for type-checker
        assert self._hard_sem is not None
        assert self._lock is not None

        # Check soft-gate preconditions before attempting semaphore waits.
        self._check_preconditions(category)

        effective_timeout = timeout if timeout is not None else self._config.max_wait_s
        is_hard = self._is_hard_category(category)

        # --- Acquire total semaphore ----------------------------------------
        try:
            await asyncio.wait_for(
                self._total_sem.acquire(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise BudgetAcquisitionError(
                f"Timed out after {effective_timeout}s waiting for total "
                f"concurrency slot [{category.name}:{name}]"
            )

        total_acquired = True
        hard_acquired = False

        try:
            # --- Acquire hard semaphore (if applicable) ---------------------
            if is_hard:
                try:
                    await asyncio.wait_for(
                        self._hard_sem.acquire(),
                        timeout=effective_timeout,
                    )
                    hard_acquired = True
                except asyncio.TimeoutError:
                    raise BudgetAcquisitionError(
                        f"Timed out after {effective_timeout}s waiting for hard "
                        f"concurrency slot [{category.name}:{name}]"
                    )

            # --- Track the slot ---------------------------------------------
            slot = TaskSlot(category=category, name=name)

            async with self._lock:
                self._active_slots.append(slot)
                current = len(self._active_slots)
                if current > self._peak_concurrent:
                    self._peak_concurrent = current

            logger.info(
                "Acquired budget slot [%s:%s] hard=%s — active=%d",
                category.name,
                name,
                is_hard,
                current,
            )

            try:
                yield slot
            finally:
                # --- Release: reverse order (hard first, then total) --------
                ended_at = time.monotonic()

                async with self._lock:
                    self._active_slots.remove(slot)
                    remaining = len(self._active_slots)

                completed = CompletedTask(
                    category=slot.category,
                    name=slot.name,
                    duration_s=ended_at - slot.acquired_at,
                    started_at=slot.acquired_at,
                    ended_at=ended_at,
                )
                self._history.append(completed)

                if hard_acquired:
                    self._hard_sem.release()
                    hard_acquired = False

                self._total_sem.release()
                total_acquired = False

                logger.info(
                    "Released budget slot [%s:%s] after %.3fs — active=%d",
                    category.name,
                    name,
                    completed.duration_s,
                    remaining,
                )

        except BaseException:
            # If we failed between acquiring semaphores and entering the
            # yield block, we must still release whatever we acquired.
            if hard_acquired:
                self._hard_sem.release()
            if total_acquired:
                self._total_sem.release()
            raise

    # -- Properties ----------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Number of currently active (held) slots."""
        return len(self._active_slots)

    @property
    def peak_concurrent(self) -> int:
        """Highest number of simultaneously active slots observed."""
        return self._peak_concurrent

    @property
    def history(self) -> List[CompletedTask]:
        """Copy of completed-task history (safe to mutate)."""
        return list(self._history)

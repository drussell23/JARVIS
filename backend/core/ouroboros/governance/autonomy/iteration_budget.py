"""Iteration Budget Guard — ledger-backed spend and iteration tracking.

Tracks accumulated API spend and iteration counts within a rolling budget
window.  Decisions are policy-driven (no hardcoded magic numbers) and every
spend event is durably persisted to the :class:`OperationLedger` as a
``BUDGET_CHECKPOINT`` entry so that the window can be faithfully reconstructed
after a restart.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Tuple

from backend.core.ouroboros.governance.autonomy.iteration_types import (
    IterationBudgetWindow,
    IterationStopPolicy,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationState,
)

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.ledger import OperationLedger


# Shared op_id used for all budget-checkpoint ledger entries (allows scanning
# a single JSONL file during load_from_ledger).
_BUDGET_OP_ID = "op-budget"


class IterationBudgetGuard:
    """Guards iteration entry based on budget and iteration-count limits.

    Parameters
    ----------
    ledger:
        The :class:`OperationLedger` used for durable checkpoint writes and
        reconstruction.  Any object with a compatible ``append`` coroutine and
        an ``all_entries`` method returning a list of :class:`LedgerEntry` is
        accepted (duck-typing), which makes test fakes trivial to write.
    stop_policy:
        The :class:`IterationStopPolicy` that supplies all numeric limits.
        No magic numbers are embedded here.
    """

    def __init__(
        self,
        ledger: "OperationLedger",
        stop_policy: IterationStopPolicy,
    ) -> None:
        self._ledger = ledger
        self._policy = stop_policy
        self._window = IterationBudgetWindow(
            window_start_utc=datetime.now(timezone.utc),
        )
        # Monotonic clock anchored at construction; used for wall-time checks.
        self._session_start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_proceed(self) -> Tuple[bool, str]:
        """Return ``(True, "")`` if iteration may continue, else ``(False, reason)``.

        Checks are evaluated in order:
        1. Reset the budget window if it has expired (new day / new window).
        2. Wall-time session limit.
        3. Accumulated API spend against ``max_spend_usd``.
        4. Iteration count against ``max_iterations_per_session``.

        All numeric thresholds come from :attr:`_policy`; nothing is
        hardcoded.
        """
        # 1. Auto-reset expired window so a new day starts fresh.
        self._window.reset_if_expired()

        # 2. Wall-time check.
        elapsed = time.monotonic() - self._session_start_time
        if elapsed >= self._policy.max_wall_time_s:
            return (
                False,
                f"wall-time limit reached: {elapsed:.1f}s >= "
                f"{self._policy.max_wall_time_s}s",
            )

        # 3. Budget spend check.
        if self._window.spend_usd >= self._policy.max_spend_usd:
            return (
                False,
                f"budget exhausted: ${self._window.spend_usd:.4f} >= "
                f"${self._policy.max_spend_usd:.4f}",
            )

        # 4. Iteration count check.
        if self._window.iterations_count >= self._policy.max_iterations_per_session:
            return (
                False,
                f"iteration limit reached: {self._window.iterations_count} >= "
                f"{self._policy.max_iterations_per_session}",
            )

        return (True, "")

    async def record_spend(self, iteration_id: str, cost_usd: float) -> None:
        """Record a completed iteration spend, incrementing window counters.

        Persists a ``BUDGET_CHECKPOINT`` entry to the ledger **before**
        updating in-memory state so the ledger is the source of truth.

        Parameters
        ----------
        iteration_id:
            Opaque identifier for the iteration that incurred the cost.
        cost_usd:
            Estimated API cost in US dollars for this iteration.
        """
        entry = LedgerEntry(
            op_id=_BUDGET_OP_ID,
            state=OperationState.BUDGET_CHECKPOINT,
            data={
                "iteration_id": iteration_id,
                "cost_usd": cost_usd,
                "window_spend_before": self._window.spend_usd,
                "window_iterations_before": self._window.iterations_count,
            },
            entry_id=iteration_id,
        )
        await self._ledger.append(entry)

        # Update in-memory window after successful ledger write.
        self._window.spend_usd += cost_usd
        self._window.iterations_count += 1

    def compute_cooldown(self, consecutive_failures: int) -> float:
        """Return exponential back-off cooldown duration in seconds.

        Formula: ``cooldown_base_s * 2 ** (consecutive_failures - 1)``,
        capped at ``max_cooldown_s``.  Zero failures returns ``0.0``.

        Parameters
        ----------
        consecutive_failures:
            Number of consecutive failed iterations.

        Returns
        -------
        float
            Cooldown duration in seconds, within ``[0.0, max_cooldown_s]``.
        """
        if consecutive_failures <= 0:
            return 0.0
        raw = self._policy.cooldown_base_s * (2 ** (consecutive_failures - 1))
        return min(raw, self._policy.max_cooldown_s)

    async def load_from_ledger(self) -> None:
        """Reconstruct the budget window by scanning today's ledger checkpoints.

        Only ``BUDGET_CHECKPOINT`` entries whose ``wall_time`` falls within the
        current budget window (i.e. within the last ``window_hours`` hours) are
        counted.  This makes the guard resilient to process restarts.

        After calling this method the in-memory window reflects all spend that
        has already been persisted, preventing double-counting after a restart.
        """
        entries = self._ledger.all_entries()

        # Determine the oldest wall_time that is still within the window.
        window_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=self._window.window_hours)
        ).timestamp()

        accumulated_spend = 0.0
        accumulated_count = 0

        for entry in entries:
            if entry.state != OperationState.BUDGET_CHECKPOINT:
                continue
            if entry.wall_time < window_cutoff:
                continue
            cost = entry.data.get("cost_usd", 0.0)
            accumulated_spend += cost
            accumulated_count += 1

        self._window.spend_usd = accumulated_spend
        self._window.iterations_count = accumulated_count

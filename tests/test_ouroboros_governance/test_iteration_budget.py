"""Tests for IterationBudgetGuard — T14, T16.

Covers:
- T14: budget exhaustion returns (False, reason)
- T16: cooldown exponential back-off: 60, 120, 240 seconds
- Iteration count exhaustion
- Cooldown capped at max_cooldown_s
- Window resets on new day (expired window)
- record_spend increments spend and iteration count
- load_from_ledger reconstructs budget window from persisted BUDGET_CHECKPOINT entries
- Empty ledger yields a fresh zero window
- can_proceed True when budget and iterations remain
- Wall-time exhaustion returns (False, reason)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.autonomy.iteration_budget import (
    IterationBudgetGuard,
)
from backend.core.ouroboros.governance.autonomy.iteration_types import (
    IterationBudgetWindow,
    IterationStopPolicy,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationState,
)


# ---------------------------------------------------------------------------
# Fake ledger — list-based, no file I/O
# ---------------------------------------------------------------------------


class FakeLedger:
    """In-memory ledger substitute that records appended entries."""

    def __init__(self, prepopulated: List[LedgerEntry] | None = None) -> None:
        self._entries: List[LedgerEntry] = list(prepopulated or [])
        self.appended: List[LedgerEntry] = []

    async def append(self, entry: LedgerEntry) -> bool:
        self._entries.append(entry)
        self.appended.append(entry)
        return True

    # load_from_ledger calls this to scan for BUDGET_CHECKPOINT entries
    def all_entries(self) -> List[LedgerEntry]:
        return list(self._entries)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _policy(
    max_spend_usd: float = 10.0,
    max_iterations_per_session: int = 25,
    max_wall_time_s: float = 3600.0,
    cooldown_base_s: float = 60.0,
    max_cooldown_s: float = 300.0,
) -> IterationStopPolicy:
    return IterationStopPolicy(
        max_spend_usd=max_spend_usd,
        max_iterations_per_session=max_iterations_per_session,
        max_wall_time_s=max_wall_time_s,
        cooldown_base_s=cooldown_base_s,
        max_cooldown_s=max_cooldown_s,
    )


def _guard(
    policy: IterationStopPolicy | None = None,
    ledger: FakeLedger | None = None,
) -> IterationBudgetGuard:
    return IterationBudgetGuard(
        ledger=ledger or FakeLedger(),
        stop_policy=policy or _policy(),
    )


# ---------------------------------------------------------------------------
# T14 — Budget exhaustion
# ---------------------------------------------------------------------------


class TestBudgetExhaustion:
    """T14: can_proceed() returns (False, reason) when spend ceiling is hit."""

    def test_budget_exhausted_returns_false(self):
        """Spending more than max_spend_usd blocks further iterations."""
        guard = _guard(policy=_policy(max_spend_usd=1.0))
        guard._window.spend_usd = 1.01  # manually exhaust
        ok, reason = guard.can_proceed()
        assert ok is False
        assert reason  # non-empty string explaining why

    def test_budget_not_exhausted_returns_true(self):
        """Within budget → can_proceed returns True."""
        guard = _guard(policy=_policy(max_spend_usd=5.0))
        guard._window.spend_usd = 3.0
        ok, reason = guard.can_proceed()
        assert ok is True
        assert reason == ""

    def test_budget_exactly_at_limit_returns_false(self):
        """Spend exactly equal to limit is also exhausted."""
        guard = _guard(policy=_policy(max_spend_usd=2.0))
        guard._window.spend_usd = 2.0
        ok, reason = guard.can_proceed()
        assert ok is False

    def test_zero_spend_returns_true(self):
        """Fresh guard (zero spend) is always allowed to proceed."""
        guard = _guard(policy=_policy(max_spend_usd=5.0))
        ok, reason = guard.can_proceed()
        assert ok is True


# ---------------------------------------------------------------------------
# Iteration count exhaustion
# ---------------------------------------------------------------------------


class TestIterationCountExhaustion:
    def test_iteration_count_exhausted_returns_false(self):
        """Reaching max_iterations_per_session blocks proceed."""
        guard = _guard(policy=_policy(max_iterations_per_session=10))
        guard._window.iterations_count = 10
        ok, reason = guard.can_proceed()
        assert ok is False
        assert reason

    def test_iteration_count_within_limit_returns_true(self):
        guard = _guard(policy=_policy(max_iterations_per_session=10))
        guard._window.iterations_count = 9
        ok, reason = guard.can_proceed()
        assert ok is True


# ---------------------------------------------------------------------------
# T16 — Cooldown exponential back-off
# ---------------------------------------------------------------------------


class TestCooldownBackoff:
    """T16: compute_cooldown produces 60, 120, 240 for failures 1, 2, 3
    when cooldown_base_s=60 and max_cooldown_s is large enough."""

    def test_cooldown_first_failure(self):
        """1 consecutive failure → base * 2^0 = 60."""
        guard = _guard(policy=_policy(cooldown_base_s=60.0, max_cooldown_s=600.0))
        assert guard.compute_cooldown(1) == pytest.approx(60.0)

    def test_cooldown_second_failure(self):
        """2 consecutive failures → base * 2^1 = 120."""
        guard = _guard(policy=_policy(cooldown_base_s=60.0, max_cooldown_s=600.0))
        assert guard.compute_cooldown(2) == pytest.approx(120.0)

    def test_cooldown_third_failure(self):
        """3 consecutive failures → base * 2^2 = 240."""
        guard = _guard(policy=_policy(cooldown_base_s=60.0, max_cooldown_s=600.0))
        assert guard.compute_cooldown(3) == pytest.approx(240.0)

    def test_cooldown_capped_at_max(self):
        """Very high failure count should not exceed max_cooldown_s."""
        guard = _guard(policy=_policy(cooldown_base_s=60.0, max_cooldown_s=300.0))
        result = guard.compute_cooldown(20)  # would be astronomically large uncapped
        assert result == pytest.approx(300.0)

    def test_cooldown_zero_failures_returns_zero(self):
        """Zero failures → no cooldown needed."""
        guard = _guard(policy=_policy(cooldown_base_s=60.0, max_cooldown_s=300.0))
        assert guard.compute_cooldown(0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Window reset on expiry
# ---------------------------------------------------------------------------


class TestWindowReset:
    def test_window_resets_on_expiry_before_proceed_check(self):
        """When the budget window has expired, can_proceed resets it and allows."""
        guard = _guard(policy=_policy(max_spend_usd=1.0))
        # Exhaust budget in the current window
        guard._window.spend_usd = 5.0
        guard._window.iterations_count = 99
        # Move window_start back by more than window_hours so is_expired() is True
        guard._window.window_start_utc = datetime.now(timezone.utc) - timedelta(hours=25)
        # After reset the window is fresh → should be able to proceed
        ok, reason = guard.can_proceed()
        assert ok is True
        assert guard._window.spend_usd == 0.0
        assert guard._window.iterations_count == 0


# ---------------------------------------------------------------------------
# record_spend
# ---------------------------------------------------------------------------


class TestRecordSpend:
    @pytest.mark.asyncio
    async def test_record_spend_increments_spend(self):
        """record_spend adds the cost to the window spend."""
        ledger = FakeLedger()
        guard = _guard(ledger=ledger)
        await guard.record_spend("iter-1", 0.25)
        assert guard._window.spend_usd == pytest.approx(0.25)

    @pytest.mark.asyncio
    async def test_record_spend_increments_iteration_count(self):
        """record_spend increments iteration count by 1."""
        ledger = FakeLedger()
        guard = _guard(ledger=ledger)
        await guard.record_spend("iter-1", 0.10)
        assert guard._window.iterations_count == 1

    @pytest.mark.asyncio
    async def test_record_spend_writes_budget_checkpoint_to_ledger(self):
        """record_spend persists a BUDGET_CHECKPOINT entry to the ledger."""
        ledger = FakeLedger()
        guard = _guard(ledger=ledger)
        await guard.record_spend("iter-42", 0.05)
        assert len(ledger.appended) == 1
        entry = ledger.appended[0]
        assert entry.state == OperationState.BUDGET_CHECKPOINT
        assert entry.data["iteration_id"] == "iter-42"
        assert entry.data["cost_usd"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_record_spend_accumulates_across_calls(self):
        """Multiple record_spend calls accumulate correctly."""
        ledger = FakeLedger()
        guard = _guard(ledger=ledger)
        await guard.record_spend("iter-1", 1.00)
        await guard.record_spend("iter-2", 0.50)
        assert guard._window.spend_usd == pytest.approx(1.50)
        assert guard._window.iterations_count == 2


# ---------------------------------------------------------------------------
# load_from_ledger
# ---------------------------------------------------------------------------


class TestLoadFromLedger:
    @pytest.mark.asyncio
    async def test_empty_ledger_gives_fresh_window(self):
        """With no BUDGET_CHECKPOINT entries, window starts at zero."""
        ledger = FakeLedger()
        guard = _guard(ledger=ledger)
        await guard.load_from_ledger()
        assert guard._window.spend_usd == pytest.approx(0.0)
        assert guard._window.iterations_count == 0

    @pytest.mark.asyncio
    async def test_load_reconstructs_spend_from_todays_checkpoints(self):
        """BUDGET_CHECKPOINT entries from today are summed into the window."""
        now_ts = time.time()
        entries = [
            LedgerEntry(
                op_id="op-budget",
                state=OperationState.BUDGET_CHECKPOINT,
                data={"cost_usd": 0.30, "iteration_id": "iter-1"},
                wall_time=now_ts,
            ),
            LedgerEntry(
                op_id="op-budget",
                state=OperationState.BUDGET_CHECKPOINT,
                data={"cost_usd": 0.70, "iteration_id": "iter-2"},
                wall_time=now_ts,
            ),
        ]
        ledger = FakeLedger(prepopulated=entries)
        guard = _guard(ledger=ledger)
        await guard.load_from_ledger()
        assert guard._window.spend_usd == pytest.approx(1.00)
        assert guard._window.iterations_count == 2

    @pytest.mark.asyncio
    async def test_load_ignores_old_checkpoints_outside_window(self):
        """BUDGET_CHECKPOINT entries older than the window duration are ignored."""
        old_ts = time.time() - 25 * 3600  # 25 hours ago, outside 24-h window
        now_ts = time.time()
        entries = [
            LedgerEntry(
                op_id="op-budget",
                state=OperationState.BUDGET_CHECKPOINT,
                data={"cost_usd": 9.99, "iteration_id": "iter-old"},
                wall_time=old_ts,
            ),
            LedgerEntry(
                op_id="op-budget",
                state=OperationState.BUDGET_CHECKPOINT,
                data={"cost_usd": 0.10, "iteration_id": "iter-new"},
                wall_time=now_ts,
            ),
        ]
        ledger = FakeLedger(prepopulated=entries)
        guard = _guard(ledger=ledger)
        await guard.load_from_ledger()
        # Only the recent entry should be counted
        assert guard._window.spend_usd == pytest.approx(0.10)
        assert guard._window.iterations_count == 1

    @pytest.mark.asyncio
    async def test_load_ignores_non_budget_checkpoint_entries(self):
        """Non-BUDGET_CHECKPOINT entries are ignored during reconstruction."""
        now_ts = time.time()
        entries = [
            LedgerEntry(
                op_id="op-1",
                state=OperationState.PLANNED,
                data={"cost_usd": 99.0},
                wall_time=now_ts,
            ),
            LedgerEntry(
                op_id="op-budget",
                state=OperationState.BUDGET_CHECKPOINT,
                data={"cost_usd": 0.20, "iteration_id": "iter-1"},
                wall_time=now_ts,
            ),
        ]
        ledger = FakeLedger(prepopulated=entries)
        guard = _guard(ledger=ledger)
        await guard.load_from_ledger()
        assert guard._window.spend_usd == pytest.approx(0.20)
        assert guard._window.iterations_count == 1


# ---------------------------------------------------------------------------
# Wall-time check
# ---------------------------------------------------------------------------


class TestWallTimeCheck:
    def test_wall_time_exceeded_returns_false(self):
        """If elapsed wall time exceeds max_wall_time_s, can_proceed returns False."""
        guard = _guard(policy=_policy(max_wall_time_s=10.0))
        # Simulate session started 20 seconds ago
        guard._session_start_time = time.monotonic() - 20.0
        ok, reason = guard.can_proceed()
        assert ok is False
        assert reason

    def test_wall_time_within_limit_returns_true(self):
        """If elapsed wall time is within max_wall_time_s, can_proceed returns True."""
        guard = _guard(policy=_policy(max_wall_time_s=3600.0))
        # Default session_start_time is set at construction → well within 1 hour
        ok, reason = guard.can_proceed()
        assert ok is True

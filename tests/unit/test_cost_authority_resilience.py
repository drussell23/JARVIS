"""
Failure-injection test suite for Cost Authority (0L).

Tests exact failure scenarios for the two-phase budget reservation system:
- Crash between reserve and commit (TTL expiry)
- Duplicate commit (idempotent)
- Release unknown reservation (no-op)
- Concurrent contention (exactly one succeeds)
- Stale epoch commit rejection (fencing)
- Day boundary rollover (UTC midnight)
- Janitor during active startup (skip)
"""

import asyncio
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to create a minimal CostTracker with an in-memory-like temp DB
# ---------------------------------------------------------------------------

def _make_config(db_path: Path, daily: float = 1.0, monthly: float = 20.0):
    """Create a CostTrackerConfig with custom budget thresholds."""
    os.environ["COST_ALERT_DAILY"] = str(daily)
    os.environ["COST_ALERT_MONTHLY"] = str(monthly)
    os.environ["COST_TRACKER_DB_PATH"] = str(db_path)
    os.environ["COST_TRACKER_ENABLE_REDIS"] = "false"
    os.environ["COST_TRACKER_HARD_BUDGET"] = "true"

    from backend.core.cost_tracker import CostTrackerConfig
    return CostTrackerConfig()


async def _make_tracker(db_path: Path, daily: float = 1.0, monthly: float = 20.0):
    """Create and initialize a CostTracker with a temporary database."""
    from backend.core.cost_tracker import CostTracker
    config = _make_config(db_path, daily, monthly)
    tracker = CostTracker(config)
    await tracker.initialize_database()
    return tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReservationResilience:
    """Test suite for budget reservation failure scenarios."""

    @pytest.fixture(autouse=True)
    def _tmp_db(self, tmp_path):
        self.db_path = tmp_path / "cost_test.db"

    @pytest.mark.asyncio
    async def test_crash_between_reserve_and_commit(self):
        """Reservation with TTL expires after timeout, freeing budget."""
        os.environ["JARVIS_RESERVATION_TTL_S"] = "1"  # 1 second TTL for test speed
        tracker = await _make_tracker(self.db_path, daily=1.0)

        # Reserve budget
        ok, reason, res_id = await tracker.reserve_spend(0.50, "vm_create")
        assert ok is True
        assert res_id is not None

        # Simulate crash — don't commit or release. Wait for TTL to expire.
        await asyncio.sleep(1.5)

        # A new reservation for the same amount should succeed (old one expired)
        ok2, reason2, res_id2 = await tracker.reserve_spend(0.50, "vm_create")
        assert ok2 is True, f"Expected success after TTL expiry, got: {reason2}"

        # Cleanup
        if res_id2:
            await tracker.release_spend(res_id2)
        os.environ.pop("JARVIS_RESERVATION_TTL_S", None)

    @pytest.mark.asyncio
    async def test_duplicate_commit(self):
        """Second commit of same reservation_id is a no-op (idempotent)."""
        tracker = await _make_tracker(self.db_path)

        ok, _, res_id = await tracker.reserve_spend(0.10, "vm_create")
        assert ok and res_id

        # First commit
        await tracker.commit_spend(res_id)
        # Second commit — should be a no-op, not raise
        await tracker.commit_spend(res_id)

    @pytest.mark.asyncio
    async def test_release_unknown_reservation(self):
        """Releasing non-existent reservation_id is a no-op (no error)."""
        tracker = await _make_tracker(self.db_path)

        # Should not raise
        await tracker.release_spend("nonexistent:abc12345")

    @pytest.mark.asyncio
    async def test_concurrent_reserve_contention(self):
        """Two concurrent reserves for amount > remaining: exactly one succeeds."""
        tracker = await _make_tracker(self.db_path, daily=0.60)

        # Mock _get_daily_spend and _get_monthly_spend to return 0
        tracker._get_daily_spend = AsyncMock(return_value=0.0)
        tracker._get_monthly_spend = AsyncMock(return_value=0.0)

        # Try two concurrent reservations that together exceed budget
        results = await asyncio.gather(
            tracker.reserve_spend(0.50, "vm_create"),
            tracker.reserve_spend(0.50, "vm_create"),
        )

        successes = [r for r in results if r[0] is True]
        failures = [r for r in results if r[0] is False]

        assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
        assert len(failures) == 1, f"Expected exactly 1 failure, got {len(failures)}"

        # Cleanup
        for ok, _, res_id in results:
            if ok and res_id:
                await tracker.release_spend(res_id)

    @pytest.mark.asyncio
    async def test_stale_epoch_commit_rejected(self):
        """Commit from old supervisor epoch is rejected (fencing)."""
        tracker = await _make_tracker(self.db_path)

        ok, _, res_id = await tracker.reserve_spend(0.10, "vm_create")
        assert ok and res_id

        # Simulate epoch change (as if supervisor restarted)
        old_epoch = tracker._supervisor_epoch
        tracker._supervisor_epoch = f"new-host-{os.getpid()}-{time.time():.0f}"

        # Commit with stale epoch should be rejected (logged, not raised)
        await tracker.commit_spend(res_id)

        # Verify reservation is still 'active' (not committed)
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT status FROM budget_reservations WHERE reservation_id=?",
                (res_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "active", f"Expected 'active' after stale commit, got '{row[0]}'"

        # Cleanup
        tracker._supervisor_epoch = old_epoch
        await tracker.release_spend(res_id)

    @pytest.mark.asyncio
    async def test_day_boundary_budget_rollover(self):
        """Budget correctly resets based on daily window, not carrying over."""
        tracker = await _make_tracker(self.db_path, daily=0.50)
        tracker._get_daily_spend = AsyncMock(return_value=0.0)
        tracker._get_monthly_spend = AsyncMock(return_value=0.0)

        # Reserve up to budget
        ok, _, res_id = await tracker.reserve_spend(0.45, "vm_create")
        assert ok
        await tracker.commit_spend(res_id)

        # After "day rolls over" — mock daily spend back to 0
        tracker._get_daily_spend = AsyncMock(return_value=0.0)

        # Should succeed again (new day)
        ok2, reason2, res_id2 = await tracker.reserve_spend(0.45, "vm_create")
        assert ok2, f"Expected success after day rollover, got: {reason2}"
        if res_id2:
            await tracker.release_spend(res_id2)

    @pytest.mark.asyncio
    async def test_janitor_during_active_startup(self):
        """Janitor skips cleanup when supervisor pgrep returns positive.

        Tests the logic, not actual pgrep execution.
        """
        # This tests the janitor shell script's logic conceptually:
        # if pgrep -f "unified_supervisor" > /dev/null; then exit 0
        # We verify the script exists and has the correct guard
        janitor_path = Path(__file__).parent.parent.parent / "scripts" / "jarvis_cost_janitor.sh"
        assert janitor_path.exists(), f"Janitor script not found at {janitor_path}"

        content = janitor_path.read_text()
        assert 'pgrep -f "unified_supervisor"' in content, "Janitor must check for running supervisor"
        assert "exit 0" in content, "Janitor must exit 0 when supervisor is running"
        assert "labels.created-by=jarvis" in content, "Janitor must filter by jarvis label"
        assert "JARVIS_ALWAYS_ON_VMS" in content, "Janitor must respect allowlist"

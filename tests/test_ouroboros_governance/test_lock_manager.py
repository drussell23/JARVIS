"""Tests for the governance lock manager."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LeaseHandle,
    LockOrderViolation,
    FencingTokenError,
    LOCK_TTLS,
)


@pytest.fixture
def lock_manager():
    """Create a GovernanceLockManager with mocked DLM."""
    return GovernanceLockManager()


# --- Lock Level Hierarchy ---


class TestLockLevels:
    def test_lock_levels_ascending_order(self):
        """All 8 lock levels have strictly ascending integer values."""
        levels = list(LockLevel)
        for i in range(len(levels) - 1):
            assert levels[i].value < levels[i + 1].value

    def test_all_eight_levels_defined(self):
        """Exactly 8 levels: FILE, REPO, CROSS_REPO_TX, POLICY, LEDGER_APPEND,
        BUILD, STAGING, PROD."""
        assert len(LockLevel) == 8
        expected = [
            "FILE_LOCK", "REPO_LOCK", "CROSS_REPO_TX", "POLICY_LOCK",
            "LEDGER_APPEND", "BUILD_LOCK", "STAGING_LOCK", "PROD_LOCK",
        ]
        assert [l.name for l in LockLevel] == expected


class TestLockModes:
    def test_shared_read_and_exclusive_write(self):
        """Two modes exist: SHARED_READ and EXCLUSIVE_WRITE."""
        assert LockMode.SHARED_READ.value == "shared_read"
        assert LockMode.EXCLUSIVE_WRITE.value == "exclusive_write"


# --- Ascending Order Enforcement ---


class TestAscendingOrder:
    @pytest.mark.asyncio
    async def test_ascending_acquisition_succeeds(self, lock_manager):
        """Acquiring locks in ascending level order succeeds."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as handle1:
            assert handle1 is not None
            assert handle1.level == LockLevel.FILE_LOCK

            async with lock_manager.acquire(
                level=LockLevel.REPO_LOCK,
                resource="jarvis",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle2:
                assert handle2 is not None
                assert handle2.level == LockLevel.REPO_LOCK

    @pytest.mark.asyncio
    async def test_descending_acquisition_raises(self, lock_manager):
        """Acquiring a lower-level lock while holding a higher one raises
        LockOrderViolation immediately (no deadlock)."""
        async with lock_manager.acquire(
            level=LockLevel.REPO_LOCK,
            resource="jarvis",
            mode=LockMode.EXCLUSIVE_WRITE,
        ):
            with pytest.raises(LockOrderViolation):
                async with lock_manager.acquire(
                    level=LockLevel.FILE_LOCK,
                    resource="src/foo.py",
                    mode=LockMode.EXCLUSIVE_WRITE,
                ):
                    pass  # Should never reach here

    @pytest.mark.asyncio
    async def test_same_level_same_resource_reentrant(self, lock_manager):
        """Acquiring same level + same resource is allowed (re-entrant)."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as h1:
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as h2:
                assert h1.fencing_token == h2.fencing_token


# --- Shared Read / Exclusive Write ---


class TestReadWriteSemantics:
    @pytest.mark.asyncio
    async def test_concurrent_shared_reads_succeed(self, lock_manager):
        """Two concurrent shared-read locks on the same resource both succeed."""
        results = []

        async def read_lock():
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.SHARED_READ,
            ) as handle:
                results.append(handle is not None)
                await asyncio.sleep(0.01)

        await asyncio.gather(read_lock(), read_lock())
        assert results == [True, True]

    @pytest.mark.asyncio
    async def test_exclusive_write_blocks_concurrent_write(self, lock_manager):
        """Only one exclusive-write holder at a time; second waits."""
        order = []

        async def write_lock(label: str, delay: float):
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ):
                order.append(f"{label}_start")
                await asyncio.sleep(delay)
                order.append(f"{label}_end")

        await asyncio.gather(
            write_lock("first", 0.05),
            write_lock("second", 0.01),
        )
        # First must complete before second starts
        assert order.index("first_end") < order.index("second_start")


# --- TTL and Fencing ---


class TestTTLAndFencing:
    def test_ttl_per_level(self):
        """Each lock level has a defined TTL."""
        assert LOCK_TTLS[LockLevel.FILE_LOCK] == 60.0
        assert LOCK_TTLS[LockLevel.REPO_LOCK] == 120.0
        assert LOCK_TTLS[LockLevel.CROSS_REPO_TX] == 300.0
        assert LOCK_TTLS[LockLevel.PROD_LOCK] == 600.0

    @pytest.mark.asyncio
    async def test_fencing_token_monotonically_increasing(self, lock_manager):
        """Successive acquisitions yield increasing fencing tokens."""
        tokens = []
        for _ in range(5):
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle:
                tokens.append(handle.fencing_token)

        for i in range(len(tokens) - 1):
            assert tokens[i] < tokens[i + 1]

    @pytest.mark.asyncio
    async def test_validate_fencing_token_rejects_stale(self, lock_manager):
        """validate_fencing_token raises FencingTokenError for stale tokens."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as handle:
            current_token = handle.fencing_token

        # Current token is now stale (lock released, new acquisition would
        # yield higher token)
        with pytest.raises(FencingTokenError):
            lock_manager.validate_fencing_token(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                token=0,  # Definitely stale
            )


# --- Fairness ---


class TestFairness:
    @pytest.mark.asyncio
    async def test_waiter_tracking(self, lock_manager):
        """Lock manager tracks how long waiters wait (for fairness metrics)."""
        stats = lock_manager.get_contention_stats()
        assert "max_wait_ms" in stats
        assert "active_locks" in stats

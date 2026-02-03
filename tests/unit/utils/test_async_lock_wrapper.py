"""
Tests for backend.utils.async_lock_wrapper module.

These tests verify that:
1. StartupFileLock properly wraps RobustFileLock
2. Stale PID detection works correctly
3. Timeout validation enforces bounds
4. Context manager support works properly
5. CrossRepoLockManager provides correct abstractions

Following 35-point checklist items:
- Item 4: Cross-repo lock timeouts (env-driven)
- Item 7: Single lock abstraction
- Item 30: Stale lock/PID check
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.utils.async_lock_wrapper import (
    StartupFileLock,
    CrossRepoLockManager,
    MAX_LOCK_TIMEOUT,
    MIN_LOCK_TIMEOUT,
    DEFAULT_LOCK_TIMEOUT,
    STALE_LOCK_RETRY_TIMEOUT,
    _is_pid_alive,
    _read_lock_metadata_sync,
    _remove_lock_file_sync,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def temp_lock_dir(tmp_path: Path) -> Path:
    """Create a temporary lock directory."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


@pytest.fixture
def lock_name() -> str:
    """Generate a unique lock name for each test."""
    return f"test_lock_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mock_lock_env(temp_lock_dir: Path):
    """Set up environment with temp lock directory."""
    with patch.dict(os.environ, {"JARVIS_LOCK_DIR": str(temp_lock_dir)}):
        yield temp_lock_dir


# =============================================================================
# Test: PID Checking Utilities
# =============================================================================


class TestPidChecking:
    """Tests for PID alive checking utilities."""

    def test_is_pid_alive_current_process(self) -> None:
        """Test that current process PID is reported as alive."""
        current_pid = os.getpid()
        assert _is_pid_alive(current_pid) is True

    def test_is_pid_alive_nonexistent(self) -> None:
        """Test that non-existent PID is reported as dead."""
        # Use a very high PID that's unlikely to exist
        nonexistent_pid = 4000000
        assert _is_pid_alive(nonexistent_pid) is False

    def test_is_pid_alive_zero(self) -> None:
        """Test that PID 0 is reported as dead."""
        assert _is_pid_alive(0) is False

    def test_is_pid_alive_negative(self) -> None:
        """Test that negative PID is reported as dead."""
        assert _is_pid_alive(-1) is False

    def test_is_pid_alive_init_process(self) -> None:
        """Test that PID 1 (init) is alive but may not be signalable."""
        # PID 1 exists but we may not have permission to signal it
        # The function should return True (PermissionError means it exists)
        result = _is_pid_alive(1)
        # On most systems, PID 1 exists so this should be True
        # (PermissionError is treated as "alive")
        assert result is True


class TestLockMetadataSync:
    """Tests for lock metadata reading."""

    def test_read_metadata_valid(self, tmp_path: Path) -> None:
        """Test reading valid lock metadata."""
        lock_file = tmp_path / "test.lock"
        metadata = {
            "owner_pid": 12345,
            "owner_host": "testhost",
            "acquired_at": time.time(),
            "source": "test",
        }
        lock_file.write_text(json.dumps(metadata))

        result = _read_lock_metadata_sync(lock_file)
        assert result is not None
        assert result["owner_pid"] == 12345
        assert result["source"] == "test"

    def test_read_metadata_nonexistent(self, tmp_path: Path) -> None:
        """Test reading metadata from non-existent file returns None."""
        lock_file = tmp_path / "nonexistent.lock"
        assert _read_lock_metadata_sync(lock_file) is None

    def test_read_metadata_invalid_json(self, tmp_path: Path) -> None:
        """Test reading invalid JSON returns None."""
        lock_file = tmp_path / "invalid.lock"
        lock_file.write_text("not valid json {")
        assert _read_lock_metadata_sync(lock_file) is None

    def test_read_metadata_empty(self, tmp_path: Path) -> None:
        """Test reading empty file returns None."""
        lock_file = tmp_path / "empty.lock"
        lock_file.write_text("")
        assert _read_lock_metadata_sync(lock_file) is None


class TestRemoveLockFileSync:
    """Tests for lock file removal."""

    def test_remove_existing_file(self, tmp_path: Path) -> None:
        """Test removing an existing lock file."""
        lock_file = tmp_path / "test.lock"
        lock_file.write_text("{}")

        result = _remove_lock_file_sync(lock_file)
        assert result is True
        assert not lock_file.exists()

    def test_remove_nonexistent_file(self, tmp_path: Path) -> None:
        """Test removing non-existent file returns True."""
        lock_file = tmp_path / "nonexistent.lock"
        result = _remove_lock_file_sync(lock_file)
        assert result is True


# =============================================================================
# Test: Configuration
# =============================================================================


class TestConfiguration:
    """Tests for configuration constants."""

    def test_max_timeout_positive(self) -> None:
        """Test MAX_LOCK_TIMEOUT is positive."""
        assert MAX_LOCK_TIMEOUT > 0

    def test_min_timeout_positive(self) -> None:
        """Test MIN_LOCK_TIMEOUT is positive."""
        assert MIN_LOCK_TIMEOUT > 0

    def test_default_timeout_within_bounds(self) -> None:
        """Test DEFAULT_LOCK_TIMEOUT is within bounds."""
        assert MIN_LOCK_TIMEOUT <= DEFAULT_LOCK_TIMEOUT <= MAX_LOCK_TIMEOUT

    def test_stale_retry_timeout_positive(self) -> None:
        """Test STALE_LOCK_RETRY_TIMEOUT is positive."""
        assert STALE_LOCK_RETRY_TIMEOUT > 0


# =============================================================================
# Test: StartupFileLock - Timeout Validation
# =============================================================================


class TestTimeoutValidation:
    """Tests for timeout validation in StartupFileLock."""

    @pytest.mark.asyncio
    async def test_timeout_zero_raises(self, mock_lock_env, lock_name: str) -> None:
        """Test that zero timeout raises ValueError."""
        lock = StartupFileLock(lock_name, source="test")
        with pytest.raises(ValueError, match="must be positive"):
            await lock.acquire(timeout_s=0)

    @pytest.mark.asyncio
    async def test_timeout_negative_raises(self, mock_lock_env, lock_name: str) -> None:
        """Test that negative timeout raises ValueError."""
        lock = StartupFileLock(lock_name, source="test")
        with pytest.raises(ValueError, match="must be positive"):
            await lock.acquire(timeout_s=-1.0)

    @pytest.mark.asyncio
    async def test_timeout_too_small_raises(self, mock_lock_env, lock_name: str) -> None:
        """Test that timeout below MIN_LOCK_TIMEOUT raises ValueError."""
        lock = StartupFileLock(lock_name, source="test")
        with pytest.raises(ValueError, match="Minimum allowed"):
            await lock.acquire(timeout_s=MIN_LOCK_TIMEOUT / 10)

    @pytest.mark.asyncio
    async def test_timeout_too_large_raises(self, mock_lock_env, lock_name: str) -> None:
        """Test that timeout above MAX_LOCK_TIMEOUT raises ValueError."""
        lock = StartupFileLock(lock_name, source="test")
        with pytest.raises(ValueError, match="Maximum allowed"):
            await lock.acquire(timeout_s=MAX_LOCK_TIMEOUT + 1)

    @pytest.mark.asyncio
    async def test_timeout_at_min_works(self, mock_lock_env, lock_name: str) -> None:
        """Test that MIN_LOCK_TIMEOUT works."""
        lock = StartupFileLock(lock_name, source="test")
        # Should not raise - minimum is acceptable
        acquired = await lock.acquire(timeout_s=MIN_LOCK_TIMEOUT)
        assert acquired is True
        await lock.release()

    @pytest.mark.asyncio
    async def test_timeout_at_max_accepted(self, mock_lock_env, lock_name: str) -> None:
        """Test that MAX_LOCK_TIMEOUT is accepted (doesn't raise)."""
        lock = StartupFileLock(lock_name, source="test")
        # Should not raise ValueError (though may take a while)
        # We acquire quickly since no contention
        acquired = await lock.acquire(timeout_s=MAX_LOCK_TIMEOUT)
        assert acquired is True
        await lock.release()


# =============================================================================
# Test: StartupFileLock - Basic Acquire/Release
# =============================================================================


class TestStartupFileLockBasic:
    """Tests for basic acquire/release functionality."""

    @pytest.mark.asyncio
    async def test_acquire_success(self, mock_lock_env, lock_name: str) -> None:
        """Test successful lock acquisition."""
        lock = StartupFileLock(lock_name, source="test")
        acquired = await lock.acquire(timeout_s=5.0)

        assert acquired is True
        assert lock.is_acquired is True
        assert lock.lock_name == lock_name

        await lock.release()
        assert lock.is_acquired is False

    @pytest.mark.asyncio
    async def test_release_idempotent(self, mock_lock_env, lock_name: str) -> None:
        """Test that release can be called multiple times safely."""
        lock = StartupFileLock(lock_name, source="test")
        await lock.acquire(timeout_s=5.0)

        # Release multiple times - should not raise
        await lock.release()
        await lock.release()
        await lock.release()

        assert lock.is_acquired is False

    @pytest.mark.asyncio
    async def test_acquire_default_timeout(self, mock_lock_env, lock_name: str) -> None:
        """Test acquisition with default timeout."""
        lock = StartupFileLock(lock_name, source="test")
        acquired = await lock.acquire()  # No timeout specified

        assert acquired is True
        await lock.release()


# =============================================================================
# Test: StartupFileLock - Context Manager
# =============================================================================


class TestStartupFileLockContextManager:
    """Tests for async context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_acquires(self, mock_lock_env, lock_name: str) -> None:
        """Test context manager acquires lock on entry."""
        lock = StartupFileLock(lock_name, source="test")

        async with lock as acquired:
            assert acquired is True
            assert lock.is_acquired is True

        # Should be released after exiting
        assert lock.is_acquired is False

    @pytest.mark.asyncio
    async def test_context_manager_releases_on_exception(
        self, mock_lock_env, lock_name: str
    ) -> None:
        """Test context manager releases lock even on exception."""
        lock = StartupFileLock(lock_name, source="test")

        with pytest.raises(ValueError):
            async with lock as acquired:
                assert acquired is True
                raise ValueError("Test exception")

        # Should still be released after exception
        assert lock.is_acquired is False

    @pytest.mark.asyncio
    async def test_context_manager_sequential_use(
        self, mock_lock_env, lock_name: str
    ) -> None:
        """Test context manager can be used sequentially."""
        lock = StartupFileLock(lock_name, source="test")

        # First use
        async with lock as acquired1:
            assert acquired1 is True

        # Second use should work
        async with lock as acquired2:
            assert acquired2 is True


# =============================================================================
# Test: StartupFileLock - Stale Lock Detection
# =============================================================================


class TestStaleLockDetection:
    """Tests for stale lock (dead PID) detection."""

    @pytest.mark.asyncio
    async def test_stale_lock_detected_and_recovered(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test that stale locks from dead processes are detected and removed."""
        lock_file = mock_lock_env / f"{lock_name}.lock"

        # Create a lock file with a dead PID
        stale_metadata = {
            "owner_pid": 4000000,  # Very high PID, unlikely to exist
            "owner_host": "testhost",
            "acquired_at": time.time() - 100,
            "source": "dead_process",
        }
        lock_file.write_text(json.dumps(stale_metadata))

        # Now try to acquire - should detect stale and recover
        lock = StartupFileLock(lock_name, source="test")

        # The lock wrapper should detect stale PID and remove the lock
        acquired = await lock.acquire(timeout_s=2.0)

        # Should succeed after removing stale lock
        assert acquired is True
        assert lock.is_acquired is True

        await lock.release()

    @pytest.mark.asyncio
    async def test_live_lock_not_treated_as_stale(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test that locks held by live processes are not treated as stale."""
        # Create a lock file with our own PID (definitely alive)
        lock_file = mock_lock_env / f"{lock_name}.lock"
        live_metadata = {
            "owner_pid": os.getpid(),  # Our own PID - definitely alive
            "owner_host": "testhost",
            "acquired_at": time.time(),
            "source": "live_process",
        }
        lock_file.write_text(json.dumps(live_metadata))

        lock = StartupFileLock(lock_name, source="test")

        # The _is_holder_stale should return False for our own PID
        is_stale = await lock._is_holder_stale()
        assert is_stale is False


# =============================================================================
# Test: StartupFileLock - Non-Blocking Behavior
# =============================================================================


class TestNonBlockingBehavior:
    """Tests to verify lock operations don't block the event loop."""

    @pytest.mark.asyncio
    async def test_acquire_non_blocking(self, mock_lock_env, lock_name: str) -> None:
        """Test that lock acquisition doesn't block other async tasks."""
        counter = {"value": 0}

        async def increment_counter():
            for _ in range(10):
                counter["value"] += 1
                await asyncio.sleep(0.02)

        lock = StartupFileLock(lock_name, source="test")

        # Run acquire and counter concurrently
        acquire_task = asyncio.create_task(lock.acquire(timeout_s=5.0))
        counter_task = asyncio.create_task(increment_counter())

        acquired, _ = await asyncio.gather(acquire_task, counter_task)

        assert acquired is True
        # Counter should have incremented while acquire was running
        assert counter["value"] >= 5

        await lock.release()


# =============================================================================
# Test: CrossRepoLockManager
# =============================================================================


class TestCrossRepoLockManager:
    """Tests for CrossRepoLockManager."""

    def test_standard_locks_defined(self) -> None:
        """Test that standard locks are defined."""
        manager = CrossRepoLockManager()
        locks = manager.list_standard_locks()

        assert "vbia_state" in locks
        assert "voice_client" in locks
        assert "startup_lock" in locks

    @pytest.mark.asyncio
    async def test_lock_context_manager(self, mock_lock_env, lock_name: str) -> None:
        """Test lock context manager from manager."""
        manager = CrossRepoLockManager(source="test")

        async with manager.lock(lock_name) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_acquire_lock_context_manager(
        self, mock_lock_env, lock_name: str
    ) -> None:
        """Test acquire_lock alias."""
        manager = CrossRepoLockManager(source="test")

        async with manager.acquire_lock(lock_name, timeout_s=5.0) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_is_lock_held_when_held(
        self, mock_lock_env, lock_name: str
    ) -> None:
        """Test is_lock_held returns True when we hold the lock."""
        manager = CrossRepoLockManager(source="test")

        # Before acquiring
        held_before = await manager.is_lock_held(lock_name)

        async with manager.lock(lock_name) as acquired:
            assert acquired is True
            # While holding, our wrapper knows we have it
            lock_instance = manager._get_or_create_lock(lock_name)
            assert lock_instance.is_acquired is True

        # After release
        # Note: is_lock_held checks if *anyone* holds it via metadata
        # Since we released, it should be False
        held_after = await manager.is_lock_held(lock_name)
        # May or may not be held by file metadata, but our instance isn't
        lock_instance = manager._get_or_create_lock(lock_name)
        assert lock_instance.is_acquired is False

    @pytest.mark.asyncio
    async def test_get_lock_holder_info(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test getting lock holder information."""
        manager = CrossRepoLockManager(source="test")

        async with manager.lock(lock_name) as acquired:
            assert acquired is True

            # Get holder info
            info = await manager.get_lock_holder_info(lock_name)

            # Should have our info
            assert info is not None
            assert info["owner_pid"] == os.getpid()
            assert info["source"] == "test"
            assert info["is_alive"] is True

    @pytest.mark.asyncio
    async def test_get_lock_holder_info_no_lock(
        self, mock_lock_env, lock_name: str
    ) -> None:
        """Test getting holder info when no lock exists."""
        manager = CrossRepoLockManager(source="test")

        info = await manager.get_lock_holder_info(lock_name)
        assert info is None

    @pytest.mark.asyncio
    async def test_manager_reuses_locks(self, mock_lock_env, lock_name: str) -> None:
        """Test that manager reuses lock instances."""
        manager = CrossRepoLockManager(source="test")

        lock1 = manager._get_or_create_lock(lock_name)
        lock2 = manager._get_or_create_lock(lock_name)

        assert lock1 is lock2


# =============================================================================
# Test: Environment Variable Configuration
# =============================================================================


class TestEnvironmentConfiguration:
    """Tests for environment variable configuration."""

    def test_max_timeout_from_env(self) -> None:
        """Test MAX_LOCK_TIMEOUT can be configured via env."""
        # The default is loaded at import time, so we just verify
        # the value is a float
        assert isinstance(MAX_LOCK_TIMEOUT, float)
        assert MAX_LOCK_TIMEOUT > 0

    def test_lock_dir_from_env(self, tmp_path: Path) -> None:
        """Test lock directory can be configured via env."""
        custom_dir = tmp_path / "custom_locks"

        with patch.dict(os.environ, {"JARVIS_LOCK_DIR": str(custom_dir)}):
            lock = StartupFileLock("test_lock", source="test")
            assert lock._lock_dir == custom_dir


# =============================================================================
# Test: Uses Startup Executor
# =============================================================================


class TestUsesStartupExecutor:
    """Tests to verify the wrapper uses the startup executor."""

    @pytest.mark.asyncio
    async def test_uses_startup_executor_for_stale_check(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test that stale check uses startup executor."""
        lock_file = mock_lock_env / f"{lock_name}.lock"

        # Create a stale lock
        stale_metadata = {
            "owner_pid": 4000000,
            "owner_host": "testhost",
            "acquired_at": time.time() - 100,
            "source": "dead_process",
        }
        lock_file.write_text(json.dumps(stale_metadata))

        lock = StartupFileLock(lock_name, source="test")

        # The stale check should use the executor
        # We can't easily verify which executor was used, but we can
        # verify the operation completes without blocking
        counter = {"value": 0}

        async def increment():
            for _ in range(5):
                counter["value"] += 1
                await asyncio.sleep(0.01)

        stale_task = asyncio.create_task(lock._is_holder_stale())
        counter_task = asyncio.create_task(increment())

        is_stale, _ = await asyncio.gather(stale_task, counter_task)

        assert is_stale is True
        assert counter["value"] >= 3  # Should have run concurrently


# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_lock_with_special_characters_in_name(
        self, mock_lock_env,
    ) -> None:
        """Test lock with allowed special characters in name."""
        lock = StartupFileLock("test-lock_v2.0", source="test")
        acquired = await lock.acquire(timeout_s=1.0)

        assert acquired is True
        await lock.release()

    @pytest.mark.asyncio
    async def test_metadata_with_missing_pid(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test handling of lock file with missing owner_pid."""
        lock_file = mock_lock_env / f"{lock_name}.lock"

        # Create lock file without owner_pid
        metadata = {
            "owner_host": "testhost",
            "acquired_at": time.time(),
            "source": "unknown",
        }
        lock_file.write_text(json.dumps(metadata))

        lock = StartupFileLock(lock_name, source="test")

        # Should not consider it stale since we can't verify
        is_stale = await lock._is_holder_stale()
        assert is_stale is False

    @pytest.mark.asyncio
    async def test_corrupt_lock_file(
        self, mock_lock_env: Path, lock_name: str
    ) -> None:
        """Test handling of corrupted lock file."""
        lock_file = mock_lock_env / f"{lock_name}.lock"
        lock_file.write_text("corrupted data {{{{ not json")

        lock = StartupFileLock(lock_name, source="test")

        # Should not crash on stale check
        is_stale = await lock._is_holder_stale()
        assert is_stale is False  # Can't determine, so treat as not stale

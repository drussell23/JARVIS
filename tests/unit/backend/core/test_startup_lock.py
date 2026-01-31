# tests/unit/backend/core/test_startup_lock.py
"""Tests for StartupLock - prevents concurrent supervisor runs."""
import pytest
import tempfile
import os
from pathlib import Path


class TestStartupLock:
    """Tests for StartupLock functionality."""

    def test_acquire_succeeds_when_no_lock(self):
        """Lock acquisition should succeed when no lock exists."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            assert lock.acquire()
            lock.release()

    def test_acquire_writes_pid(self):
        """Lock acquisition should write current PID to lock file."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            lock.acquire()

            pid = lock.lock_file.read_text().strip()
            assert pid == str(os.getpid())

            lock.release()

    def test_second_acquire_fails(self):
        """Second lock acquisition should fail when lock is held."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock1 = StartupLock(state_dir=Path(tmpdir))
            lock2 = StartupLock(state_dir=Path(tmpdir))

            assert lock1.acquire()
            assert not lock2.acquire()  # Should fail

            lock1.release()

    def test_stale_lock_recovered(self):
        """Stale lock from dead process should be recovered."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "supervisor.lock"
            lock_file.parent.mkdir(parents=True, exist_ok=True)

            # Write a fake stale lock (non-existent PID)
            lock_file.write_text("999999999")  # Very unlikely to exist

            lock = StartupLock(state_dir=Path(tmpdir))
            # Should detect stale and acquire successfully
            assert lock.acquire()
            lock.release()

    def test_release_allows_new_acquire(self):
        """After release, new acquisition should succeed."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock1 = StartupLock(state_dir=Path(tmpdir))
            lock2 = StartupLock(state_dir=Path(tmpdir))

            assert lock1.acquire()
            lock1.release()

            # Now lock2 should be able to acquire
            assert lock2.acquire()
            lock2.release()

    def test_context_manager_acquire_release(self):
        """Context manager should acquire and release lock."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            with StartupLock(state_dir=Path(tmpdir)) as lock:
                # Lock should be held
                pid = lock.lock_file.read_text().strip()
                assert pid == str(os.getpid())

            # After exit, another lock should succeed
            lock2 = StartupLock(state_dir=Path(tmpdir))
            assert lock2.acquire()
            lock2.release()

    def test_context_manager_raises_on_failure(self):
        """Context manager should raise RuntimeError if lock fails."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock1 = StartupLock(state_dir=Path(tmpdir))
            lock1.acquire()

            try:
                with pytest.raises(RuntimeError, match="Failed to acquire startup lock"):
                    with StartupLock(state_dir=Path(tmpdir)):
                        pass  # Should not reach here
            finally:
                lock1.release()

    def test_lock_file_path(self):
        """Lock file should be in state_dir with correct name."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            expected_path = Path(tmpdir) / "supervisor.lock"
            assert lock.lock_file == expected_path

    def test_creates_state_dir_if_missing(self):
        """Lock acquisition should create state directory if it doesn't exist."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            nested_dir = Path(tmpdir) / "nested" / "state"
            lock = StartupLock(state_dir=nested_dir)
            assert lock.acquire()
            assert nested_dir.exists()
            lock.release()

    def test_multiple_release_calls_safe(self):
        """Multiple release calls should be safe (idempotent)."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            lock.acquire()

            # Multiple releases should not raise
            lock.release()
            lock.release()
            lock.release()

    def test_release_without_acquire_safe(self):
        """Release without acquire should be safe."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            # Should not raise
            lock.release()

    def test_is_stale_lock_returns_true_for_dead_pid(self):
        """_is_stale_lock should return True for non-existent PID."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            # Create a lock file with a fake PID
            lock.lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock.lock_file.write_text("999999999")

            assert lock._is_stale_lock() is True

    def test_is_stale_lock_returns_false_for_running_pid(self):
        """_is_stale_lock should return False for running PID."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            # Create a lock file with our own PID (definitely running)
            lock.lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock.lock_file.write_text(str(os.getpid()))

            assert lock._is_stale_lock() is False

    def test_is_pid_running_returns_true_for_current_process(self):
        """_is_pid_running should return True for current process."""
        from backend.core.startup_lock import StartupLock

        assert StartupLock._is_pid_running(os.getpid()) is True

    def test_is_pid_running_returns_false_for_nonexistent(self):
        """_is_pid_running should return False for non-existent PID."""
        from backend.core.startup_lock import StartupLock

        assert StartupLock._is_pid_running(999999999) is False

    def test_read_lock_pid_returns_none_for_missing_file(self):
        """_read_lock_pid should return None if lock file doesn't exist."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            assert lock._read_lock_pid() is None

    def test_read_lock_pid_returns_none_for_invalid_content(self):
        """_read_lock_pid should return None for non-integer content."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            lock.lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock.lock_file.write_text("not-a-pid")

            assert lock._read_lock_pid() is None

    def test_read_lock_pid_returns_valid_pid(self):
        """_read_lock_pid should return PID for valid content."""
        from backend.core.startup_lock import StartupLock

        with tempfile.TemporaryDirectory() as tmpdir:
            lock = StartupLock(state_dir=Path(tmpdir))
            lock.lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock.lock_file.write_text("12345")

            assert lock._read_lock_pid() == 12345

    def test_default_state_dir(self):
        """Default state_dir should point to ~/.jarvis/state."""
        from backend.core.startup_lock import StartupLock, DEFAULT_STATE_DIR

        lock = StartupLock()
        assert lock.state_dir == DEFAULT_STATE_DIR
        assert lock.lock_file == DEFAULT_STATE_DIR / "supervisor.lock"

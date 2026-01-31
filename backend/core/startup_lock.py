# backend/core/startup_lock.py
"""
StartupLock - Prevents concurrent supervisor runs.

Uses file locking with stale lock detection to ensure only one
supervisor instance runs at a time.

Features:
- POSIX file locking via fcntl.flock (non-blocking)
- Stale lock detection (checks if lock holder PID is still running)
- Automatic recovery from crashed process locks
- Context manager support for clean acquisition/release
- PID file for debugging and monitoring

Usage:
    from backend.core.startup_lock import StartupLock

    # Basic usage
    lock = StartupLock()
    if lock.acquire():
        try:
            # Run supervisor
            pass
        finally:
            lock.release()

    # Context manager usage
    with StartupLock() as lock:
        # Run supervisor
        pass
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import Optional, IO

logger = logging.getLogger("jarvis.startup_lock")

DEFAULT_STATE_DIR = Path("~/.jarvis/state").expanduser()


class StartupLock:
    """
    File-based lock to prevent concurrent supervisor runs.

    Handles stale locks from crashed processes by checking if the
    PID in the lock file is still running.

    Attributes:
        state_dir: Directory containing state files.
        lock_file: Path to the lock file.
    """

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        """
        Initialize StartupLock.

        Args:
            state_dir: Directory to store lock file. Defaults to ~/.jarvis/state.
        """
        self.state_dir = state_dir
        self.lock_file = state_dir / "supervisor.lock"
        self._file: Optional[IO[str]] = None

    def acquire(self, _retry: bool = True) -> bool:
        """
        Attempt to acquire the lock.

        Returns True if lock acquired, False if another instance is running.
        Handles stale locks from dead processes automatically.

        Args:
            _retry: Internal parameter to prevent infinite recursion.

        Returns:
            True if lock acquired, False if another instance holds the lock.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Read PID before opening file (opening with "w" truncates)
        existing_pid = self._read_lock_pid()

        try:
            # Open in r+ mode to preserve content if we can't get lock
            # Fall back to w mode if file doesn't exist
            try:
                self._file = open(self.lock_file, "r+")
            except FileNotFoundError:
                self._file = open(self.lock_file, "w")

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Got the lock - truncate and write our PID
            self._file.seek(0)
            self._file.truncate()
            self._file.write(str(os.getpid()))
            self._file.flush()

            logger.debug(f"Acquired startup lock (PID {os.getpid()})")
            return True

        except BlockingIOError:
            # Lock held by another process
            if self._file:
                self._file.close()
                self._file = None

            # Check for stale lock using the PID we read before opening
            if _retry and self._is_stale_lock_with_pid(existing_pid):
                logger.info("Detected stale lock from dead process, removing")
                try:
                    self.lock_file.unlink()
                except OSError:
                    pass
                return self.acquire(_retry=False)  # Retry once only

            logger.error(f"Another supervisor already running (PID {existing_pid})")
            return False

        except Exception as e:
            logger.error(f"Failed to acquire startup lock: {e}")
            if self._file:
                self._file.close()
                self._file = None
            return False

    def release(self) -> None:
        """
        Release the lock.

        Safe to call multiple times or without prior acquire.
        """
        if self._file is None:
            return

        if self._file.closed:
            self._file = None
            return

        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            logger.debug("Released startup lock")
        except Exception as e:
            logger.warning(f"Error releasing lock: {e}")
        finally:
            self._file = None

    def _is_stale_lock(self) -> bool:
        """
        Check if the lock holder is still running.

        Returns:
            True if lock is stale (holder dead or can't read PID), False otherwise.
        """
        pid = self._read_lock_pid()
        return self._is_stale_lock_with_pid(pid)

    def _is_stale_lock_with_pid(self, pid: Optional[int]) -> bool:
        """
        Check if a lock with given PID is stale.

        Args:
            pid: PID from lock file, or None if unknown.

        Returns:
            True if lock is stale (holder dead or can't read PID), False otherwise.
        """
        if pid is None:
            return True  # Can't read PID, assume stale

        return not self._is_pid_running(pid)

    def _read_lock_pid(self) -> Optional[int]:
        """
        Read PID from lock file.

        Returns:
            PID from lock file, or None if file doesn't exist or invalid content.
        """
        try:
            content = self.lock_file.read_text().strip()
            return int(content)
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        """
        Check if a process is running.

        Uses os.kill with signal 0 which doesn't actually send a signal,
        but checks if the process exists and we have permission to signal it.

        Args:
            pid: Process ID to check.

        Returns:
            True if process is running, False otherwise.
        """
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True
        except OSError:
            return False

    def __enter__(self) -> 'StartupLock':
        """
        Context manager entry - acquire lock.

        Raises:
            RuntimeError: If lock acquisition fails.
        """
        if not self.acquire():
            raise RuntimeError("Failed to acquire startup lock")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - release lock."""
        self.release()


def get_startup_lock(state_dir: Path = DEFAULT_STATE_DIR) -> StartupLock:
    """
    Factory function to create a StartupLock.

    Convenience function for creating a StartupLock with optional
    custom state directory.

    Args:
        state_dir: Directory to store lock file.

    Returns:
        New StartupLock instance.
    """
    return StartupLock(state_dir)

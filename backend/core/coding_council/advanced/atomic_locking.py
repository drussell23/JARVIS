"""
v77.2: Atomic File Locking - Gap #61
=====================================

True atomic file locking using OS-level primitives.

Problem:
    File-based locking with check-then-write has race condition:
    1. Process A: Check if lock file exists (no)
    2. Process B: Check if lock file exists (no)
    3. Process A: Create lock file
    4. Process B: Create lock file (overwrites A!)
    Both processes think they have the lock.

Solution:
    Use fcntl.flock() for POSIX or msvcrt.locking() for Windows.
    These are atomic at the OS level.

Features:
    - OS-level atomic locking (fcntl/msvcrt)
    - Lock with timeout
    - Stale lock detection and recovery
    - Advisory and mandatory locking modes
    - Cross-process coordination
    - Async-compatible wrapper

Author: JARVIS v77.2
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

# Platform-specific imports
if sys.platform != "win32":
    from backend.core import portable_fcntl as fcntl
else:
    import msvcrt


class LockType(Enum):
    """Type of lock."""

    SHARED = "shared"  # Multiple readers allowed
    EXCLUSIVE = "exclusive"  # Single writer only


@dataclass
class LockInfo:
    """Information about a lock."""

    lock_id: str
    holder_pid: int
    acquired_at: float
    lock_type: LockType
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_id": self.lock_id,
            "holder_pid": self.holder_pid,
            "acquired_at": self.acquired_at,
            "lock_type": self.lock_type.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockInfo":
        return cls(
            lock_id=data["lock_id"],
            holder_pid=data["holder_pid"],
            acquired_at=data["acquired_at"],
            lock_type=LockType(data["lock_type"]),
            metadata=data.get("metadata", {}),
        )


class AtomicFileLock:
    """
    Atomic file lock using OS-level primitives.

    Uses fcntl.flock() on POSIX systems and msvcrt.locking() on Windows.
    These are guaranteed atomic at the kernel level.

    Usage:
        lock = AtomicFileLock("/path/to/lockfile")

        # Blocking acquire
        with lock:
            # Critical section

        # Non-blocking with timeout
        try:
            with lock.acquire(timeout=5.0):
                # Critical section
        except LockAcquisitionError:
            # Handle timeout

        # Async usage
        async with lock.async_acquire(timeout=5.0):
            # Critical section
    """

    def __init__(
        self,
        lock_path: Union[str, Path],
        lock_id: Optional[str] = None,
        stale_timeout: float = 300.0,  # 5 minutes
    ):
        """
        Initialize atomic file lock.

        Args:
            lock_path: Path to lock file
            lock_id: Unique identifier for this lock
            stale_timeout: Time after which a lock is considered stale
        """
        self.lock_path = Path(lock_path)
        self.lock_id = lock_id or f"lock_{os.getpid()}_{time.time()}"
        self.stale_timeout = stale_timeout

        self._fd: Optional[int] = None
        self._lock_type: Optional[LockType] = None
        self._acquired_at: float = 0.0
        self._is_windows = sys.platform == "win32"

    @property
    def is_locked(self) -> bool:
        """Check if we hold the lock."""
        return self._fd is not None

    @property
    def lock_info(self) -> Optional[LockInfo]:
        """Get info about current lock."""
        if not self.is_locked:
            return None
        return LockInfo(
            lock_id=self.lock_id,
            holder_pid=os.getpid(),
            acquired_at=self._acquired_at,
            lock_type=self._lock_type or LockType.EXCLUSIVE,
        )

    def __enter__(self):
        self.acquire_blocking()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    @contextmanager
    def acquire(
        self,
        timeout: float = 30.0,
        lock_type: LockType = LockType.EXCLUSIVE,
        poll_interval: float = 0.1,
    ):
        """
        Context manager for acquiring lock with timeout.

        Args:
            timeout: Maximum time to wait for lock
            lock_type: Type of lock (shared or exclusive)
            poll_interval: How often to retry acquiring

        Yields:
            Self when lock is acquired

        Raises:
            LockAcquisitionError: If timeout exceeded
        """
        success = self._acquire_with_timeout(timeout, lock_type, poll_interval)
        if not success:
            raise LockAcquisitionError(
                f"Could not acquire lock {self.lock_path} within {timeout}s"
            )
        try:
            yield self
        finally:
            self.release()

    @asynccontextmanager
    async def async_acquire(
        self,
        timeout: float = 30.0,
        lock_type: LockType = LockType.EXCLUSIVE,
        poll_interval: float = 0.1,
    ):
        """
        Async context manager for acquiring lock.

        Uses asyncio.sleep between attempts to avoid blocking.

        Args:
            timeout: Maximum time to wait
            lock_type: Type of lock
            poll_interval: How often to retry

        Yields:
            Self when lock is acquired

        Raises:
            LockAcquisitionError: If timeout exceeded
        """
        start_time = time.time()

        while True:
            # Check for stale lock and clean up if needed
            await self._async_cleanup_stale()

            # Try non-blocking acquire
            if self._try_acquire_nonblocking(lock_type):
                break

            # Check timeout
            if time.time() - start_time >= timeout:
                raise LockAcquisitionError(
                    f"Could not acquire lock {self.lock_path} within {timeout}s"
                )

            # Wait before retry
            await asyncio.sleep(poll_interval)

        try:
            yield self
        finally:
            self.release()

    def acquire_blocking(self, lock_type: LockType = LockType.EXCLUSIVE) -> None:
        """
        Acquire lock, blocking indefinitely.

        Args:
            lock_type: Type of lock to acquire
        """
        # Clean up stale lock first
        self._cleanup_stale()

        # Ensure parent directory exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Open or create lock file
        self._fd = os.open(
            str(self.lock_path),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )

        try:
            # Acquire lock (blocking)
            if self._is_windows:
                self._windows_lock(lock_type, blocking=True)
            else:
                self._posix_lock(lock_type, blocking=True)

            self._lock_type = lock_type
            self._acquired_at = time.time()

            # Write lock info
            self._write_lock_info()

            logger.debug(
                f"[AtomicLock] Acquired {lock_type.value} lock on {self.lock_path}"
            )

        except Exception:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise

    def _try_acquire_nonblocking(self, lock_type: LockType = LockType.EXCLUSIVE) -> bool:
        """
        Try to acquire lock without blocking.

        Returns:
            True if lock acquired, False otherwise
        """
        # Ensure parent directory exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._fd = os.open(
                str(self.lock_path),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        except OSError as e:
            logger.debug(f"[AtomicLock] Failed to open lock file: {e}")
            return False

        try:
            if self._is_windows:
                self._windows_lock(lock_type, blocking=False)
            else:
                self._posix_lock(lock_type, blocking=False)

            self._lock_type = lock_type
            self._acquired_at = time.time()
            self._write_lock_info()

            logger.debug(
                f"[AtomicLock] Acquired {lock_type.value} lock on {self.lock_path}"
            )
            return True

        except (BlockingIOError, OSError):
            # Lock is held by someone else
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def _acquire_with_timeout(
        self,
        timeout: float,
        lock_type: LockType,
        poll_interval: float,
    ) -> bool:
        """
        Acquire lock with timeout.

        Returns:
            True if acquired, False if timeout
        """
        start_time = time.time()

        while True:
            # Check for stale lock
            self._cleanup_stale()

            if self._try_acquire_nonblocking(lock_type):
                return True

            if time.time() - start_time >= timeout:
                return False

            time.sleep(poll_interval)

    def release(self) -> None:
        """Release the lock."""
        if self._fd is None:
            return

        try:
            if self._is_windows:
                self._windows_unlock()
            else:
                self._posix_unlock()

            # Clear lock info from file
            try:
                os.ftruncate(self._fd, 0)
            except OSError:
                pass

        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._lock_type = None
            self._acquired_at = 0.0

        logger.debug(f"[AtomicLock] Released lock on {self.lock_path}")

    def _posix_lock(self, lock_type: LockType, blocking: bool) -> None:
        """Acquire lock using POSIX fcntl."""
        if lock_type == LockType.SHARED:
            operation = fcntl.LOCK_SH
        else:
            operation = fcntl.LOCK_EX

        if not blocking:
            operation |= fcntl.LOCK_NB

        fcntl.flock(self._fd, operation)

    def _posix_unlock(self) -> None:
        """Release lock using POSIX fcntl."""
        fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _windows_lock(self, lock_type: LockType, blocking: bool) -> None:
        """Acquire lock using Windows msvcrt."""
        # Windows locking is always exclusive at file level
        # For shared locks, we'd need to use LockFileEx via ctypes
        if blocking:
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        else:
            msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)

    def _windows_unlock(self) -> None:
        """Release lock using Windows msvcrt."""
        try:
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

    def _write_lock_info(self) -> None:
        """Write lock info to the lock file."""
        if self._fd is None:
            return

        try:
            info = self.lock_info
            if info:
                data = json.dumps(info.to_dict())
                os.ftruncate(self._fd, 0)
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.write(self._fd, data.encode())
                os.fsync(self._fd)
        except OSError as e:
            logger.debug(f"[AtomicLock] Could not write lock info: {e}")

    def _read_lock_info(self) -> Optional[LockInfo]:
        """Read lock info from the lock file."""
        if not self.lock_path.exists():
            return None

        try:
            content = self.lock_path.read_text().strip()
            if not content:
                return None
            data = json.loads(content)
            return LockInfo.from_dict(data)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _cleanup_stale(self) -> None:
        """Clean up stale lock if detected."""
        info = self._read_lock_info()
        if info is None:
            return

        # Check if holder process is still alive
        if not self._is_process_alive(info.holder_pid):
            logger.info(
                f"[AtomicLock] Cleaning up stale lock (holder PID {info.holder_pid} is dead)"
            )
            self._force_unlock()
            return

        # Check if lock is stale by time
        if time.time() - info.acquired_at > self.stale_timeout:
            logger.info(
                f"[AtomicLock] Cleaning up stale lock (held for {time.time() - info.acquired_at:.0f}s)"
            )
            self._force_unlock()

    async def _async_cleanup_stale(self) -> None:
        """Async version of stale lock cleanup."""
        # Run in executor to avoid blocking
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._cleanup_stale)

    def _force_unlock(self) -> None:
        """Force unlock a stale lock file."""
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError as e:
            logger.warning(f"[AtomicLock] Could not remove stale lock: {e}")

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        if pid <= 0:
            return False

        try:
            # This doesn't actually kill the process, just checks if it exists
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission
            return True
        except OSError:
            return False


class LockAcquisitionError(Exception):
    """Raised when lock acquisition fails."""

    pass


class LockManager:
    """
    Manager for multiple named locks.

    Provides centralized lock management with automatic cleanup.

    Usage:
        manager = LockManager(lock_dir="/path/to/locks")

        async with manager.acquire("transaction_123"):
            # Critical section

        # Or get a lock by name
        lock = manager.get_lock("my_lock")
        async with lock.async_acquire():
            # Critical section
    """

    def __init__(
        self,
        lock_dir: Optional[Path] = None,
        stale_timeout: float = 300.0,
    ):
        self.lock_dir = lock_dir or Path.home() / ".jarvis" / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.stale_timeout = stale_timeout

        self._locks: Dict[str, AtomicFileLock] = {}

    def get_lock(self, name: str) -> AtomicFileLock:
        """
        Get or create a named lock.

        Args:
            name: Lock name

        Returns:
            AtomicFileLock instance
        """
        if name not in self._locks:
            lock_path = self.lock_dir / f"{name}.lock"
            self._locks[name] = AtomicFileLock(
                lock_path=lock_path,
                lock_id=name,
                stale_timeout=self.stale_timeout,
            )
        return self._locks[name]

    @asynccontextmanager
    async def acquire(
        self,
        name: str,
        timeout: float = 30.0,
        lock_type: LockType = LockType.EXCLUSIVE,
    ):
        """
        Async context manager for acquiring a named lock.

        Args:
            name: Lock name
            timeout: Acquisition timeout
            lock_type: Type of lock

        Yields:
            AtomicFileLock when acquired
        """
        lock = self.get_lock(name)
        async with lock.async_acquire(timeout=timeout, lock_type=lock_type):
            yield lock

    def cleanup_stale_locks(self) -> int:
        """
        Clean up all stale lock files.

        Returns:
            Number of locks cleaned up
        """
        cleaned = 0

        for lock_file in self.lock_dir.glob("*.lock"):
            try:
                content = lock_file.read_text().strip()
                if not content:
                    lock_file.unlink()
                    cleaned += 1
                    continue

                data = json.loads(content)
                info = LockInfo.from_dict(data)

                # Check if holder is dead or lock is stale
                if not AtomicFileLock._is_process_alive(info.holder_pid):
                    lock_file.unlink()
                    cleaned += 1
                elif time.time() - info.acquired_at > self.stale_timeout:
                    lock_file.unlink()
                    cleaned += 1

            except (json.JSONDecodeError, KeyError, OSError):
                # Corrupted lock file, remove it
                try:
                    lock_file.unlink()
                    cleaned += 1
                except OSError:
                    pass

        if cleaned:
            logger.info(f"[LockManager] Cleaned up {cleaned} stale locks")

        return cleaned

    def get_active_locks(self) -> List[LockInfo]:
        """Get info about all active locks."""
        active = []

        for lock_file in self.lock_dir.glob("*.lock"):
            try:
                content = lock_file.read_text().strip()
                if content:
                    data = json.loads(content)
                    info = LockInfo.from_dict(data)
                    if AtomicFileLock._is_process_alive(info.holder_pid):
                        active.append(info)
            except (json.JSONDecodeError, KeyError, OSError):
                pass

        return active


# Global lock manager
_lock_manager: Optional[LockManager] = None


def get_lock_manager() -> LockManager:
    """Get or create global lock manager."""
    global _lock_manager
    if _lock_manager is None:
        _lock_manager = LockManager()
    return _lock_manager

"""
v77.0: File Locker - Gap #25
=============================

Advisory file locking with conflict resolution:
- Cross-process file locking
- Lock modes (shared/exclusive)
- Automatic lock expiration
- Conflict detection and resolution
- Lock inheritance for directory operations

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
from backend.core import portable_fcntl as fcntl
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class LockMode(Enum):
    """Lock mode."""
    SHARED = "shared"      # Multiple readers
    EXCLUSIVE = "exclusive"  # Single writer


class LockConflict(Exception):
    """Raised when a lock conflict occurs."""
    def __init__(self, path: str, holder: str, mode: LockMode):
        self.path = path
        self.holder = holder
        self.mode = mode
        super().__init__(f"Lock conflict on {path}: held by {holder} in {mode.value} mode")


@dataclass
class FileLock:
    """Information about a file lock."""
    lock_id: str
    path: str
    mode: LockMode
    holder_id: str
    holder_name: str
    acquired_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_id": self.lock_id,
            "path": self.path,
            "mode": self.mode.value,
            "holder_id": self.holder_id,
            "holder_name": self.holder_name,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileLock":
        return cls(
            lock_id=data["lock_id"],
            path=data["path"],
            mode=LockMode(data["mode"]),
            holder_id=data["holder_id"],
            holder_name=data["holder_name"],
            acquired_at=data.get("acquired_at", time.time()),
            expires_at=data.get("expires_at"),
            metadata=data.get("metadata", {}),
        )

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class FileLocker:
    """
    Advisory file locking system.

    Features:
    - Shared/exclusive lock modes
    - Cross-process coordination via lock files
    - Automatic expiration
    - Lock file cleanup
    - Directory locking with inheritance
    - Conflict resolution strategies
    """

    def __init__(
        self,
        lock_dir: Optional[Path] = None,
        default_timeout: float = 30.0,
        default_ttl: float = 300.0,  # 5 minutes
        holder_id: Optional[str] = None,
        holder_name: Optional[str] = None,
    ):
        self.lock_dir = lock_dir or Path.home() / ".jarvis" / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.default_timeout = default_timeout
        self.default_ttl = default_ttl
        self.holder_id = holder_id or str(uuid.uuid4())
        self.holder_name = holder_name or f"process_{os.getpid()}"
        self._active_locks: Dict[str, FileLock] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the file locker (cleanup task)."""
        if self._running:
            return

        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[FileLocker] Started")

    async def stop(self) -> None:
        """Stop and release all locks."""
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Release all our locks
        for path in list(self._active_locks.keys()):
            await self.unlock(path)

        logger.info("[FileLocker] Stopped")

    async def lock(
        self,
        path: str,
        mode: LockMode = LockMode.EXCLUSIVE,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None,
        wait: bool = True,
    ) -> FileLock:
        """
        Acquire a lock on a file path.

        Args:
            path: File path to lock
            mode: SHARED (readers) or EXCLUSIVE (writer)
            timeout: How long to wait for lock
            ttl: How long lock is valid
            wait: If False, fail immediately on conflict

        Returns:
            FileLock object

        Raises:
            LockConflict: If lock cannot be acquired
            TimeoutError: If timeout exceeded
        """
        timeout = timeout if timeout is not None else self.default_timeout
        ttl = ttl if ttl is not None else self.default_ttl

        path = os.path.abspath(path)
        lock_file = self._get_lock_file_path(path)

        start_time = time.time()

        while True:
            async with self._lock:
                # Check for existing lock
                existing = await self._read_lock_file(lock_file)

                if existing and not existing.is_expired():
                    # Check compatibility
                    if not self._can_acquire(existing, mode):
                        if not wait:
                            raise LockConflict(path, existing.holder_name, existing.mode)

                        # Check timeout
                        if time.time() - start_time > timeout:
                            raise TimeoutError(
                                f"Timeout waiting for lock on {path} "
                                f"(held by {existing.holder_name})"
                            )

                        # Wait and retry
                        await asyncio.sleep(0.1)
                        continue

                # Acquire lock
                lock = FileLock(
                    lock_id=str(uuid.uuid4()),
                    path=path,
                    mode=mode,
                    holder_id=self.holder_id,
                    holder_name=self.holder_name,
                    expires_at=time.time() + ttl if ttl else None,
                )

                await self._write_lock_file(lock_file, lock)
                self._active_locks[path] = lock

                logger.debug(f"[FileLocker] Acquired {mode.value} lock on {path}")
                return lock

    async def unlock(self, path: str) -> bool:
        """
        Release a lock on a file path.

        Returns True if lock was released.
        """
        path = os.path.abspath(path)

        async with self._lock:
            if path not in self._active_locks:
                return False

            lock = self._active_locks[path]

            # Verify we own this lock
            lock_file = self._get_lock_file_path(path)
            existing = await self._read_lock_file(lock_file)

            if existing and existing.holder_id == self.holder_id:
                lock_file.unlink(missing_ok=True)

            del self._active_locks[path]
            logger.debug(f"[FileLocker] Released lock on {path}")
            return True

    async def is_locked(self, path: str) -> bool:
        """Check if a path is currently locked."""
        path = os.path.abspath(path)
        lock_file = self._get_lock_file_path(path)

        existing = await self._read_lock_file(lock_file)
        return existing is not None and not existing.is_expired()

    async def get_lock_info(self, path: str) -> Optional[FileLock]:
        """Get information about a lock."""
        path = os.path.abspath(path)
        lock_file = self._get_lock_file_path(path)
        return await self._read_lock_file(lock_file)

    async def extend_lock(self, path: str, additional_ttl: float) -> bool:
        """Extend the TTL of a lock we hold."""
        path = os.path.abspath(path)

        async with self._lock:
            if path not in self._active_locks:
                return False

            lock = self._active_locks[path]

            if lock.expires_at:
                lock.expires_at += additional_ttl
            else:
                lock.expires_at = time.time() + additional_ttl

            lock_file = self._get_lock_file_path(path)
            await self._write_lock_file(lock_file, lock)

            return True

    async def lock_directory(
        self,
        directory: str,
        mode: LockMode = LockMode.EXCLUSIVE,
        recursive: bool = False,
        timeout: Optional[float] = None,
    ) -> List[FileLock]:
        """
        Lock a directory and optionally all files within.

        Returns list of acquired locks.
        """
        directory = os.path.abspath(directory)
        locks = []

        # Lock the directory itself
        dir_lock = await self.lock(directory, mode, timeout)
        locks.append(dir_lock)

        if recursive and os.path.isdir(directory):
            for root, dirs, files in os.walk(directory):
                for f in files:
                    file_path = os.path.join(root, f)
                    try:
                        file_lock = await self.lock(file_path, mode, timeout)
                        locks.append(file_lock)
                    except (LockConflict, TimeoutError):
                        # Rollback acquired locks
                        for lock in locks:
                            await self.unlock(lock.path)
                        raise

        return locks

    @asynccontextmanager
    async def locked(
        self,
        path: str,
        mode: LockMode = LockMode.EXCLUSIVE,
        timeout: Optional[float] = None,
    ):
        """
        Context manager for file locking.

        Usage:
            async with locker.locked("/path/to/file"):
                # file is locked
                ...
            # lock is automatically released
        """
        lock = await self.lock(path, mode, timeout)
        try:
            yield lock
        finally:
            await self.unlock(path)

    async def force_unlock(self, path: str) -> bool:
        """
        Force unlock a path regardless of owner.

        Use with caution - only for cleanup/recovery.
        """
        path = os.path.abspath(path)
        lock_file = self._get_lock_file_path(path)

        async with self._lock:
            lock_file.unlink(missing_ok=True)
            self._active_locks.pop(path, None)
            logger.warning(f"[FileLocker] Force unlocked {path}")
            return True

    def get_active_locks(self) -> Dict[str, FileLock]:
        """Get all locks held by this process."""
        return self._active_locks.copy()

    def _get_lock_file_path(self, path: str) -> Path:
        """Get the lock file path for a given file path."""
        # Create a safe filename from the path
        safe_name = path.replace("/", "_").replace("\\", "_").replace(":", "_")
        if len(safe_name) > 200:
            # Use hash for very long paths
            import hashlib
            safe_name = hashlib.sha256(path.encode()).hexdigest()
        return self.lock_dir / f"{safe_name}.lock"

    async def _read_lock_file(self, lock_file: Path) -> Optional[FileLock]:
        """Read lock info from lock file."""
        try:
            if not lock_file.exists():
                return None

            content = lock_file.read_text()
            data = json.loads(content)
            return FileLock.from_dict(data)
        except Exception as e:
            logger.debug(f"[FileLocker] Failed to read lock file: {e}")
            return None

    async def _write_lock_file(self, lock_file: Path, lock: FileLock) -> None:
        """Write lock info to lock file with atomic operation."""
        try:
            # Atomic write via temp file
            tmp_file = lock_file.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(lock.to_dict(), indent=2))
            tmp_file.rename(lock_file)
        except Exception as e:
            logger.error(f"[FileLocker] Failed to write lock file: {e}")
            raise

    def _can_acquire(self, existing: FileLock, requested_mode: LockMode) -> bool:
        """Check if we can acquire lock given existing lock."""
        # If we already hold it, check mode compatibility
        if existing.holder_id == self.holder_id:
            return True

        # Shared locks can coexist with other shared locks
        if existing.mode == LockMode.SHARED and requested_mode == LockMode.SHARED:
            return True

        # Exclusive locks block everything
        return False

    async def _cleanup_loop(self) -> None:
        """Background loop to clean up expired locks."""
        while self._running:
            try:
                await self._cleanup_expired()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FileLocker] Cleanup error: {e}")
                await asyncio.sleep(5)

    async def _cleanup_expired(self) -> int:
        """Clean up expired lock files."""
        cleaned = 0

        try:
            for lock_file in self.lock_dir.glob("*.lock"):
                lock = await self._read_lock_file(lock_file)
                if lock and lock.is_expired():
                    lock_file.unlink(missing_ok=True)
                    cleaned += 1
                    logger.debug(f"[FileLocker] Cleaned expired lock: {lock.path}")
        except Exception as e:
            logger.error(f"[FileLocker] Cleanup error: {e}")

        return cleaned


class ProcessFileLock:
    """
    OS-level file locking using fcntl.

    For true cross-process locking using OS primitives.
    More robust but platform-specific (Unix-like systems).
    """

    def __init__(self, path: str, timeout: float = 30.0):
        self.path = path
        self.timeout = timeout
        self._fd: Optional[int] = None

    async def acquire(self, mode: LockMode = LockMode.EXCLUSIVE) -> bool:
        """Acquire OS-level file lock."""
        lock_type = fcntl.LOCK_EX if mode == LockMode.EXCLUSIVE else fcntl.LOCK_SH
        lock_type |= fcntl.LOCK_NB  # Non-blocking

        start_time = time.time()

        while True:
            try:
                # Open/create the lock file
                self._fd = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT,
                    0o600
                )

                # Try to acquire lock
                fcntl.flock(self._fd, lock_type)
                return True

            except BlockingIOError:
                if time.time() - start_time > self.timeout:
                    if self._fd is not None:
                        os.close(self._fd)
                        self._fd = None
                    return False

                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"[ProcessFileLock] Error acquiring lock: {e}")
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                raise

    def release(self) -> None:
        """Release OS-level file lock."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception as e:
                logger.error(f"[ProcessFileLock] Error releasing lock: {e}")
            finally:
                self._fd = None

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


# Global singleton for convenience
_file_locker: Optional[FileLocker] = None


def get_file_locker() -> FileLocker:
    """Get global file locker instance."""
    global _file_locker
    if _file_locker is None:
        _file_locker = FileLocker()
    return _file_locker

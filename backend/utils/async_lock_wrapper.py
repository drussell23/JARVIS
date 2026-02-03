"""
Async Lock Wrapper for RobustFileLock with Startup Executor
============================================================

This module provides async wrappers around RobustFileLock that:
1. Use the dedicated startup executor (not the default executor)
2. Validate timeouts against configurable bounds
3. Check for stale PIDs before timing out (dead process detection)
4. Provide a centralized CrossRepoLockManager for standard lock paths

The wrapper delegates to RobustFileLock for actual locking mechanics but adds:
- Stale lock detection: If a lock holder's PID is dead, clean up and retry
- Timeout validation: Prevent excessively short or long timeouts
- Executor isolation: Use bounded startup executor to prevent thread pool exhaustion

Following 35-point checklist items:
- Item 4: Cross-repo lock timeouts (env-driven)
- Item 7: Single lock abstraction (wraps RobustFileLock)
- Item 30: Stale lock/PID check (detect dead processes)
- Item 32: Use dedicated startup executor

Usage:
    from backend.utils.async_lock_wrapper import StartupFileLock, CrossRepoLockManager

    # Direct use
    lock = StartupFileLock("my_resource", source="my_service")
    async with lock:
        # Protected code
        pass

    # Or with manager for standard paths
    lock_mgr = CrossRepoLockManager()
    async with await lock_mgr.acquire_lock("vbia_state") as acquired:
        if acquired:
            # Protected code
            pass
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from backend.core.robust_file_lock import RobustFileLock
from backend.utils.async_startup import _STARTUP_EXECUTOR

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION (Environment-Driven)
# =============================================================================

# Maximum allowed lock timeout (prevents excessive waits)
MAX_LOCK_TIMEOUT = float(os.environ.get("JARVIS_MAX_LOCK_TIMEOUT", "300.0"))

# Minimum allowed lock timeout (prevents spin-lock behavior)
MIN_LOCK_TIMEOUT = float(os.environ.get("JARVIS_MIN_LOCK_TIMEOUT", "0.1"))

# Default lock timeout for StartupFileLock
DEFAULT_LOCK_TIMEOUT = float(os.environ.get("JARVIS_DEFAULT_LOCK_TIMEOUT", "5.0"))

# How long to wait when retrying after stale lock removal
STALE_LOCK_RETRY_TIMEOUT = float(os.environ.get("JARVIS_STALE_LOCK_RETRY_TIMEOUT", "1.0"))


# =============================================================================
# PID CHECKING UTILITIES
# =============================================================================


def _is_pid_alive(pid: int) -> bool:
    """
    Check if a process with the given PID is alive.

    Uses os.kill with signal 0 (doesn't actually send a signal, just checks existence).

    Args:
        pid: Process ID to check

    Returns:
        True if process exists and we can signal it, False otherwise
    """
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Process doesn't exist
        return False
    except PermissionError:
        # Process exists but we can't signal it (different user)
        # Treat as alive since it's still running
        return True
    except OSError:
        # Other OS error, treat as dead
        return False


def _read_lock_metadata_sync(lock_file: Path) -> Optional[Dict[str, Any]]:
    """
    Read metadata from a lock file (blocking, run in executor).

    Args:
        lock_file: Path to the lock file

    Returns:
        Parsed metadata dict, or None if file doesn't exist or is invalid
    """
    try:
        if not lock_file.exists():
            return None
        with open(lock_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _remove_lock_file_sync(lock_file: Path) -> bool:
    """
    Remove a lock file (blocking, run in executor).

    Args:
        lock_file: Path to the lock file to remove

    Returns:
        True if removed or didn't exist, False on error
    """
    try:
        if lock_file.exists():
            lock_file.unlink()
            logger.info(f"[AsyncLockWrapper] Removed stale lock file: {lock_file}")
        return True
    except OSError as e:
        logger.warning(f"[AsyncLockWrapper] Failed to remove stale lock: {lock_file}: {e}")
        return False


# =============================================================================
# STARTUP FILE LOCK WRAPPER
# =============================================================================


class StartupFileLock:
    """
    Async wrapper around RobustFileLock that uses the dedicated startup executor
    and provides stale PID detection.

    This class:
    1. Delegates actual locking to RobustFileLock
    2. Uses the bounded startup executor instead of default executor
    3. Validates timeouts against configurable bounds
    4. Detects and cleans up stale locks (dead process holders)

    The wrapper is designed for use during JARVIS startup where we need:
    - Predictable executor usage (bounded thread pool)
    - Recovery from crashed processes that left locks behind
    - Reasonable timeout bounds to prevent hangs

    Example:
        lock = StartupFileLock("vbia_state", source="jarvis")

        # Context manager (recommended)
        async with lock as acquired:
            if acquired:
                # Protected code
                pass

        # Or manual control
        if await lock.acquire(timeout_s=10.0):
            try:
                # Protected code
                pass
            finally:
                await lock.release()
    """

    def __init__(self, lock_name: str, source: str = "jarvis"):
        """
        Initialize the wrapper.

        Args:
            lock_name: Unique name for this lock (e.g., "vbia_state")
            source: Identifier for the process holding the lock (for debugging)
        """
        self._lock = RobustFileLock(lock_name, source)
        self._lock_name = lock_name
        self._source = source
        self._acquired = False

        # Compute lock file path (same logic as RobustFileLock)
        lock_dir_raw = os.environ.get("JARVIS_LOCK_DIR", "~/.jarvis/cross_repo/locks")
        self._lock_dir = Path(os.path.expanduser(os.path.expandvars(lock_dir_raw)))
        self._lock_file = self._lock_dir / f"{lock_name}.lock"

    @property
    def lock_name(self) -> str:
        """Get the lock name."""
        return self._lock_name

    @property
    def is_acquired(self) -> bool:
        """Check if this wrapper has successfully acquired the lock."""
        return self._acquired

    def _validate_timeout(self, timeout_s: float) -> None:
        """
        Validate timeout is within acceptable bounds.

        Args:
            timeout_s: Timeout in seconds to validate

        Raises:
            ValueError: If timeout is out of bounds
        """
        if timeout_s <= 0:
            raise ValueError(
                f"Invalid timeout: {timeout_s}s. Timeout must be positive."
            )
        if timeout_s < MIN_LOCK_TIMEOUT:
            raise ValueError(
                f"Invalid timeout: {timeout_s}s. Minimum allowed is {MIN_LOCK_TIMEOUT}s."
            )
        if timeout_s > MAX_LOCK_TIMEOUT:
            raise ValueError(
                f"Invalid timeout: {timeout_s}s. Maximum allowed is {MAX_LOCK_TIMEOUT}s."
            )

    async def _is_holder_stale(self) -> bool:
        """
        Check if the current lock holder's PID is dead (stale lock).

        Returns:
            True if lock file exists, has a valid owner_pid, and that PID is dead.
            False otherwise (no lock file, no PID, or PID is alive).
        """
        loop = asyncio.get_running_loop()

        # Read metadata in executor
        metadata = await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _read_lock_metadata_sync,
            self._lock_file,
        )

        if metadata is None:
            return False

        owner_pid = metadata.get("owner_pid")
        if owner_pid is None:
            return False

        # Check if PID is alive in executor
        is_alive = await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _is_pid_alive,
            int(owner_pid),
        )

        if not is_alive:
            logger.warning(
                f"[AsyncLockWrapper] Stale lock detected: {self._lock_name} "
                f"held by dead PID {owner_pid} (source: {metadata.get('source', 'unknown')})"
            )
            return True

        return False

    async def _remove_stale_lock(self) -> bool:
        """
        Remove a stale lock file.

        This should only be called after confirming the holder PID is dead.

        Returns:
            True if lock was removed or didn't exist, False on error
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _remove_lock_file_sync,
            self._lock_file,
        )

    async def acquire(self, timeout_s: Optional[float] = None) -> bool:
        """
        Acquire the lock with timeout.

        Uses the dedicated startup executor for all blocking operations.
        If the initial acquire times out, checks for stale locks and retries once.

        Args:
            timeout_s: Timeout in seconds. Defaults to JARVIS_DEFAULT_LOCK_TIMEOUT.
                       Must be between MIN_LOCK_TIMEOUT and MAX_LOCK_TIMEOUT.

        Returns:
            True if lock was acquired, False on timeout or error

        Raises:
            ValueError: If timeout is invalid (out of bounds)
            RuntimeError: If same process already holds this lock
        """
        if timeout_s is None:
            timeout_s = DEFAULT_LOCK_TIMEOUT

        # Validate timeout
        self._validate_timeout(timeout_s)

        # Attempt to acquire using underlying RobustFileLock
        acquired = await self._lock.acquire(timeout_s=timeout_s)

        if acquired:
            self._acquired = True
            logger.debug(f"[AsyncLockWrapper] Acquired: {self._lock_name}")
            return True

        # Acquisition failed - check for stale lock
        logger.debug(f"[AsyncLockWrapper] Initial acquire timed out for {self._lock_name}, checking for stale lock")

        if await self._is_holder_stale():
            # Holder is dead, remove stale lock and retry
            if await self._remove_stale_lock():
                # Retry with short timeout
                retry_timeout = min(STALE_LOCK_RETRY_TIMEOUT, timeout_s)
                logger.info(
                    f"[AsyncLockWrapper] Retrying acquire after stale lock removal: {self._lock_name}"
                )
                acquired = await self._lock.acquire(timeout_s=retry_timeout)

                if acquired:
                    self._acquired = True
                    logger.debug(f"[AsyncLockWrapper] Acquired after stale lock removal: {self._lock_name}")
                    return True

        logger.warning(f"[AsyncLockWrapper] Failed to acquire: {self._lock_name}")
        return False

    async def release(self) -> None:
        """
        Release the lock.

        Safe to call multiple times.
        """
        if self._acquired:
            await self._lock.release()
            self._acquired = False
            logger.debug(f"[AsyncLockWrapper] Released: {self._lock_name}")

    async def __aenter__(self) -> bool:
        """
        Async context manager entry.

        Returns:
            True if lock was acquired, False on timeout
        """
        return await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Async context manager exit - always releases lock.
        """
        await self.release()


# =============================================================================
# CROSS-REPO LOCK MANAGER
# =============================================================================


class CrossRepoLockManager:
    """
    Centralized manager for cross-repository locks.

    Provides:
    - Standard lock names for common resources
    - Easy lock acquisition with proper settings
    - Lock status inspection

    Standard lock names:
    - "vbia_state": Voice biometric authentication state
    - "voice_client": Shared voice client IPC
    - "startup_lock": Single-instance startup coordination
    - "config_sync": Configuration synchronization
    - "health_check": Health check coordination

    Example:
        manager = CrossRepoLockManager()

        # Using context manager
        async with manager.lock("vbia_state") as acquired:
            if acquired:
                # Protected code
                pass

        # Or acquire directly
        async with await manager.acquire_lock("voice_client", timeout_s=10.0) as acquired:
            if acquired:
                # Protected code
                pass
    """

    # Standard lock names and their purposes
    STANDARD_LOCKS = {
        "vbia_state": "Voice biometric authentication state",
        "voice_client": "Shared voice client IPC",
        "startup_lock": "Single-instance startup coordination",
        "config_sync": "Configuration synchronization",
        "health_check": "Health check coordination",
    }

    def __init__(self, source: str = "jarvis"):
        """
        Initialize the lock manager.

        Args:
            source: Identifier for the process using locks (for debugging)
        """
        self._source = source
        self._locks: Dict[str, StartupFileLock] = {}

    def _get_or_create_lock(self, lock_name: str) -> StartupFileLock:
        """
        Get or create a lock instance for the given name.

        Args:
            lock_name: Name of the lock

        Returns:
            StartupFileLock instance for this name
        """
        if lock_name not in self._locks:
            self._locks[lock_name] = StartupFileLock(lock_name, source=self._source)
        return self._locks[lock_name]

    @asynccontextmanager
    async def lock(self, lock_name: str, timeout_s: Optional[float] = None):
        """
        Acquire a lock using an async context manager.

        Args:
            lock_name: Name of the lock to acquire
            timeout_s: Optional timeout in seconds

        Yields:
            bool: True if lock was acquired, False otherwise

        Example:
            async with manager.lock("vbia_state") as acquired:
                if acquired:
                    # Protected code
                    pass
        """
        lock_instance = self._get_or_create_lock(lock_name)

        try:
            acquired = await lock_instance.acquire(timeout_s=timeout_s)
            yield acquired
        finally:
            await lock_instance.release()

    @asynccontextmanager
    async def acquire_lock(self, lock_name: str, timeout_s: Optional[float] = None):
        """
        Alias for lock() for backwards compatibility.

        Args:
            lock_name: Name of the lock to acquire
            timeout_s: Optional timeout in seconds

        Yields:
            bool: True if lock was acquired, False otherwise
        """
        async with self.lock(lock_name, timeout_s=timeout_s) as acquired:
            yield acquired

    async def is_lock_held(self, lock_name: str) -> bool:
        """
        Check if a lock appears to be held by a live process.

        Note: This is a point-in-time check and may be stale by the time you act on it.

        Args:
            lock_name: Name of the lock to check

        Returns:
            True if lock file exists and holder PID is alive, False otherwise
        """
        lock_instance = self._get_or_create_lock(lock_name)

        # If we hold it, return True
        if lock_instance.is_acquired:
            return True

        # Check if someone else holds it
        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _read_lock_metadata_sync,
            lock_instance._lock_file,
        )

        if metadata is None:
            return False

        owner_pid = metadata.get("owner_pid")
        if owner_pid is None:
            return False

        is_alive = await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _is_pid_alive,
            int(owner_pid),
        )

        return is_alive

    async def get_lock_holder_info(self, lock_name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about the current lock holder.

        Args:
            lock_name: Name of the lock to check

        Returns:
            Dict with holder info if lock is held, None otherwise.
            Includes: owner_pid, owner_host, acquired_at, source, is_alive
        """
        lock_instance = self._get_or_create_lock(lock_name)

        loop = asyncio.get_running_loop()
        metadata = await loop.run_in_executor(
            _STARTUP_EXECUTOR,
            _read_lock_metadata_sync,
            lock_instance._lock_file,
        )

        if metadata is None:
            return None

        owner_pid = metadata.get("owner_pid")
        if owner_pid is not None:
            is_alive = await loop.run_in_executor(
                _STARTUP_EXECUTOR,
                _is_pid_alive,
                int(owner_pid),
            )
            metadata["is_alive"] = is_alive

        return metadata

    def list_standard_locks(self) -> Dict[str, str]:
        """
        Get the list of standard lock names and their purposes.

        Returns:
            Dict mapping lock name to description
        """
        return dict(self.STANDARD_LOCKS)


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Main wrapper class
    "StartupFileLock",
    # Lock manager
    "CrossRepoLockManager",
    # Configuration
    "MAX_LOCK_TIMEOUT",
    "MIN_LOCK_TIMEOUT",
    "DEFAULT_LOCK_TIMEOUT",
    "STALE_LOCK_RETRY_TIMEOUT",
]

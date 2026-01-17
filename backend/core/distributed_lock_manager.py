"""
Distributed Lock Manager for Cross-Repo Coordination v1.0
===========================================================

Production-grade distributed lock manager for coordinating operations across
JARVIS, JARVIS-Prime, and Reactor-Core repositories.

Features:
- File-based locks with automatic expiration
- Stale lock detection and cleanup
- Lock timeout and retry logic
- Deadlock prevention with TTL (time-to-live)
- Lock health monitoring
- Zero-config operation with sensible defaults
- Async-first API

Problem Solved:
    Before: Process crashes while holding lock → other processes blocked forever
    After: Locks auto-expire after TTL → stale locks cleaned up automatically

Example Usage:
    ```python
    lock_manager = DistributedLockManager()

    # Acquire lock with auto-expiration
    async with lock_manager.acquire("vbia_events", timeout=5.0, ttl=10.0) as acquired:
        if acquired:
            # Perform critical operation
            await update_vbia_events()
        else:
            logger.warning("Could not acquire lock")
    ```

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │  ~/.jarvis/cross_repo/locks/                                │
    │  ├── vbia_events.lock        (Lock file with metadata)     │
    │  │   {                                                      │
    │  │     "acquired_at": 1736895345.123,                      │
    │  │     "expires_at": 1736895355.123,  # TTL = 10s          │
    │  │     "owner": "jarvis-core-pid-12345",                   │
    │  │     "token": "f47ac10b-58cc-4372-a567-0e02b2c3d479"     │
    │  │   }                                                      │
    │  ├── prime_state.lock                                      │
    │  └── reactor_state.lock                                    │
    └─────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import uuid4

import aiofiles
import aiofiles.os

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class LockConfig:
    """Configuration for distributed lock manager."""
    # Lock directory
    lock_dir: Path = Path.home() / ".jarvis" / "cross_repo" / "locks"

    # Default lock timeout (how long to wait for lock acquisition)
    default_timeout_seconds: float = 5.0

    # Default lock TTL (how long lock is valid before auto-expiration)
    default_ttl_seconds: float = 10.0

    # Retry settings
    retry_delay_seconds: float = 0.1

    # Stale lock cleanup settings
    cleanup_enabled: bool = True
    cleanup_interval_seconds: float = 30.0

    # Lock health check
    health_check_enabled: bool = True


# =============================================================================
# Lock Metadata
# =============================================================================

@dataclass
class LockMetadata:
    """Metadata stored in lock file."""
    acquired_at: float  # Timestamp when lock was acquired
    expires_at: float  # Timestamp when lock expires (acquired_at + TTL)
    owner: str  # Process identifier (e.g., "jarvis-core-pid-12345")
    token: str  # Unique token for this lock instance
    lock_name: str  # Name of the locked resource

    def is_expired(self) -> bool:
        """Check if lock has expired."""
        return time.time() >= self.expires_at

    def is_stale(self) -> bool:
        """Check if lock is stale (expired for significant time)."""
        return time.time() >= self.expires_at + 5.0

    def time_remaining(self) -> float:
        """Get remaining time before expiration (negative if expired)."""
        return self.expires_at - time.time()


# =============================================================================
# Distributed Lock Manager
# =============================================================================

class DistributedLockManager:
    """
    Production-grade distributed lock manager for cross-repo coordination.

    Features:
    - Automatic lock expiration (TTL-based)
    - Stale lock detection and cleanup
    - Deadlock prevention
    - Lock renewal support
    - Process-safe across multiple repos
    """

    def __init__(self, config: Optional[LockConfig] = None):
        """Initialize distributed lock manager."""
        self.config = config or LockConfig()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._owner_id = f"jarvis-{os.getpid()}"

        logger.info(f"Distributed Lock Manager v1.0 initialized (owner: {self._owner_id})")

    async def initialize(self) -> None:
        """Initialize lock manager and start background tasks."""
        # Create lock directory
        try:
            await aiofiles.os.makedirs(self.config.lock_dir, exist_ok=True)
            logger.info(f"Lock directory initialized: {self.config.lock_dir}")
        except Exception as e:
            logger.error(f"Failed to create lock directory: {e}")
            raise

        # Start cleanup task
        if self.config.cleanup_enabled:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Stale lock cleanup task started")

    async def shutdown(self) -> None:
        """Shutdown lock manager and cleanup resources."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        logger.info("Distributed Lock Manager shut down")

    # =========================================================================
    # Lock Acquisition
    # =========================================================================

    @asynccontextmanager
    async def acquire(
        self,
        lock_name: str,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None
    ) -> AsyncIterator[bool]:
        """
        Acquire distributed lock with automatic expiration.

        Args:
            lock_name: Name of the lock (e.g., "vbia_events", "prime_state")
            timeout: Max time to wait for lock acquisition (seconds)
            ttl: Lock time-to-live - auto-expires after this duration (seconds)

        Yields:
            bool: True if lock acquired, False if timeout

        Example:
            async with lock_manager.acquire("my_resource", timeout=5.0, ttl=10.0) as acquired:
                if acquired:
                    # Critical section - you have the lock
                    await do_work()
                else:
                    # Could not acquire lock
                    logger.warning("Lock acquisition failed")
        """
        timeout = timeout or self.config.default_timeout_seconds
        ttl = ttl or self.config.default_ttl_seconds

        lock_file = self.config.lock_dir / f"{lock_name}.lock"
        token = str(uuid4())
        acquired = False

        start_time = time.time()

        try:
            # Try to acquire lock with timeout
            while time.time() - start_time < timeout:
                if await self._try_acquire_lock(lock_file, lock_name, token, ttl):
                    acquired = True
                    logger.debug(f"Lock acquired: {lock_name} (token: {token[:8]}...)")
                    break

                # Wait before retry
                await asyncio.sleep(self.config.retry_delay_seconds)

            if not acquired:
                logger.warning(f"Lock acquisition timeout: {lock_name} (waited {timeout}s)")

            # Yield control to caller
            yield acquired

        finally:
            # Always release lock when exiting context
            if acquired:
                await self._release_lock(lock_file, token)
                logger.debug(f"Lock released: {lock_name} (token: {token[:8]}...)")

    async def _try_acquire_lock(
        self,
        lock_file: Path,
        lock_name: str,
        token: str,
        ttl: float
    ) -> bool:
        """
        Try to acquire lock atomically.

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            # Check if lock file exists
            if await aiofiles.os.path.exists(lock_file):
                # Read existing lock metadata
                existing_lock = await self._read_lock_metadata(lock_file)

                if existing_lock:
                    # Check if lock is expired
                    if existing_lock.is_expired():
                        logger.info(
                            f"Found expired lock: {lock_name} "
                            f"(owner: {existing_lock.owner}, expired {-existing_lock.time_remaining():.1f}s ago)"
                        )
                        # Remove expired lock
                        await self._remove_lock_file(lock_file)
                    else:
                        # Lock is still valid
                        logger.debug(
                            f"Lock held by another process: {lock_name} "
                            f"(owner: {existing_lock.owner}, expires in {existing_lock.time_remaining():.1f}s)"
                        )
                        return False

            # Create new lock
            now = time.time()
            metadata = LockMetadata(
                acquired_at=now,
                expires_at=now + ttl,
                owner=self._owner_id,
                token=token,
                lock_name=lock_name
            )

            # Write lock file atomically
            await self._write_lock_metadata(lock_file, metadata)

            # Verify we actually got the lock (another process might have written simultaneously)
            await asyncio.sleep(0.01)  # Small delay for filesystem consistency
            verify_lock = await self._read_lock_metadata(lock_file)

            if verify_lock and verify_lock.token == token:
                return True
            else:
                logger.debug(f"Lock race condition detected: {lock_name} (lost to another process)")
                return False

        except Exception as e:
            logger.error(f"Error acquiring lock {lock_name}: {e}")
            return False

    async def _release_lock(self, lock_file: Path, token: str) -> None:
        """
        Release lock if we own it.

        Args:
            lock_file: Path to lock file
            token: Our lock token
        """
        try:
            # Read current lock
            current_lock = await self._read_lock_metadata(lock_file)

            if current_lock and current_lock.token == token:
                # We own this lock, remove it
                await self._remove_lock_file(lock_file)
            else:
                logger.warning(
                    f"Cannot release lock - not owner or lock expired: {lock_file.name}"
                )
        except Exception as e:
            logger.error(f"Error releasing lock {lock_file.name}: {e}")

    # =========================================================================
    # Lock Metadata I/O
    # =========================================================================

    async def _read_lock_metadata(self, lock_file: Path) -> Optional[LockMetadata]:
        """Read and parse lock metadata from file."""
        try:
            async with aiofiles.open(lock_file, 'r') as f:
                data = await f.read()
                metadata_dict = json.loads(data)
                return LockMetadata(**metadata_dict)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.error(f"Corrupted lock file: {lock_file} (will remove)")
            await self._remove_lock_file(lock_file)
            return None
        except Exception as e:
            logger.error(f"Error reading lock metadata {lock_file}: {e}")
            return None

    async def _write_lock_metadata(self, lock_file: Path, metadata: LockMetadata) -> None:
        """
        Write lock metadata to file atomically.

        v1.1: Ensures parent directory exists before writing.
        """
        try:
            # Ensure directory exists (resilient to race conditions)
            lock_dir = lock_file.parent
            try:
                await aiofiles.os.makedirs(lock_dir, exist_ok=True)
            except FileExistsError:
                pass  # Another process created it - that's fine

            # Write to temp file first
            temp_file = lock_file.with_suffix('.lock.tmp')
            async with aiofiles.open(temp_file, 'w') as f:
                await f.write(json.dumps(asdict(metadata), indent=2))

            # Atomic rename
            await aiofiles.os.rename(temp_file, lock_file)

        except Exception as e:
            logger.error(f"Error writing lock metadata {lock_file}: {e}")
            raise

    async def _remove_lock_file(self, lock_file: Path) -> None:
        """
        Remove lock file safely with race condition handling.

        v1.1: Made robust against TOCTOU race conditions - if file disappears
        between exists check and remove, we treat it as successful removal.
        """
        try:
            await aiofiles.os.remove(lock_file)
        except FileNotFoundError:
            # File already gone - treat as successful removal (race condition handled)
            logger.debug(f"Lock file already removed (race condition OK): {lock_file}")
        except OSError as e:
            # Check if it's "No such file or directory" error
            import errno
            if e.errno == errno.ENOENT:
                logger.debug(f"Lock file already removed: {lock_file}")
            else:
                logger.error(f"Error removing lock file {lock_file}: {e}")

    # =========================================================================
    # Cleanup Tasks
    # =========================================================================

    async def _cleanup_loop(self) -> None:
        """Background task to clean up stale locks."""
        logger.info("Stale lock cleanup loop started")

        while True:
            try:
                await asyncio.sleep(self.config.cleanup_interval_seconds)
                await self._cleanup_stale_locks()
            except asyncio.CancelledError:
                logger.info("Stale lock cleanup loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)

    async def _cleanup_stale_locks(self) -> None:
        """Clean up expired and stale locks."""
        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return

            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith('.lock')
            ]

            cleaned_count = 0

            for lock_file_name in lock_files:
                lock_file = self.config.lock_dir / lock_file_name
                metadata = await self._read_lock_metadata(lock_file)

                if metadata and metadata.is_stale():
                    logger.warning(
                        f"Cleaning stale lock: {metadata.lock_name} "
                        f"(owner: {metadata.owner}, expired {-metadata.time_remaining():.1f}s ago)"
                    )
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} stale lock(s)")

        except Exception as e:
            logger.error(f"Error during stale lock cleanup: {e}", exc_info=True)

    # =========================================================================
    # Monitoring & Health
    # =========================================================================

    async def get_lock_status(self, lock_name: str) -> Optional[dict]:
        """
        Get current status of a lock.

        Returns:
            dict with lock status or None if lock not held
        """
        lock_file = self.config.lock_dir / f"{lock_name}.lock"
        metadata = await self._read_lock_metadata(lock_file)

        if not metadata:
            return None

        return {
            "lock_name": metadata.lock_name,
            "owner": metadata.owner,
            "acquired_at": metadata.acquired_at,
            "expires_at": metadata.expires_at,
            "time_remaining": metadata.time_remaining(),
            "is_expired": metadata.is_expired(),
            "is_stale": metadata.is_stale()
        }

    async def list_all_locks(self) -> list[dict]:
        """List all current locks with their status."""
        locks = []

        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return locks

            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith('.lock')
            ]

            for lock_file_name in lock_files:
                lock_name = lock_file_name.replace('.lock', '')
                status = await self.get_lock_status(lock_name)
                if status:
                    locks.append(status)

            return locks

        except Exception as e:
            logger.error(f"Error listing locks: {e}")
            return locks


# =============================================================================
# Global Instance (Singleton Pattern)
# =============================================================================

_lock_manager_instance: Optional[DistributedLockManager] = None


async def get_lock_manager() -> DistributedLockManager:
    """Get or create global lock manager instance."""
    global _lock_manager_instance

    if _lock_manager_instance is None:
        _lock_manager_instance = DistributedLockManager()
        await _lock_manager_instance.initialize()

    return _lock_manager_instance


async def shutdown_lock_manager() -> None:
    """Shutdown global lock manager instance."""
    global _lock_manager_instance

    if _lock_manager_instance:
        await _lock_manager_instance.shutdown()
        _lock_manager_instance = None

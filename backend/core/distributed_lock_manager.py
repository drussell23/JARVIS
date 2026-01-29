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
from typing import AsyncIterator, Optional, Tuple
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
    """
    v96.0: Enhanced metadata stored in lock file.

    CRITICAL FIX for Problem 19 (Startup lock not released on crash):
    - Added process_start_time for PID reuse detection
    - Added process_name and process_cmdline for identity validation
    - Added machine_id for distributed environments
    - Enhanced stale detection with multiple signals

    Lock File Format (v96.0):
    {
        "acquired_at": 1736895345.123,
        "expires_at": 1736895355.123,
        "owner": "jarvis-12345-1736895300.5",  # pid-process_start_time
        "token": "f47ac10b-58cc-4372-a567...",
        "lock_name": "vbia_events",
        "process_start_time": 1736895300.5,  # v96.0
        "process_name": "python3",            # v96.0
        "process_cmdline": "python3 -m ...",  # v96.0
        "machine_id": "darwin-hostname"       # v96.0
    }
    """
    acquired_at: float  # Timestamp when lock was acquired
    expires_at: float  # Timestamp when lock expires (acquired_at + TTL)
    owner: str  # Process identifier (e.g., "jarvis-{pid}-{start_time}")
    token: str  # Unique token for this lock instance
    lock_name: str  # Name of the locked resource

    # v96.0: Enhanced process tracking for PID reuse detection
    process_start_time: float = 0.0    # Process creation timestamp
    process_name: str = ""              # Process name (e.g., "python3")
    process_cmdline: str = ""           # Command line (truncated)
    machine_id: str = ""                # Machine identifier

    def is_expired(self) -> bool:
        """Check if lock has expired."""
        return time.time() >= self.expires_at

    def is_stale(self) -> bool:
        """Check if lock is stale (expired for significant time)."""
        return time.time() >= self.expires_at + 5.0

    def time_remaining(self) -> float:
        """Get remaining time before expiration (negative if expired)."""
        return self.expires_at - time.time()

    def get_pid(self) -> int:
        """
        v96.0: Extract PID from owner string.

        Supports both old format "jarvis-{pid}" and new format "jarvis-{pid}-{start_time}".
        """
        try:
            parts = self.owner.split("-")
            if len(parts) >= 2:
                return int(parts[1])
        except (ValueError, IndexError):
            pass
        return 0

    def get_owner_start_time(self) -> float:
        """
        v96.0: Extract process start time from owner string.

        Returns 0.0 if using old format without start time.
        """
        try:
            parts = self.owner.split("-")
            if len(parts) >= 3:
                return float(parts[2])
        except (ValueError, IndexError):
            pass
        # Fall back to stored process_start_time
        return self.process_start_time


# =============================================================================
# Distributed Lock Manager
# =============================================================================

class DistributedLockManager:
    """
    v96.0: Production-grade distributed lock manager for cross-repo coordination.

    Features:
    - Automatic lock expiration (TTL-based)
    - Stale lock detection and cleanup
    - Deadlock prevention
    - Lock renewal support
    - Process-safe across multiple repos
    - v93.0: Aggressive pre-cleanup on initialization
    - v93.0: PID validation for stale lock detection
    - v93.0: Faster acquisition with optimistic locking
    - v96.0: Process start time tracking for PID reuse detection (Problem 19 fix)
    - v96.0: Process fingerprint in lock metadata
    - v96.0: Machine ID tracking for distributed environments
    - v96.0: Enhanced owner ID format: "jarvis-{pid}-{start_time}"

    CRITICAL FIX (v96.0) - Problem 19: Startup lock not released on crash
    ════════════════════════════════════════════════════════════════════════
    Before: Process crash → PID reused → stale lock appears valid → deadlock
    After: Process start time validated → PID reuse detected → stale lock cleaned
    """

    def __init__(self, config: Optional[LockConfig] = None):
        """Initialize distributed lock manager."""
        self.config = config or LockConfig()
        self._cleanup_task: Optional[asyncio.Task] = None

        # v96.0: Enhanced owner ID with process start time for PID reuse detection
        self._process_start_time = self._get_own_process_start_time()
        self._owner_id = f"jarvis-{os.getpid()}-{self._process_start_time:.1f}"
        self._process_name = self._get_own_process_name()
        self._process_cmdline = self._get_own_process_cmdline()
        self._machine_id = self._get_machine_id()

        self._initialized = False
        self._init_lock = asyncio.Lock()

        logger.info(f"Distributed Lock Manager v96.0 initialized (owner: {self._owner_id})")

    def _get_own_process_start_time(self) -> float:
        """v96.0: Get our own process start time for owner ID."""
        try:
            import psutil
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return time.time()  # Fallback to current time

    def _get_own_process_name(self) -> str:
        """v96.0: Get our own process name for lock metadata."""
        try:
            import psutil
            return psutil.Process(os.getpid()).name()
        except Exception:
            return "unknown"

    def _get_own_process_cmdline(self) -> str:
        """v96.0: Get our own command line for lock metadata (truncated)."""
        try:
            import psutil
            cmdline = psutil.Process(os.getpid()).cmdline()
            return " ".join(cmdline)[:500] if cmdline else ""
        except Exception:
            return ""

    def _get_machine_id(self) -> str:
        """v96.0: Get machine identifier for distributed environments."""
        try:
            import platform
            import socket
            return f"{platform.system().lower()}-{socket.gethostname()}"
        except Exception:
            return "unknown"

    async def initialize(self) -> None:
        """
        v93.0: Initialize lock manager with aggressive pre-cleanup.

        This method ensures any stale locks from crashed processes are
        cleaned up BEFORE we start operations, preventing the common
        "Lock acquisition timeout" issue.
        """
        async with self._init_lock:
            if self._initialized:
                return

            # Create lock directory
            try:
                await aiofiles.os.makedirs(self.config.lock_dir, exist_ok=True)
                logger.info(f"Lock directory initialized: {self.config.lock_dir}")
            except Exception as e:
                logger.error(f"Failed to create lock directory: {e}")
                raise

            # v93.0: CRITICAL - Aggressive pre-cleanup of stale locks
            # This runs SYNCHRONOUSLY before anything else to ensure clean state
            cleaned = await self._aggressive_stale_cleanup()
            if cleaned > 0:
                logger.info(f"[v93.0] Pre-cleaned {cleaned} stale lock(s) at startup")

            # Start cleanup task
            if self.config.cleanup_enabled:
                self._cleanup_task = asyncio.create_task(
                    self._cleanup_loop(),
                    name="lock_cleanup_loop"
                )
                logger.info("Stale lock cleanup task started")

            self._initialized = True

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
        v93.0: Try to acquire lock atomically with dead process detection.

        Enhancements:
        - Immediately removes locks from expired TTL
        - Immediately removes locks from dead processes (no waiting!)
        - Optimistic locking with verification

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            # Check if lock file exists
            if await aiofiles.os.path.exists(lock_file):
                # Read existing lock metadata
                existing_lock = await self._read_lock_metadata(lock_file)

                if existing_lock:
                    should_remove = False
                    remove_reason = ""

                    # Check 1: Lock is expired
                    if existing_lock.is_expired():
                        should_remove = True
                        remove_reason = f"expired {-existing_lock.time_remaining():.1f}s ago"

                    # v93.0: Check 2: Owner process is dead - DON'T WAIT!
                    elif not self._is_process_alive(existing_lock.owner):
                        should_remove = True
                        remove_reason = f"owner process {existing_lock.owner} is dead"

                    if should_remove:
                        logger.info(
                            f"[v93.0] Removing lock: {lock_name} ({remove_reason})"
                        )
                        await self._remove_lock_file(lock_file)
                    else:
                        # Lock is still valid AND owner is alive
                        logger.debug(
                            f"Lock held by another process: {lock_name} "
                            f"(owner: {existing_lock.owner}, expires in {existing_lock.time_remaining():.1f}s)"
                        )
                        return False

            # v96.0: Create new lock with enhanced metadata for PID reuse detection
            now = time.time()
            metadata = LockMetadata(
                acquired_at=now,
                expires_at=now + ttl,
                owner=self._owner_id,
                token=token,
                lock_name=lock_name,
                # v96.0: Process fingerprint for identity validation
                process_start_time=self._process_start_time,
                process_name=self._process_name,
                process_cmdline=self._process_cmdline,
                machine_id=self._machine_id,
            )

            # Write lock file atomically (with fsync for durability)
            await self._write_lock_metadata(lock_file, metadata)

            # v93.0: Verify we actually got the lock with exponential backoff
            # This handles slow filesystems (NFS, cloud storage) that may have delays
            verification_delays = [0.005, 0.01, 0.02, 0.05]  # Exponential backoff
            for delay in verification_delays:
                await asyncio.sleep(delay)
                verify_lock = await self._read_lock_metadata(lock_file)

                if verify_lock is None:
                    # Lock file disappeared - race condition, we lost
                    logger.debug(f"Lock file disappeared during verification: {lock_name}")
                    return False

                if verify_lock.token == token:
                    # We successfully own the lock
                    return True

                # Someone else's token - continue checking in case of read timing issue
                continue

            # After all retries, still not our token - we lost the race
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
        """
        v96.1: Read and parse lock metadata from file with robust error handling.

        Handles both old format (without v96.0 fields) and new format (with process fingerprint).

        v96.1 Enhancements:
        - Pre-check for empty files before JSON parsing
        - Better error categorization for diagnostics
        - More resilient against filesystem race conditions
        """
        try:
            # v96.1: Check file size before reading to detect empty/corrupted files early
            try:
                file_size = (await aiofiles.os.stat(lock_file)).st_size
                if file_size == 0:
                    logger.debug(f"Empty lock file detected: {lock_file} (will remove)")
                    await self._remove_lock_file(lock_file)
                    return None
            except FileNotFoundError:
                return None
            except OSError:
                pass  # Fall through to normal read, will handle errors there

            async with aiofiles.open(lock_file, 'r') as f:
                data = await f.read()

                # v96.1: Additional empty check after read (race condition protection)
                if not data or not data.strip():
                    logger.debug(f"Lock file has no content: {lock_file} (will remove)")
                    await self._remove_lock_file(lock_file)
                    return None

                metadata_dict = json.loads(data)

                # v96.0: Handle legacy lock files without new fields
                # Set default values for missing fields to maintain backward compatibility
                if "process_start_time" not in metadata_dict:
                    metadata_dict["process_start_time"] = 0.0
                if "process_name" not in metadata_dict:
                    metadata_dict["process_name"] = ""
                if "process_cmdline" not in metadata_dict:
                    metadata_dict["process_cmdline"] = ""
                if "machine_id" not in metadata_dict:
                    metadata_dict["machine_id"] = ""

                return LockMetadata(**metadata_dict)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.error(f"Corrupted lock file: {lock_file} (will remove)")
            await self._remove_lock_file(lock_file)
            return None
        except TypeError as e:
            # v96.0: Handle unknown fields from newer lock format gracefully
            logger.debug(f"Lock file has unknown fields: {lock_file} - {e}")
            try:
                # Try reading with only known fields
                async with aiofiles.open(lock_file, 'r') as f:
                    data = await f.read()
                    metadata_dict = json.loads(data)
                    # Extract only the fields we know about
                    return LockMetadata(
                        acquired_at=metadata_dict.get("acquired_at", 0.0),
                        expires_at=metadata_dict.get("expires_at", 0.0),
                        owner=metadata_dict.get("owner", ""),
                        token=metadata_dict.get("token", ""),
                        lock_name=metadata_dict.get("lock_name", ""),
                        process_start_time=metadata_dict.get("process_start_time", 0.0),
                        process_name=metadata_dict.get("process_name", ""),
                        process_cmdline=metadata_dict.get("process_cmdline", ""),
                        machine_id=metadata_dict.get("machine_id", ""),
                    )
            except Exception:
                return None
        except Exception as e:
            logger.error(f"Error reading lock metadata {lock_file}: {e}")
            return None

    async def _write_lock_metadata(self, lock_file: Path, metadata: LockMetadata) -> None:
        """
        v96.1: Write lock metadata to file atomically with enhanced durability.

        v1.1: Ensures parent directory exists before writing.
        v93.0: Added fsync to ensure filesystem consistency before verification.
               This prevents race conditions on slow filesystems (NFS, cloud storage).
        v96.1: Added content verification after write to detect silent corruption.
               Improved temp file cleanup and retry logic for robustness.
        """
        max_retries = 3
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                # Ensure directory exists (resilient to race conditions)
                lock_dir = lock_file.parent
                try:
                    await aiofiles.os.makedirs(lock_dir, exist_ok=True)
                except FileExistsError:
                    pass  # Another process created it - that's fine

                # v96.1: Serialize the content first to catch any serialization errors
                content = json.dumps(asdict(metadata), indent=2)
                if not content or len(content) < 10:
                    raise ValueError(f"Invalid metadata serialization: {content[:50]}")

                # Write to temp file first with explicit flush
                temp_file = lock_file.with_suffix(f'.lock.tmp.{os.getpid()}')

                try:
                    async with aiofiles.open(temp_file, 'w') as f:
                        await f.write(content)
                        await f.flush()
                        # v93.0: Force write to disk before rename
                        os.fsync(f.fileno())

                    # v96.1: Verify temp file was written correctly before rename
                    temp_size = (await aiofiles.os.stat(temp_file)).st_size
                    if temp_size != len(content.encode('utf-8')):
                        raise IOError(
                            f"Temp file size mismatch: expected {len(content.encode('utf-8'))}, "
                            f"got {temp_size}"
                        )

                    # Atomic rename
                    await aiofiles.os.rename(temp_file, lock_file)

                except Exception:
                    # Clean up temp file on failure
                    try:
                        await aiofiles.os.remove(temp_file)
                    except (FileNotFoundError, OSError):
                        pass
                    raise

                # v93.0: Sync directory to ensure rename is durable
                try:
                    dir_fd = os.open(str(lock_dir), os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except (OSError, AttributeError):
                    # O_DIRECTORY not supported on some platforms - fall back to brief sleep
                    await asyncio.sleep(0.001)

                # v96.1: Verify final file was written correctly
                final_size = (await aiofiles.os.stat(lock_file)).st_size
                if final_size == 0:
                    raise IOError("Lock file is empty after write - filesystem issue detected")

                # Success - exit retry loop
                return

            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Lock write attempt {attempt + 1} failed: {e}, retrying..."
                    )
                    await asyncio.sleep(0.01 * (attempt + 1))  # Brief exponential backoff
                continue

        # All retries failed
        logger.error(f"Error writing lock metadata {lock_file} after {max_retries} attempts: {last_error}")
        raise last_error or IOError(f"Failed to write lock file: {lock_file}")

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
    # v96.0: Advanced Cleanup with PID Reuse Detection
    # =========================================================================

    def _is_process_alive(
        self,
        owner_id: str,
        lock_metadata: Optional[LockMetadata] = None,
    ) -> bool:
        """
        v96.0: Check if the process that owns a lock is still alive.

        CRITICAL FIX for Problem 19 (PID reuse detection):
        - Extracts PID from owner ID
        - Validates process start time against stored value
        - Detects PID reuse and treats as stale lock

        Args:
            owner_id: Lock owner identifier (format: "jarvis-{pid}-{start_time}")
            lock_metadata: Optional metadata for additional validation

        Returns:
            True if process is alive AND matches stored identity
            False if dead, PID reused, or unable to determine

        Validation Phases:
        ┌─────────────────────────────────────────────────────────────────────┐
        │ Phase 1: PID Extraction                                             │
        │   - Parse owner_id for PID                                          │
        │   - Handle both old "jarvis-{pid}" and new "jarvis-{pid}-{start}"  │
        ├─────────────────────────────────────────────────────────────────────┤
        │ Phase 2: Process Existence Check                                    │
        │   - Use signal 0 to check if PID exists                            │
        │   - Handle permission errors                                        │
        ├─────────────────────────────────────────────────────────────────────┤
        │ Phase 3: PID Reuse Detection (v96.0 CRITICAL FIX)                  │
        │   - Compare process start time with stored value                    │
        │   - If mismatch > 1s, PID was reused → treat as stale             │
        │   - Optionally validate process name/cmdline                        │
        └─────────────────────────────────────────────────────────────────────┘
        """
        import psutil

        try:
            # ═══════════════════════════════════════════════════════════════
            # Phase 1: PID Extraction
            # ═══════════════════════════════════════════════════════════════
            if "-" not in owner_id:
                logger.debug(f"Cannot extract PID from owner_id: {owner_id}")
                return True  # Can't validate, assume alive (conservative)

            parts = owner_id.split("-")

            # Extract PID (always second part)
            try:
                pid = int(parts[1]) if len(parts) >= 2 else 0
            except ValueError:
                logger.debug(f"Invalid PID in owner_id: {owner_id}")
                return False  # Invalid PID format = dead process

            if pid <= 0:
                logger.debug(f"Invalid PID value: {pid}")
                return False  # Invalid PID = dead process

            # v96.0: Extract stored start time from owner_id (third part)
            stored_start_time = 0.0
            if len(parts) >= 3:
                try:
                    stored_start_time = float(parts[2])
                except ValueError:
                    pass

            # Fall back to metadata if owner_id doesn't have start time
            if stored_start_time == 0.0 and lock_metadata:
                stored_start_time = lock_metadata.process_start_time

            # ═══════════════════════════════════════════════════════════════
            # Phase 2: Process Existence Check
            # ═══════════════════════════════════════════════════════════════
            try:
                os.kill(pid, 0)
                # Process exists - continue to Phase 3
            except ProcessLookupError:
                # Process doesn't exist - definitely dead
                logger.debug(f"[v96.0] Process {pid} does not exist (ProcessLookupError)")
                return False
            except PermissionError:
                # Process exists but we can't signal it (different user)
                # Continue to Phase 3 for start time validation
                pass
            except OSError as e:
                import errno
                if e.errno == errno.ESRCH:  # No such process
                    return False
                elif e.errno == errno.EPERM:  # Permission denied
                    pass  # Process exists, continue to Phase 3
                else:
                    logger.debug(f"OSError checking PID {pid}: {e}")
                    return False  # Conservative: assume dead

            # ═══════════════════════════════════════════════════════════════
            # Phase 3: PID Reuse Detection (v96.0 CRITICAL FIX)
            # ═══════════════════════════════════════════════════════════════
            if stored_start_time > 0.0:
                try:
                    process = psutil.Process(pid)
                    current_start_time = process.create_time()

                    # Allow 1 second tolerance for timing differences
                    time_diff = abs(current_start_time - stored_start_time)
                    if time_diff > 1.0:
                        logger.info(
                            f"[v96.0] PID REUSE DETECTED for {owner_id}: "
                            f"stored_start={stored_start_time:.1f}, "
                            f"current_start={current_start_time:.1f}, "
                            f"diff={time_diff:.1f}s"
                        )
                        return False  # PID was reused → stale lock

                    # v96.0: Optional process name validation
                    if lock_metadata and lock_metadata.process_name:
                        current_name = process.name()
                        if current_name != lock_metadata.process_name:
                            logger.info(
                                f"[v96.0] Process NAME MISMATCH for PID {pid}: "
                                f"expected={lock_metadata.process_name}, "
                                f"actual={current_name}"
                            )
                            return False  # Different process → stale lock

                except psutil.NoSuchProcess:
                    logger.debug(f"[v96.0] Process {pid} gone during validation")
                    return False
                except psutil.AccessDenied:
                    # Can't validate start time, assume process is valid
                    logger.debug(f"[v96.0] Access denied validating PID {pid}")
                    pass
                except Exception as e:
                    logger.debug(f"[v96.0] Error validating PID {pid}: {e}")
                    pass

            # Process exists and passed all validation checks
            return True

        except Exception as e:
            # v93.0: Log unexpected errors instead of silently assuming alive
            logger.warning(f"Unexpected error checking process {owner_id}: {e}")
            # Conservative approach: assume dead to prevent stale lock accumulation
            return False

    def _validate_lock_owner_identity(
        self,
        metadata: LockMetadata,
    ) -> Tuple[bool, str]:
        """
        v96.0: Comprehensive validation of lock owner identity.

        This is used during lock acquisition to determine if an existing lock
        is still valid or should be removed as stale.

        Args:
            metadata: Lock metadata to validate

        Returns:
            Tuple of (is_valid: bool, reason: str)
        """
        import psutil

        pid = metadata.get_pid()
        if pid <= 0:
            return False, "invalid_pid"

        # Check if process exists
        try:
            process = psutil.Process(pid)
            if not process.is_running():
                return False, "process_not_running"

            if process.status() == psutil.STATUS_ZOMBIE:
                return False, "zombie_process"

        except psutil.NoSuchProcess:
            return False, "process_not_found"
        except psutil.AccessDenied:
            # Process exists but can't inspect - assume valid
            return True, "access_denied_assumed_valid"

        # Validate start time
        stored_start_time = metadata.get_owner_start_time()
        if stored_start_time > 0.0:
            try:
                current_start_time = process.create_time()
                time_diff = abs(current_start_time - stored_start_time)
                if time_diff > 1.0:
                    return False, f"pid_reused:time_diff={time_diff:.1f}s"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        # Validate process name
        if metadata.process_name:
            try:
                current_name = process.name()
                if current_name != metadata.process_name:
                    return False, f"name_mismatch:{metadata.process_name}!={current_name}"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        # Validate machine ID (for distributed setups)
        if metadata.machine_id and metadata.machine_id != self._machine_id:
            # Different machine - can't validate PID, but lock may be valid
            # Check TTL instead
            if metadata.is_expired():
                return False, "expired_remote_lock"
            # Assume valid if not expired (distributed scenario)
            return True, "remote_lock_not_expired"

        return True, "identity_verified"

    async def _aggressive_stale_cleanup(self) -> int:
        """
        v93.0: Aggressive cleanup of stale locks at startup.

        This is more aggressive than the normal cleanup:
        1. Cleans ALL expired locks (not just stale ones)
        2. Validates PIDs for locks that aren't expired yet
        3. Removes locks from dead processes immediately

        This prevents the "Lock acquisition timeout" issue when starting
        after a crash.

        Returns:
            Number of locks cleaned
        """
        cleaned_count = 0

        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return 0

            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith('.lock')
            ]

            for lock_file_name in lock_files:
                lock_file = self.config.lock_dir / lock_file_name
                metadata = await self._read_lock_metadata(lock_file)

                if not metadata:
                    # Corrupted lock file - remove it
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1
                    continue

                should_clean = False
                reason = ""

                # Check 1: Lock is expired
                if metadata.is_expired():
                    should_clean = True
                    reason = f"expired {-metadata.time_remaining():.1f}s ago"

                # Check 2: Owner process is dead (even if not expired yet)
                # v96.0: Pass metadata for enhanced PID reuse detection
                elif not self._is_process_alive(metadata.owner, metadata):
                    should_clean = True
                    reason = f"owner process dead (was {metadata.owner})"

                # Check 3: v96.0 Enhanced - Validate lock owner identity comprehensively
                else:
                    is_valid, validation_reason = self._validate_lock_owner_identity(metadata)
                    if not is_valid:
                        should_clean = True
                        reason = f"identity validation failed: {validation_reason}"
                    # Also check for our own stale lock from previous run
                    elif metadata.owner == self._owner_id:
                        # Same owner ID as us - check if this is truly our lock
                        # or a stale lock from a previous run that got same PID + start_time collision
                        if metadata.acquired_at < time.time() - 60:  # More than 1 min old
                            # If lock was acquired more than 60s ago but we just started,
                            # this is definitely stale
                            should_clean = True
                            reason = "own stale lock from previous run"

                if should_clean:
                    logger.info(
                        f"[v93.0] Cleaning lock: {metadata.lock_name} ({reason})"
                    )
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1

        except Exception as e:
            logger.error(f"[v93.0] Error in aggressive cleanup: {e}")

        return cleaned_count

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
        """
        v93.0: Enhanced cleanup with PID validation.

        Cleans locks that are:
        1. Past their TTL (expired/stale)
        2. Owned by dead processes (PID validation)
        """
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

                if not metadata:
                    continue

                should_clean = False

                # Check TTL expiration
                if metadata.is_stale():
                    logger.warning(
                        f"Cleaning stale lock: {metadata.lock_name} "
                        f"(owner: {metadata.owner}, expired {-metadata.time_remaining():.1f}s ago)"
                    )
                    should_clean = True

                # v96.0: Enhanced - Check if owner process is dead with PID reuse detection
                elif not self._is_process_alive(metadata.owner, metadata):
                    logger.warning(
                        f"Cleaning orphaned lock: {metadata.lock_name} "
                        f"(owner process {metadata.owner} is dead)"
                    )
                    should_clean = True

                # v96.0: Also validate lock owner identity comprehensively
                else:
                    is_valid, validation_reason = self._validate_lock_owner_identity(metadata)
                    if not is_valid:
                        logger.warning(
                            f"Cleaning invalid lock: {metadata.lock_name} "
                            f"(identity validation failed: {validation_reason})"
                        )
                        should_clean = True

                if should_clean:
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} stale/orphaned lock(s)")

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

        # v96.0: Include identity validation in status
        is_valid, validation_reason = self._validate_lock_owner_identity(metadata)
        is_alive = self._is_process_alive(metadata.owner, metadata)

        return {
            "lock_name": metadata.lock_name,
            "owner": metadata.owner,
            "acquired_at": metadata.acquired_at,
            "expires_at": metadata.expires_at,
            "time_remaining": metadata.time_remaining(),
            "is_expired": metadata.is_expired(),
            "is_stale": metadata.is_stale(),
            # v96.0: Enhanced validation fields
            "owner_alive": is_alive,
            "identity_valid": is_valid,
            "validation_reason": validation_reason,
            "process_name": metadata.process_name,
            "machine_id": metadata.machine_id,
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

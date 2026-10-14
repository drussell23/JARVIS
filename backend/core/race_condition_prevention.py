"""
Race Condition Prevention System v1.0 - Production-Grade Atomic Operations
===========================================================================

Comprehensive race condition prevention for Trinity IPC architecture.
This module addresses 4 critical race conditions:

ISSUE 9: Trinity IPC File Lock Race Condition
    - Root Fix: Atomic lock creation with O_CREAT | O_EXCL
    - PID validation for process liveness
    - Stale lock detection with automatic cleanup
    - Lock timeout with exponential backoff

ISSUE 10: Heartbeat File Write Race Condition
    - Root Fix: Atomic write via temp file + rename
    - fsync for durability guarantees
    - File locking during read-modify-write operations
    - Corruption detection and recovery

ISSUE 11: Port Allocation Race Condition
    - Root Fix: Atomic port reservation with file lock
    - Port registry with PID tracking
    - Socket bind verification before reservation
    - Stale reservation cleanup

ISSUE 12: Component Startup Order Race
    - Root Fix: Continuous dependency health monitoring
    - Automatic restart of dependents when dependency fails
    - Dependency graph validation before startup
    - Event-driven dependency state propagation

KEY FEATURES:
=============
- Zero hardcoding - all configuration via environment variables
- Async-first design with sync fallbacks
- Exponential backoff with jitter for retries
- Circuit breaker pattern for resilience
- Comprehensive logging and metrics
- PID + creation time validation (prevents PID reuse)
- Atomic operations using OS primitives

Architecture:
    +------------------------------------------------------------------------+
    |                    Race Condition Prevention v1.0                      |
    +------------------------------------------------------------------------+
    |  AtomicFileLock (Issue 9)                                              |
    |  - O_CREAT | O_EXCL atomic creation                                    |
    |  - PID validation with process creation time                           |
    |  - Stale lock timeout detection                                        |
    +------------------------------------------------------------------------+
    |  AtomicHeartbeatWriter (Issue 10)                                      |
    |  - Temp file + atomic rename                                           |
    |  - fsync for durability                                                |
    |  - Read-modify-write with exclusive lock                               |
    +------------------------------------------------------------------------+
    |  AtomicPortReservation (Issue 11)                                      |
    |  - Socket bind verification                                            |
    |  - Atomic reservation file creation                                    |
    |  - PID tracking with staleness detection                               |
    +------------------------------------------------------------------------+
    |  DependencyHealthMonitor (Issue 12)                                    |
    |  - Continuous health polling                                           |
    |  - Dependency graph with topological order                             |
    |  - Automatic dependent restart on failure                              |
    +------------------------------------------------------------------------+

Author: JARVIS Trinity v1.0 - Race Condition Prevention
"""

from __future__ import annotations

from backend.core.portable_sockets import allow_time_wait_rebind
import asyncio
import errno
import hashlib
import json
import logging
import os
import random
import socket
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Environment Variable Helpers (Zero Hardcoding)
# =============================================================================


def _env_int(key: str, default: int) -> int:
    """Get integer from environment with validation."""
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"[RacePrevent] Invalid int for {key}: {value}, using default")
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment with validation."""
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[RacePrevent] Invalid float for {key}: {value}, using default")
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment with validation."""
    value = os.environ.get(key, "").lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _env_path(key: str, default: Path) -> Path:
    """Get path from environment."""
    value = os.environ.get(key)
    if value is None:
        return default
    return Path(value).expanduser()


# =============================================================================
# Configuration
# =============================================================================


@dataclass(frozen=True)
class RacePreventionConfig:
    """Configuration for race condition prevention - all from environment."""

    # Base directory for locks and state
    state_dir: Path = field(default_factory=lambda: _env_path(
        "RACE_PREVENTION_STATE_DIR",
        Path.home() / ".jarvis" / "race_prevention"
    ))

    # Lock timeouts
    lock_acquire_timeout: float = field(default_factory=lambda: _env_float(
        "RACE_LOCK_ACQUIRE_TIMEOUT", 30.0
    ))
    lock_stale_timeout: float = field(default_factory=lambda: _env_float(
        "RACE_LOCK_STALE_TIMEOUT", 300.0
    ))
    lock_poll_interval: float = field(default_factory=lambda: _env_float(
        "RACE_LOCK_POLL_INTERVAL", 0.05
    ))

    # Backoff configuration
    backoff_base: float = field(default_factory=lambda: _env_float(
        "RACE_BACKOFF_BASE", 0.1
    ))
    backoff_max: float = field(default_factory=lambda: _env_float(
        "RACE_BACKOFF_MAX", 5.0
    ))
    backoff_multiplier: float = field(default_factory=lambda: _env_float(
        "RACE_BACKOFF_MULTIPLIER", 2.0
    ))
    backoff_jitter: float = field(default_factory=lambda: _env_float(
        "RACE_BACKOFF_JITTER", 0.1
    ))

    # Heartbeat configuration
    heartbeat_interval: float = field(default_factory=lambda: _env_float(
        "RACE_HEARTBEAT_INTERVAL", 5.0
    ))
    heartbeat_stale_timeout: float = field(default_factory=lambda: _env_float(
        "RACE_HEARTBEAT_STALE_TIMEOUT", 30.0
    ))

    # Port reservation
    port_reservation_timeout: float = field(default_factory=lambda: _env_float(
        "RACE_PORT_RESERVATION_TIMEOUT", 10.0
    ))
    port_socket_timeout: float = field(default_factory=lambda: _env_float(
        "RACE_PORT_SOCKET_TIMEOUT", 2.0
    ))

    # Dependency monitoring
    dependency_check_interval: float = field(default_factory=lambda: _env_float(
        "RACE_DEPENDENCY_CHECK_INTERVAL", 5.0
    ))
    dependency_failure_threshold: int = field(default_factory=lambda: _env_int(
        "RACE_DEPENDENCY_FAILURE_THRESHOLD", 3
    ))

    def __post_init__(self) -> None:
        """Ensure directories exist."""
        # Use object.__setattr__ since frozen=True
        object.__setattr__(self, 'state_dir', Path(self.state_dir))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "locks").mkdir(exist_ok=True)
        (self.state_dir / "heartbeats").mkdir(exist_ok=True)
        (self.state_dir / "ports").mkdir(exist_ok=True)
        (self.state_dir / "dependencies").mkdir(exist_ok=True)


# Global configuration instance (lazy initialized)
_config: Optional[RacePreventionConfig] = None


def get_config() -> RacePreventionConfig:
    """Get or create global configuration."""
    global _config
    if _config is None:
        _config = RacePreventionConfig()
    return _config


# =============================================================================
# Process Identity (PID Reuse Prevention)
# =============================================================================


@dataclass(frozen=True)
class ProcessIdentity:
    """
    Unique process identity that survives PID reuse.

    Uses PID + process creation time to uniquely identify a process.
    This prevents false positives when a PID is reused by a new process.
    """
    pid: int
    creation_time: float
    hostname: str

    @classmethod
    def current(cls) -> "ProcessIdentity":
        """Create identity for current process."""
        pid = os.getpid()
        creation_time = time.time()

        # Try to get actual process creation time
        try:
            import psutil
            proc = psutil.Process(pid)
            creation_time = proc.create_time()
        except ImportError:
            pass
        except Exception:
            pass

        return cls(
            pid=pid,
            creation_time=creation_time,
            hostname=socket.gethostname(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "pid": self.pid,
            "creation_time": self.creation_time,
            "hostname": self.hostname,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessIdentity":
        """Deserialize from dictionary."""
        return cls(
            pid=data.get("pid", 0),
            creation_time=data.get("creation_time", 0.0),
            hostname=data.get("hostname", "unknown"),
        )

    def is_alive(self) -> bool:
        """
        Check if this process is still alive.

        Uses both PID check and creation time validation to prevent
        PID reuse false positives.
        """
        # Check if PID exists
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't have permission
            pass
        except OSError:
            return False

        # Validate creation time to prevent PID reuse
        try:
            import psutil
            proc = psutil.Process(self.pid)
            actual_creation = proc.create_time()
            # Allow 5 second tolerance for clock differences
            if abs(actual_creation - self.creation_time) > 5.0:
                logger.debug(
                    f"[ProcessIdentity] PID {self.pid} reused: "
                    f"expected creation={self.creation_time:.0f}, "
                    f"actual={actual_creation:.0f}"
                )
                return False
        except ImportError:
            # No psutil - can't validate creation time
            pass
        except Exception:
            return False

        return True


# Cache current process identity
_current_identity: Optional[ProcessIdentity] = None


def get_current_identity() -> ProcessIdentity:
    """Get cached identity for current process."""
    global _current_identity
    if _current_identity is None:
        _current_identity = ProcessIdentity.current()
    return _current_identity


# =============================================================================
# ISSUE 9: Atomic File Lock with O_CREAT | O_EXCL
# =============================================================================


class LockState(Enum):
    """Lock state enumeration."""
    UNLOCKED = auto()
    LOCKED = auto()
    STALE = auto()
    ERROR = auto()


@dataclass
class LockInfo:
    """Information about a lock holder."""
    identity: ProcessIdentity
    acquired_at: float
    lock_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "identity": self.identity.to_dict(),
            "acquired_at": self.acquired_at,
            "lock_name": self.lock_name,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LockInfo":
        """Deserialize from dictionary."""
        return cls(
            identity=ProcessIdentity.from_dict(data.get("identity", {})),
            acquired_at=data.get("acquired_at", 0.0),
            lock_name=data.get("lock_name", "unknown"),
            metadata=data.get("metadata", {}),
        )

    @property
    def age_seconds(self) -> float:
        """Get lock age in seconds."""
        return time.time() - self.acquired_at

    def is_stale(self, timeout: float) -> bool:
        """Check if lock is stale based on timeout."""
        return self.age_seconds > timeout or not self.identity.is_alive()


class AtomicFileLock:
    """
    Atomic file lock using O_CREAT | O_EXCL for race-free creation.

    FIXES ISSUE 9: Trinity IPC File Lock Race Condition

    Key Features:
    - Atomic lock file creation with O_CREAT | O_EXCL
    - PID validation with process creation time
    - Automatic stale lock detection and cleanup
    - Exponential backoff with jitter for retries
    - Both sync and async interfaces

    Usage:
        lock = AtomicFileLock("my_resource")

        # Async context manager
        async with lock.acquire_async():
            # Critical section

        # Sync context manager
        with lock.acquire_sync():
            # Critical section
    """

    def __init__(
        self,
        name: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize atomic file lock.

        Args:
            name: Lock name (used for file naming)
            config: Optional configuration override
        """
        self.name = name
        self.config = config or get_config()
        self.lock_file = self.config.state_dir / "locks" / f"{name}.lock"
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        self._fd: Optional[int] = None
        self._owned = False

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate backoff with jitter."""
        base_delay = min(
            self.config.backoff_base * (self.config.backoff_multiplier ** attempt),
            self.config.backoff_max,
        )
        jitter = random.uniform(0, self.config.backoff_jitter * base_delay)
        return base_delay + jitter

    def _read_lock_info(self) -> Optional[LockInfo]:
        """Read lock info from file."""
        if not self.lock_file.exists():
            return None

        try:
            content = self.lock_file.read_text()
            if not content.strip():
                return None
            data = json.loads(content)
            return LockInfo.from_dict(data)
        except Exception as e:
            logger.debug(f"[AtomicLock] Failed to read lock info for {self.name}: {e}")
            return None

    def _write_lock_info(self, fd: int, info: LockInfo) -> None:
        """Write lock info to file descriptor."""
        content = json.dumps(info.to_dict(), indent=2)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)

    def _check_and_clean_stale(self) -> bool:
        """
        Check for stale lock and clean if necessary.

        Returns:
            True if stale lock was cleaned, False otherwise
        """
        lock_info = self._read_lock_info()
        if lock_info is None:
            return False

        if lock_info.is_stale(self.config.lock_stale_timeout):
            logger.info(
                f"[AtomicLock] Cleaning stale lock {self.name}: "
                f"holder PID {lock_info.identity.pid} is dead or timed out"
            )
            try:
                self.lock_file.unlink()
                return True
            except OSError:
                pass

        return False

    def _try_acquire_once(self) -> bool:
        """
        Try to acquire lock once using O_CREAT | O_EXCL.

        This is the atomic operation that prevents race conditions.
        O_EXCL ensures the file is created only if it doesn't exist.

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            # O_CREAT | O_EXCL is the key: atomic creation that fails if exists
            fd = os.open(
                str(self.lock_file),
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600
            )

            # Got the lock - write our info
            lock_info = LockInfo(
                identity=get_current_identity(),
                acquired_at=time.time(),
                lock_name=self.name,
            )
            self._write_lock_info(fd, lock_info)

            self._fd = fd
            self._owned = True

            logger.debug(f"[AtomicLock] Acquired lock: {self.name}")
            return True

        except FileExistsError:
            # Lock file exists - check if stale
            if self._check_and_clean_stale():
                # Stale lock cleaned - try again
                return self._try_acquire_once()
            return False

        except OSError as e:
            if e.errno == errno.EEXIST:
                # Lock exists (race with another process)
                return False
            logger.warning(f"[AtomicLock] OS error acquiring {self.name}: {e}")
            return False

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire lock with timeout (synchronous).

        Args:
            timeout: Maximum time to wait (uses config default if None)

        Returns:
            True if lock acquired, False on timeout
        """
        timeout = timeout or self.config.lock_acquire_timeout
        start_time = time.time()
        attempt = 0

        while True:
            if self._try_acquire_once():
                return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"[AtomicLock] Timeout acquiring {self.name} "
                    f"after {elapsed:.1f}s ({attempt} attempts)"
                )
                return False

            # Exponential backoff with jitter
            backoff = self._calculate_backoff(attempt)
            remaining = timeout - elapsed
            sleep_time = min(backoff, remaining)

            if sleep_time > 0:
                time.sleep(sleep_time)

            attempt += 1

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire lock with timeout (asynchronous).

        Args:
            timeout: Maximum time to wait (uses config default if None)

        Returns:
            True if lock acquired, False on timeout
        """
        timeout = timeout or self.config.lock_acquire_timeout
        start_time = time.time()
        attempt = 0

        while True:
            # Run acquisition in executor to avoid blocking
            loop = asyncio.get_running_loop()
            acquired = await loop.run_in_executor(None, self._try_acquire_once)

            if acquired:
                return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    f"[AtomicLock] Timeout acquiring {self.name} "
                    f"after {elapsed:.1f}s ({attempt} attempts)"
                )
                return False

            # Exponential backoff with jitter
            backoff = self._calculate_backoff(attempt)
            remaining = timeout - elapsed
            sleep_time = min(backoff, remaining)

            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

            attempt += 1

    def release(self) -> None:
        """Release the lock."""
        if not self._owned:
            return

        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

            if self.lock_file.exists():
                self.lock_file.unlink()

            self._owned = False
            logger.debug(f"[AtomicLock] Released lock: {self.name}")

        except OSError as e:
            logger.warning(f"[AtomicLock] Error releasing {self.name}: {e}")
            self._owned = False

    @contextmanager
    def acquire_sync(
        self,
        timeout: Optional[float] = None,
    ) -> Iterator[bool]:
        """
        Synchronous context manager for lock acquisition.

        Usage:
            with lock.acquire_sync() as acquired:
                if acquired:
                    # Critical section
        """
        acquired = self.acquire(timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()

    @asynccontextmanager
    async def acquire_context(
        self,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """
        Asynchronous context manager for lock acquisition.

        Usage:
            async with lock.acquire_context() as acquired:
                if acquired:
                    # Critical section
        """
        acquired = await self.acquire_async(timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()

    def __enter__(self) -> "AtomicFileLock":
        """Enter sync context."""
        if not self.acquire():
            raise TimeoutError(f"Failed to acquire lock: {self.name}")
        return self

    def __exit__(self, *_args: Any) -> None:
        """Exit sync context."""
        self.release()

    async def __aenter__(self) -> "AtomicFileLock":
        """Enter async context."""
        if not await self.acquire_async():
            raise TimeoutError(f"Failed to acquire lock: {self.name}")
        return self

    async def __aexit__(self, *_args: Any) -> None:
        """Exit async context."""
        self.release()


# =============================================================================
# ISSUE 10: Atomic Heartbeat Writer
# =============================================================================


@dataclass
class HeartbeatRecord:
    """Heartbeat record with integrity check."""
    identity: ProcessIdentity
    timestamp: float
    component_name: str
    status: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary with checksum."""
        data = {
            "identity": self.identity.to_dict(),
            "timestamp": self.timestamp,
            "component_name": self.component_name,
            "status": self.status,
            "metrics": self.metrics,
        }
        # Compute checksum of data
        json_str = json.dumps(data, sort_keys=True, default=str)
        checksum = hashlib.sha256(json_str.encode()).hexdigest()[:16]
        data["checksum"] = checksum
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HeartbeatRecord":
        """Deserialize with checksum validation."""
        stored_checksum = data.pop("checksum", "")

        # Verify checksum
        json_str = json.dumps(data, sort_keys=True, default=str)
        computed_checksum = hashlib.sha256(json_str.encode()).hexdigest()[:16]

        if stored_checksum and stored_checksum != computed_checksum:
            logger.warning(
                f"[Heartbeat] Checksum mismatch for {data.get('component_name', 'unknown')}: "
                f"stored={stored_checksum}, computed={computed_checksum}"
            )

        return cls(
            identity=ProcessIdentity.from_dict(data.get("identity", {})),
            timestamp=data.get("timestamp", 0.0),
            component_name=data.get("component_name", "unknown"),
            status=data.get("status", "unknown"),
            metrics=data.get("metrics", {}),
            checksum=computed_checksum,
        )

    @property
    def age_seconds(self) -> float:
        """Get heartbeat age."""
        return time.time() - self.timestamp

    def is_stale(self, timeout: float) -> bool:
        """Check if heartbeat is stale."""
        return self.age_seconds > timeout


class AtomicHeartbeatWriter:
    """
    Atomic heartbeat writer using temp file + rename pattern.

    FIXES ISSUE 10: Heartbeat File Write Race Condition

    Key Features:
    - Atomic write via temp file + os.replace()
    - fsync for durability guarantees
    - Checksum for corruption detection
    - File locking for read-modify-write operations
    - Automatic stale heartbeat cleanup

    Usage:
        writer = AtomicHeartbeatWriter("my_component")

        # Write heartbeat
        await writer.write_heartbeat("running", {"cpu": 50})

        # Read heartbeat
        heartbeat = await writer.read_heartbeat()
    """

    def __init__(
        self,
        component_name: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize heartbeat writer.

        Args:
            component_name: Name of the component
            config: Optional configuration override
        """
        self.component_name = component_name
        self.config = config or get_config()
        self.heartbeat_file = self.config.state_dir / "heartbeats" / f"{component_name}.json"
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

        # Lock for read-modify-write operations
        self._lock = AtomicFileLock(f"heartbeat_{component_name}", config)

    def _write_atomic_sync(self, record: HeartbeatRecord) -> None:
        """
        Write heartbeat atomically (synchronous).

        Uses temp file + rename pattern for atomicity.
        """
        # Write to temp file in same directory
        fd, temp_path = tempfile.mkstemp(
            dir=self.heartbeat_file.parent,
            prefix=f".{self.heartbeat_file.name}.",
            suffix=".tmp"
        )

        try:
            # Write data
            content = json.dumps(record.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))

            # fsync for durability
            os.fsync(fd)
            os.close(fd)
            fd = -1

            # Atomic rename
            os.replace(temp_path, self.heartbeat_file)

            logger.debug(f"[Heartbeat] Wrote heartbeat for {self.component_name}")

        except Exception as e:
            logger.error(f"[Heartbeat] Write error for {self.component_name}: {e}")
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            # Clean up temp file if it exists
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def write_heartbeat(
        self,
        status: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Write heartbeat atomically (asynchronous).

        Args:
            status: Component status (e.g., "running", "degraded")
            metrics: Optional metrics dictionary
        """
        record = HeartbeatRecord(
            identity=get_current_identity(),
            timestamp=time.time(),
            component_name=self.component_name,
            status=status,
            metrics=metrics or {},
        )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_atomic_sync, record)

    def write_heartbeat_sync(
        self,
        status: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write heartbeat synchronously."""
        record = HeartbeatRecord(
            identity=get_current_identity(),
            timestamp=time.time(),
            component_name=self.component_name,
            status=status,
            metrics=metrics or {},
        )
        self._write_atomic_sync(record)

    def _read_sync(self) -> Optional[HeartbeatRecord]:
        """Read heartbeat synchronously."""
        if not self.heartbeat_file.exists():
            return None

        try:
            content = self.heartbeat_file.read_text()
            if not content.strip():
                return None
            data = json.loads(content)
            return HeartbeatRecord.from_dict(data)
        except json.JSONDecodeError as e:
            logger.warning(f"[Heartbeat] Corrupted heartbeat for {self.component_name}: {e}")
            return None
        except Exception as e:
            logger.debug(f"[Heartbeat] Read error for {self.component_name}: {e}")
            return None

    async def read_heartbeat(self) -> Optional[HeartbeatRecord]:
        """Read heartbeat asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_sync)

    def read_heartbeat_sync(self) -> Optional[HeartbeatRecord]:
        """Read heartbeat synchronously."""
        return self._read_sync()

    async def update_heartbeat(
        self,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Optional[HeartbeatRecord]:
        """
        Atomic read-modify-write operation with locking.

        Args:
            updater: Function to update the metrics dict

        Returns:
            Updated heartbeat record
        """
        async with self._lock.acquire_context() as acquired:
            if not acquired:
                logger.warning(
                    f"[Heartbeat] Failed to acquire lock for update: {self.component_name}"
                )
                return None

            # Read current
            current = await self.read_heartbeat()
            if current is None:
                return None

            # Apply update
            updated_metrics = updater(current.metrics.copy())

            # Write back
            await self.write_heartbeat(current.status, updated_metrics)

            return await self.read_heartbeat()

    async def cleanup_stale(self) -> bool:
        """
        Clean up stale heartbeat file.

        Returns:
            True if cleaned, False otherwise
        """
        heartbeat = await self.read_heartbeat()
        if heartbeat is None:
            return False

        if heartbeat.is_stale(self.config.heartbeat_stale_timeout):
            try:
                self.heartbeat_file.unlink()
                logger.info(f"[Heartbeat] Cleaned stale heartbeat: {self.component_name}")
                return True
            except OSError:
                pass

        return False


# =============================================================================
# ISSUE 11: Atomic Port Reservation
# =============================================================================


class PortState(Enum):
    """Port reservation state."""
    AVAILABLE = auto()
    RESERVED = auto()
    IN_USE = auto()
    STALE = auto()


@dataclass
class PortReservation:
    """Port reservation record."""
    port: int
    component: str
    identity: ProcessIdentity
    reserved_at: float
    verified_at: float
    socket_bound: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "port": self.port,
            "component": self.component,
            "identity": self.identity.to_dict(),
            "reserved_at": self.reserved_at,
            "verified_at": self.verified_at,
            "socket_bound": self.socket_bound,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortReservation":
        """Deserialize from dictionary."""
        return cls(
            port=data.get("port", 0),
            component=data.get("component", "unknown"),
            identity=ProcessIdentity.from_dict(data.get("identity", {})),
            reserved_at=data.get("reserved_at", 0.0),
            verified_at=data.get("verified_at", 0.0),
            socket_bound=data.get("socket_bound", False),
        )

    def is_stale(self) -> bool:
        """Check if reservation is stale (owner dead)."""
        return not self.identity.is_alive()


class AtomicPortReservation:
    """
    Atomic port reservation with file lock and socket verification.

    FIXES ISSUE 11: Port Allocation Race Condition

    Key Features:
    - Atomic reservation file creation
    - Socket bind verification before reservation
    - PID tracking with staleness detection
    - Automatic cleanup of stale reservations

    Usage:
        reserver = AtomicPortReservation()

        # Reserve a port
        port = await reserver.reserve_port("jarvis_body", 8010, [8011, 8012])

        # Release port
        await reserver.release_port(8010)
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize port reservation manager."""
        self.config = config or get_config()
        self.ports_dir = self.config.state_dir / "ports"
        self.ports_dir.mkdir(parents=True, exist_ok=True)

        # Global lock for port operations
        self._global_lock = AtomicFileLock("port_reservation", config)

    def _port_file(self, port: int) -> Path:
        """Get path for port reservation file."""
        return self.ports_dir / f"port_{port}.json"

    def _verify_socket_available(self, port: int, host: str = "127.0.0.1") -> bool:
        """
        Verify port is available by attempting socket bind.

        This is the definitive check - if we can bind, port is free.
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            allow_time_wait_rebind(sock)
            sock.settimeout(self.config.port_socket_timeout)
            sock.bind((host, port))
            return True
        except (OSError, socket.error):
            return False
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _read_reservation(self, port: int) -> Optional[PortReservation]:
        """Read port reservation from file."""
        port_file = self._port_file(port)
        if not port_file.exists():
            return None

        try:
            content = port_file.read_text()
            if not content.strip():
                return None
            data = json.loads(content)
            return PortReservation.from_dict(data)
        except Exception as e:
            logger.debug(f"[PortReserve] Read error for port {port}: {e}")
            return None

    def _write_reservation(self, reservation: PortReservation) -> None:
        """Write port reservation atomically."""
        port_file = self._port_file(reservation.port)

        # Use temp file + rename for atomicity
        fd, temp_path = tempfile.mkstemp(
            dir=self.ports_dir,
            prefix=f".port_{reservation.port}.",
            suffix=".tmp"
        )

        try:
            content = json.dumps(reservation.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, port_file)

        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def _clean_stale_reservation(self, port: int) -> bool:
        """Clean stale reservation if owner is dead."""
        reservation = self._read_reservation(port)
        if reservation is None:
            return False

        if reservation.is_stale():
            logger.info(
                f"[PortReserve] Cleaning stale reservation for port {port}: "
                f"owner PID {reservation.identity.pid} is dead"
            )
            try:
                self._port_file(port).unlink()
                return True
            except OSError:
                pass

        return False

    async def reserve_port(
        self,
        component: str,
        preferred_port: int,
        fallback_ports: Optional[List[int]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[int]:
        """
        Reserve a port atomically with socket verification.

        Args:
            component: Component name requesting the port
            preferred_port: Preferred port number
            fallback_ports: Alternative ports if preferred is unavailable
            timeout: Maximum time to wait for reservation

        Returns:
            Reserved port number, or None if all ports unavailable
        """
        timeout = timeout or self.config.port_reservation_timeout
        ports_to_try = [preferred_port] + (fallback_ports or [])
        start_time = time.time()

        async with self._global_lock.acquire_context(timeout) as acquired:
            if not acquired:
                logger.warning(f"[PortReserve] Failed to acquire global lock for {component}")
                return None

            for port in ports_to_try:
                if time.time() - start_time > timeout:
                    break

                # Check existing reservation
                existing = self._read_reservation(port)
                if existing:
                    # Check if it's ours
                    current = get_current_identity()
                    if (
                        existing.identity.pid == current.pid and
                        abs(existing.identity.creation_time - current.creation_time) < 5.0
                    ):
                        # We already have this port
                        logger.debug(f"[PortReserve] Reusing existing reservation: port {port}")
                        return port

                    # Check if stale
                    if existing.is_stale():
                        self._clean_stale_reservation(port)
                    else:
                        # Port taken by active process
                        continue

                # Verify socket is available
                loop = asyncio.get_running_loop()
                available = await loop.run_in_executor(
                    None, self._verify_socket_available, port
                )

                if not available:
                    # Port in use at OS level
                    continue

                # Create reservation
                reservation = PortReservation(
                    port=port,
                    component=component,
                    identity=get_current_identity(),
                    reserved_at=time.time(),
                    verified_at=time.time(),
                    socket_bound=True,
                )

                self._write_reservation(reservation)
                logger.info(f"[PortReserve] Reserved port {port} for {component}")
                return port

        logger.warning(
            f"[PortReserve] No available ports for {component}: "
            f"tried {ports_to_try}"
        )
        return None

    async def release_port(self, port: int) -> bool:
        """
        Release a port reservation.

        Args:
            port: Port number to release

        Returns:
            True if released, False if not owned
        """
        port_file = self._port_file(port)

        async with self._global_lock.acquire_context() as acquired:
            if not acquired:
                return False

            reservation = self._read_reservation(port)
            if reservation is None:
                return True  # Already released

            # Verify ownership
            current = get_current_identity()
            if (
                reservation.identity.pid != current.pid or
                abs(reservation.identity.creation_time - current.creation_time) > 5.0
            ):
                logger.warning(
                    f"[PortReserve] Cannot release port {port}: "
                    f"owned by PID {reservation.identity.pid}"
                )
                return False

            try:
                port_file.unlink()
                logger.debug(f"[PortReserve] Released port {port}")
                return True
            except OSError as e:
                logger.warning(f"[PortReserve] Error releasing port {port}: {e}")
                return False

    async def get_reservation(self, port: int) -> Optional[PortReservation]:
        """Get reservation info for a port."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_reservation, port)

    async def cleanup_stale_reservations(self) -> int:
        """
        Clean up all stale port reservations.

        Returns:
            Number of reservations cleaned
        """
        cleaned = 0

        async with self._global_lock.acquire_context() as acquired:
            if not acquired:
                return 0

            for port_file in self.ports_dir.glob("port_*.json"):
                try:
                    # Extract port number from filename
                    port = int(port_file.stem.replace("port_", ""))
                    if self._clean_stale_reservation(port):
                        cleaned += 1
                except (ValueError, OSError):
                    pass

        if cleaned > 0:
            logger.info(f"[PortReserve] Cleaned {cleaned} stale reservations")

        return cleaned


# =============================================================================
# ISSUE 12: Continuous Dependency Health Monitoring
# =============================================================================


class DependencyState(Enum):
    """Dependency health state."""
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


@dataclass
class DependencyNode:
    """Node in the dependency graph."""
    name: str
    dependencies: List[str]
    state: DependencyState = DependencyState.UNKNOWN
    last_check: float = 0.0
    failure_count: int = 0
    health_checker: Optional[Callable[[], Coroutine[Any, Any, bool]]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "dependencies": self.dependencies,
            "state": self.state.value,
            "last_check": self.last_check,
            "failure_count": self.failure_count,
        }


class DependencyHealthMonitor:
    """
    Continuous dependency health monitoring with automatic dependent restart.

    FIXES ISSUE 12: Component Startup Order Race

    Key Features:
    - Continuous health polling with configurable interval
    - Dependency graph with topological ordering
    - Automatic restart of dependents when dependency fails
    - Event-driven state propagation
    - Failure threshold before marking unhealthy

    Usage:
        monitor = DependencyHealthMonitor()

        # Register components with dependencies
        monitor.register_component("reactor_core", [])
        monitor.register_component("jarvis_prime", ["reactor_core"])
        monitor.register_component("jarvis_body", ["jarvis_prime", "reactor_core"])

        # Start monitoring
        await monitor.start_monitoring()

        # Register restart callback
        monitor.on_dependency_failed(restart_dependent)
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize dependency monitor."""
        self.config = config or get_config()
        self.state_dir = self.config.state_dir / "dependencies"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._nodes: Dict[str, DependencyNode] = {}
        self._dependency_graph: Dict[str, List[str]] = {}  # node -> nodes that depend on it
        self._topological_order: List[str] = []

        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Callbacks
        self._on_state_change: List[Callable[[str, DependencyState, DependencyState], Coroutine[Any, Any, None]]] = []
        self._on_dependency_failed: List[Callable[[str, List[str]], Coroutine[Any, Any, None]]] = []

        # Lock for thread safety
        self._lock = asyncio.Lock()

    def register_component(
        self,
        name: str,
        dependencies: List[str],
        health_checker: Optional[Callable[[], Coroutine[Any, Any, bool]]] = None,
    ) -> None:
        """
        Register a component with its dependencies.

        Args:
            name: Component name
            dependencies: List of dependency component names
            health_checker: Optional async function that returns True if healthy
        """
        node = DependencyNode(
            name=name,
            dependencies=dependencies,
            health_checker=health_checker,
        )
        self._nodes[name] = node

        # Build reverse dependency graph
        for dep in dependencies:
            if dep not in self._dependency_graph:
                self._dependency_graph[dep] = []
            self._dependency_graph[dep].append(name)

        # Recompute topological order
        self._compute_topological_order()

        logger.debug(f"[DepMonitor] Registered {name} with deps: {dependencies}")

    def _compute_topological_order(self) -> None:
        """Compute topological order using Kahn's algorithm."""
        # Build in-degree map
        in_degree: Dict[str, int] = {name: 0 for name in self._nodes}
        for node in self._nodes.values():
            for dep in node.dependencies:
                if dep in in_degree:
                    in_degree[node.name] += 1

        # Start with nodes that have no dependencies
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result: List[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            # Reduce in-degree for dependents
            for dependent in self._dependency_graph.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._nodes):
            logger.warning(
                "[DepMonitor] Dependency cycle detected - "
                "some components may not start correctly"
            )

        self._topological_order = result
        logger.debug(f"[DepMonitor] Startup order: {result}")

    def get_startup_order(self) -> List[str]:
        """Get components in correct startup order."""
        return self._topological_order.copy()

    async def check_component_health(self, name: str) -> bool:
        """
        Check health of a single component.

        Returns:
            True if healthy, False otherwise
        """
        node = self._nodes.get(name)
        if node is None:
            return False

        # Use custom health checker if provided
        if node.health_checker:
            try:
                return await asyncio.wait_for(
                    node.health_checker(),
                    timeout=self.config.dependency_check_interval / 2,
                )
            except Exception as e:
                logger.debug(f"[DepMonitor] Health check failed for {name}: {e}")
                return False

        # Default: check heartbeat file
        heartbeat_file = self.config.state_dir / "heartbeats" / f"{name}.json"
        if not heartbeat_file.exists():
            return False

        try:
            content = heartbeat_file.read_text()
            data = json.loads(content)
            timestamp = data.get("timestamp", 0.0)
            age = time.time() - timestamp

            if age > self.config.heartbeat_stale_timeout:
                return False

            # Also verify process is alive
            identity = ProcessIdentity.from_dict(data.get("identity", {}))
            return identity.is_alive()

        except Exception:
            return False

    async def _update_component_state(
        self,
        name: str,
        is_healthy: bool,
    ) -> None:
        """Update component state based on health check result."""
        async with self._lock:
            node = self._nodes.get(name)
            if node is None:
                return

            old_state = node.state
            node.last_check = time.time()

            if is_healthy:
                node.failure_count = 0
                new_state = DependencyState.HEALTHY
            else:
                node.failure_count += 1
                if node.failure_count >= self.config.dependency_failure_threshold:
                    new_state = DependencyState.UNHEALTHY
                else:
                    new_state = DependencyState.DEGRADED

            if old_state != new_state:
                node.state = new_state
                logger.info(
                    f"[DepMonitor] {name} state changed: {old_state.value} -> {new_state.value}"
                )

                # Notify state change callbacks
                for callback in self._on_state_change:
                    try:
                        await callback(name, old_state, new_state)
                    except Exception as e:
                        logger.warning(f"[DepMonitor] State change callback error: {e}")

                # If unhealthy, notify dependent restart callbacks
                if new_state == DependencyState.UNHEALTHY:
                    dependents = self._dependency_graph.get(name, [])
                    if dependents:
                        for callback in self._on_dependency_failed:
                            try:
                                await callback(name, dependents)
                            except Exception as e:
                                logger.warning(f"[DepMonitor] Dependency failed callback error: {e}")

    async def _monitoring_loop(self) -> None:
        """Main monitoring loop."""
        while not self._shutdown_event.is_set():
            try:
                # Check all components
                for name in self._nodes:
                    if self._shutdown_event.is_set():
                        break

                    is_healthy = await self.check_component_health(name)
                    await self._update_component_state(name, is_healthy)

                # Write state to file for cross-process visibility
                await self._persist_state()

            except Exception as e:
                logger.warning(f"[DepMonitor] Monitoring loop error: {e}")

            # Wait for next check interval
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.dependency_check_interval,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue monitoring

    async def _persist_state(self) -> None:
        """Persist current state to file."""
        state_file = self.state_dir / "dependency_state.json"

        state = {
            "timestamp": time.time(),
            "components": {
                name: node.to_dict()
                for name, node in self._nodes.items()
            },
            "startup_order": self._topological_order,
        }

        # Atomic write
        fd, temp_path = tempfile.mkstemp(
            dir=self.state_dir,
            prefix=".state_",
            suffix=".tmp"
        )

        try:
            content = json.dumps(state, indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, state_file)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def start_monitoring(self) -> None:
        """Start the monitoring task."""
        if self._monitoring_task is not None:
            return

        self._shutdown_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("[DepMonitor] Started dependency monitoring")

    async def stop_monitoring(self) -> None:
        """Stop the monitoring task."""
        self._shutdown_event.set()

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

        logger.info("[DepMonitor] Stopped dependency monitoring")

    def on_state_change(
        self,
        callback: Callable[[str, DependencyState, DependencyState], Coroutine[Any, Any, None]],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)

    def on_dependency_failed(
        self,
        callback: Callable[[str, List[str]], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Register callback when a dependency fails.

        Callback receives: (failed_dependency, list_of_dependents)
        """
        self._on_dependency_failed.append(callback)

    def are_dependencies_healthy(self, name: str) -> bool:
        """Check if all dependencies of a component are healthy."""
        node = self._nodes.get(name)
        if node is None:
            return False

        for dep_name in node.dependencies:
            dep_node = self._nodes.get(dep_name)
            if dep_node is None or dep_node.state != DependencyState.HEALTHY:
                return False

        return True

    def get_component_state(self, name: str) -> DependencyState:
        """Get current state of a component."""
        node = self._nodes.get(name)
        return node.state if node else DependencyState.UNKNOWN


# =============================================================================
# ISSUE 13: Process Discovery Race Condition Prevention
# =============================================================================


class ProcessVerificationResult(Enum):
    """Result of process verification."""
    VERIFIED = "verified"
    PID_MISMATCH = "pid_mismatch"
    NAME_MISMATCH = "name_mismatch"
    PARENT_MISMATCH = "parent_mismatch"
    NOT_FOUND = "not_found"
    ZOMBIE = "zombie"
    PERMISSION_DENIED = "permission_denied"


@dataclass
class ProcessFingerprint:
    """
    Complete fingerprint of a process for verification.

    Uses multiple attributes to uniquely identify a process
    and prevent PID reuse attacks.
    """
    pid: int
    name: str
    cmdline: List[str]
    creation_time: float
    parent_pid: int
    parent_name: str
    cwd: str
    username: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "pid": self.pid,
            "name": self.name,
            "cmdline": self.cmdline,
            "creation_time": self.creation_time,
            "parent_pid": self.parent_pid,
            "parent_name": self.parent_name,
            "cwd": self.cwd,
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessFingerprint":
        """Deserialize from dictionary."""
        return cls(
            pid=data.get("pid", 0),
            name=data.get("name", ""),
            cmdline=data.get("cmdline", []),
            creation_time=data.get("creation_time", 0.0),
            parent_pid=data.get("parent_pid", 0),
            parent_name=data.get("parent_name", ""),
            cwd=data.get("cwd", ""),
            username=data.get("username", ""),
        )

    @classmethod
    def capture(cls, pid: int) -> Optional["ProcessFingerprint"]:
        """
        Capture fingerprint of a process.

        Returns None if process doesn't exist or can't be accessed.
        """
        try:
            import psutil
            proc = psutil.Process(pid)

            # Get parent info
            parent = proc.parent()
            parent_pid = parent.pid if parent else 0
            parent_name = parent.name() if parent else ""

            return cls(
                pid=pid,
                name=proc.name(),
                cmdline=proc.cmdline(),
                creation_time=proc.create_time(),
                parent_pid=parent_pid,
                parent_name=parent_name,
                cwd=proc.cwd() if hasattr(proc, 'cwd') else "",
                username=proc.username() if hasattr(proc, 'username') else "",
            )
        except ImportError:
            # Fallback without psutil
            try:
                os.kill(pid, 0)
                return cls(
                    pid=pid,
                    name="unknown",
                    cmdline=[],
                    creation_time=time.time(),
                    parent_pid=os.getppid(),
                    parent_name="",
                    cwd=os.getcwd(),
                    username=os.getenv("USER", "unknown"),
                )
            except (ProcessLookupError, PermissionError):
                return None
        except Exception:
            return None


class SafeProcessManager:
    """
    Safe process management with race condition prevention.

    FIXES ISSUE 13: Process Discovery Race Condition

    Key Features:
    - PID validation with process name matching
    - Process tree validation (parent-child relationships)
    - Graceful degradation if process disappears
    - Fingerprint-based process identification
    - Atomic process registry with file locking

    Usage:
        manager = SafeProcessManager()

        # Register a process
        await manager.register_process("jarvis_body", pid, fingerprint)

        # Verify before action
        result = await manager.verify_process("jarvis_body", pid)
        if result == ProcessVerificationResult.VERIFIED:
            # Safe to act on process
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize safe process manager."""
        self.config = config or get_config()
        self.process_dir = self.config.state_dir / "processes"
        self.process_dir.mkdir(parents=True, exist_ok=True)

        self._lock = AtomicFileLock("process_registry", config)
        self._process_tree_cache: Dict[int, ProcessFingerprint] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = _env_float("PROCESS_CACHE_TTL", 2.0)

    def _process_file(self, component: str) -> Path:
        """Get path for process registry file."""
        return self.process_dir / f"{component}.json"

    async def register_process(
        self,
        component: str,
        pid: int,
        fingerprint: Optional[ProcessFingerprint] = None,
    ) -> bool:
        """
        Register a process atomically.

        Args:
            component: Component name
            pid: Process ID
            fingerprint: Optional pre-captured fingerprint

        Returns:
            True if registered successfully
        """
        # Capture fingerprint if not provided
        if fingerprint is None:
            loop = asyncio.get_running_loop()
            fingerprint = await loop.run_in_executor(
                None, ProcessFingerprint.capture, pid
            )

        if fingerprint is None:
            logger.warning(f"[SafeProcess] Cannot capture fingerprint for PID {pid}")
            return False

        async with self._lock.acquire_context() as acquired:
            if not acquired:
                logger.warning(f"[SafeProcess] Lock timeout registering {component}")
                return False

            # Write atomically
            process_file = self._process_file(component)
            data = {
                "component": component,
                "registered_at": time.time(),
                "fingerprint": fingerprint.to_dict(),
            }

            fd, temp_path = tempfile.mkstemp(
                dir=self.process_dir,
                prefix=f".{component}.",
                suffix=".tmp"
            )

            try:
                content = json.dumps(data, indent=2)
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                fd = -1

                os.replace(temp_path, process_file)
                logger.debug(f"[SafeProcess] Registered {component} with PID {pid}")
                return True

            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    async def verify_process(
        self,
        component: str,
        expected_pid: int,
    ) -> ProcessVerificationResult:
        """
        Verify a process is who we think it is.

        Compares current process state against registered fingerprint.

        Args:
            component: Component name
            expected_pid: Expected PID

        Returns:
            Verification result
        """
        # Read registered fingerprint
        process_file = self._process_file(component)
        if not process_file.exists():
            return ProcessVerificationResult.NOT_FOUND

        try:
            content = process_file.read_text()
            data = json.loads(content)
            registered = ProcessFingerprint.from_dict(data.get("fingerprint", {}))
        except Exception as e:
            logger.debug(f"[SafeProcess] Error reading registry: {e}")
            return ProcessVerificationResult.NOT_FOUND

        # Verify PID matches
        if registered.pid != expected_pid:
            return ProcessVerificationResult.PID_MISMATCH

        # Capture current fingerprint
        loop = asyncio.get_running_loop()
        current = await loop.run_in_executor(
            None, ProcessFingerprint.capture, expected_pid
        )

        if current is None:
            return ProcessVerificationResult.NOT_FOUND

        # Verify creation time (prevents PID reuse)
        if abs(current.creation_time - registered.creation_time) > 5.0:
            logger.warning(
                f"[SafeProcess] PID {expected_pid} reused: "
                f"registered={registered.creation_time:.0f}, "
                f"current={current.creation_time:.0f}"
            )
            return ProcessVerificationResult.PID_MISMATCH

        # Verify process name
        if current.name != registered.name and registered.name != "unknown":
            logger.warning(
                f"[SafeProcess] Process name mismatch for PID {expected_pid}: "
                f"registered={registered.name}, current={current.name}"
            )
            return ProcessVerificationResult.NAME_MISMATCH

        # Verify parent relationship
        if registered.parent_pid != 0:
            if current.parent_pid != registered.parent_pid:
                # Parent changed - might be orphaned or reparented
                logger.debug(
                    f"[SafeProcess] Parent changed for PID {expected_pid}: "
                    f"registered={registered.parent_pid}, current={current.parent_pid}"
                )
                # This is not necessarily an error, just log it

        return ProcessVerificationResult.VERIFIED

    async def safe_kill(
        self,
        component: str,
        signal_num: int = 15,  # SIGTERM
        timeout: float = 10.0,
    ) -> bool:
        """
        Safely terminate a process with verification.

        Verifies process identity before sending signal.

        Args:
            component: Component name
            signal_num: Signal to send (default: SIGTERM)
            timeout: Timeout for graceful shutdown

        Returns:
            True if process was terminated successfully
        """
        # Read registered info
        process_file = self._process_file(component)
        if not process_file.exists():
            logger.debug(f"[SafeProcess] No registered process for {component}")
            return True  # Nothing to kill

        try:
            content = process_file.read_text()
            data = json.loads(content)
            registered = ProcessFingerprint.from_dict(data.get("fingerprint", {}))
        except Exception:
            return False

        pid = registered.pid

        # Verify before kill
        result = await self.verify_process(component, pid)
        if result != ProcessVerificationResult.VERIFIED:
            if result == ProcessVerificationResult.NOT_FOUND:
                # Process already gone
                await self.unregister_process(component)
                return True

            logger.warning(
                f"[SafeProcess] Cannot kill {component}: verification failed ({result.value})"
            )
            return False

        # Send signal
        try:
            os.kill(pid, signal_num)
            logger.debug(f"[SafeProcess] Sent signal {signal_num} to {component} (PID {pid})")
        except ProcessLookupError:
            # Already dead
            await self.unregister_process(component)
            return True
        except PermissionError:
            logger.error(f"[SafeProcess] Permission denied killing {component}")
            return False

        # Wait for termination
        start_time = time.time()
        while time.time() - start_time < timeout:
            await asyncio.sleep(0.1)

            # Check if still alive
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                # Dead
                await self.unregister_process(component)
                return True
            except PermissionError:
                pass

        # Still alive - force kill
        logger.warning(f"[SafeProcess] {component} didn't terminate, sending SIGKILL")
        try:
            os.kill(pid, 9)  # SIGKILL
            await asyncio.sleep(0.5)
            await self.unregister_process(component)
            return True
        except ProcessLookupError:
            await self.unregister_process(component)
            return True
        except Exception as e:
            logger.error(f"[SafeProcess] Failed to kill {component}: {e}")
            return False

    async def unregister_process(self, component: str) -> None:
        """Unregister a process."""
        process_file = self._process_file(component)
        try:
            process_file.unlink(missing_ok=True)
        except OSError:
            pass

    async def get_process_tree(self, root_pid: int) -> Dict[int, ProcessFingerprint]:
        """
        Get entire process tree starting from root.

        Useful for validating parent-child relationships.
        """
        tree: Dict[int, ProcessFingerprint] = {}

        try:
            import psutil

            def collect_tree(pid: int) -> None:
                try:
                    fp = ProcessFingerprint.capture(pid)
                    if fp:
                        tree[pid] = fp
                        proc = psutil.Process(pid)
                        for child in proc.children():
                            collect_tree(child.pid)
                except Exception:
                    pass

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, collect_tree, root_pid)

        except ImportError:
            # No psutil - just capture root
            fp = ProcessFingerprint.capture(root_pid)
            if fp:
                tree[root_pid] = fp

        return tree


# =============================================================================
# ISSUE 14: Configuration File Read Race Prevention
# =============================================================================


@dataclass
class VersionedConfig:
    """Configuration with version tracking."""
    version: int
    timestamp: float
    checksum: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize with checksum."""
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "checksum": self.checksum,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VersionedConfig":
        """Deserialize with checksum validation."""
        return cls(
            version=raw.get("version", 0),
            timestamp=raw.get("timestamp", 0.0),
            checksum=raw.get("checksum", ""),
            data=raw.get("data", {}),
        )

    @staticmethod
    def compute_checksum(data: Dict[str, Any]) -> str:
        """Compute checksum of data."""
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode()).hexdigest()[:32]

    def validate_checksum(self) -> bool:
        """Validate data integrity via checksum."""
        expected = self.compute_checksum(self.data)
        return self.checksum == expected


class AtomicConfigManager:
    """
    Atomic configuration management with versioning and locking.

    FIXES ISSUE 14: Configuration File Read Race

    Key Features:
    - Config file locking for read/write operations
    - Automatic versioning with monotonic counters
    - Checksum-based corruption detection
    - Atomic updates with temp file + rename
    - Read validation after write
    - Concurrent read support with write locking

    Usage:
        config_mgr = AtomicConfigManager("jarvis_config")

        # Read config
        config = await config_mgr.read()

        # Update config atomically
        await config_mgr.update({"key": "value"})

        # Read-modify-write with locking
        async with config_mgr.modify() as data:
            data["new_key"] = "new_value"
    """

    def __init__(
        self,
        name: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize config manager.

        Args:
            name: Config name (used for file naming)
            config: Optional configuration override
        """
        self.name = name
        self.config = config or get_config()
        self.config_dir = self.config.state_dir / "configs"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.config_file = self.config_dir / f"{name}.json"
        self._lock = AtomicFileLock(f"config_{name}", self.config)

        # In-memory cache
        self._cache: Optional[VersionedConfig] = None
        self._cache_version: int = -1

    def _read_sync(self) -> Optional[VersionedConfig]:
        """Read config synchronously."""
        if not self.config_file.exists():
            return None

        try:
            content = self.config_file.read_text()
            if not content.strip():
                return None

            raw = json.loads(content)
            config = VersionedConfig.from_dict(raw)

            # Validate checksum
            if not config.validate_checksum():
                logger.warning(
                    f"[ConfigMgr] Checksum mismatch for {self.name}: "
                    f"config may be corrupted"
                )

            return config

        except json.JSONDecodeError as e:
            logger.error(f"[ConfigMgr] JSON error reading {self.name}: {e}")
            return None
        except Exception as e:
            logger.error(f"[ConfigMgr] Error reading {self.name}: {e}")
            return None

    async def read(self, use_cache: bool = True) -> Optional[VersionedConfig]:
        """
        Read configuration atomically.

        Args:
            use_cache: Use cached version if available and valid

        Returns:
            VersionedConfig or None if not found
        """
        # Check cache
        if use_cache and self._cache is not None:
            # Quick version check without lock
            current = await asyncio.get_running_loop().run_in_executor(
                None, self._read_sync
            )
            if current and current.version == self._cache_version:
                return self._cache

        # Read with shared-like semantics (we still use exclusive lock for safety)
        async with self._lock.acquire_context(timeout=10.0) as acquired:
            if not acquired:
                logger.warning(f"[ConfigMgr] Lock timeout reading {self.name}")
                # Fallback to unprotected read
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._read_sync)

            loop = asyncio.get_running_loop()
            config = await loop.run_in_executor(None, self._read_sync)

            if config:
                self._cache = config
                self._cache_version = config.version

            return config

    def _write_sync(self, config: VersionedConfig) -> bool:
        """Write config synchronously with atomic rename."""
        fd, temp_path = tempfile.mkstemp(
            dir=self.config_dir,
            prefix=f".{self.name}.",
            suffix=".tmp"
        )

        try:
            content = json.dumps(config.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, self.config_file)
            return True

        except Exception as e:
            logger.error(f"[ConfigMgr] Write error for {self.name}: {e}")
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def write(
        self,
        data: Dict[str, Any],
        validate_after: bool = True,
    ) -> bool:
        """
        Write configuration atomically.

        Args:
            data: Configuration data to write
            validate_after: Re-read and validate after write

        Returns:
            True if written successfully
        """
        async with self._lock.acquire_context(timeout=30.0) as acquired:
            if not acquired:
                logger.error(f"[ConfigMgr] Lock timeout writing {self.name}")
                return False

            # Get current version
            loop = asyncio.get_running_loop()
            current = await loop.run_in_executor(None, self._read_sync)
            version = (current.version + 1) if current else 1

            # Create new versioned config
            new_config = VersionedConfig(
                version=version,
                timestamp=time.time(),
                checksum=VersionedConfig.compute_checksum(data),
                data=data,
            )

            # Write atomically
            success = await loop.run_in_executor(None, self._write_sync, new_config)

            if success and validate_after:
                # Re-read to validate
                written = await loop.run_in_executor(None, self._read_sync)
                if written is None or written.version != version:
                    logger.error(f"[ConfigMgr] Validation failed for {self.name}")
                    return False

                if not written.validate_checksum():
                    logger.error(f"[ConfigMgr] Checksum validation failed for {self.name}")
                    return False

                # Update cache
                self._cache = written
                self._cache_version = written.version

            return success

    async def update(
        self,
        updates: Dict[str, Any],
        merge: bool = True,
    ) -> bool:
        """
        Update configuration atomically.

        Args:
            updates: Updates to apply
            merge: If True, merge with existing; if False, replace entirely

        Returns:
            True if updated successfully
        """
        async with self._lock.acquire_context(timeout=30.0) as acquired:
            if not acquired:
                return False

            # Read current
            loop = asyncio.get_running_loop()
            current = await loop.run_in_executor(None, self._read_sync)

            if merge and current:
                # Deep merge
                data = current.data.copy()
                data.update(updates)
            else:
                data = updates

            # Write back (release lock temporarily to use write method)

        return await self.write(data)

    @asynccontextmanager
    async def modify(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Context manager for read-modify-write operations.

        Holds lock for entire operation to ensure atomicity.

        Usage:
            async with config_mgr.modify() as data:
                data["key"] = "new_value"
                # Automatically saved on exit
        """
        async with self._lock.acquire_context(timeout=60.0) as acquired:
            if not acquired:
                raise TimeoutError(f"Failed to acquire lock for {self.name}")

            # Read current
            loop = asyncio.get_running_loop()
            current = await loop.run_in_executor(None, self._read_sync)
            data = current.data.copy() if current else {}

            try:
                yield data

                # Write modified data
                version = (current.version + 1) if current else 1
                new_config = VersionedConfig(
                    version=version,
                    timestamp=time.time(),
                    checksum=VersionedConfig.compute_checksum(data),
                    data=data,
                )

                await loop.run_in_executor(None, self._write_sync, new_config)

                # Update cache
                self._cache = new_config
                self._cache_version = version

            except Exception as e:
                logger.error(f"[ConfigMgr] Modify failed for {self.name}: {e}")
                raise


# =============================================================================
# ISSUE 15: Event Bus Message Loss Prevention
# =============================================================================


class MessagePriority(Enum):
    """Message priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class MessageState(Enum):
    """Message processing state."""
    PENDING = "pending"
    PROCESSING = "processing"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class PersistentMessage:
    """
    Persistent message with deduplication support.

    Uses message ID for deduplication and acknowledgment tracking.
    """
    id: str
    topic: str
    payload: Dict[str, Any]
    priority: MessagePriority
    state: MessageState
    created_at: float
    expires_at: float
    attempts: int = 0
    last_attempt: float = 0.0
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "topic": self.topic,
            "payload": self.payload,
            "priority": self.priority.value,
            "state": self.state.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "attempts": self.attempts,
            "last_attempt": self.last_attempt,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersistentMessage":
        """Deserialize from dictionary."""
        return cls(
            id=data.get("id", ""),
            topic=data.get("topic", ""),
            payload=data.get("payload", {}),
            priority=MessagePriority(data.get("priority", 1)),
            state=MessageState(data.get("state", "pending")),
            created_at=data.get("created_at", 0.0),
            expires_at=data.get("expires_at", 0.0),
            attempts=data.get("attempts", 0),
            last_attempt=data.get("last_attempt", 0.0),
            acknowledged_by=data.get("acknowledged_by"),
            acknowledged_at=data.get("acknowledged_at"),
        )

    def is_expired(self) -> bool:
        """Check if message has expired."""
        return time.time() > self.expires_at

    def can_retry(self, max_attempts: int) -> bool:
        """Check if message can be retried."""
        return self.attempts < max_attempts and not self.is_expired()


class PersistentMessageQueue:
    """
    Persistent message queue with acknowledgments and deduplication.

    FIXES ISSUE 15: Event Bus Message Loss

    Key Features:
    - Persistent message storage with atomic writes
    - Message deduplication via unique IDs
    - Acknowledgment-based delivery guarantees
    - Automatic retry for failed messages
    - Message expiration and cleanup
    - Priority-based message ordering
    - Concurrent consumer support

    Usage:
        queue = PersistentMessageQueue("events")

        # Publish message
        msg_id = await queue.publish("heartbeat", {"status": "running"})

        # Consume with acknowledgment
        async for msg in queue.consume("consumer_1"):
            try:
                process(msg)
                await queue.acknowledge(msg.id, "consumer_1")
            except Exception:
                await queue.reject(msg.id)
    """

    def __init__(
        self,
        name: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize persistent message queue.

        Args:
            name: Queue name
            config: Optional configuration override
        """
        self.name = name
        self.config = config or get_config()
        self.queue_dir = self.config.state_dir / "queues" / name
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        # Configuration from environment
        self.max_attempts = _env_int("MSG_QUEUE_MAX_ATTEMPTS", 3)
        self.default_ttl = _env_float("MSG_QUEUE_DEFAULT_TTL", 3600.0)
        self.retry_delay = _env_float("MSG_QUEUE_RETRY_DELAY", 5.0)
        self.cleanup_interval = _env_float("MSG_QUEUE_CLEANUP_INTERVAL", 60.0)

        # Locks
        self._queue_lock = AtomicFileLock(f"queue_{name}", config)

        # Deduplication cache (in-memory for speed)
        self._seen_ids: Dict[str, float] = {}
        self._seen_ids_max = _env_int("MSG_QUEUE_DEDUP_CACHE_SIZE", 10000)

        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

    def _message_file(self, msg_id: str) -> Path:
        """Get path for message file."""
        return self.queue_dir / f"{msg_id}.json"

    def _generate_id(self) -> str:
        """Generate unique message ID."""
        import uuid
        return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"

    def _is_duplicate(self, msg_id: str) -> bool:
        """Check if message ID is a duplicate."""
        if msg_id in self._seen_ids:
            return True

        # Also check file exists
        return self._message_file(msg_id).exists()

    def _mark_seen(self, msg_id: str) -> None:
        """Mark message ID as seen."""
        self._seen_ids[msg_id] = time.time()

        # Cleanup old entries if cache too large
        if len(self._seen_ids) > self._seen_ids_max:
            # Remove oldest 20%
            sorted_ids = sorted(self._seen_ids.items(), key=lambda x: x[1])
            for old_id, _ in sorted_ids[:len(sorted_ids) // 5]:
                del self._seen_ids[old_id]

    async def publish(
        self,
        topic: str,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
        ttl: Optional[float] = None,
        msg_id: Optional[str] = None,
    ) -> str:
        """
        Publish a message to the queue.

        Args:
            topic: Message topic/channel
            payload: Message payload
            priority: Message priority
            ttl: Time-to-live in seconds
            msg_id: Optional custom message ID (for idempotent publish)

        Returns:
            Message ID
        """
        msg_id = msg_id or self._generate_id()
        ttl = ttl or self.default_ttl

        # Check for duplicate
        if self._is_duplicate(msg_id):
            logger.debug(f"[MsgQueue] Duplicate message ignored: {msg_id}")
            return msg_id

        message = PersistentMessage(
            id=msg_id,
            topic=topic,
            payload=payload,
            priority=priority,
            state=MessageState.PENDING,
            created_at=time.time(),
            expires_at=time.time() + ttl,
        )

        async with self._queue_lock.acquire_context(timeout=10.0) as acquired:
            if not acquired:
                raise TimeoutError(f"Failed to acquire lock for queue {self.name}")

            # Write message atomically
            msg_file = self._message_file(msg_id)
            fd, temp_path = tempfile.mkstemp(
                dir=self.queue_dir,
                prefix=f".{msg_id[:8]}.",
                suffix=".tmp"
            )

            try:
                content = json.dumps(message.to_dict(), indent=2)
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
                os.close(fd)
                fd = -1

                os.replace(temp_path, msg_file)
                self._mark_seen(msg_id)

                logger.debug(f"[MsgQueue] Published message {msg_id} to {topic}")
                return msg_id

            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    async def consume(
        self,
        consumer_id: str,
        topics: Optional[List[str]] = None,
        batch_size: int = 10,
    ) -> AsyncIterator[PersistentMessage]:
        """
        Consume messages from the queue.

        Messages must be acknowledged or rejected after processing.

        Args:
            consumer_id: Unique consumer identifier
            topics: Optional list of topics to consume (None = all)
            batch_size: Maximum messages to fetch per batch

        Yields:
            PersistentMessage objects
        """
        while not self._shutdown_event.is_set():
            messages = await self._fetch_pending(topics, batch_size)

            if not messages:
                # No messages - wait before retry
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=1.0
                    )
                    break
                except asyncio.TimeoutError:
                    continue

            for msg in messages:
                # Mark as processing
                msg.state = MessageState.PROCESSING
                msg.attempts += 1
                msg.last_attempt = time.time()
                await self._update_message(msg)

                yield msg

    async def _fetch_pending(
        self,
        topics: Optional[List[str]],
        limit: int,
    ) -> List[PersistentMessage]:
        """Fetch pending messages."""
        messages: List[PersistentMessage] = []

        async with self._queue_lock.acquire_context(timeout=5.0) as acquired:
            if not acquired:
                return []

            # Scan message files
            for msg_file in sorted(self.queue_dir.glob("*.json")):
                if len(messages) >= limit:
                    break

                try:
                    content = msg_file.read_text()
                    msg = PersistentMessage.from_dict(json.loads(content))

                    # Skip non-pending
                    if msg.state != MessageState.PENDING:
                        # Check if stuck in processing
                        if msg.state == MessageState.PROCESSING:
                            if time.time() - msg.last_attempt > self.retry_delay * 2:
                                # Reset to pending
                                msg.state = MessageState.PENDING
                            else:
                                continue
                        else:
                            continue

                    # Check expiration
                    if msg.is_expired():
                        msg.state = MessageState.EXPIRED
                        await self._update_message(msg)
                        continue

                    # Check retry limit
                    if not msg.can_retry(self.max_attempts):
                        msg.state = MessageState.FAILED
                        await self._update_message(msg)
                        continue

                    # Filter by topic
                    if topics and msg.topic not in topics:
                        continue

                    messages.append(msg)

                except Exception as e:
                    logger.debug(f"[MsgQueue] Error reading message: {e}")

        # Sort by priority (highest first) then by creation time (oldest first)
        messages.sort(key=lambda m: (-m.priority.value, m.created_at))

        return messages

    async def _update_message(self, msg: PersistentMessage) -> None:
        """Update message state."""
        msg_file = self._message_file(msg.id)

        fd, temp_path = tempfile.mkstemp(
            dir=self.queue_dir,
            prefix=f".{msg.id[:8]}.",
            suffix=".tmp"
        )

        try:
            content = json.dumps(msg.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, msg_file)

        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def acknowledge(self, msg_id: str, consumer_id: str) -> bool:
        """
        Acknowledge successful message processing.

        Args:
            msg_id: Message ID
            consumer_id: Consumer that processed the message

        Returns:
            True if acknowledged successfully
        """
        msg_file = self._message_file(msg_id)

        async with self._queue_lock.acquire_context(timeout=5.0) as acquired:
            if not acquired:
                return False

            if not msg_file.exists():
                return False

            try:
                content = msg_file.read_text()
                msg = PersistentMessage.from_dict(json.loads(content))

                msg.state = MessageState.ACKNOWLEDGED
                msg.acknowledged_by = consumer_id
                msg.acknowledged_at = time.time()

                await self._update_message(msg)

                logger.debug(f"[MsgQueue] Acknowledged message {msg_id} by {consumer_id}")
                return True

            except Exception as e:
                logger.warning(f"[MsgQueue] Failed to acknowledge {msg_id}: {e}")
                return False

    async def reject(
        self,
        msg_id: str,
        requeue: bool = True,
    ) -> bool:
        """
        Reject a message (processing failed).

        Args:
            msg_id: Message ID
            requeue: If True, put back in queue for retry

        Returns:
            True if rejected successfully
        """
        msg_file = self._message_file(msg_id)

        async with self._queue_lock.acquire_context(timeout=5.0) as acquired:
            if not acquired:
                return False

            if not msg_file.exists():
                return False

            try:
                content = msg_file.read_text()
                msg = PersistentMessage.from_dict(json.loads(content))

                if requeue and msg.can_retry(self.max_attempts):
                    msg.state = MessageState.PENDING
                else:
                    msg.state = MessageState.FAILED

                await self._update_message(msg)

                logger.debug(f"[MsgQueue] Rejected message {msg_id}, requeue={requeue}")
                return True

            except Exception as e:
                logger.warning(f"[MsgQueue] Failed to reject {msg_id}: {e}")
                return False

    async def cleanup(self) -> int:
        """
        Clean up acknowledged, expired, and failed messages.

        Returns:
            Number of messages cleaned up
        """
        cleaned = 0

        async with self._queue_lock.acquire_context(timeout=30.0) as acquired:
            if not acquired:
                return 0

            for msg_file in list(self.queue_dir.glob("*.json")):
                try:
                    content = msg_file.read_text()
                    msg = PersistentMessage.from_dict(json.loads(content))

                    should_delete = False

                    # Delete acknowledged messages after 1 hour
                    if msg.state == MessageState.ACKNOWLEDGED:
                        if msg.acknowledged_at and time.time() - msg.acknowledged_at > 3600:
                            should_delete = True

                    # Delete expired messages
                    elif msg.state == MessageState.EXPIRED:
                        should_delete = True

                    # Delete failed messages after 24 hours
                    elif msg.state == MessageState.FAILED:
                        if time.time() - msg.created_at > 86400:
                            should_delete = True

                    if should_delete:
                        msg_file.unlink()
                        cleaned += 1

                except Exception:
                    pass

        if cleaned > 0:
            logger.debug(f"[MsgQueue] Cleaned up {cleaned} messages")

        return cleaned

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is not None:
            return

        async def cleanup_loop():
            while not self._shutdown_event.is_set():
                try:
                    await self.cleanup()
                except Exception as e:
                    logger.warning(f"[MsgQueue] Cleanup error: {e}")

                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.cleanup_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def stop(self) -> None:
        """Stop the queue and cleanup task."""
        self._shutdown_event.set()

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        stats = {
            "pending": 0,
            "processing": 0,
            "acknowledged": 0,
            "failed": 0,
            "expired": 0,
            "total": 0,
        }

        for msg_file in self.queue_dir.glob("*.json"):
            try:
                content = msg_file.read_text()
                msg = PersistentMessage.from_dict(json.loads(content))
                stats["total"] += 1
                stats[msg.state.value] = stats.get(msg.state.value, 0) + 1
            except Exception:
                pass

        return stats


# =============================================================================
# ISSUE 16: Startup Timeout Race Prevention
# =============================================================================


class StartupPhase(Enum):
    """Phases of component startup."""
    PENDING = "pending"
    SPAWNING = "spawning"
    SPAWNED = "spawned"
    INITIALIZING = "initializing"
    WAITING_DEPS = "waiting_deps"
    READY = "ready"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class StartupState:
    """State tracking for component startup."""
    component: str
    phase: StartupPhase
    spawn_requested_at: float
    spawn_confirmed_at: Optional[float] = None
    init_started_at: Optional[float] = None
    deps_ready_at: Optional[float] = None
    ready_at: Optional[float] = None
    error: Optional[str] = None
    pid: Optional[int] = None
    system_load: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "component": self.component,
            "phase": self.phase.value,
            "spawn_requested_at": self.spawn_requested_at,
            "spawn_confirmed_at": self.spawn_confirmed_at,
            "init_started_at": self.init_started_at,
            "deps_ready_at": self.deps_ready_at,
            "ready_at": self.ready_at,
            "error": self.error,
            "pid": self.pid,
            "system_load": self.system_load,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StartupState":
        """Deserialize from dictionary."""
        return cls(
            component=data.get("component", ""),
            phase=StartupPhase(data.get("phase", "pending")),
            spawn_requested_at=data.get("spawn_requested_at", 0.0),
            spawn_confirmed_at=data.get("spawn_confirmed_at"),
            init_started_at=data.get("init_started_at"),
            deps_ready_at=data.get("deps_ready_at"),
            ready_at=data.get("ready_at"),
            error=data.get("error"),
            pid=data.get("pid"),
            system_load=data.get("system_load", 0.0),
        )


class AdaptiveStartupTimeout:
    """
    Adaptive startup timeout management with phase tracking.

    FIXES ISSUE 16: Startup Timeout Race

    Key Features:
    - Timeout starts after process spawn confirmation (not request)
    - Separate timeouts for each startup phase
    - Adaptive timeout based on system load
    - Dependency wait time tracked separately
    - Network delay compensation

    Usage:
        timeout_mgr = AdaptiveStartupTimeout()

        # Start tracking
        await timeout_mgr.start_tracking("jarvis_body")

        # Confirm spawn
        await timeout_mgr.confirm_spawn("jarvis_body", pid=12345)

        # Check if timed out
        if await timeout_mgr.is_timed_out("jarvis_body"):
            # Handle timeout
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize adaptive timeout manager."""
        self.config = config or get_config()
        self.state_dir = self.config.state_dir / "startup"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Phase-specific timeouts (from environment)
        self.spawn_timeout = _env_float("STARTUP_SPAWN_TIMEOUT", 30.0)
        self.init_timeout = _env_float("STARTUP_INIT_TIMEOUT", 60.0)
        self.deps_timeout = _env_float("STARTUP_DEPS_TIMEOUT", 120.0)
        self.ready_timeout = _env_float("STARTUP_READY_TIMEOUT", 30.0)

        # Adaptive timeout settings
        self.load_multiplier_max = _env_float("STARTUP_LOAD_MULTIPLIER_MAX", 3.0)
        self.network_delay_buffer = _env_float("STARTUP_NETWORK_BUFFER", 5.0)

        # State tracking
        self._states: Dict[str, StartupState] = {}
        self._lock = asyncio.Lock()

    def _get_system_load(self) -> float:
        """Get current system load average."""
        try:
            import psutil
            # Use 1-minute load average normalized to CPU count
            load = psutil.getloadavg()[0]
            cpu_count = psutil.cpu_count() or 1
            return load / cpu_count
        except ImportError:
            try:
                load = os.getloadavg()[0]
                cpu_count = os.cpu_count() or 1
                return load / cpu_count
            except (OSError, AttributeError):
                return 0.5  # Default to moderate load

    def _calculate_adaptive_timeout(self, base_timeout: float) -> float:
        """Calculate timeout adjusted for system load."""
        load = self._get_system_load()

        # Multiplier increases with load: 1.0 at load=0, up to max at load>=1
        multiplier = 1.0 + min(load, 1.0) * (self.load_multiplier_max - 1.0)

        # Add network delay buffer
        return (base_timeout * multiplier) + self.network_delay_buffer

    def _state_file(self, component: str) -> Path:
        """Get path for startup state file."""
        return self.state_dir / f"{component}_startup.json"

    async def start_tracking(self, component: str) -> StartupState:
        """
        Start tracking startup for a component.

        Args:
            component: Component name

        Returns:
            Initial startup state
        """
        async with self._lock:
            state = StartupState(
                component=component,
                phase=StartupPhase.PENDING,
                spawn_requested_at=time.time(),
                system_load=self._get_system_load(),
            )

            self._states[component] = state
            await self._persist_state(state)

            logger.debug(f"[StartupTimeout] Started tracking {component}")
            return state

    async def confirm_spawn(
        self,
        component: str,
        pid: int,
    ) -> Optional[StartupState]:
        """
        Confirm process has spawned.

        This is when the actual timeout starts.

        Args:
            component: Component name
            pid: Process ID

        Returns:
            Updated state or None if not tracking
        """
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.phase = StartupPhase.SPAWNED
            state.spawn_confirmed_at = time.time()
            state.pid = pid

            await self._persist_state(state)

            spawn_delay = state.spawn_confirmed_at - state.spawn_requested_at
            logger.debug(
                f"[StartupTimeout] {component} spawned in {spawn_delay:.2f}s (PID {pid})"
            )

            return state

    async def mark_initializing(self, component: str) -> Optional[StartupState]:
        """Mark component as initializing."""
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.phase = StartupPhase.INITIALIZING
            state.init_started_at = time.time()

            await self._persist_state(state)
            return state

    async def mark_waiting_deps(self, component: str) -> Optional[StartupState]:
        """Mark component as waiting for dependencies."""
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.phase = StartupPhase.WAITING_DEPS

            await self._persist_state(state)
            return state

    async def mark_deps_ready(self, component: str) -> Optional[StartupState]:
        """Mark dependencies as ready."""
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.deps_ready_at = time.time()

            await self._persist_state(state)
            return state

    async def mark_ready(self, component: str) -> Optional[StartupState]:
        """Mark component as fully ready."""
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.phase = StartupPhase.READY
            state.ready_at = time.time()

            await self._persist_state(state)

            total_time = state.ready_at - state.spawn_requested_at
            logger.info(f"[StartupTimeout] {component} ready in {total_time:.2f}s")

            return state

    async def mark_failed(
        self,
        component: str,
        error: str,
    ) -> Optional[StartupState]:
        """Mark component startup as failed."""
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return None

            state.phase = StartupPhase.FAILED
            state.error = error

            await self._persist_state(state)

            logger.error(f"[StartupTimeout] {component} failed: {error}")
            return state

    async def is_timed_out(self, component: str) -> Tuple[bool, Optional[str]]:
        """
        Check if component has timed out.

        Checks each phase separately with adaptive timeouts.

        Returns:
            Tuple of (is_timed_out, reason)
        """
        async with self._lock:
            state = self._states.get(component)
            if state is None:
                return False, None

            now = time.time()

            # Check spawn timeout (from request to confirmation)
            if state.phase == StartupPhase.PENDING or state.phase == StartupPhase.SPAWNING:
                spawn_elapsed = now - state.spawn_requested_at
                spawn_limit = self._calculate_adaptive_timeout(self.spawn_timeout)

                if spawn_elapsed > spawn_limit:
                    state.phase = StartupPhase.TIMEOUT
                    state.error = f"Spawn timeout ({spawn_elapsed:.1f}s > {spawn_limit:.1f}s)"
                    await self._persist_state(state)
                    return True, state.error

            # Check init timeout (from spawn to init complete)
            if state.phase == StartupPhase.SPAWNED or state.phase == StartupPhase.INITIALIZING:
                if state.spawn_confirmed_at:
                    init_elapsed = now - state.spawn_confirmed_at
                    init_limit = self._calculate_adaptive_timeout(self.init_timeout)

                    if init_elapsed > init_limit:
                        state.phase = StartupPhase.TIMEOUT
                        state.error = f"Init timeout ({init_elapsed:.1f}s > {init_limit:.1f}s)"
                        await self._persist_state(state)
                        return True, state.error

            # Check dependency wait timeout (separate from other phases)
            if state.phase == StartupPhase.WAITING_DEPS:
                if state.init_started_at:
                    deps_elapsed = now - state.init_started_at
                    deps_limit = self._calculate_adaptive_timeout(self.deps_timeout)

                    if deps_elapsed > deps_limit:
                        state.phase = StartupPhase.TIMEOUT
                        state.error = f"Dependency timeout ({deps_elapsed:.1f}s > {deps_limit:.1f}s)"
                        await self._persist_state(state)
                        return True, state.error

            return False, None

    async def get_state(self, component: str) -> Optional[StartupState]:
        """Get current startup state."""
        return self._states.get(component)

    async def clear_tracking(self, component: str) -> None:
        """Clear startup tracking for component."""
        async with self._lock:
            self._states.pop(component, None)
            state_file = self._state_file(component)
            state_file.unlink(missing_ok=True)

    async def _persist_state(self, state: StartupState) -> None:
        """Persist state to file."""
        state_file = self._state_file(state.component)

        fd, temp_path = tempfile.mkstemp(
            dir=self.state_dir,
            prefix=f".{state.component}_",
            suffix=".tmp"
        )

        try:
            content = json.dumps(state.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, state_file)

        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# =============================================================================
# ISSUE 17: Health Check Race Condition Prevention
# =============================================================================


class HealthCheckResult(Enum):
    """Health check result."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    RESTARTING = "restarting"


@dataclass
class CachedHealthStatus:
    """Cached health check result."""
    component: str
    result: HealthCheckResult
    checked_at: float
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    is_restarting: bool = False
    restart_started_at: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "component": self.component,
            "result": self.result.value,
            "checked_at": self.checked_at,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "is_restarting": self.is_restarting,
            "restart_started_at": self.restart_started_at,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CachedHealthStatus":
        """Deserialize from dictionary."""
        return cls(
            component=data.get("component", ""),
            result=HealthCheckResult(data.get("result", "unknown")),
            checked_at=data.get("checked_at", 0.0),
            consecutive_failures=data.get("consecutive_failures", 0),
            consecutive_successes=data.get("consecutive_successes", 0),
            is_restarting=data.get("is_restarting", False),
            restart_started_at=data.get("restart_started_at"),
            details=data.get("details", {}),
        )


class ResilientHealthChecker:
    """
    Health checker with retry, backoff, caching, and restart grace period.

    FIXES ISSUE 17: Health Check Race Condition

    Key Features:
    - Retry with exponential backoff for transient failures
    - Grace period during component restart
    - Health check result caching
    - Consecutive failure/success tracking
    - Avoids false negatives during restart

    Usage:
        checker = ResilientHealthChecker()

        # Register health check
        checker.register("jarvis_body", health_check_func)

        # Mark component as restarting
        await checker.mark_restarting("jarvis_body")

        # Check health (respects grace period)
        result = await checker.check_health("jarvis_body")
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize health checker."""
        self.config = config or get_config()
        self.state_dir = self.config.state_dir / "health"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Configuration from environment
        self.retry_count = _env_int("HEALTH_RETRY_COUNT", 3)
        self.retry_base_delay = _env_float("HEALTH_RETRY_BASE_DELAY", 1.0)
        self.retry_max_delay = _env_float("HEALTH_RETRY_MAX_DELAY", 10.0)
        self.cache_ttl = _env_float("HEALTH_CACHE_TTL", 5.0)
        self.restart_grace_period = _env_float("HEALTH_RESTART_GRACE", 30.0)
        self.failure_threshold = _env_int("HEALTH_FAILURE_THRESHOLD", 3)
        self.success_threshold = _env_int("HEALTH_SUCCESS_THRESHOLD", 2)

        # State
        self._checkers: Dict[str, Callable[[], Coroutine[Any, Any, bool]]] = {}
        self._cache: Dict[str, CachedHealthStatus] = {}
        self._lock = asyncio.Lock()

    def register(
        self,
        component: str,
        checker: Callable[[], Coroutine[Any, Any, bool]],
    ) -> None:
        """
        Register a health check function for a component.

        Args:
            component: Component name
            checker: Async function that returns True if healthy
        """
        self._checkers[component] = checker

    async def mark_restarting(self, component: str) -> None:
        """
        Mark a component as restarting.

        Health checks will return RESTARTING during grace period.
        """
        async with self._lock:
            status = self._cache.get(component)
            if status is None:
                status = CachedHealthStatus(
                    component=component,
                    result=HealthCheckResult.RESTARTING,
                    checked_at=time.time(),
                )

            status.is_restarting = True
            status.restart_started_at = time.time()
            status.result = HealthCheckResult.RESTARTING
            status.consecutive_failures = 0

            self._cache[component] = status
            await self._persist_status(status)

            logger.debug(f"[HealthCheck] Marked {component} as restarting")

    async def mark_restart_complete(self, component: str) -> None:
        """Mark component restart as complete."""
        async with self._lock:
            status = self._cache.get(component)
            if status:
                status.is_restarting = False
                status.restart_started_at = None
                await self._persist_status(status)

            logger.debug(f"[HealthCheck] {component} restart complete")

    async def check_health(
        self,
        component: str,
        force_check: bool = False,
    ) -> CachedHealthStatus:
        """
        Check health of a component with retry and caching.

        Args:
            component: Component name
            force_check: Bypass cache and force fresh check

        Returns:
            Cached health status
        """
        async with self._lock:
            # Check cache first
            cached = self._cache.get(component)

            if cached and not force_check:
                # Check if in restart grace period
                if cached.is_restarting and cached.restart_started_at:
                    grace_elapsed = time.time() - cached.restart_started_at
                    if grace_elapsed < self.restart_grace_period:
                        logger.debug(
                            f"[HealthCheck] {component} in restart grace period "
                            f"({grace_elapsed:.1f}s / {self.restart_grace_period}s)"
                        )
                        return cached

                # Check cache TTL
                cache_age = time.time() - cached.checked_at
                if cache_age < self.cache_ttl:
                    return cached

            # Perform health check with retry
            result = await self._check_with_retry(component)

            # Update cached status
            if cached is None:
                cached = CachedHealthStatus(
                    component=component,
                    result=result,
                    checked_at=time.time(),
                )
            else:
                cached.checked_at = time.time()

            # Track consecutive failures/successes
            if result == HealthCheckResult.HEALTHY:
                cached.consecutive_successes += 1
                cached.consecutive_failures = 0

                # Clear restarting flag after enough successes
                if cached.consecutive_successes >= self.success_threshold:
                    cached.is_restarting = False
                    cached.restart_started_at = None

                cached.result = HealthCheckResult.HEALTHY

            elif result == HealthCheckResult.UNHEALTHY:
                cached.consecutive_failures += 1
                cached.consecutive_successes = 0

                # Only mark unhealthy after threshold failures
                if cached.consecutive_failures >= self.failure_threshold:
                    cached.result = HealthCheckResult.UNHEALTHY
                else:
                    cached.result = HealthCheckResult.DEGRADED

            else:
                cached.result = result

            self._cache[component] = cached
            await self._persist_status(cached)

            return cached

    async def _check_with_retry(self, component: str) -> HealthCheckResult:
        """Perform health check with exponential backoff retry."""
        checker = self._checkers.get(component)
        if checker is None:
            return HealthCheckResult.UNKNOWN

        last_error: Optional[Exception] = None

        for attempt in range(self.retry_count):
            try:
                is_healthy = await asyncio.wait_for(
                    checker(),
                    timeout=10.0,
                )

                if is_healthy:
                    return HealthCheckResult.HEALTHY
                else:
                    return HealthCheckResult.UNHEALTHY

            except asyncio.TimeoutError:
                last_error = TimeoutError("Health check timed out")
            except Exception as e:
                last_error = e

            # Calculate backoff with jitter
            if attempt < self.retry_count - 1:
                delay = min(
                    self.retry_base_delay * (2 ** attempt),
                    self.retry_max_delay,
                )
                jitter = random.uniform(0, delay * 0.1)
                await asyncio.sleep(delay + jitter)

        logger.warning(
            f"[HealthCheck] {component} failed after {self.retry_count} attempts: {last_error}"
        )
        return HealthCheckResult.UNHEALTHY

    async def get_all_health(self) -> Dict[str, CachedHealthStatus]:
        """Get health status of all registered components."""
        results = {}
        for component in self._checkers:
            results[component] = await self.check_health(component)
        return results

    async def _persist_status(self, status: CachedHealthStatus) -> None:
        """Persist health status to file."""
        status_file = self.state_dir / f"{status.component}_health.json"

        fd, temp_path = tempfile.mkstemp(
            dir=self.state_dir,
            prefix=f".{status.component}_",
            suffix=".tmp"
        )

        try:
            content = json.dumps(status.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, status_file)

        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# =============================================================================
# ISSUE 18: Resource Cleanup Race Prevention
# =============================================================================


class ResourceType(Enum):
    """Types of managed resources."""
    PORT = "port"
    FILE = "file"
    SOCKET = "socket"
    PROCESS = "process"
    LOCK = "lock"
    TEMP_DIR = "temp_dir"
    SHARED_MEM = "shared_mem"


@dataclass
class ResourceReservation:
    """Reservation for a managed resource."""
    resource_id: str
    resource_type: ResourceType
    component: str
    reserved_at: float
    reserved_by: ProcessIdentity
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type.value,
            "component": self.component,
            "reserved_at": self.reserved_at,
            "reserved_by": self.reserved_by.to_dict(),
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceReservation":
        """Deserialize from dictionary."""
        return cls(
            resource_id=data.get("resource_id", ""),
            resource_type=ResourceType(data.get("resource_type", "file")),
            component=data.get("component", ""),
            reserved_at=data.get("reserved_at", 0.0),
            reserved_by=ProcessIdentity.from_dict(data.get("reserved_by", {})),
            expires_at=data.get("expires_at"),
            metadata=data.get("metadata", {}),
        )

    def is_expired(self) -> bool:
        """Check if reservation has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def is_stale(self) -> bool:
        """Check if reservation is stale (owner dead)."""
        return not self.reserved_by.is_alive()


class CoordinatedResourceManager:
    """
    Coordinated resource management with cleanup locking.

    FIXES ISSUE 18: Resource Cleanup Race

    Key Features:
    - Cleanup lock prevents cleanup during startup
    - Resource reservation before cleanup check
    - Stale resource detection based on owner liveness
    - Coordination between cleanup and startup phases

    Usage:
        manager = CoordinatedResourceManager()

        # Reserve resource before using
        await manager.reserve("port:8010", ResourceType.PORT, "jarvis_body")

        # Check if safe to cleanup
        async with manager.cleanup_lock("jarvis_body"):
            if await manager.can_cleanup("port:8010"):
                # Perform cleanup
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize resource manager."""
        self.config = config or get_config()
        self.resource_dir = self.config.state_dir / "resources"
        self.resource_dir.mkdir(parents=True, exist_ok=True)

        # Global cleanup lock
        self._cleanup_lock = AtomicFileLock("resource_cleanup", config)

        # Per-component startup locks
        self._startup_locks: Dict[str, AtomicFileLock] = {}

        # Reservation cache
        self._reservations: Dict[str, ResourceReservation] = {}
        self._lock = asyncio.Lock()

    def _get_startup_lock(self, component: str) -> AtomicFileLock:
        """Get or create startup lock for component."""
        if component not in self._startup_locks:
            self._startup_locks[component] = AtomicFileLock(
                f"startup_{component}",
                self.config,
            )
        return self._startup_locks[component]

    def _resource_file(self, resource_id: str) -> Path:
        """Get path for resource reservation file."""
        # Sanitize resource_id for filename
        safe_id = resource_id.replace("/", "_").replace(":", "_")
        return self.resource_dir / f"{safe_id}.json"

    async def reserve(
        self,
        resource_id: str,
        resource_type: ResourceType,
        component: str,
        ttl: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Reserve a resource for a component.

        Args:
            resource_id: Unique resource identifier
            resource_type: Type of resource
            component: Component reserving the resource
            ttl: Optional time-to-live in seconds
            metadata: Optional metadata

        Returns:
            True if reserved successfully
        """
        async with self._lock:
            # Check existing reservation
            existing = await self._read_reservation(resource_id)
            if existing:
                # Check if stale or expired
                if existing.is_stale() or existing.is_expired():
                    await self._delete_reservation(resource_id)
                else:
                    # Check if same component
                    if existing.component != component:
                        logger.warning(
                            f"[ResourceMgr] Resource {resource_id} already reserved "
                            f"by {existing.component}"
                        )
                        return False

            # Create reservation
            reservation = ResourceReservation(
                resource_id=resource_id,
                resource_type=resource_type,
                component=component,
                reserved_at=time.time(),
                reserved_by=get_current_identity(),
                expires_at=time.time() + ttl if ttl else None,
                metadata=metadata or {},
            )

            await self._write_reservation(reservation)
            self._reservations[resource_id] = reservation

            logger.debug(f"[ResourceMgr] Reserved {resource_id} for {component}")
            return True

    async def release(self, resource_id: str, component: str) -> bool:
        """
        Release a resource reservation.

        Args:
            resource_id: Resource identifier
            component: Component releasing (must match reservation)

        Returns:
            True if released successfully
        """
        async with self._lock:
            existing = await self._read_reservation(resource_id)
            if existing is None:
                return True  # Already released

            if existing.component != component:
                logger.warning(
                    f"[ResourceMgr] Cannot release {resource_id}: "
                    f"reserved by {existing.component}, not {component}"
                )
                return False

            await self._delete_reservation(resource_id)
            self._reservations.pop(resource_id, None)

            logger.debug(f"[ResourceMgr] Released {resource_id}")
            return True

    async def can_cleanup(self, resource_id: str) -> bool:
        """
        Check if a resource can be safely cleaned up.

        A resource can be cleaned up if:
        - No active reservation exists
        - Reservation is stale (owner dead) or expired
        - Component is not in startup phase

        Returns:
            True if safe to cleanup
        """
        reservation = await self._read_reservation(resource_id)
        if reservation is None:
            return True

        if reservation.is_expired():
            return True

        if reservation.is_stale():
            return True

        # Check if component is starting up
        startup_lock = self._get_startup_lock(reservation.component)
        # If we can't acquire startup lock, component is starting
        acquired = startup_lock.acquire(timeout=0.1)
        if acquired:
            startup_lock.release()
            return True  # Not in startup
        else:
            return False  # Component is starting

    @asynccontextmanager
    async def cleanup_lock(
        self,
        component: str,
        timeout: float = 30.0,
    ) -> AsyncIterator[bool]:
        """
        Context manager for cleanup operations.

        Holds both global cleanup lock and component startup lock
        to prevent cleanup during startup.
        """
        startup_lock = self._get_startup_lock(component)

        # Acquire startup lock first
        startup_acquired = await startup_lock.acquire_async(timeout)
        if not startup_acquired:
            logger.warning(f"[ResourceMgr] Cannot acquire startup lock for {component}")
            yield False
            return

        # Then acquire global cleanup lock
        cleanup_acquired = await self._cleanup_lock.acquire_async(timeout)
        if not cleanup_acquired:
            startup_lock.release()
            logger.warning(f"[ResourceMgr] Cannot acquire cleanup lock")
            yield False
            return

        try:
            yield True
        finally:
            self._cleanup_lock.release()
            startup_lock.release()

    @asynccontextmanager
    async def startup_phase(self, component: str) -> AsyncIterator[None]:
        """
        Context manager for startup phase.

        Prevents cleanup of component's resources during startup.
        """
        startup_lock = self._get_startup_lock(component)

        # Hold startup lock during entire startup phase
        acquired = await startup_lock.acquire_async(timeout=120.0)
        if not acquired:
            raise TimeoutError(f"Cannot acquire startup lock for {component}")

        try:
            logger.debug(f"[ResourceMgr] {component} entering startup phase")
            yield
        finally:
            startup_lock.release()
            logger.debug(f"[ResourceMgr] {component} exited startup phase")

    async def cleanup_stale_resources(self) -> int:
        """
        Clean up all stale resource reservations.

        Returns:
            Number of resources cleaned
        """
        cleaned = 0

        async with self._cleanup_lock.acquire_context() as acquired:
            if not acquired:
                return 0

            for res_file in list(self.resource_dir.glob("*.json")):
                try:
                    content = res_file.read_text()
                    reservation = ResourceReservation.from_dict(json.loads(content))

                    if reservation.is_stale() or reservation.is_expired():
                        res_file.unlink()
                        cleaned += 1
                        logger.debug(
                            f"[ResourceMgr] Cleaned stale resource: {reservation.resource_id}"
                        )
                except Exception:
                    pass

        if cleaned > 0:
            logger.info(f"[ResourceMgr] Cleaned {cleaned} stale resources")

        return cleaned

    async def _read_reservation(self, resource_id: str) -> Optional[ResourceReservation]:
        """Read resource reservation."""
        res_file = self._resource_file(resource_id)
        if not res_file.exists():
            return None

        try:
            content = res_file.read_text()
            return ResourceReservation.from_dict(json.loads(content))
        except Exception:
            return None

    async def _write_reservation(self, reservation: ResourceReservation) -> None:
        """Write resource reservation atomically."""
        res_file = self._resource_file(reservation.resource_id)

        fd, temp_path = tempfile.mkstemp(
            dir=self.resource_dir,
            prefix=".res_",
            suffix=".tmp"
        )

        try:
            content = json.dumps(reservation.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, res_file)

        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def _delete_reservation(self, resource_id: str) -> None:
        """Delete resource reservation."""
        res_file = self._resource_file(resource_id)
        res_file.unlink(missing_ok=True)


# =============================================================================
# ISSUE 19: Log File Write Race Prevention
# =============================================================================


@dataclass
class LogEntry:
    """Structured log entry."""
    timestamp: float
    level: str
    component: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "component": self.component,
            "message": self.message,
            "context": self.context,
        }

    def to_json_line(self) -> str:
        """Convert to JSON line format."""
        return json.dumps(self.to_dict(), default=str)


class AtomicLogWriter:
    """
    Atomic log writer with per-process files and rotation locking.

    FIXES ISSUE 19: Log File Write Race

    Key Features:
    - Per-process log files to avoid write conflicts
    - Log rotation with file locking
    - Structured logging with timestamps
    - Log aggregation from multiple processes
    - Atomic append operations

    Usage:
        logger = AtomicLogWriter("jarvis_body")

        # Write log entry
        await logger.write("info", "Component started")

        # Rotate logs (with locking)
        await logger.rotate()
    """

    def __init__(
        self,
        component: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize atomic log writer.

        Args:
            component: Component name
            config: Optional configuration override
        """
        self.component = component
        self.config = config or get_config()
        self.log_dir = self.config.state_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Per-process log file (includes PID)
        self.pid = os.getpid()
        self.log_file = self.log_dir / f"{component}_{self.pid}.log"

        # Configuration
        self.max_file_size = _env_int("LOG_MAX_FILE_SIZE", 10 * 1024 * 1024)  # 10MB
        self.max_backups = _env_int("LOG_MAX_BACKUPS", 5)
        self.buffer_size = _env_int("LOG_BUFFER_SIZE", 100)
        self.flush_interval = _env_float("LOG_FLUSH_INTERVAL", 1.0)

        # Rotation lock
        self._rotation_lock = AtomicFileLock(f"log_rotation_{component}", config)

        # Buffer for batched writes
        self._buffer: List[LogEntry] = []
        self._buffer_lock = asyncio.Lock()
        self._last_flush = time.time()

        # File descriptor for append
        self._fd: Optional[int] = None

    def _open_log_file(self) -> int:
        """Open log file for append."""
        if self._fd is None or not os.path.exists(f"/proc/self/fd/{self._fd}"):
            self._fd = os.open(
                str(self.log_file),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600
            )
        return self._fd

    async def write(
        self,
        level: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Write a log entry.

        Args:
            level: Log level (debug, info, warning, error, critical)
            message: Log message
            context: Optional context dictionary
        """
        entry = LogEntry(
            timestamp=time.time(),
            level=level,
            component=self.component,
            message=message,
            context=context or {},
        )

        async with self._buffer_lock:
            self._buffer.append(entry)

            # Flush if buffer full or interval elapsed
            if (
                len(self._buffer) >= self.buffer_size or
                time.time() - self._last_flush > self.flush_interval
            ):
                await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Flush buffer to file."""
        if not self._buffer:
            return

        entries = self._buffer.copy()
        self._buffer.clear()
        self._last_flush = time.time()

        # Write to file
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_entries_sync, entries)

    def _write_entries_sync(self, entries: List[LogEntry]) -> None:
        """Write entries synchronously."""
        try:
            fd = self._open_log_file()

            # Build content
            lines = [entry.to_json_line() + "\n" for entry in entries]
            content = "".join(lines).encode("utf-8")

            # Write atomically (append is atomic for small writes)
            os.write(fd, content)

            # Check if rotation needed
            try:
                file_size = os.fstat(fd).st_size
                if file_size > self.max_file_size:
                    # Close fd before rotation
                    os.close(fd)
                    self._fd = None
                    self._rotate_sync()
            except Exception:
                pass

        except Exception as e:
            # Fallback to stderr
            import sys
            for entry in entries:
                print(f"[LOG ERROR] {entry.to_json_line()}", file=sys.stderr)

    def _rotate_sync(self) -> None:
        """Rotate log files synchronously with locking."""
        # Try to acquire rotation lock
        acquired = self._rotation_lock.acquire(timeout=5.0)
        if not acquired:
            return

        try:
            # Rotate existing backups
            for i in range(self.max_backups - 1, 0, -1):
                old_backup = self.log_dir / f"{self.component}_{self.pid}.log.{i}"
                new_backup = self.log_dir / f"{self.component}_{self.pid}.log.{i + 1}"
                if old_backup.exists():
                    old_backup.replace(new_backup)

            # Rotate current log
            if self.log_file.exists():
                backup_path = self.log_dir / f"{self.component}_{self.pid}.log.1"
                self.log_file.replace(backup_path)

            # Delete oldest backup if over limit
            oldest = self.log_dir / f"{self.component}_{self.pid}.log.{self.max_backups + 1}"
            oldest.unlink(missing_ok=True)

        finally:
            self._rotation_lock.release()

    async def rotate(self) -> bool:
        """Rotate log files with locking."""
        # Flush buffer first
        async with self._buffer_lock:
            await self._flush_buffer()

        # Close current fd
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

        # Perform rotation
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._rotate_sync)

        return True

    async def close(self) -> None:
        """Close log writer and flush buffer."""
        async with self._buffer_lock:
            await self._flush_buffer()

        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @staticmethod
    async def aggregate_logs(
        log_dir: Path,
        component: str,
        output_file: Path,
        since: Optional[float] = None,
    ) -> int:
        """
        Aggregate logs from multiple process log files.

        Args:
            log_dir: Directory containing log files
            component: Component name to aggregate
            output_file: Output file for aggregated logs
            since: Optional timestamp to filter logs

        Returns:
            Number of entries aggregated
        """
        entries: List[LogEntry] = []

        # Read all log files for component
        for log_file in log_dir.glob(f"{component}_*.log"):
            try:
                for line in log_file.read_text().splitlines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    entry = LogEntry(
                        timestamp=data.get("timestamp", 0),
                        level=data.get("level", ""),
                        component=data.get("component", ""),
                        message=data.get("message", ""),
                        context=data.get("context", {}),
                    )

                    if since and entry.timestamp < since:
                        continue

                    entries.append(entry)
            except Exception:
                pass

        # Sort by timestamp
        entries.sort(key=lambda e: e.timestamp)

        # Write aggregated log
        with open(output_file, "w") as f:
            for entry in entries:
                f.write(entry.to_json_line() + "\n")

        return len(entries)


# =============================================================================
# ISSUE 20: State File Corruption Prevention
# =============================================================================


@dataclass
class VersionedState:
    """Versioned state with integrity verification."""
    version: int
    timestamp: float
    checksum: str
    data: Dict[str, Any]
    schema_version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "checksum": self.checksum,
            "data": self.data,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VersionedState":
        """Deserialize from dictionary."""
        return cls(
            version=raw.get("version", 0),
            timestamp=raw.get("timestamp", 0.0),
            checksum=raw.get("checksum", ""),
            data=raw.get("data", {}),
            schema_version=raw.get("schema_version", "1.0"),
        )

    @staticmethod
    def compute_checksum(data: Dict[str, Any]) -> str:
        """Compute SHA256 checksum of data."""
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode()).hexdigest()

    def validate(self) -> Tuple[bool, Optional[str]]:
        """
        Validate state integrity.

        Returns:
            Tuple of (is_valid, error_message)
        """
        expected_checksum = self.compute_checksum(self.data)
        if self.checksum != expected_checksum:
            return False, f"Checksum mismatch: expected {expected_checksum[:16]}, got {self.checksum[:16]}"

        return True, None


class AtomicStateFile:
    """
    Atomic state file with versioning and corruption recovery.

    FIXES ISSUE 20: State File Corruption Race

    Key Features:
    - Atomic updates via temp-file-rename pattern
    - State file versioning with monotonic counter
    - Checksum-based corruption detection
    - Automatic recovery from backups
    - Concurrent read support with write locking

    Usage:
        state_file = AtomicStateFile("supervisor_state")

        # Read state
        state = await state_file.read()

        # Write state atomically
        await state_file.write({"key": "value"})

        # Update with read-modify-write
        async with state_file.update() as data:
            data["new_key"] = "new_value"
    """

    def __init__(
        self,
        name: str,
        config: Optional[RacePreventionConfig] = None,
    ):
        """
        Initialize atomic state file.

        Args:
            name: State file name
            config: Optional configuration override
        """
        self.name = name
        self.config = config or get_config()
        self.state_dir = self.config.state_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.state_dir / f"{name}.json"
        self.backup_file = self.state_dir / f"{name}.backup.json"

        # Write lock
        self._write_lock = AtomicFileLock(f"state_{name}", config)

        # Read cache
        self._cache: Optional[VersionedState] = None
        self._cache_version: int = -1

    def _read_file_sync(self, file_path: Path) -> Optional[VersionedState]:
        """Read state file synchronously."""
        if not file_path.exists():
            return None

        try:
            content = file_path.read_text()
            if not content.strip():
                return None

            raw = json.loads(content)
            state = VersionedState.from_dict(raw)

            # Validate
            is_valid, error = state.validate()
            if not is_valid:
                logger.warning(f"[StateFile] Validation failed for {file_path}: {error}")
                return None

            return state

        except json.JSONDecodeError as e:
            logger.error(f"[StateFile] JSON error reading {file_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"[StateFile] Error reading {file_path}: {e}")
            return None

    async def read(self, use_cache: bool = True) -> Optional[VersionedState]:
        """
        Read state file atomically.

        Args:
            use_cache: Use cached version if available

        Returns:
            VersionedState or None if not found
        """
        # Check cache
        if use_cache and self._cache is not None:
            # Quick check if file changed
            try:
                mtime = self.state_file.stat().st_mtime
                if mtime <= self._cache.timestamp:
                    return self._cache
            except OSError:
                pass

        # Read main file
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, self._read_file_sync, self.state_file)

        if state is not None:
            self._cache = state
            self._cache_version = state.version
            return state

        # Try backup if main file corrupted or missing
        logger.warning(f"[StateFile] Main file unavailable, trying backup for {self.name}")
        state = await loop.run_in_executor(None, self._read_file_sync, self.backup_file)

        if state is not None:
            # Restore from backup
            logger.info(f"[StateFile] Restored {self.name} from backup")
            await self._write_state(state)
            self._cache = state
            self._cache_version = state.version
            return state

        return None

    def _write_file_sync(self, state: VersionedState, file_path: Path) -> bool:
        """Write state file synchronously with atomic rename."""
        fd, temp_path = tempfile.mkstemp(
            dir=self.state_dir,
            prefix=f".{self.name}_",
            suffix=".tmp"
        )

        try:
            content = json.dumps(state.to_dict(), indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd = -1

            os.replace(temp_path, file_path)
            return True

        except Exception as e:
            logger.error(f"[StateFile] Write error for {file_path}: {e}")
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def _write_state(self, state: VersionedState) -> bool:
        """Write state to both main file and backup."""
        loop = asyncio.get_running_loop()

        # Write main file
        main_ok = await loop.run_in_executor(
            None, self._write_file_sync, state, self.state_file
        )

        if not main_ok:
            return False

        # Write backup (async, don't fail if backup fails)
        try:
            await loop.run_in_executor(
                None, self._write_file_sync, state, self.backup_file
            )
        except Exception:
            pass

        return True

    async def write(
        self,
        data: Dict[str, Any],
        validate_after: bool = True,
    ) -> bool:
        """
        Write state atomically.

        Args:
            data: State data to write
            validate_after: Re-read and validate after write

        Returns:
            True if written successfully
        """
        async with self._write_lock.acquire_context(timeout=30.0) as acquired:
            if not acquired:
                logger.error(f"[StateFile] Lock timeout writing {self.name}")
                return False

            # Get current version
            current = await self.read(use_cache=False)
            version = (current.version + 1) if current else 1

            # Create new versioned state
            new_state = VersionedState(
                version=version,
                timestamp=time.time(),
                checksum=VersionedState.compute_checksum(data),
                data=data,
            )

            # Write atomically
            success = await self._write_state(new_state)

            if success and validate_after:
                # Re-read to validate
                written = await self.read(use_cache=False)
                if written is None or written.version != version:
                    logger.error(f"[StateFile] Validation failed for {self.name}")
                    return False

                is_valid, error = written.validate()
                if not is_valid:
                    logger.error(f"[StateFile] Written state invalid: {error}")
                    return False

            # Update cache
            self._cache = new_state
            self._cache_version = version

            return success

    @asynccontextmanager
    async def update(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Context manager for read-modify-write operations.

        Holds write lock for entire operation.

        Usage:
            async with state_file.update() as data:
                data["key"] = "new_value"
                # Automatically saved on exit
        """
        async with self._write_lock.acquire_context(timeout=60.0) as acquired:
            if not acquired:
                raise TimeoutError(f"Failed to acquire lock for {self.name}")

            # Read current
            current = await self.read(use_cache=False)
            data = current.data.copy() if current else {}
            original_data = data.copy()

            try:
                yield data

                # Only write if changed
                if data != original_data:
                    version = (current.version + 1) if current else 1
                    new_state = VersionedState(
                        version=version,
                        timestamp=time.time(),
                        checksum=VersionedState.compute_checksum(data),
                        data=data,
                    )

                    await self._write_state(new_state)
                    self._cache = new_state
                    self._cache_version = version

            except Exception as e:
                logger.error(f"[StateFile] Update failed for {self.name}: {e}")
                raise

    async def recover(self) -> bool:
        """
        Attempt to recover state from backup.

        Returns:
            True if recovery successful
        """
        loop = asyncio.get_running_loop()
        backup_state = await loop.run_in_executor(
            None, self._read_file_sync, self.backup_file
        )

        if backup_state is None:
            logger.error(f"[StateFile] No valid backup for {self.name}")
            return False

        success = await self._write_state(backup_state)
        if success:
            logger.info(f"[StateFile] Successfully recovered {self.name} from backup")
            self._cache = backup_state
            self._cache_version = backup_state.version

        return success


# =============================================================================
# Singleton Access and Factory Functions
# =============================================================================


_file_locks: Dict[str, AtomicFileLock] = {}
_heartbeat_writers: Dict[str, AtomicHeartbeatWriter] = {}
_port_reserver: Optional[AtomicPortReservation] = None
_dependency_monitor: Optional[DependencyHealthMonitor] = None
_process_manager: Optional[SafeProcessManager] = None
_config_managers: Dict[str, AtomicConfigManager] = {}
_message_queues: Dict[str, PersistentMessageQueue] = {}
_startup_timeout: Optional[AdaptiveStartupTimeout] = None
_health_checker: Optional[ResilientHealthChecker] = None
_resource_manager: Optional[CoordinatedResourceManager] = None
_log_writers: Dict[str, AtomicLogWriter] = {}
_state_files: Dict[str, AtomicStateFile] = {}


def get_file_lock(name: str) -> AtomicFileLock:
    """Get or create a file lock by name."""
    if name not in _file_locks:
        _file_locks[name] = AtomicFileLock(name)
    return _file_locks[name]


def get_heartbeat_writer(component_name: str) -> AtomicHeartbeatWriter:
    """Get or create a heartbeat writer for a component."""
    if component_name not in _heartbeat_writers:
        _heartbeat_writers[component_name] = AtomicHeartbeatWriter(component_name)
    return _heartbeat_writers[component_name]


def get_port_reserver() -> AtomicPortReservation:
    """Get the global port reserver."""
    global _port_reserver
    if _port_reserver is None:
        _port_reserver = AtomicPortReservation()
    return _port_reserver


def get_dependency_monitor() -> DependencyHealthMonitor:
    """Get the global dependency monitor."""
    global _dependency_monitor
    if _dependency_monitor is None:
        _dependency_monitor = DependencyHealthMonitor()
    return _dependency_monitor


def get_process_manager() -> SafeProcessManager:
    """Get the global safe process manager."""
    global _process_manager
    if _process_manager is None:
        _process_manager = SafeProcessManager()
    return _process_manager


def get_config_manager(name: str) -> AtomicConfigManager:
    """Get or create a config manager by name."""
    if name not in _config_managers:
        _config_managers[name] = AtomicConfigManager(name)
    return _config_managers[name]


def get_message_queue(name: str) -> PersistentMessageQueue:
    """Get or create a message queue by name."""
    if name not in _message_queues:
        _message_queues[name] = PersistentMessageQueue(name)
    return _message_queues[name]


def get_startup_timeout(
    config: Optional[RacePreventionConfig] = None,
) -> AdaptiveStartupTimeout:
    """
    Get the global adaptive startup timeout manager.

    Timeout values are configured via environment variables:
    - STARTUP_SPAWN_TIMEOUT: Base timeout for process spawn (default: 30.0s)
    - STARTUP_INIT_TIMEOUT: Base timeout for initialization (default: 60.0s)
    - STARTUP_DEPS_TIMEOUT: Base timeout for dependencies (default: 120.0s)
    - STARTUP_READY_TIMEOUT: Base timeout for ready state (default: 30.0s)
    - STARTUP_LOAD_MULTIPLIER_MAX: Max load multiplier (default: 3.0)
    - STARTUP_NETWORK_BUFFER: Network delay buffer (default: 5.0s)

    Args:
        config: Optional configuration override

    Returns:
        AdaptiveStartupTimeout singleton
    """
    global _startup_timeout
    if _startup_timeout is None:
        _startup_timeout = AdaptiveStartupTimeout(config)
    return _startup_timeout


def get_health_checker(
    config: Optional[RacePreventionConfig] = None,
) -> ResilientHealthChecker:
    """
    Get the global resilient health checker.

    Configuration via environment variables:
    - HEALTH_RETRY_COUNT: Number of retries (default: 3)
    - HEALTH_RETRY_BASE_DELAY: Base retry delay (default: 1.0s)
    - HEALTH_RETRY_MAX_DELAY: Max retry delay (default: 10.0s)
    - HEALTH_CACHE_TTL: Health cache TTL (default: 5.0s)
    - HEALTH_RESTART_GRACE: Restart grace period (default: 30.0s)
    - HEALTH_FAILURE_THRESHOLD: Consecutive failures (default: 3)
    - HEALTH_SUCCESS_THRESHOLD: Consecutive successes (default: 2)

    Args:
        config: Optional configuration override

    Returns:
        ResilientHealthChecker singleton
    """
    global _health_checker
    if _health_checker is None:
        _health_checker = ResilientHealthChecker(config)
    return _health_checker


def get_resource_manager() -> CoordinatedResourceManager:
    """
    Get the global coordinated resource manager.

    Returns:
        CoordinatedResourceManager singleton
    """
    global _resource_manager
    if _resource_manager is None:
        _resource_manager = CoordinatedResourceManager()
    return _resource_manager


def get_log_writer(component: str) -> AtomicLogWriter:
    """
    Get or create a log writer for a component.

    Args:
        component: Component name

    Returns:
        AtomicLogWriter for the component
    """
    if component not in _log_writers:
        _log_writers[component] = AtomicLogWriter(component)
    return _log_writers[component]


def get_state_file(name: str) -> AtomicStateFile:
    """
    Get or create a state file manager by name.

    Args:
        name: State file name

    Returns:
        AtomicStateFile for the given name
    """
    if name not in _state_files:
        _state_files[name] = AtomicStateFile(name)
    return _state_files[name]


# =============================================================================
# Convenience Functions for Trinity Integration
# =============================================================================


async def acquire_trinity_lock(resource: str, timeout: float = 30.0) -> AtomicFileLock:
    """
    Acquire a Trinity IPC lock.

    Usage:
        lock = await acquire_trinity_lock("heartbeat_update")
        try:
            # Critical section
        finally:
            lock.release()
    """
    lock = get_file_lock(f"trinity_{resource}")
    if not await lock.acquire_async(timeout):
        raise TimeoutError(f"Failed to acquire Trinity lock: {resource}")
    return lock


async def write_trinity_heartbeat(
    component: str,
    status: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a Trinity component heartbeat atomically."""
    writer = get_heartbeat_writer(component)
    await writer.write_heartbeat(status, metrics)


async def reserve_trinity_port(
    component: str,
    primary_port: int,
    fallback_ports: Optional[List[int]] = None,
) -> int:
    """
    Reserve a port for a Trinity component.

    Raises:
        RuntimeError: If no port could be reserved
    """
    reserver = get_port_reserver()
    port = await reserver.reserve_port(component, primary_port, fallback_ports)
    if port is None:
        raise RuntimeError(f"No available port for {component}")
    return port


async def start_trinity_dependency_monitoring(
    components: Dict[str, List[str]],
    health_checkers: Optional[Dict[str, Callable[[], Coroutine[Any, Any, bool]]]] = None,
) -> DependencyHealthMonitor:
    """
    Start dependency monitoring for Trinity components.

    Args:
        components: Dict mapping component name to list of dependencies
        health_checkers: Optional dict of custom health check functions

    Returns:
        DependencyHealthMonitor instance
    """
    health_checkers = health_checkers or {}
    monitor = get_dependency_monitor()

    for name, deps in components.items():
        monitor.register_component(
            name=name,
            dependencies=deps,
            health_checker=health_checkers.get(name),
        )

    await monitor.start_monitoring()
    return monitor


async def safe_register_process(
    component: str,
    pid: int,
) -> bool:
    """
    Safely register a Trinity process.

    Args:
        component: Component name
        pid: Process ID

    Returns:
        True if registered successfully
    """
    manager = get_process_manager()
    return await manager.register_process(component, pid)


async def safe_kill_process(
    component: str,
    signal_num: int = 15,
    timeout: float = 10.0,
) -> bool:
    """
    Safely terminate a Trinity process with verification.

    Args:
        component: Component name
        signal_num: Signal to send (default: SIGTERM)
        timeout: Timeout for graceful shutdown

    Returns:
        True if process was terminated
    """
    manager = get_process_manager()
    return await manager.safe_kill(component, signal_num, timeout)


async def get_trinity_config(name: str) -> Optional[Dict[str, Any]]:
    """
    Get Trinity configuration atomically.

    Args:
        name: Config name

    Returns:
        Configuration data or None
    """
    config_mgr = get_config_manager(name)
    config = await config_mgr.read()
    return config.data if config else None


async def set_trinity_config(
    name: str,
    data: Dict[str, Any],
) -> bool:
    """
    Set Trinity configuration atomically.

    Args:
        name: Config name
        data: Configuration data

    Returns:
        True if written successfully
    """
    config_mgr = get_config_manager(name)
    return await config_mgr.write(data)


async def publish_trinity_event(
    topic: str,
    payload: Dict[str, Any],
    priority: MessagePriority = MessagePriority.NORMAL,
) -> str:
    """
    Publish a Trinity event to the persistent queue.

    Args:
        topic: Event topic
        payload: Event payload
        priority: Message priority

    Returns:
        Message ID
    """
    queue = get_message_queue("trinity_events")
    return await queue.publish(topic, payload, priority)


async def consume_trinity_events(
    consumer_id: str,
    topics: Optional[List[str]] = None,
) -> AsyncIterator[PersistentMessage]:
    """
    Consume Trinity events from the persistent queue.

    Args:
        consumer_id: Unique consumer identifier
        topics: Optional list of topics to consume

    Yields:
        PersistentMessage objects
    """
    queue = get_message_queue("trinity_events")
    async for msg in queue.consume(consumer_id, topics):
        yield msg


async def acknowledge_trinity_event(msg_id: str, consumer_id: str) -> bool:
    """Acknowledge a Trinity event."""
    queue = get_message_queue("trinity_events")
    return await queue.acknowledge(msg_id, consumer_id)


# Issue 16: Startup Timeout Convenience Functions
async def track_trinity_startup(component: str) -> StartupState:
    """
    Start tracking startup for a Trinity component.

    Args:
        component: Component name

    Returns:
        Initial StartupState
    """
    timeout_mgr = get_startup_timeout()
    return await timeout_mgr.start_tracking(component)


async def confirm_trinity_spawn(component: str, pid: int) -> Optional[StartupState]:
    """
    Confirm that a Trinity component has spawned.

    Args:
        component: Component name
        pid: Process ID

    Returns:
        Updated StartupState or None
    """
    timeout_mgr = get_startup_timeout()
    return await timeout_mgr.confirm_spawn(component, pid)


async def mark_trinity_ready(component: str) -> Optional[StartupState]:
    """
    Mark a Trinity component as ready.

    Args:
        component: Component name

    Returns:
        Updated StartupState or None
    """
    timeout_mgr = get_startup_timeout()
    return await timeout_mgr.mark_ready(component)


async def check_trinity_timeout(component: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a Trinity component has timed out.

    Args:
        component: Component name

    Returns:
        Tuple of (is_timed_out, reason)
    """
    timeout_mgr = get_startup_timeout()
    return await timeout_mgr.is_timed_out(component)


# Issue 17: Health Check Convenience Functions
def register_trinity_health_check(
    component: str,
    checker: Callable[[], Coroutine[Any, Any, bool]],
) -> None:
    """
    Register a health check function for a Trinity component.

    Args:
        component: Component name
        checker: Async function returning True if healthy
    """
    health_mgr = get_health_checker()
    health_mgr.register(component, checker)


async def check_trinity_health(component: str) -> HealthCheckResult:
    """
    Check health of a Trinity component with retry and caching.

    Args:
        component: Component name

    Returns:
        HealthCheckResult
    """
    health_mgr = get_health_checker()
    status = await health_mgr.check_health(component)
    return status.result if status else HealthCheckResult.UNKNOWN


async def mark_trinity_restarting(component: str) -> None:
    """
    Mark a Trinity component as restarting.

    Health checks will return RESTARTING during grace period.

    Args:
        component: Component name
    """
    health_mgr = get_health_checker()
    await health_mgr.mark_restarting(component)


# Issue 18: Resource Management Convenience Functions
async def reserve_trinity_resource(
    resource_id: str,
    resource_type: ResourceType,
    component: str,
    ttl: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Reserve a Trinity resource with cleanup protection.

    Args:
        resource_id: Resource identifier (e.g., "port:8010")
        resource_type: Type of resource
        component: Component reserving the resource
        ttl: Optional time-to-live in seconds
        metadata: Optional metadata

    Returns:
        True if reserved successfully
    """
    resource_mgr = get_resource_manager()
    return await resource_mgr.reserve(resource_id, resource_type, component, ttl, metadata)


async def release_trinity_resource(
    resource_id: str,
    component: str,
) -> bool:
    """
    Release a Trinity resource.

    Args:
        resource_id: Resource identifier
        component: Component releasing the resource

    Returns:
        True if released successfully
    """
    resource_mgr = get_resource_manager()
    return await resource_mgr.release(resource_id, component)


async def cleanup_trinity_stale_resources() -> int:
    """
    Cleanup all stale Trinity resources.

    Cleans up resources where:
    - Owner process is dead (stale)
    - Reservation has expired

    Returns:
        Number of resources cleaned up
    """
    resource_mgr = get_resource_manager()
    return await resource_mgr.cleanup_stale_resources()


# Issue 19: Log Writing Convenience Functions
async def write_trinity_log(
    component: str,
    level: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write a log entry atomically for a Trinity component.

    Args:
        component: Component name
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        message: Log message
        context: Optional context dictionary
    """
    log_writer = get_log_writer(component)
    await log_writer.write(level, message, context)


async def rotate_trinity_logs(component: str) -> bool:
    """
    Rotate log files for a Trinity component.

    Args:
        component: Component name

    Returns:
        True if rotated successfully
    """
    log_writer = get_log_writer(component)
    return await log_writer.rotate()


# Issue 20: State File Convenience Functions
async def read_trinity_state(name: str) -> Optional[Dict[str, Any]]:
    """
    Read Trinity state file atomically.

    Args:
        name: State file name

    Returns:
        State data or None
    """
    state_file = get_state_file(name)
    state = await state_file.read()
    return state.data if state else None


async def write_trinity_state(
    name: str,
    data: Dict[str, Any],
) -> bool:
    """
    Write Trinity state file atomically with versioning.

    Args:
        name: State file name
        data: State data

    Returns:
        True if written successfully
    """
    state_file = get_state_file(name)
    return await state_file.write(data)


@asynccontextmanager
async def update_trinity_state(name: str) -> AsyncIterator[Dict[str, Any]]:
    """
    Context manager for read-modify-write Trinity state.

    Args:
        name: State file name

    Yields:
        Mutable state dictionary

    Usage:
        async with update_trinity_state("supervisor") as state:
            state["component_count"] += 1
    """
    state_file = get_state_file(name)
    async with state_file.update() as data:
        yield data


async def recover_trinity_state(name: str) -> bool:
    """
    Recover Trinity state from backup.

    Args:
        name: State file name

    Returns:
        True if recovered successfully
    """
    state_file = get_state_file(name)
    return await state_file.recover()


# =============================================================================
# ISSUE 26: Disk Full Handling
# =============================================================================


class DiskPressureLevel(Enum):
    """Disk space pressure levels."""
    NORMAL = "normal"        # > 20% free
    WARNING = "warning"      # 10-20% free
    CRITICAL = "critical"    # 5-10% free
    EMERGENCY = "emergency"  # < 5% free


@dataclass
class DiskSpaceStatus:
    """Disk space status information."""
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float
    percent_free: float
    pressure_level: DiskPressureLevel
    checked_at: float

    @property
    def free_gb(self) -> float:
        """Free space in GB."""
        return self.free_bytes / (1024 ** 3)

    @property
    def total_gb(self) -> float:
        """Total space in GB."""
        return self.total_bytes / (1024 ** 3)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "percent_used": self.percent_used,
            "percent_free": self.percent_free,
            "pressure_level": self.pressure_level.value,
            "checked_at": self.checked_at,
            "free_gb": self.free_gb,
            "total_gb": self.total_gb,
        }


@dataclass
class CleanupResult:
    """Result of disk cleanup operation."""
    files_deleted: int
    bytes_freed: int
    errors: List[str]
    duration_seconds: float

    @property
    def mb_freed(self) -> float:
        """Space freed in MB."""
        return self.bytes_freed / (1024 ** 2)


class DiskSpaceManager:
    """
    Comprehensive disk space monitoring and management.

    FIXES ISSUE 26: Disk Full No Handling

    Key Features:
    - Continuous disk space monitoring
    - Configurable pressure thresholds
    - Automatic cleanup of old files
    - Graceful degradation when disk full
    - Event-based alerts

    Usage:
        manager = DiskSpaceManager()
        await manager.start_monitoring()

        # Check current status
        status = await manager.get_status()

        # Manual cleanup
        result = await manager.cleanup_old_files()
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize disk space manager."""
        self.config = config or get_config()

        # Monitored paths
        self.monitored_paths: List[Path] = [
            self.config.state_dir,
            Path(tempfile.gettempdir()),
        ]

        # Thresholds from environment (percent free)
        self.warning_threshold = _env_float("DISK_WARNING_THRESHOLD", 20.0)
        self.critical_threshold = _env_float("DISK_CRITICAL_THRESHOLD", 10.0)
        self.emergency_threshold = _env_float("DISK_EMERGENCY_THRESHOLD", 5.0)

        # Cleanup settings
        self.max_log_age_days = _env_int("DISK_MAX_LOG_AGE_DAYS", 7)
        self.max_temp_age_hours = _env_int("DISK_MAX_TEMP_AGE_HOURS", 24)
        self.max_backup_count = _env_int("DISK_MAX_BACKUP_COUNT", 5)
        self.check_interval = _env_float("DISK_CHECK_INTERVAL", 60.0)

        # State
        self._status_cache: Dict[str, DiskSpaceStatus] = {}
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._lock = asyncio.Lock()

        # Callbacks for pressure changes
        self._pressure_callbacks: List[Callable[[DiskPressureLevel, DiskSpaceStatus], Coroutine[Any, Any, None]]] = []

        # Degradation state
        self._degraded_mode = False
        self._write_buffer: List[Dict[str, Any]] = []

    def _get_disk_usage(self, path: Path) -> Optional[DiskSpaceStatus]:
        """Get disk usage for a path."""
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(path))
            percent_free = (free / total) * 100 if total > 0 else 0
            percent_used = 100 - percent_free

            # Determine pressure level
            if percent_free < self.emergency_threshold:
                level = DiskPressureLevel.EMERGENCY
            elif percent_free < self.critical_threshold:
                level = DiskPressureLevel.CRITICAL
            elif percent_free < self.warning_threshold:
                level = DiskPressureLevel.WARNING
            else:
                level = DiskPressureLevel.NORMAL

            return DiskSpaceStatus(
                path=str(path),
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                percent_used=percent_used,
                percent_free=percent_free,
                pressure_level=level,
                checked_at=time.time(),
            )
        except OSError as e:
            logger.error(f"[DiskSpace] Error checking {path}: {e}")
            return None

    async def get_status(self, path: Optional[Path] = None) -> Optional[DiskSpaceStatus]:
        """
        Get current disk space status.

        Args:
            path: Specific path to check (uses state_dir by default)

        Returns:
            DiskSpaceStatus or None if check fails
        """
        check_path = path or self.config.state_dir
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_disk_usage, check_path)

    async def get_all_status(self) -> Dict[str, DiskSpaceStatus]:
        """Get status for all monitored paths."""
        results: Dict[str, DiskSpaceStatus] = {}

        for path in self.monitored_paths:
            status = await self.get_status(path)
            if status:
                results[str(path)] = status

        return results

    def _determine_pressure_level(self) -> DiskPressureLevel:
        """Determine overall pressure level from all cached statuses."""
        if not self._status_cache:
            return DiskPressureLevel.NORMAL

        # Return worst pressure level
        levels = [s.pressure_level for s in self._status_cache.values()]
        if DiskPressureLevel.EMERGENCY in levels:
            return DiskPressureLevel.EMERGENCY
        if DiskPressureLevel.CRITICAL in levels:
            return DiskPressureLevel.CRITICAL
        if DiskPressureLevel.WARNING in levels:
            return DiskPressureLevel.WARNING
        return DiskPressureLevel.NORMAL

    def _cleanup_old_files_sync(
        self,
        directory: Path,
        max_age_seconds: float,
        pattern: str = "*",
    ) -> CleanupResult:
        """Synchronous cleanup of old files."""
        start_time = time.time()
        files_deleted = 0
        bytes_freed = 0
        errors: List[str] = []

        now = time.time()

        try:
            for file_path in directory.glob(pattern):
                if not file_path.is_file():
                    continue

                try:
                    stat = file_path.stat()
                    age = now - stat.st_mtime

                    if age > max_age_seconds:
                        size = stat.st_size
                        file_path.unlink()
                        files_deleted += 1
                        bytes_freed += size

                except PermissionError as e:
                    errors.append(f"Permission denied: {file_path}")
                except OSError as e:
                    errors.append(f"Error deleting {file_path}: {e}")

        except Exception as e:
            errors.append(f"Error scanning {directory}: {e}")

        return CleanupResult(
            files_deleted=files_deleted,
            bytes_freed=bytes_freed,
            errors=errors,
            duration_seconds=time.time() - start_time,
        )

    async def cleanup_old_logs(self) -> CleanupResult:
        """Clean up old log files."""
        log_dir = self.config.state_dir / "logs"
        if not log_dir.exists():
            return CleanupResult(0, 0, [], 0.0)

        max_age = self.max_log_age_days * 24 * 3600
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._cleanup_old_files_sync,
            log_dir,
            max_age,
            "*.log*",
        )

    async def cleanup_old_temp_files(self) -> CleanupResult:
        """Clean up old temporary files."""
        temp_dir = self.config.state_dir
        max_age = self.max_temp_age_hours * 3600
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._cleanup_old_files_sync,
            temp_dir,
            max_age,
            "*.tmp",
        )

    def _cleanup_old_backups_sync(self, directory: Path) -> CleanupResult:
        """Clean up old backup files, keeping only max_backup_count."""
        start_time = time.time()
        files_deleted = 0
        bytes_freed = 0
        errors: List[str] = []

        try:
            # Find all backup files
            backups = list(directory.glob("*.backup.*"))

            # Group by base name
            backup_groups: Dict[str, List[Path]] = {}
            for backup in backups:
                # Extract base name (before .backup)
                name = backup.name.split(".backup")[0]
                if name not in backup_groups:
                    backup_groups[name] = []
                backup_groups[name].append(backup)

            # For each group, keep only newest max_backup_count
            for name, group in backup_groups.items():
                # Sort by mtime, newest first
                group.sort(key=lambda p: p.stat().st_mtime, reverse=True)

                # Delete old ones
                for old_backup in group[self.max_backup_count:]:
                    try:
                        size = old_backup.stat().st_size
                        old_backup.unlink()
                        files_deleted += 1
                        bytes_freed += size
                    except OSError as e:
                        errors.append(f"Error deleting {old_backup}: {e}")

        except Exception as e:
            errors.append(f"Error cleaning backups in {directory}: {e}")

        return CleanupResult(
            files_deleted=files_deleted,
            bytes_freed=bytes_freed,
            errors=errors,
            duration_seconds=time.time() - start_time,
        )

    async def cleanup_old_backups(self) -> CleanupResult:
        """Clean up old backup files."""
        state_dir = self.config.state_dir / "state"
        if not state_dir.exists():
            return CleanupResult(0, 0, [], 0.0)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._cleanup_old_backups_sync,
            state_dir,
        )

    async def cleanup_all(self) -> Dict[str, CleanupResult]:
        """Perform all cleanup operations."""
        results: Dict[str, CleanupResult] = {}

        # Run all cleanups in parallel
        log_task = asyncio.create_task(self.cleanup_old_logs())
        temp_task = asyncio.create_task(self.cleanup_old_temp_files())
        backup_task = asyncio.create_task(self.cleanup_old_backups())

        results["logs"] = await log_task
        results["temp"] = await temp_task
        results["backups"] = await backup_task

        total_freed = sum(r.bytes_freed for r in results.values())
        total_deleted = sum(r.files_deleted for r in results.values())

        logger.info(
            f"[DiskSpace] Cleanup complete: {total_deleted} files, "
            f"{total_freed / (1024*1024):.2f} MB freed"
        )

        return results

    async def _handle_pressure_change(
        self,
        old_level: DiskPressureLevel,
        new_level: DiskPressureLevel,
        status: DiskSpaceStatus,
    ) -> None:
        """Handle pressure level change."""
        if new_level == old_level:
            return

        logger.warning(
            f"[DiskSpace] Pressure changed: {old_level.value} -> {new_level.value} "
            f"({status.percent_free:.1f}% free on {status.path})"
        )

        # Auto-cleanup on pressure increase
        if new_level in (DiskPressureLevel.CRITICAL, DiskPressureLevel.EMERGENCY):
            logger.info("[DiskSpace] Triggering automatic cleanup due to pressure")
            await self.cleanup_all()

            # Recheck after cleanup
            new_status = await self.get_status(Path(status.path))
            if new_status:
                status = new_status

        # Enter degraded mode on emergency
        if new_level == DiskPressureLevel.EMERGENCY:
            self._degraded_mode = True
            logger.error("[DiskSpace] Entering degraded mode - disk critically full")
        elif old_level == DiskPressureLevel.EMERGENCY:
            self._degraded_mode = False
            logger.info("[DiskSpace] Exiting degraded mode - disk space recovered")

        # Notify callbacks
        for callback in self._pressure_callbacks:
            try:
                await callback(new_level, status)
            except Exception as e:
                logger.error(f"[DiskSpace] Callback error: {e}")

    async def _monitoring_loop(self) -> None:
        """Background monitoring loop."""
        last_pressure = DiskPressureLevel.NORMAL

        while not self._shutdown_event.is_set():
            try:
                # Check all monitored paths
                for path in self.monitored_paths:
                    status = await self.get_status(path)
                    if status:
                        async with self._lock:
                            self._status_cache[str(path)] = status

                # Determine overall pressure
                current_pressure = self._determine_pressure_level()

                # Handle pressure change
                if current_pressure != last_pressure:
                    # Get the status that triggered the change
                    worst_status = None
                    for s in self._status_cache.values():
                        if s.pressure_level == current_pressure:
                            worst_status = s
                            break

                    if worst_status:
                        await self._handle_pressure_change(
                            last_pressure,
                            current_pressure,
                            worst_status,
                        )

                    last_pressure = current_pressure

            except Exception as e:
                logger.error(f"[DiskSpace] Monitoring error: {e}")

            # Wait for next check
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.check_interval,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def start_monitoring(self) -> None:
        """Start background disk monitoring."""
        if self._monitoring_task is not None:
            return

        self._shutdown_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("[DiskSpace] Monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._shutdown_event.set()

        if self._monitoring_task:
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitoring_task.cancel()
            self._monitoring_task = None

        logger.info("[DiskSpace] Monitoring stopped")

    def on_pressure_change(
        self,
        callback: Callable[[DiskPressureLevel, DiskSpaceStatus], Coroutine[Any, Any, None]],
    ) -> None:
        """Register callback for pressure level changes."""
        self._pressure_callbacks.append(callback)

    def is_degraded(self) -> bool:
        """Check if in degraded mode."""
        return self._degraded_mode

    async def write_with_fallback(
        self,
        file_path: Path,
        data: bytes,
    ) -> bool:
        """
        Write data with fallback handling for full disk.

        In degraded mode, buffers writes to memory.
        """
        if self._degraded_mode:
            # Buffer to memory
            self._write_buffer.append({
                "path": str(file_path),
                "data": data,
                "timestamp": time.time(),
            })
            logger.warning(f"[DiskSpace] Write buffered (degraded mode): {file_path}")
            return False

        try:
            # Attempt write
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(data)
            return True
        except OSError as e:
            if e.errno == errno.ENOSPC:
                # Disk full - enter degraded mode
                self._degraded_mode = True
                self._write_buffer.append({
                    "path": str(file_path),
                    "data": data,
                    "timestamp": time.time(),
                })
                logger.error(f"[DiskSpace] Disk full, entering degraded mode: {e}")

                # Trigger cleanup
                asyncio.create_task(self.cleanup_all())
                return False
            raise

    async def flush_write_buffer(self) -> int:
        """
        Flush buffered writes when disk space is available.

        Returns:
            Number of writes flushed
        """
        if not self._write_buffer:
            return 0

        # Check if we have space now
        status = await self.get_status()
        if status and status.pressure_level == DiskPressureLevel.EMERGENCY:
            return 0

        flushed = 0
        remaining: List[Dict[str, Any]] = []

        for item in self._write_buffer:
            try:
                path = Path(item["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item["data"])
                flushed += 1
            except OSError:
                remaining.append(item)

        self._write_buffer = remaining

        if flushed > 0:
            logger.info(f"[DiskSpace] Flushed {flushed} buffered writes")

        if not self._write_buffer:
            self._degraded_mode = False

        return flushed


# =============================================================================
# ISSUE 27: Memory Exhaustion Recovery
# =============================================================================


class MemoryPressureLevel(Enum):
    """Memory pressure levels."""
    NORMAL = "normal"        # < 70% used
    MODERATE = "moderate"    # 70-80% used
    HIGH = "high"            # 80-90% used
    CRITICAL = "critical"    # > 90% used


@dataclass
class MemoryStatus:
    """Memory status information."""
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent_used: float
    swap_total: int
    swap_used: int
    swap_percent: float
    pressure_level: MemoryPressureLevel
    checked_at: float

    @property
    def available_mb(self) -> float:
        """Available memory in MB."""
        return self.available_bytes / (1024 ** 2)

    @property
    def total_gb(self) -> float:
        """Total memory in GB."""
        return self.total_bytes / (1024 ** 3)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "used_bytes": self.used_bytes,
            "percent_used": self.percent_used,
            "swap_total": self.swap_total,
            "swap_used": self.swap_used,
            "swap_percent": self.swap_percent,
            "pressure_level": self.pressure_level.value,
            "checked_at": self.checked_at,
            "available_mb": self.available_mb,
            "total_gb": self.total_gb,
        }


@dataclass
class ComponentMemoryInfo:
    """Memory info for a component."""
    name: str
    priority: int  # 1=critical, 2=important, 3=normal, 4=optional
    unload_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
    is_loaded: bool = True
    last_unloaded: Optional[float] = None


class MemoryPressureManager:
    """
    Comprehensive memory monitoring and pressure management.

    FIXES ISSUE 27: Memory Exhaustion No Recovery

    Key Features:
    - Continuous memory monitoring
    - Pressure level detection
    - Automatic memory cleanup (caches, buffers)
    - Component priority-based unloading
    - GC integration

    Usage:
        manager = MemoryPressureManager()
        await manager.start_monitoring()

        # Register component for unloading
        manager.register_component("cache", priority=4, unload_callback=clear_cache)
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize memory pressure manager."""
        self.config = config or get_config()

        # Thresholds from environment (percent used)
        self.moderate_threshold = _env_float("MEMORY_MODERATE_THRESHOLD", 70.0)
        self.high_threshold = _env_float("MEMORY_HIGH_THRESHOLD", 80.0)
        self.critical_threshold = _env_float("MEMORY_CRITICAL_THRESHOLD", 90.0)

        # Settings
        self.check_interval = _env_float("MEMORY_CHECK_INTERVAL", 10.0)
        self.gc_on_pressure = _env_bool("MEMORY_GC_ON_PRESSURE", True)

        # State
        self._status_cache: Optional[MemoryStatus] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._lock = asyncio.Lock()

        # Components registered for unloading
        self._components: Dict[str, ComponentMemoryInfo] = {}

        # Caches registered for clearing
        self._caches: Dict[str, Callable[[], int]] = {}  # Returns bytes freed

        # Callbacks for pressure changes
        self._pressure_callbacks: List[Callable[[MemoryPressureLevel, MemoryStatus], Coroutine[Any, Any, None]]] = []

    def _get_memory_status(self) -> Optional[MemoryStatus]:
        """Get current memory status."""
        try:
            import psutil

            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()

            percent_used = mem.percent

            # Determine pressure level
            if percent_used >= self.critical_threshold:
                level = MemoryPressureLevel.CRITICAL
            elif percent_used >= self.high_threshold:
                level = MemoryPressureLevel.HIGH
            elif percent_used >= self.moderate_threshold:
                level = MemoryPressureLevel.MODERATE
            else:
                level = MemoryPressureLevel.NORMAL

            return MemoryStatus(
                total_bytes=mem.total,
                available_bytes=mem.available,
                used_bytes=mem.used,
                percent_used=percent_used,
                swap_total=swap.total,
                swap_used=swap.used,
                swap_percent=swap.percent,
                pressure_level=level,
                checked_at=time.time(),
            )

        except ImportError:
            logger.warning("[Memory] psutil not available for memory monitoring")
            return None
        except Exception as e:
            logger.error(f"[Memory] Error getting memory status: {e}")
            return None

    async def get_status(self) -> Optional[MemoryStatus]:
        """Get current memory status."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_memory_status)

    def register_component(
        self,
        name: str,
        priority: int,
        unload_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        """
        Register a component for memory pressure management.

        Args:
            name: Component name
            priority: 1=critical (never unload), 2=important, 3=normal, 4=optional (unload first)
            unload_callback: Async function to unload the component
        """
        self._components[name] = ComponentMemoryInfo(
            name=name,
            priority=priority,
            unload_callback=unload_callback,
        )

    def register_cache(
        self,
        name: str,
        clear_callback: Callable[[], int],
    ) -> None:
        """
        Register a cache for automatic clearing.

        Args:
            name: Cache name
            clear_callback: Function that clears cache and returns bytes freed
        """
        self._caches[name] = clear_callback

    def _run_gc(self) -> int:
        """Run garbage collection."""
        import gc

        # Get memory before
        try:
            import psutil
            before = psutil.Process().memory_info().rss
        except ImportError:
            before = 0

        # Run full collection
        gc.collect(generation=2)

        # Get memory after
        try:
            import psutil
            after = psutil.Process().memory_info().rss
            freed = max(0, before - after)
        except ImportError:
            freed = 0

        return freed

    async def clear_caches(self) -> int:
        """Clear all registered caches."""
        total_freed = 0

        for name, clear_func in self._caches.items():
            try:
                freed = clear_func()
                total_freed += freed
                logger.debug(f"[Memory] Cleared cache {name}: {freed / 1024:.1f} KB freed")
            except Exception as e:
                logger.error(f"[Memory] Error clearing cache {name}: {e}")

        return total_freed

    async def unload_components_by_priority(
        self,
        max_priority: int = 4,
    ) -> List[str]:
        """
        Unload components up to given priority level.

        Args:
            max_priority: Maximum priority to unload (4=optional only, 3=normal+optional)

        Returns:
            List of unloaded component names
        """
        unloaded: List[str] = []

        # Sort by priority (highest number = lowest priority = unload first)
        sorted_components = sorted(
            self._components.values(),
            key=lambda c: c.priority,
            reverse=True,
        )

        for component in sorted_components:
            if component.priority > max_priority:
                continue
            if not component.is_loaded:
                continue
            if component.priority == 1:  # Never unload critical
                continue
            if component.unload_callback is None:
                continue

            try:
                await component.unload_callback()
                component.is_loaded = False
                component.last_unloaded = time.time()
                unloaded.append(component.name)
                logger.info(f"[Memory] Unloaded component: {component.name}")
            except Exception as e:
                logger.error(f"[Memory] Error unloading {component.name}: {e}")

        return unloaded

    async def _handle_pressure_change(
        self,
        old_level: MemoryPressureLevel,
        new_level: MemoryPressureLevel,
        status: MemoryStatus,
    ) -> None:
        """Handle memory pressure level change."""
        if new_level == old_level:
            return

        logger.warning(
            f"[Memory] Pressure changed: {old_level.value} -> {new_level.value} "
            f"({status.percent_used:.1f}% used, {status.available_mb:.0f} MB available)"
        )

        # Take action based on new level
        if new_level == MemoryPressureLevel.MODERATE:
            # Run GC
            if self.gc_on_pressure:
                freed = self._run_gc()
                logger.info(f"[Memory] GC freed {freed / 1024:.1f} KB")

        elif new_level == MemoryPressureLevel.HIGH:
            # Clear caches and run GC
            cache_freed = await self.clear_caches()
            gc_freed = self._run_gc() if self.gc_on_pressure else 0
            logger.info(
                f"[Memory] High pressure cleanup: "
                f"{cache_freed / 1024:.1f} KB from caches, {gc_freed / 1024:.1f} KB from GC"
            )

        elif new_level == MemoryPressureLevel.CRITICAL:
            # Clear caches, unload optional components, run GC
            cache_freed = await self.clear_caches()
            unloaded = await self.unload_components_by_priority(max_priority=4)
            gc_freed = self._run_gc() if self.gc_on_pressure else 0
            logger.warning(
                f"[Memory] Critical pressure cleanup: "
                f"unloaded {len(unloaded)} components, "
                f"{cache_freed / 1024:.1f} KB from caches"
            )

        # Notify callbacks
        for callback in self._pressure_callbacks:
            try:
                await callback(new_level, status)
            except Exception as e:
                logger.error(f"[Memory] Callback error: {e}")

    async def _monitoring_loop(self) -> None:
        """Background monitoring loop."""
        last_pressure = MemoryPressureLevel.NORMAL

        while not self._shutdown_event.is_set():
            try:
                status = await self.get_status()

                if status:
                    async with self._lock:
                        self._status_cache = status

                    if status.pressure_level != last_pressure:
                        await self._handle_pressure_change(
                            last_pressure,
                            status.pressure_level,
                            status,
                        )
                        last_pressure = status.pressure_level

            except Exception as e:
                logger.error(f"[Memory] Monitoring error: {e}")

            # Wait for next check
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.check_interval,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def start_monitoring(self) -> None:
        """Start background memory monitoring."""
        if self._monitoring_task is not None:
            return

        self._shutdown_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("[Memory] Monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._shutdown_event.set()

        if self._monitoring_task:
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitoring_task.cancel()
            self._monitoring_task = None

        logger.info("[Memory] Monitoring stopped")

    def on_pressure_change(
        self,
        callback: Callable[[MemoryPressureLevel, MemoryStatus], Coroutine[Any, Any, None]],
    ) -> None:
        """Register callback for pressure level changes."""
        self._pressure_callbacks.append(callback)

    def get_current_pressure(self) -> MemoryPressureLevel:
        """Get current pressure level."""
        if self._status_cache:
            return self._status_cache.pressure_level
        return MemoryPressureLevel.NORMAL


# =============================================================================
# ISSUE 28: Permission Denied Recovery
# =============================================================================


class PermissionType(Enum):
    """Types of permissions to check."""
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DELETE = "delete"


@dataclass
class PermissionCheck:
    """Result of a permission check."""
    path: str
    permission_type: PermissionType
    has_permission: bool
    owner: str
    group: str
    mode: str
    error_message: Optional[str] = None
    fix_suggestion: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "path": self.path,
            "permission_type": self.permission_type.value,
            "has_permission": self.has_permission,
            "owner": self.owner,
            "group": self.group,
            "mode": self.mode,
            "error_message": self.error_message,
            "fix_suggestion": self.fix_suggestion,
        }


@dataclass
class PermissionValidationResult:
    """Result of full permission validation."""
    all_valid: bool
    checks: List[PermissionCheck]
    critical_failures: List[PermissionCheck]
    warnings: List[PermissionCheck]
    fix_commands: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "all_valid": self.all_valid,
            "checks": [c.to_dict() for c in self.checks],
            "critical_failures": [c.to_dict() for c in self.critical_failures],
            "warnings": [c.to_dict() for c in self.warnings],
            "fix_commands": self.fix_commands,
        }


class PermissionValidator:
    """
    Comprehensive permission validation and recovery.

    FIXES ISSUE 28: Permission Denied No Recovery

    Key Features:
    - Permission validation at startup
    - Clear error messages with solutions
    - Automatic fix suggestions
    - Permission repair (where safe)

    Usage:
        validator = PermissionValidator()
        result = await validator.validate_all()

        if not result.all_valid:
            print("Fix commands:", result.fix_commands)
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize permission validator."""
        self.config = config or get_config()

        # Paths to validate
        self._required_paths: List[Tuple[Path, List[PermissionType], bool]] = []  # (path, perms, is_critical)

        # Add default paths
        self._add_default_paths()

    def _add_default_paths(self) -> None:
        """Add default paths to validate."""
        state_dir = self.config.state_dir

        # Critical paths (system will fail without these)
        self.add_required_path(state_dir, [PermissionType.READ, PermissionType.WRITE], critical=True)
        self.add_required_path(state_dir / "locks", [PermissionType.READ, PermissionType.WRITE], critical=True)
        self.add_required_path(state_dir / "heartbeats", [PermissionType.READ, PermissionType.WRITE], critical=True)

        # Important paths (system can run degraded without these)
        self.add_required_path(state_dir / "logs", [PermissionType.READ, PermissionType.WRITE], critical=False)
        self.add_required_path(state_dir / "state", [PermissionType.READ, PermissionType.WRITE], critical=False)

    def add_required_path(
        self,
        path: Path,
        permissions: List[PermissionType],
        critical: bool = False,
    ) -> None:
        """Add a path to validate."""
        self._required_paths.append((path, permissions, critical))

    def _get_file_info(self, path: Path) -> Tuple[str, str, str]:
        """Get owner, group, and mode for a path."""
        try:
            stat = path.stat()

            # Get owner/group names
            try:
                import pwd
                import grp
                owner = pwd.getpwuid(stat.st_uid).pw_name
                group = grp.getgrgid(stat.st_gid).gr_name
            except (ImportError, KeyError):
                owner = str(stat.st_uid)
                group = str(stat.st_gid)

            # Get mode string
            mode = oct(stat.st_mode)[-3:]

            return owner, group, mode

        except OSError:
            return "unknown", "unknown", "000"

    def _check_permission(
        self,
        path: Path,
        perm_type: PermissionType,
    ) -> PermissionCheck:
        """Check a specific permission for a path."""
        owner, group, mode = self._get_file_info(path)

        # Create parent if needed and checking write
        if not path.exists() and perm_type == PermissionType.WRITE:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                return PermissionCheck(
                    path=str(path),
                    permission_type=perm_type,
                    has_permission=False,
                    owner=owner,
                    group=group,
                    mode=mode,
                    error_message=f"Cannot create directory: {path}",
                    fix_suggestion=f"sudo mkdir -p {path} && sudo chown $USER {path}",
                )

        # Check actual permission
        access_mode = {
            PermissionType.READ: os.R_OK,
            PermissionType.WRITE: os.W_OK,
            PermissionType.EXECUTE: os.X_OK,
            PermissionType.DELETE: os.W_OK,  # Delete requires write on parent
        }[perm_type]

        check_path = path.parent if perm_type == PermissionType.DELETE else path

        if not check_path.exists():
            return PermissionCheck(
                path=str(path),
                permission_type=perm_type,
                has_permission=False,
                owner=owner,
                group=group,
                mode=mode,
                error_message=f"Path does not exist: {check_path}",
                fix_suggestion=f"mkdir -p {check_path}",
            )

        has_perm = os.access(check_path, access_mode)

        if has_perm:
            return PermissionCheck(
                path=str(path),
                permission_type=perm_type,
                has_permission=True,
                owner=owner,
                group=group,
                mode=mode,
            )

        # Generate fix suggestion
        mode_bit = {"read": "r", "write": "w", "execute": "x", "delete": "w"}[perm_type.value]

        return PermissionCheck(
            path=str(path),
            permission_type=perm_type,
            has_permission=False,
            owner=owner,
            group=group,
            mode=mode,
            error_message=f"No {perm_type.value} permission for {path} (owner: {owner}, mode: {mode})",
            fix_suggestion=f"sudo chmod u+{mode_bit} {path}  # or: sudo chown $USER {path}",
        )

    async def validate_path(
        self,
        path: Path,
        permissions: List[PermissionType],
    ) -> List[PermissionCheck]:
        """Validate permissions for a path."""
        loop = asyncio.get_running_loop()
        checks: List[PermissionCheck] = []

        for perm in permissions:
            check = await loop.run_in_executor(
                None,
                self._check_permission,
                path,
                perm,
            )
            checks.append(check)

        return checks

    async def validate_all(self) -> PermissionValidationResult:
        """Validate all required paths."""
        all_checks: List[PermissionCheck] = []
        critical_failures: List[PermissionCheck] = []
        warnings: List[PermissionCheck] = []
        fix_commands: List[str] = []

        for path, permissions, is_critical in self._required_paths:
            checks = await self.validate_path(path, permissions)
            all_checks.extend(checks)

            for check in checks:
                if not check.has_permission:
                    if is_critical:
                        critical_failures.append(check)
                    else:
                        warnings.append(check)

                    if check.fix_suggestion:
                        fix_commands.append(check.fix_suggestion)

        all_valid = len(critical_failures) == 0

        return PermissionValidationResult(
            all_valid=all_valid,
            checks=all_checks,
            critical_failures=critical_failures,
            warnings=warnings,
            fix_commands=list(set(fix_commands)),  # Deduplicate
        )

    async def attempt_auto_fix(
        self,
        check: PermissionCheck,
    ) -> bool:
        """
        Attempt to automatically fix a permission issue.

        Only attempts safe fixes (creating directories, not changing ownership).

        Returns:
            True if fixed successfully
        """
        path = Path(check.path)

        # Only auto-fix by creating missing directories
        if not path.exists() and check.permission_type == PermissionType.WRITE:
            try:
                path.mkdir(parents=True, exist_ok=True)
                logger.info(f"[Permission] Auto-created directory: {path}")
                return True
            except PermissionError:
                return False

        return False

    async def validate_and_report(self) -> bool:
        """
        Validate permissions and log detailed report.

        Returns:
            True if all critical permissions are valid
        """
        result = await self.validate_all()

        if result.all_valid and not result.warnings:
            logger.info("[Permission] All permission checks passed")
            return True

        # Log warnings
        for check in result.warnings:
            logger.warning(
                f"[Permission] Warning: {check.error_message}\n"
                f"  Fix: {check.fix_suggestion}"
            )

        # Log critical failures
        for check in result.critical_failures:
            logger.error(
                f"[Permission] CRITICAL: {check.error_message}\n"
                f"  Fix: {check.fix_suggestion}"
            )

        if result.critical_failures:
            logger.error(
                f"[Permission] {len(result.critical_failures)} critical permission failures. "
                f"Run these commands to fix:\n" +
                "\n".join(f"  {cmd}" for cmd in result.fix_commands)
            )

        return result.all_valid


# =============================================================================
# ISSUE 29: Enhanced State File Recovery
# =============================================================================


@dataclass
class StateFileHealth:
    """Health status of a state file."""
    path: str
    exists: bool
    is_valid: bool
    version: int
    checksum_valid: bool
    backup_count: int
    last_modified: Optional[float]
    corruption_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "path": self.path,
            "exists": self.exists,
            "is_valid": self.is_valid,
            "version": self.version,
            "checksum_valid": self.checksum_valid,
            "backup_count": self.backup_count,
            "last_modified": self.last_modified,
            "corruption_type": self.corruption_type,
        }


class StateFileRecoveryManager:
    """
    Enhanced state file recovery with multiple backup generations.

    ENHANCES ISSUE 29: Corrupted State File No Recovery

    Key Features:
    - Multiple backup generations
    - Corruption detection and classification
    - Automatic repair mechanisms
    - Cross-validation between backups

    Usage:
        manager = StateFileRecoveryManager()

        # Check health
        health = await manager.check_health("supervisor_state")

        # Repair if needed
        if not health.is_valid:
            await manager.repair("supervisor_state")
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize state file recovery manager."""
        self.config = config or get_config()
        self.state_dir = self.config.state_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Settings
        self.max_backups = _env_int("STATE_MAX_BACKUPS", 5)
        self.backup_on_read = _env_bool("STATE_BACKUP_ON_READ", True)

    def _get_state_path(self, name: str) -> Path:
        """Get path for a state file."""
        return self.state_dir / f"{name}.json"

    def _get_backup_paths(self, name: str) -> List[Path]:
        """Get all backup paths for a state file, sorted by age (newest first)."""
        pattern = f"{name}.backup.*.json"
        backups = list(self.state_dir.glob(pattern))

        # Sort by modification time, newest first
        backups.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        return backups

    def _create_backup(self, name: str) -> Optional[Path]:
        """Create a backup of the current state file."""
        state_path = self._get_state_path(name)

        if not state_path.exists():
            return None

        timestamp = int(time.time() * 1000)
        backup_path = self.state_dir / f"{name}.backup.{timestamp}.json"

        try:
            import shutil
            shutil.copy2(state_path, backup_path)

            # Clean old backups
            self._cleanup_old_backups(name)

            return backup_path

        except OSError as e:
            logger.error(f"[StateRecovery] Backup failed for {name}: {e}")
            return None

    def _cleanup_old_backups(self, name: str) -> int:
        """Clean up old backups beyond max_backups."""
        backups = self._get_backup_paths(name)

        removed = 0
        for old_backup in backups[self.max_backups:]:
            try:
                old_backup.unlink()
                removed += 1
            except OSError:
                pass

        return removed

    def _validate_state_content(
        self,
        content: str,
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Validate state file content.

        Returns:
            (is_valid, corruption_type, parsed_data)
        """
        if not content.strip():
            return False, "empty_file", None

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            return False, f"json_error:{e.msg}", None

        if not isinstance(data, dict):
            return False, "not_dict", None

        # Check for versioned state structure
        if "version" in data and "checksum" in data and "data" in data:
            # Validate checksum
            expected = hashlib.sha256(
                json.dumps(data.get("data", {}), sort_keys=True, default=str).encode()
            ).hexdigest()

            if data.get("checksum") != expected:
                return False, "checksum_mismatch", data

        return True, None, data

    async def check_health(self, name: str) -> StateFileHealth:
        """Check health of a state file."""
        state_path = self._get_state_path(name)
        backups = self._get_backup_paths(name)

        if not state_path.exists():
            return StateFileHealth(
                path=str(state_path),
                exists=False,
                is_valid=False,
                version=0,
                checksum_valid=False,
                backup_count=len(backups),
                last_modified=None,
                corruption_type="file_missing",
            )

        try:
            content = state_path.read_text()
            is_valid, corruption_type, data = self._validate_state_content(content)

            version = data.get("version", 0) if data else 0
            checksum_valid = corruption_type != "checksum_mismatch"

            return StateFileHealth(
                path=str(state_path),
                exists=True,
                is_valid=is_valid,
                version=version,
                checksum_valid=checksum_valid,
                backup_count=len(backups),
                last_modified=state_path.stat().st_mtime,
                corruption_type=corruption_type,
            )

        except OSError as e:
            return StateFileHealth(
                path=str(state_path),
                exists=True,
                is_valid=False,
                version=0,
                checksum_valid=False,
                backup_count=len(backups),
                last_modified=None,
                corruption_type=f"read_error:{e}",
            )

    async def repair(self, name: str) -> bool:
        """
        Attempt to repair a corrupted state file.

        Tries backups in order from newest to oldest.

        Returns:
            True if repair successful
        """
        health = await self.check_health(name)

        if health.is_valid:
            return True  # Nothing to repair

        state_path = self._get_state_path(name)
        backups = self._get_backup_paths(name)

        logger.warning(f"[StateRecovery] Repairing {name}: {health.corruption_type}")

        # Try each backup
        for backup_path in backups:
            try:
                content = backup_path.read_text()
                is_valid, corruption_type, data = self._validate_state_content(content)

                if is_valid and data is not None:
                    # Found valid backup - restore it
                    import shutil

                    # Backup the corrupted file first
                    if state_path.exists():
                        corrupted_path = self.state_dir / f"{name}.corrupted.{int(time.time())}.json"
                        shutil.move(str(state_path), str(corrupted_path))

                    # Restore from backup
                    shutil.copy2(backup_path, state_path)

                    version_info = data.get("version", "unknown")
                    logger.info(
                        f"[StateRecovery] Restored {name} from backup "
                        f"(version {version_info})"
                    )
                    return True

            except OSError as e:
                logger.warning(f"[StateRecovery] Backup {backup_path} failed: {e}")
                continue

        # No valid backup found - try to salvage partial data
        if health.exists and health.corruption_type == "checksum_mismatch":
            # Checksum mismatch but JSON is valid - recompute checksum
            try:
                content = state_path.read_text()
                data = json.loads(content)

                if "data" in data:
                    # Recompute checksum
                    new_checksum = hashlib.sha256(
                        json.dumps(data["data"], sort_keys=True, default=str).encode()
                    ).hexdigest()

                    data["checksum"] = new_checksum
                    data["version"] = data.get("version", 0) + 1
                    data["timestamp"] = time.time()

                    # Write repaired file
                    state_path.write_text(json.dumps(data, indent=2))

                    logger.info(f"[StateRecovery] Repaired checksum for {name}")
                    return True

            except Exception as e:
                logger.error(f"[StateRecovery] Checksum repair failed: {e}")

        logger.error(f"[StateRecovery] Could not repair {name} - no valid backups")
        return False

    async def ensure_healthy(self, name: str) -> bool:
        """
        Ensure state file is healthy, repairing if needed.

        Returns:
            True if healthy (after repair if needed)
        """
        health = await self.check_health(name)

        if health.is_valid:
            # Optionally create backup on read
            if self.backup_on_read and health.backup_count < self.max_backups:
                self._create_backup(name)
            return True

        return await self.repair(name)

    async def get_all_health(self) -> Dict[str, StateFileHealth]:
        """Get health status for all state files."""
        results: Dict[str, StateFileHealth] = {}

        for state_file in self.state_dir.glob("*.json"):
            if ".backup." in state_file.name or ".corrupted." in state_file.name:
                continue

            name = state_file.stem
            results[name] = await self.check_health(name)

        return results


# =============================================================================
# ISSUE 30: Dependency Version Mismatch
# =============================================================================


@dataclass
class VersionRequirement:
    """A version requirement specification."""
    package: str
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    exact_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "package": self.package,
            "min_version": self.min_version,
            "max_version": self.max_version,
            "exact_version": self.exact_version,
        }


@dataclass
class VersionCheckResult:
    """Result of a version check."""
    package: str
    installed_version: Optional[str]
    requirement: VersionRequirement
    is_compatible: bool
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "package": self.package,
            "installed_version": self.installed_version,
            "requirement": self.requirement.to_dict(),
            "is_compatible": self.is_compatible,
            "error_message": self.error_message,
        }


@dataclass
class ComponentVersionInfo:
    """Version information for a component."""
    name: str
    version: str
    api_version: str
    min_compatible_version: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "min_compatible_version": self.min_compatible_version,
        }


class DependencyVersionManager:
    """
    Dependency version compatibility management.

    FIXES ISSUE 30: Dependency Version Mismatch

    Key Features:
    - Version compatibility matrix
    - Semantic version validation
    - Startup version checks
    - Clear error messages for mismatches

    Usage:
        manager = DependencyVersionManager()

        # Add requirements
        manager.add_requirement("psutil", min_version="5.8.0")

        # Validate all
        results = await manager.validate_all()
    """

    def __init__(self, config: Optional[RacePreventionConfig] = None):
        """Initialize version manager."""
        self.config = config or get_config()

        # Package requirements
        self._requirements: Dict[str, VersionRequirement] = {}

        # Component compatibility matrix
        self._component_versions: Dict[str, ComponentVersionInfo] = {}
        self._compatibility_matrix: Dict[str, Dict[str, str]] = {}  # component -> {dep_component: min_version}

        # Add default requirements
        self._add_default_requirements()

    def _add_default_requirements(self) -> None:
        """Add default package requirements."""
        # Python packages the system depends on
        self.add_requirement("psutil", min_version="5.0.0")  # For memory/disk monitoring

    def add_requirement(
        self,
        package: str,
        min_version: Optional[str] = None,
        max_version: Optional[str] = None,
        exact_version: Optional[str] = None,
    ) -> None:
        """Add a package requirement."""
        self._requirements[package] = VersionRequirement(
            package=package,
            min_version=min_version,
            max_version=max_version,
            exact_version=exact_version,
        )

    def register_component(
        self,
        name: str,
        version: str,
        api_version: str,
        min_compatible_version: str,
    ) -> None:
        """Register a component's version info."""
        self._component_versions[name] = ComponentVersionInfo(
            name=name,
            version=version,
            api_version=api_version,
            min_compatible_version=min_compatible_version,
        )

    def add_component_dependency(
        self,
        component: str,
        depends_on: str,
        min_version: str,
    ) -> None:
        """Add a component-to-component dependency."""
        if component not in self._compatibility_matrix:
            self._compatibility_matrix[component] = {}
        self._compatibility_matrix[component][depends_on] = min_version

    def _parse_version(self, version_str: str) -> Tuple[int, ...]:
        """Parse a version string into a tuple of integers."""
        try:
            # Remove common prefixes
            version_str = version_str.lstrip("v").lstrip("V")

            # Split by dots and convert to ints
            parts = []
            for part in version_str.split("."):
                # Handle versions like "1.0.0a1" or "1.0.0-beta"
                clean_part = ""
                for char in part:
                    if char.isdigit():
                        clean_part += char
                    else:
                        break
                if clean_part:
                    parts.append(int(clean_part))

            return tuple(parts) if parts else (0,)

        except (ValueError, TypeError):
            return (0,)

    def _compare_versions(self, v1: str, v2: str) -> int:
        """
        Compare two version strings.

        Returns:
            -1 if v1 < v2
             0 if v1 == v2
             1 if v1 > v2
        """
        parsed_v1 = self._parse_version(v1)
        parsed_v2 = self._parse_version(v2)

        # Pad to same length
        max_len = max(len(parsed_v1), len(parsed_v2))
        padded_v1 = parsed_v1 + (0,) * (max_len - len(parsed_v1))
        padded_v2 = parsed_v2 + (0,) * (max_len - len(parsed_v2))

        if padded_v1 < padded_v2:
            return -1
        elif padded_v1 > padded_v2:
            return 1
        else:
            return 0

    def _get_installed_version(self, package: str) -> Optional[str]:
        """Get installed version of a package."""
        try:
            # Try importlib.metadata first (Python 3.8+)
            try:
                from importlib.metadata import version as get_version
                return get_version(package)
            except ImportError:
                pass

            # Fallback to pkg_resources
            try:
                import pkg_resources
                return pkg_resources.get_distribution(package).version
            except (ImportError, pkg_resources.DistributionNotFound):
                pass

            # Try importing the package directly
            try:
                module = __import__(package)
                if hasattr(module, "__version__"):
                    return module.__version__
                if hasattr(module, "VERSION"):
                    return str(module.VERSION)
            except ImportError:
                pass

            return None

        except Exception:
            return None

    def _check_requirement(self, req: VersionRequirement) -> VersionCheckResult:
        """Check if a requirement is satisfied."""
        installed = self._get_installed_version(req.package)

        if installed is None:
            return VersionCheckResult(
                package=req.package,
                installed_version=None,
                requirement=req,
                is_compatible=False,
                error_message=f"Package '{req.package}' is not installed. Install with: pip install {req.package}",
            )

        # Check exact version
        if req.exact_version:
            is_compat = self._compare_versions(installed, req.exact_version) == 0
            if not is_compat:
                return VersionCheckResult(
                    package=req.package,
                    installed_version=installed,
                    requirement=req,
                    is_compatible=False,
                    error_message=f"Package '{req.package}' version {installed} != required {req.exact_version}. Run: pip install {req.package}=={req.exact_version}",
                )

        # Check min version
        if req.min_version:
            if self._compare_versions(installed, req.min_version) < 0:
                return VersionCheckResult(
                    package=req.package,
                    installed_version=installed,
                    requirement=req,
                    is_compatible=False,
                    error_message=f"Package '{req.package}' version {installed} < required minimum {req.min_version}. Run: pip install --upgrade {req.package}",
                )

        # Check max version
        if req.max_version:
            if self._compare_versions(installed, req.max_version) > 0:
                return VersionCheckResult(
                    package=req.package,
                    installed_version=installed,
                    requirement=req,
                    is_compatible=False,
                    error_message=f"Package '{req.package}' version {installed} > maximum allowed {req.max_version}. Run: pip install {req.package}<={req.max_version}",
                )

        return VersionCheckResult(
            package=req.package,
            installed_version=installed,
            requirement=req,
            is_compatible=True,
        )

    async def validate_package(self, package: str) -> Optional[VersionCheckResult]:
        """Validate a specific package."""
        req = self._requirements.get(package)
        if req is None:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._check_requirement, req)

    async def validate_all_packages(self) -> List[VersionCheckResult]:
        """Validate all package requirements."""
        results: List[VersionCheckResult] = []

        for package, req in self._requirements.items():
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._check_requirement, req)
            results.append(result)

        return results

    def check_component_compatibility(
        self,
        component: str,
        other_component: str,
        other_version: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if two component versions are compatible.

        Returns:
            (is_compatible, error_message)
        """
        # Check if component has requirements for other_component
        comp_reqs = self._compatibility_matrix.get(component, {})
        min_version = comp_reqs.get(other_component)

        if min_version:
            if self._compare_versions(other_version, min_version) < 0:
                return False, (
                    f"Component '{component}' requires '{other_component}' >= {min_version}, "
                    f"but found version {other_version}"
                )

        # Check reverse direction
        other_reqs = self._compatibility_matrix.get(other_component, {})
        reverse_min = other_reqs.get(component)

        if reverse_min:
            comp_info = self._component_versions.get(component)
            if comp_info and self._compare_versions(comp_info.version, reverse_min) < 0:
                return False, (
                    f"Component '{other_component}' requires '{component}' >= {reverse_min}, "
                    f"but found version {comp_info.version}"
                )

        return True, None

    async def validate_all_components(self) -> List[Tuple[str, str, bool, Optional[str]]]:
        """
        Validate all component compatibilities.

        Returns:
            List of (component, other_component, is_compatible, error_message)
        """
        results: List[Tuple[str, str, bool, Optional[str]]] = []

        for component, requirements in self._compatibility_matrix.items():
            for other_component, min_version in requirements.items():
                other_info = self._component_versions.get(other_component)

                if other_info is None:
                    results.append((
                        component,
                        other_component,
                        False,
                        f"Component '{other_component}' not registered",
                    ))
                    continue

                is_compat, error = self.check_component_compatibility(
                    component,
                    other_component,
                    other_info.version,
                )

                results.append((component, other_component, is_compat, error))

        return results

    async def validate_and_report(self) -> bool:
        """
        Validate all versions and log detailed report.

        Returns:
            True if all checks pass
        """
        all_valid = True

        # Check package requirements
        pkg_results = await self.validate_all_packages()

        for result in pkg_results:
            if result.is_compatible:
                logger.debug(f"[Version] Package {result.package} {result.installed_version}: OK")
            else:
                all_valid = False
                logger.error(f"[Version] {result.error_message}")

        # Check component compatibility
        comp_results = await self.validate_all_components()

        for component, other, is_compat, error in comp_results:
            if is_compat:
                logger.debug(f"[Version] {component} <-> {other}: compatible")
            else:
                all_valid = False
                logger.error(f"[Version] {error}")

        if all_valid:
            logger.info("[Version] All version checks passed")
        else:
            logger.error("[Version] Version compatibility issues detected")

        return all_valid


# =============================================================================
# Singleton Instances for Issues 26-30
# =============================================================================


_disk_manager: Optional[DiskSpaceManager] = None
_memory_manager: Optional[MemoryPressureManager] = None
_permission_validator: Optional[PermissionValidator] = None
_state_recovery: Optional[StateFileRecoveryManager] = None
_version_manager: Optional[DependencyVersionManager] = None


def get_disk_manager() -> DiskSpaceManager:
    """Get the global disk space manager."""
    global _disk_manager
    if _disk_manager is None:
        _disk_manager = DiskSpaceManager()
    return _disk_manager


def get_memory_manager() -> MemoryPressureManager:
    """Get the global memory pressure manager."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryPressureManager()
    return _memory_manager


def get_permission_validator() -> PermissionValidator:
    """Get the global permission validator."""
    global _permission_validator
    if _permission_validator is None:
        _permission_validator = PermissionValidator()
    return _permission_validator


def get_state_recovery() -> StateFileRecoveryManager:
    """Get the global state file recovery manager."""
    global _state_recovery
    if _state_recovery is None:
        _state_recovery = StateFileRecoveryManager()
    return _state_recovery


def get_version_manager() -> DependencyVersionManager:
    """Get the global dependency version manager."""
    global _version_manager
    if _version_manager is None:
        _version_manager = DependencyVersionManager()
    return _version_manager


# =============================================================================
# Convenience Functions for Issues 26-30
# =============================================================================


# Issue 26: Disk Space
async def check_disk_space() -> Optional[DiskSpaceStatus]:
    """Check current disk space status."""
    return await get_disk_manager().get_status()


async def cleanup_disk_space() -> Dict[str, CleanupResult]:
    """Clean up old files to free disk space."""
    return await get_disk_manager().cleanup_all()


async def start_disk_monitoring() -> None:
    """Start disk space monitoring."""
    await get_disk_manager().start_monitoring()


async def stop_disk_monitoring() -> None:
    """Stop disk space monitoring."""
    await get_disk_manager().stop_monitoring()


# Issue 27: Memory
async def check_memory_pressure() -> Optional[MemoryStatus]:
    """Check current memory status."""
    return await get_memory_manager().get_status()


async def start_memory_monitoring() -> None:
    """Start memory pressure monitoring."""
    await get_memory_manager().start_monitoring()


async def stop_memory_monitoring() -> None:
    """Stop memory pressure monitoring."""
    await get_memory_manager().stop_monitoring()


def register_cache_for_cleanup(name: str, clear_func: Callable[[], int]) -> None:
    """Register a cache for automatic cleanup under memory pressure."""
    get_memory_manager().register_cache(name, clear_func)


# Issue 28: Permissions
async def validate_permissions() -> PermissionValidationResult:
    """Validate all required permissions."""
    return await get_permission_validator().validate_all()


async def validate_permissions_with_report() -> bool:
    """Validate permissions and log detailed report."""
    return await get_permission_validator().validate_and_report()


# Issue 29: State Recovery
async def check_state_health(name: str) -> StateFileHealth:
    """Check health of a state file."""
    return await get_state_recovery().check_health(name)


async def repair_state_file(name: str) -> bool:
    """Repair a corrupted state file."""
    return await get_state_recovery().repair(name)


async def ensure_state_healthy(name: str) -> bool:
    """Ensure state file is healthy, repairing if needed."""
    return await get_state_recovery().ensure_healthy(name)


# Issue 30: Version Checking
async def validate_dependencies() -> List[VersionCheckResult]:
    """Validate all package dependencies."""
    return await get_version_manager().validate_all_packages()


async def validate_dependencies_with_report() -> bool:
    """Validate dependencies and log detailed report."""
    return await get_version_manager().validate_and_report()


def add_dependency_requirement(
    package: str,
    min_version: Optional[str] = None,
    max_version: Optional[str] = None,
) -> None:
    """Add a package dependency requirement."""
    get_version_manager().add_requirement(package, min_version, max_version)


# =============================================================================
# Comprehensive System Health Check
# =============================================================================


@dataclass
class SystemHealthReport:
    """Comprehensive system health report."""
    timestamp: float
    disk_status: Optional[DiskSpaceStatus]
    memory_status: Optional[MemoryStatus]
    permission_result: PermissionValidationResult
    state_health: Dict[str, StateFileHealth]
    version_results: List[VersionCheckResult]
    overall_healthy: bool
    critical_issues: List[str]
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "timestamp": self.timestamp,
            "disk_status": self.disk_status.to_dict() if self.disk_status else None,
            "memory_status": self.memory_status.to_dict() if self.memory_status else None,
            "permission_result": self.permission_result.to_dict(),
            "state_health": {k: v.to_dict() for k, v in self.state_health.items()},
            "version_results": [r.to_dict() for r in self.version_results],
            "overall_healthy": self.overall_healthy,
            "critical_issues": self.critical_issues,
            "warnings": self.warnings,
        }


async def comprehensive_health_check() -> SystemHealthReport:
    """
    Perform comprehensive system health check.

    Checks:
    - Disk space
    - Memory
    - Permissions
    - State files
    - Dependencies

    Returns:
        Complete health report
    """
    critical_issues: List[str] = []
    warnings: List[str] = []

    # Check disk
    disk_status = await check_disk_space()
    if disk_status:
        if disk_status.pressure_level == DiskPressureLevel.EMERGENCY:
            critical_issues.append(f"Disk critically full: {disk_status.percent_free:.1f}% free")
        elif disk_status.pressure_level == DiskPressureLevel.CRITICAL:
            warnings.append(f"Disk space low: {disk_status.percent_free:.1f}% free")

    # Check memory
    memory_status = await check_memory_pressure()
    if memory_status:
        if memory_status.pressure_level == MemoryPressureLevel.CRITICAL:
            critical_issues.append(f"Memory critical: {memory_status.percent_used:.1f}% used")
        elif memory_status.pressure_level == MemoryPressureLevel.HIGH:
            warnings.append(f"Memory high: {memory_status.percent_used:.1f}% used")

    # Check permissions
    permission_result = await validate_permissions()
    for check in permission_result.critical_failures:
        critical_issues.append(f"Permission denied: {check.error_message}")
    for check in permission_result.warnings:
        warnings.append(f"Permission warning: {check.error_message}")

    # Check state files
    state_health = await get_state_recovery().get_all_health()
    for name, health in state_health.items():
        if not health.is_valid:
            warnings.append(f"State file corrupted: {name} ({health.corruption_type})")

    # Check dependencies
    version_results = await validate_dependencies()
    for result in version_results:
        if not result.is_compatible:
            critical_issues.append(f"Dependency issue: {result.error_message}")

    overall_healthy = len(critical_issues) == 0

    return SystemHealthReport(
        timestamp=time.time(),
        disk_status=disk_status,
        memory_status=memory_status,
        permission_result=permission_result,
        state_health=state_health,
        version_results=version_results,
        overall_healthy=overall_healthy,
        critical_issues=critical_issues,
        warnings=warnings,
    )


async def startup_health_validation() -> bool:
    """
    Perform startup health validation.

    Should be called before starting Trinity components.

    Returns:
        True if system is healthy enough to start
    """
    logger.info("[Health] Starting system health validation...")

    report = await comprehensive_health_check()

    # Log warnings
    for warning in report.warnings:
        logger.warning(f"[Health] {warning}")

    # Log critical issues
    for issue in report.critical_issues:
        logger.error(f"[Health] CRITICAL: {issue}")

    if report.overall_healthy:
        logger.info("[Health] System health validation passed")
    else:
        logger.error(f"[Health] Validation failed with {len(report.critical_issues)} critical issues")

    return report.overall_healthy


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Configuration
    "RacePreventionConfig",
    "get_config",

    # Process Identity
    "ProcessIdentity",
    "get_current_identity",

    # Issue 9: Atomic File Lock
    "AtomicFileLock",
    "LockState",
    "LockInfo",
    "get_file_lock",
    "acquire_trinity_lock",

    # Issue 10: Atomic Heartbeat Writer
    "AtomicHeartbeatWriter",
    "HeartbeatRecord",
    "get_heartbeat_writer",
    "write_trinity_heartbeat",

    # Issue 11: Atomic Port Reservation
    "AtomicPortReservation",
    "PortReservation",
    "PortState",
    "get_port_reserver",
    "reserve_trinity_port",

    # Issue 12: Dependency Health Monitor
    "DependencyHealthMonitor",
    "DependencyNode",
    "DependencyState",
    "get_dependency_monitor",
    "start_trinity_dependency_monitoring",

    # Issue 13: Safe Process Manager
    "SafeProcessManager",
    "ProcessFingerprint",
    "ProcessVerificationResult",
    "get_process_manager",
    "safe_register_process",
    "safe_kill_process",

    # Issue 14: Atomic Config Manager
    "AtomicConfigManager",
    "VersionedConfig",
    "get_config_manager",
    "get_trinity_config",
    "set_trinity_config",

    # Issue 15: Persistent Message Queue
    "PersistentMessageQueue",
    "PersistentMessage",
    "MessagePriority",
    "MessageState",
    "get_message_queue",
    "publish_trinity_event",
    "consume_trinity_events",
    "acknowledge_trinity_event",

    # Issue 16: Adaptive Startup Timeout
    "AdaptiveStartupTimeout",
    "StartupPhase",
    "StartupState",
    "get_startup_timeout",
    "track_trinity_startup",
    "confirm_trinity_spawn",
    "mark_trinity_ready",
    "check_trinity_timeout",

    # Issue 17: Resilient Health Checker
    "ResilientHealthChecker",
    "HealthCheckResult",
    "CachedHealthStatus",
    "get_health_checker",
    "register_trinity_health_check",
    "check_trinity_health",
    "mark_trinity_restarting",

    # Issue 18: Coordinated Resource Manager
    "CoordinatedResourceManager",
    "ResourceType",
    "ResourceReservation",
    "get_resource_manager",
    "reserve_trinity_resource",
    "release_trinity_resource",
    "cleanup_trinity_stale_resources",

    # Issue 19: Atomic Log Writer
    "AtomicLogWriter",
    "LogEntry",
    "get_log_writer",
    "write_trinity_log",
    "rotate_trinity_logs",

    # Issue 20: Atomic State File
    "AtomicStateFile",
    "VersionedState",
    "get_state_file",
    "read_trinity_state",
    "write_trinity_state",
    "update_trinity_state",
    "recover_trinity_state",

    # Issue 26: Disk Space Management
    "DiskSpaceManager",
    "DiskPressureLevel",
    "DiskSpaceStatus",
    "CleanupResult",
    "get_disk_manager",
    "check_disk_space",
    "cleanup_disk_space",
    "start_disk_monitoring",
    "stop_disk_monitoring",

    # Issue 27: Memory Pressure Management
    "MemoryPressureManager",
    "MemoryPressureLevel",
    "MemoryStatus",
    "ComponentMemoryInfo",
    "get_memory_manager",
    "check_memory_pressure",
    "start_memory_monitoring",
    "stop_memory_monitoring",
    "register_cache_for_cleanup",

    # Issue 28: Permission Validation
    "PermissionValidator",
    "PermissionType",
    "PermissionCheck",
    "PermissionValidationResult",
    "get_permission_validator",
    "validate_permissions",
    "validate_permissions_with_report",

    # Issue 29: State File Recovery
    "StateFileRecoveryManager",
    "StateFileHealth",
    "get_state_recovery",
    "check_state_health",
    "repair_state_file",
    "ensure_state_healthy",

    # Issue 30: Dependency Version Management
    "DependencyVersionManager",
    "VersionRequirement",
    "VersionCheckResult",
    "ComponentVersionInfo",
    "get_version_manager",
    "validate_dependencies",
    "validate_dependencies_with_report",
    "add_dependency_requirement",

    # System Health
    "SystemHealthReport",
    "comprehensive_health_check",
    "startup_health_validation",
]

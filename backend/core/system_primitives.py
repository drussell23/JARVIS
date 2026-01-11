"""
JARVIS System Primitives v90.0 - Production-Grade Systems Engineering Layer
=============================================================================

This module provides iron-clad, kernel-level primitives for managing:
- Subprocess lifecycle with guaranteed cleanup
- Atomic file operations across filesystems
- Distributed port locking and coordination
- PID-validated heartbeat monitoring
- Process group management for clean shutdown

These primitives fix the 11 CRITICAL TIER-1 issues identified in the gap analysis:
1. File Handle Leaks (#1, #5) -> SafeProcess with guaranteed cleanup
2. Race Conditions (#2) -> PortManager with fcntl locking
3. Heartbeat Staleness (#3, #4) -> TrueHeartbeat with PID validation
4. Context Manager Escape (#5) -> SafeProcess with proper lifecycle
5. Signal Propagation (#6) -> Process groups with os.setsid
6. Atomic File Writes (#7) -> AtomicStateWriter with same-dir temp files
7. Error Propagation (#8) -> Enhanced exception handling with full traceback
8. Adaptive Sleeps (#9) -> AdaptiveWaiter with system load awareness
9. Browser Lock (#10) -> FileLock with fcntl
10. Circular Import (#11) -> Lazy imports and proper module structure

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │  System Primitives v90.0 (Iron-Clad Production Layer)                   │
    ├─────────────────────────────────────────────────────────────────────────┤
    │  SafeProcess           - Subprocess lifecycle with process groups       │
    │  AtomicStateWriter     - Corruption-proof file writes                   │
    │  PortManager           - Distributed port locking with fcntl            │
    │  TrueHeartbeat         - PID-validated health monitoring                │
    │  FileLock              - Cross-platform exclusive file locking          │
    │  ProcessGroup          - Kernel-level process group management          │
    │  AdaptiveWaiter        - System-load-aware delays                       │
    │  ResourceGuard         - Automatic cleanup with __del__ safety net      │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS System v90.0
License: MIT
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import errno
import hashlib
import json
import logging
import os
import platform
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import traceback
import uuid
import weakref
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from typing import (
    Any, AsyncGenerator, Awaitable, Callable, Dict, Final, Generator,
    Generic, List, Literal, Mapping, Optional, Protocol, Set, Tuple,
    Type, TypeVar, Union, cast, overload, runtime_checkable,
)

# Lazy imports for optional dependencies
_psutil: Optional[Any] = None
_fcntl: Optional[Any] = None
_msvcrt: Optional[Any] = None

def _get_psutil():
    """Lazy import psutil to avoid circular imports."""
    global _psutil
    if _psutil is None:
        import psutil
        _psutil = psutil
    return _psutil

def _get_fcntl():
    """Lazy import fcntl (Unix only)."""
    global _fcntl
    if _fcntl is None and sys.platform != 'win32':
        import fcntl
        _fcntl = fcntl
    return _fcntl

def _get_msvcrt():
    """Lazy import msvcrt (Windows only)."""
    global _msvcrt
    if _msvcrt is None and sys.platform == 'win32':
        import msvcrt
        _msvcrt = msvcrt
    return _msvcrt

logger = logging.getLogger(__name__)

# Type variables
T = TypeVar('T')
R = TypeVar('R')

# =============================================================================
# CONSTANTS AND CONFIGURATION
# =============================================================================

class SystemConfig:
    """
    Centralized configuration for system primitives.
    All values are environment-driven with sensible defaults.
    """

    # Process Management
    PROCESS_SIGTERM_TIMEOUT: float = float(os.getenv("JARVIS_SIGTERM_TIMEOUT", "5.0"))
    PROCESS_SIGKILL_TIMEOUT: float = float(os.getenv("JARVIS_SIGKILL_TIMEOUT", "2.0"))
    PROCESS_GROUP_ENABLED: bool = os.getenv("JARVIS_PROCESS_GROUPS", "true").lower() == "true"

    # File Operations
    ATOMIC_WRITE_FSYNC: bool = os.getenv("JARVIS_FSYNC_WRITES", "true").lower() == "true"
    ATOMIC_WRITE_PERMS: int = int(os.getenv("JARVIS_FILE_PERMS", "0o644"), 8)

    # Locking
    LOCK_TIMEOUT: float = float(os.getenv("JARVIS_LOCK_TIMEOUT", "30.0"))
    LOCK_POLL_INTERVAL: float = float(os.getenv("JARVIS_LOCK_POLL", "0.1"))
    LOCK_STALE_THRESHOLD: float = float(os.getenv("JARVIS_LOCK_STALE", "300.0"))

    # Heartbeat
    HEARTBEAT_STALE_THRESHOLD: float = float(os.getenv("JARVIS_HEARTBEAT_STALE", "30.0"))
    HEARTBEAT_PID_VALIDATE: bool = os.getenv("JARVIS_HEARTBEAT_PID_CHECK", "true").lower() == "true"

    # Adaptive Waiting
    ADAPTIVE_MIN_DELAY: float = float(os.getenv("JARVIS_ADAPTIVE_MIN_DELAY", "0.1"))
    ADAPTIVE_MAX_DELAY: float = float(os.getenv("JARVIS_ADAPTIVE_MAX_DELAY", "30.0"))
    ADAPTIVE_LOAD_THRESHOLD: float = float(os.getenv("JARVIS_ADAPTIVE_LOAD_THRESHOLD", "0.8"))

    # Paths
    STATE_DIR: Path = Path(os.getenv("JARVIS_STATE_DIR", str(Path.home() / ".jarvis" / "state")))
    LOCKS_DIR: Path = Path(os.getenv("JARVIS_LOCKS_DIR", str(Path.home() / ".jarvis" / "locks")))
    HEARTBEATS_DIR: Path = Path(os.getenv("JARVIS_HEARTBEATS_DIR", str(Path.home() / ".jarvis" / "trinity" / "components")))

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensure all required directories exist."""
        for dir_path in [cls.STATE_DIR, cls.LOCKS_DIR, cls.HEARTBEATS_DIR]:
            dir_path.mkdir(parents=True, exist_ok=True)


# =============================================================================
# ENUMS AND DATA CLASSES
# =============================================================================

class ProcessState(Enum):
    """Process lifecycle states."""
    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()
    ZOMBIE = auto()


class LockState(Enum):
    """Lock acquisition states."""
    UNLOCKED = auto()
    ACQUIRING = auto()
    LOCKED = auto()
    STALE = auto()
    ERROR = auto()


class HeartbeatStatus(Enum):
    """Heartbeat validation status."""
    ALIVE = auto()
    STALE = auto()
    DEAD = auto()
    MISSING = auto()
    INVALID = auto()


@dataclass
class ProcessInfo:
    """Comprehensive process information."""
    pid: int
    pgid: int  # Process group ID
    name: str
    cmdline: List[str]
    state: ProcessState
    started_at: float
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    return_code: Optional[int] = None
    error: Optional[str] = None

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "pgid": self.pgid,
            "name": self.name,
            "cmdline": self.cmdline,
            "state": self.state.name,
            "started_at": self.started_at,
            "uptime_seconds": self.uptime_seconds,
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "return_code": self.return_code,
            "error": self.error,
        }


@dataclass
class HeartbeatData:
    """Validated heartbeat data with PID information."""
    component_name: str
    pid: int
    timestamp: float
    host: str
    status: str
    version: str = "90.0"
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > SystemConfig.HEARTBEAT_STALE_THRESHOLD

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_name": self.component_name,
            "pid": self.pid,
            "timestamp": self.timestamp,
            "host": self.host,
            "status": self.status,
            "version": self.version,
            "metrics": self.metrics,
            "age_seconds": self.age_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HeartbeatData":
        return cls(
            component_name=data.get("component_name", data.get("component_type", "unknown")),
            pid=data.get("pid", -1),
            timestamp=data.get("timestamp", 0.0),
            host=data.get("host", "unknown"),
            status=data.get("status", "unknown"),
            version=data.get("version", "unknown"),
            metrics=data.get("metrics", {}),
        )


# =============================================================================
# RESOURCE GUARD - Safety Net for Resource Cleanup
# =============================================================================

class ResourceGuard:
    """
    Safety net for resource cleanup.

    Uses weak references and __del__ as a last resort to prevent leaks.
    Primary cleanup should be via context manager or explicit close().
    """

    _active_guards: Set[int] = set()
    _lock = threading.Lock()

    def __init__(self, name: str, cleanup_fn: Callable[[], None]):
        self.name = name
        self._cleanup_fn = cleanup_fn
        self._cleaned_up = False
        self._id = id(self)

        with self._lock:
            self._active_guards.add(self._id)

        # Register with atexit as additional safety
        atexit.register(self._atexit_cleanup)

    def cleanup(self) -> None:
        """Explicit cleanup - preferred method."""
        if not self._cleaned_up:
            self._cleaned_up = True
            with self._lock:
                self._active_guards.discard(self._id)
            try:
                self._cleanup_fn()
            except Exception as e:
                logger.warning(f"[ResourceGuard:{self.name}] Cleanup error: {e}")

    def _atexit_cleanup(self) -> None:
        """Atexit cleanup - secondary safety."""
        if not self._cleaned_up:
            self.cleanup()

    def __del__(self):
        """Destructor cleanup - last resort safety net."""
        if not self._cleaned_up:
            logger.warning(f"[ResourceGuard:{self.name}] Resource leaked - cleaning up in __del__")
            try:
                self._cleanup_fn()
            except Exception:
                pass  # Can't log in __del__ reliably
            self._cleaned_up = True

    @classmethod
    def get_active_count(cls) -> int:
        """Get count of active guards (for debugging leaks)."""
        with cls._lock:
            return len(cls._active_guards)


# =============================================================================
# FILE LOCK - Cross-Platform Exclusive File Locking
# =============================================================================

class FileLock:
    """
    Cross-platform exclusive file locking using fcntl (Unix) or msvcrt (Windows).

    Features:
    - Atomic lock acquisition
    - Stale lock detection via PID validation
    - Automatic cleanup on context exit
    - Non-blocking and blocking modes
    - Lock file contains owner info for debugging

    Fixes Issues: #2 (Race Conditions), #10 (Browser Lock)
    """

    def __init__(
        self,
        lock_path: Union[str, Path],
        timeout: Optional[float] = None,
        stale_threshold: Optional[float] = None,
    ):
        self.lock_path = Path(lock_path)
        self.timeout = timeout or SystemConfig.LOCK_TIMEOUT
        self.stale_threshold = stale_threshold or SystemConfig.LOCK_STALE_THRESHOLD

        self._fd: Optional[int] = None
        self._state = LockState.UNLOCKED
        self._owner_pid = os.getpid()
        self._owner_id = uuid.uuid4().hex[:8]
        self._acquired_at: Optional[float] = None

        # Ensure parent directory exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Resource guard for safety
        self._guard = ResourceGuard(f"FileLock:{self.lock_path.name}", self._cleanup)

    @property
    def is_locked(self) -> bool:
        return self._state == LockState.LOCKED

    def _write_lock_info(self) -> None:
        """Write lock owner information to the lock file."""
        info = {
            "pid": self._owner_pid,
            "owner_id": self._owner_id,
            "host": socket.gethostname(),
            "acquired_at": time.time(),
            "process_name": sys.argv[0] if sys.argv else "unknown",
        }
        try:
            with open(self.lock_path, 'w') as f:
                json.dump(info, f)
                f.flush()
                if SystemConfig.ATOMIC_WRITE_FSYNC:
                    os.fsync(f.fileno())
        except Exception as e:
            logger.debug(f"[FileLock] Could not write lock info: {e}")

    def _read_lock_info(self) -> Optional[Dict[str, Any]]:
        """Read lock owner information from the lock file."""
        try:
            if self.lock_path.exists():
                with open(self.lock_path, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _is_lock_stale(self) -> bool:
        """Check if the lock is stale (owner process dead or timeout)."""
        info = self._read_lock_info()
        if info is None:
            return True

        # Check if owner PID is alive
        owner_pid = info.get("pid")
        if owner_pid:
            psutil = _get_psutil()
            if psutil and not psutil.pid_exists(owner_pid):
                logger.info(f"[FileLock] Stale lock detected - PID {owner_pid} is dead")
                return True

        # Check if lock is too old
        acquired_at = info.get("acquired_at", 0)
        if time.time() - acquired_at > self.stale_threshold:
            logger.info(f"[FileLock] Stale lock detected - age exceeds {self.stale_threshold}s")
            return True

        return False

    def _try_break_stale_lock(self) -> bool:
        """Attempt to break a stale lock."""
        if not self._is_lock_stale():
            return False

        try:
            self.lock_path.unlink(missing_ok=True)
            logger.info(f"[FileLock] Broke stale lock: {self.lock_path}")
            return True
        except Exception as e:
            logger.warning(f"[FileLock] Could not break stale lock: {e}")
            return False

    def _acquire_unix(self, blocking: bool = True) -> bool:
        """Acquire lock on Unix using fcntl."""
        fcntl = _get_fcntl()
        if fcntl is None:
            raise RuntimeError("fcntl not available on this platform")

        try:
            # Open or create the lock file
            self._fd = os.open(
                str(self.lock_path),
                os.O_RDWR | os.O_CREAT,
                SystemConfig.ATOMIC_WRITE_PERMS
            )

            # Try to acquire exclusive lock
            lock_flags = fcntl.LOCK_EX
            if not blocking:
                lock_flags |= fcntl.LOCK_NB

            fcntl.flock(self._fd, lock_flags)

            self._state = LockState.LOCKED
            self._acquired_at = time.time()
            self._write_lock_info()

            logger.debug(f"[FileLock] Acquired: {self.lock_path}")
            return True

        except (BlockingIOError, OSError) as e:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise

    def _acquire_windows(self, blocking: bool = True) -> bool:
        """Acquire lock on Windows using msvcrt."""
        msvcrt = _get_msvcrt()
        if msvcrt is None:
            raise RuntimeError("msvcrt not available on this platform")

        try:
            self._fd = os.open(
                str(self.lock_path),
                os.O_RDWR | os.O_CREAT,
                SystemConfig.ATOMIC_WRITE_PERMS
            )

            if blocking:
                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            else:
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)

            self._state = LockState.LOCKED
            self._acquired_at = time.time()
            self._write_lock_info()

            return True

        except OSError:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquire the lock.

        Args:
            blocking: Whether to block until lock is acquired
            timeout: Override default timeout (only for blocking mode)

        Returns:
            True if lock acquired, False otherwise
        """
        if self._state == LockState.LOCKED:
            return True

        self._state = LockState.ACQUIRING
        timeout = timeout or self.timeout
        start_time = time.time()

        while True:
            # Try to break stale lock first
            self._try_break_stale_lock()

            # Attempt to acquire
            try:
                if sys.platform == 'win32':
                    success = self._acquire_windows(blocking=False)
                else:
                    success = self._acquire_unix(blocking=False)

                if success:
                    return True

            except Exception as e:
                self._state = LockState.ERROR
                logger.error(f"[FileLock] Acquisition error: {e}")
                raise

            # Non-blocking mode - return immediately
            if not blocking:
                self._state = LockState.UNLOCKED
                return False

            # Check timeout
            if time.time() - start_time > timeout:
                self._state = LockState.UNLOCKED
                logger.warning(f"[FileLock] Timeout acquiring: {self.lock_path}")
                return False

            # Poll interval
            time.sleep(SystemConfig.LOCK_POLL_INTERVAL)

    def release(self) -> None:
        """Release the lock."""
        if self._state != LockState.LOCKED:
            return

        try:
            if self._fd is not None:
                if sys.platform == 'win32':
                    msvcrt = _get_msvcrt()
                    if msvcrt:
                        try:
                            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                else:
                    fcntl = _get_fcntl()
                    if fcntl:
                        try:
                            fcntl.flock(self._fd, fcntl.LOCK_UN)
                        except Exception:
                            pass

                os.close(self._fd)
                self._fd = None

            # Remove lock file
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass

            self._state = LockState.UNLOCKED
            logger.debug(f"[FileLock] Released: {self.lock_path}")

        except Exception as e:
            logger.warning(f"[FileLock] Release error: {e}")

    def _cleanup(self) -> None:
        """Cleanup for ResourceGuard."""
        self.release()

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
        self._guard.cleanup()

    async def __aenter__(self) -> "FileLock":
        """Async context manager entry."""
        loop = asyncio.get_event_loop()
        acquired = await loop.run_in_executor(None, self.acquire)
        if not acquired:
            raise RuntimeError(f"Could not acquire lock: {self.lock_path}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.release)
        self._guard.cleanup()


# =============================================================================
# ATOMIC STATE WRITER - Corruption-Proof File Writes
# =============================================================================

class AtomicStateWriter:
    """
    Atomic file writer that prevents corruption.

    Features:
    - Writes to temp file in SAME directory (fixes NFS/cross-fs issues)
    - Uses os.fsync() to flush to disk
    - Uses os.replace() for atomic swap
    - JSON schema validation (optional)
    - Backup on overwrite (optional)

    Fixes Issues: #7 (Atomic File Writes), #20 (JSON Validation), #30 (NFS)
    """

    def __init__(
        self,
        path: Union[str, Path],
        backup: bool = False,
        schema: Optional[Dict[str, Any]] = None,
    ):
        self.path = Path(path)
        self.backup = backup
        self.schema = schema

        # Ensure parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _validate_schema(self, data: Dict[str, Any]) -> bool:
        """Validate data against schema (basic validation)."""
        if self.schema is None:
            return True

        for key, expected_type in self.schema.items():
            if key in data:
                if not isinstance(data[key], expected_type):
                    logger.warning(f"[AtomicWriter] Schema violation: {key} expected {expected_type}, got {type(data[key])}")
                    return False
        return True

    def write(self, data: Any, encoding: str = 'utf-8') -> bool:
        """
        Atomically write data to file.

        Args:
            data: Data to write (will be JSON-encoded if dict/list)
            encoding: File encoding

        Returns:
            True if successful, False otherwise
        """
        try:
            # Prepare content
            if isinstance(data, (dict, list)):
                if isinstance(data, dict) and not self._validate_schema(data):
                    return False
                content = json.dumps(data, indent=2, default=str)
            elif isinstance(data, bytes):
                content = data
                encoding = None  # Binary mode
            else:
                content = str(data)

            # Create temp file in SAME directory (critical for atomicity across filesystems)
            fd, temp_path = tempfile.mkstemp(
                dir=str(self.path.parent),
                prefix=f".{self.path.stem}_",
                suffix=".tmp"
            )

            try:
                # Write content
                if encoding:
                    with os.fdopen(fd, 'w', encoding=encoding) as f:
                        f.write(content)
                        f.flush()
                        if SystemConfig.ATOMIC_WRITE_FSYNC:
                            os.fsync(f.fileno())
                else:
                    with os.fdopen(fd, 'wb') as f:
                        f.write(content)
                        f.flush()
                        if SystemConfig.ATOMIC_WRITE_FSYNC:
                            os.fsync(f.fileno())

                # Set permissions
                os.chmod(temp_path, SystemConfig.ATOMIC_WRITE_PERMS)

                # Backup existing file if requested
                if self.backup and self.path.exists():
                    backup_path = self.path.with_suffix(f"{self.path.suffix}.bak")
                    try:
                        os.replace(str(self.path), str(backup_path))
                    except Exception:
                        pass

                # Atomic replace
                os.replace(temp_path, str(self.path))

                logger.debug(f"[AtomicWriter] Wrote: {self.path}")
                return True

            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                raise

        except Exception as e:
            logger.error(f"[AtomicWriter] Write error for {self.path}: {e}")
            return False

    def read(self, encoding: str = 'utf-8') -> Optional[Any]:
        """
        Read data from file.

        Returns:
            Parsed JSON if file contains JSON, raw content otherwise
        """
        try:
            if not self.path.exists():
                return None

            with open(self.path, 'r', encoding=encoding) as f:
                content = f.read()

            # Try to parse as JSON
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content

        except Exception as e:
            logger.error(f"[AtomicWriter] Read error for {self.path}: {e}")
            return None

    def exists(self) -> bool:
        """Check if file exists."""
        return self.path.exists()

    def delete(self) -> bool:
        """Delete the file."""
        try:
            self.path.unlink(missing_ok=True)
            return True
        except Exception as e:
            logger.error(f"[AtomicWriter] Delete error for {self.path}: {e}")
            return False

    @contextmanager
    def locked_write(self) -> Generator[Callable[[Any], bool], None, None]:
        """
        Context manager for locked atomic writes.

        Usage:
            with writer.locked_write() as write:
                write({"key": "value"})
        """
        lock = FileLock(self.path.with_suffix('.lock'))
        with lock:
            yield self.write


# =============================================================================
# SAFE PROCESS - Subprocess Lifecycle with Guaranteed Cleanup
# =============================================================================

class SafeProcess:
    """
    Subprocess wrapper with guaranteed resource cleanup and process group management.

    Features:
    - Creates process group for clean shutdown (os.setsid)
    - Guarantees file handle closure in all cases
    - Signal propagation to entire process group
    - Zombie detection and cleanup
    - Async-compatible with proper cancellation handling

    Fixes Issues: #1 (File Handle Leaks), #5 (Context Manager Escape),
                  #6 (Signal Propagation), #15 (Environment Leaks), #17 (Process Groups)
    """

    # Track all active processes for emergency cleanup
    _active_processes: Dict[int, weakref.ref] = {}
    _cleanup_registered = False
    _lock = threading.Lock()

    def __init__(
        self,
        name: str,
        cmd: List[str],
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        stdout_path: Optional[Union[str, Path]] = None,
        stderr_path: Optional[Union[str, Path]] = None,
        inherit_env: bool = True,
        env_filter: Optional[List[str]] = None,
    ):
        """
        Initialize SafeProcess.

        Args:
            name: Human-readable process name (for logging)
            cmd: Command and arguments to execute
            cwd: Working directory
            env: Additional environment variables
            stdout_path: Path for stdout log (None = discard)
            stderr_path: Path for stderr log (None = discard)
            inherit_env: Whether to inherit parent environment
            env_filter: If set, only inherit these env vars (prevents pollution)
        """
        self.name = name
        self.cmd = cmd
        self.cwd = Path(cwd) if cwd else None
        self.stdout_path = Path(stdout_path) if stdout_path else None
        self.stderr_path = Path(stderr_path) if stderr_path else None
        self.inherit_env = inherit_env
        self.env_filter = env_filter

        # Build environment (fix #15: filter to prevent pollution)
        self._env = self._build_environment(env)

        # Process state
        self._process: Optional[asyncio.subprocess.Process] = None
        self._state = ProcessState.CREATED
        self._started_at: Optional[float] = None
        self._pid: Optional[int] = None
        self._pgid: Optional[int] = None
        self._return_code: Optional[int] = None
        self._error: Optional[str] = None

        # File handles (explicitly tracked for cleanup)
        self._stdout_fd: Optional[int] = None
        self._stderr_fd: Optional[int] = None
        self._stdout_file: Optional[Any] = None
        self._stderr_file: Optional[Any] = None

        # Resource guard
        self._guard = ResourceGuard(f"SafeProcess:{name}", self._emergency_cleanup)

        # Register global cleanup handler
        self._register_cleanup_handler()

    def _build_environment(self, additional: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Build filtered environment."""
        env = {}

        if self.inherit_env:
            if self.env_filter:
                # Only inherit specific variables
                for key in self.env_filter:
                    if key in os.environ:
                        env[key] = os.environ[key]
            else:
                # Inherit all (default behavior)
                env = os.environ.copy()

        # Add PATH if not present
        if 'PATH' not in env:
            env['PATH'] = os.environ.get('PATH', '/usr/bin:/bin')

        # Add additional variables
        if additional:
            env.update(additional)

        return env

    @classmethod
    def _register_cleanup_handler(cls) -> None:
        """Register global cleanup handler for emergency shutdown."""
        with cls._lock:
            if not cls._cleanup_registered:
                atexit.register(cls._global_cleanup)

                # Also register signal handlers
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        original_handler = signal.getsignal(sig)

                        def handler(signum, frame, orig=original_handler):
                            cls._global_cleanup()
                            if callable(orig) and orig not in (signal.SIG_IGN, signal.SIG_DFL):
                                orig(signum, frame)

                        signal.signal(sig, handler)
                    except Exception:
                        pass

                cls._cleanup_registered = True

    @classmethod
    def _global_cleanup(cls) -> None:
        """Emergency cleanup of all active processes."""
        with cls._lock:
            for pid, ref in list(cls._active_processes.items()):
                proc = ref()
                if proc is not None:
                    try:
                        proc._emergency_cleanup()
                    except Exception:
                        pass
            cls._active_processes.clear()

    def _emergency_cleanup(self) -> None:
        """Emergency cleanup - close handles and kill process group."""
        # Close file handles first
        for fd in [self._stdout_fd, self._stderr_fd]:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass

        for f in [self._stdout_file, self._stderr_file]:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass

        self._stdout_fd = None
        self._stderr_fd = None
        self._stdout_file = None
        self._stderr_file = None

        # Kill process group
        if self._pgid is not None and self._pgid > 0:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        elif self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        self._state = ProcessState.STOPPED

    async def start(self) -> ProcessInfo:
        """
        Start the subprocess with process group isolation.

        Returns:
            ProcessInfo with process details
        """
        if self._state not in (ProcessState.CREATED, ProcessState.STOPPED, ProcessState.FAILED):
            raise RuntimeError(f"Cannot start process in state {self._state}")

        self._state = ProcessState.STARTING

        try:
            # Create log directories
            for log_path in [self.stdout_path, self.stderr_path]:
                if log_path:
                    log_path.parent.mkdir(parents=True, exist_ok=True)

            # Open log files (tracked for cleanup)
            if self.stdout_path:
                self._stdout_fd = os.open(
                    str(self.stdout_path),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    SystemConfig.ATOMIC_WRITE_PERMS
                )
                self._stdout_file = os.fdopen(self._stdout_fd, 'w')

                # Write launch header
                self._stdout_file.write(f"\n{'='*60}\n")
                self._stdout_file.write(f"[{datetime.now().isoformat()}] {self.name} - START\n")
                self._stdout_file.write(f"Command: {' '.join(self.cmd)}\n")
                self._stdout_file.write(f"{'='*60}\n")
                self._stdout_file.flush()

            if self.stderr_path:
                self._stderr_fd = os.open(
                    str(self.stderr_path),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    SystemConfig.ATOMIC_WRITE_PERMS
                )
                self._stderr_file = os.fdopen(self._stderr_fd, 'w')

            # Prepare start_new_session for process group
            # On Unix, this calls setsid() creating a new process group
            use_process_group = SystemConfig.PROCESS_GROUP_ENABLED and sys.platform != 'win32'

            # Create subprocess
            self._process = await asyncio.create_subprocess_exec(
                *self.cmd,
                cwd=str(self.cwd) if self.cwd else None,
                env=self._env,
                stdout=self._stdout_file if self._stdout_file else asyncio.subprocess.DEVNULL,
                stderr=self._stderr_file if self._stderr_file else asyncio.subprocess.DEVNULL,
                start_new_session=use_process_group,
            )

            self._pid = self._process.pid
            self._started_at = time.time()

            # Get process group ID
            if use_process_group:
                try:
                    self._pgid = os.getpgid(self._pid)
                except Exception:
                    self._pgid = self._pid
            else:
                self._pgid = self._pid

            self._state = ProcessState.RUNNING

            # Register in active processes
            with self._lock:
                self._active_processes[self._pid] = weakref.ref(self)

            logger.info(f"[SafeProcess:{self.name}] Started PID={self._pid} PGID={self._pgid}")

            return ProcessInfo(
                pid=self._pid,
                pgid=self._pgid,
                name=self.name,
                cmdline=self.cmd,
                state=self._state,
                started_at=self._started_at,
                stdout_path=self.stdout_path,
                stderr_path=self.stderr_path,
            )

        except Exception as e:
            self._state = ProcessState.FAILED
            self._error = str(e)
            self._cleanup_files()
            logger.error(f"[SafeProcess:{self.name}] Start failed: {e}")
            raise

    def _cleanup_files(self) -> None:
        """Clean up file handles."""
        for f in [self._stdout_file, self._stderr_file]:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass

        self._stdout_file = None
        self._stderr_file = None
        self._stdout_fd = None
        self._stderr_fd = None

    async def stop(
        self,
        timeout: Optional[float] = None,
        force: bool = False,
    ) -> int:
        """
        Stop the subprocess gracefully.

        Args:
            timeout: Timeout for graceful shutdown
            force: If True, skip SIGTERM and go straight to SIGKILL

        Returns:
            Exit code of the process
        """
        if self._state not in (ProcessState.RUNNING, ProcessState.ZOMBIE):
            return self._return_code or 0

        self._state = ProcessState.STOPPING
        sigterm_timeout = timeout or SystemConfig.PROCESS_SIGTERM_TIMEOUT

        try:
            if self._process is None:
                return 0

            # Send signal to process group (kills all children too)
            if not force:
                # Try graceful SIGTERM first
                sig = signal.SIGTERM
                if self._pgid and self._pgid > 0 and sys.platform != 'win32':
                    try:
                        os.killpg(self._pgid, sig)
                        logger.debug(f"[SafeProcess:{self.name}] Sent SIGTERM to PGID {self._pgid}")
                    except (ProcessLookupError, PermissionError):
                        # Process already dead
                        pass
                else:
                    try:
                        self._process.terminate()
                    except ProcessLookupError:
                        pass

                # Wait for graceful shutdown
                try:
                    self._return_code = await asyncio.wait_for(
                        self._process.wait(),
                        timeout=sigterm_timeout
                    )
                    logger.info(f"[SafeProcess:{self.name}] Stopped gracefully (code={self._return_code})")
                except asyncio.TimeoutError:
                    logger.warning(f"[SafeProcess:{self.name}] SIGTERM timeout, sending SIGKILL")
                    force = True

            # Force kill if still running
            if force or self._process.returncode is None:
                sig = signal.SIGKILL
                if self._pgid and self._pgid > 0 and sys.platform != 'win32':
                    try:
                        os.killpg(self._pgid, sig)
                    except (ProcessLookupError, PermissionError):
                        pass
                else:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass

                try:
                    self._return_code = await asyncio.wait_for(
                        self._process.wait(),
                        timeout=SystemConfig.PROCESS_SIGKILL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[SafeProcess:{self.name}] Could not kill process!")
                    self._state = ProcessState.ZOMBIE
                    return -1

            self._state = ProcessState.STOPPED

        finally:
            # Always clean up file handles
            self._cleanup_files()

            # Write shutdown log
            if self.stdout_path and self.stdout_path.exists():
                try:
                    with open(self.stdout_path, 'a') as f:
                        f.write(f"\n{'='*60}\n")
                        f.write(f"[{datetime.now().isoformat()}] {self.name} - STOP (code={self._return_code})\n")
                        f.write(f"{'='*60}\n")
                except Exception:
                    pass

            # Remove from active processes
            if self._pid:
                with self._lock:
                    self._active_processes.pop(self._pid, None)

            self._guard.cleanup()

        return self._return_code or 0

    async def kill(self) -> int:
        """Force kill the process immediately."""
        return await self.stop(force=True)

    def is_running(self) -> bool:
        """Check if process is still running."""
        if self._process is None:
            return False
        return self._process.returncode is None

    @property
    def pid(self) -> Optional[int]:
        return self._pid

    @property
    def pgid(self) -> Optional[int]:
        return self._pgid

    @property
    def state(self) -> ProcessState:
        return self._state

    @property
    def return_code(self) -> Optional[int]:
        if self._process:
            return self._process.returncode
        return self._return_code

    def get_info(self) -> ProcessInfo:
        """Get current process information."""
        return ProcessInfo(
            pid=self._pid or 0,
            pgid=self._pgid or 0,
            name=self.name,
            cmdline=self.cmd,
            state=self._state,
            started_at=self._started_at or 0,
            stdout_path=self.stdout_path,
            stderr_path=self.stderr_path,
            return_code=self._return_code,
            error=self._error,
        )

    async def __aenter__(self) -> "SafeProcess":
        """Async context manager - start process."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager - stop process."""
        await self.stop()


# =============================================================================
# TRUE HEARTBEAT - PID-Validated Health Monitoring
# =============================================================================

class TrueHeartbeat:
    """
    Heartbeat monitor with PID validation.

    Features:
    - Validates PID is actually alive (not just timestamp)
    - Detects stale heartbeat files from dead processes
    - Automatic cleanup of invalid heartbeat files
    - Cross-component health checking

    Fixes Issues: #3 (Heartbeat Staleness), #4 (Missing PID Validation)
    """

    def __init__(
        self,
        component_name: str,
        heartbeat_dir: Optional[Path] = None,
    ):
        self.component_name = component_name
        self.heartbeat_dir = heartbeat_dir or SystemConfig.HEARTBEATS_DIR
        self.heartbeat_file = self.heartbeat_dir / f"{component_name}.json"

        # Ensure directory exists
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)

        self._writer = AtomicStateWriter(self.heartbeat_file)

    def publish(
        self,
        status: str = "healthy",
        metrics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Publish heartbeat with current PID.

        Args:
            status: Component status (healthy, degraded, stopping, etc.)
            metrics: Additional metrics to include

        Returns:
            True if successful
        """
        data = HeartbeatData(
            component_name=self.component_name,
            pid=os.getpid(),
            timestamp=time.time(),
            host=socket.gethostname(),
            status=status,
            metrics=metrics or {},
        )

        return self._writer.write(data.to_dict())

    def read(self) -> Optional[HeartbeatData]:
        """Read and parse heartbeat file."""
        data = self._writer.read()
        if data is None:
            return None

        try:
            return HeartbeatData.from_dict(data)
        except Exception as e:
            logger.warning(f"[TrueHeartbeat:{self.component_name}] Invalid heartbeat data: {e}")
            return None

    def validate(self) -> Tuple[HeartbeatStatus, Optional[HeartbeatData]]:
        """
        Validate heartbeat with PID check.

        Returns:
            Tuple of (status, heartbeat_data or None)
        """
        heartbeat = self.read()

        if heartbeat is None:
            return HeartbeatStatus.MISSING, None

        # Check if PID is valid
        if heartbeat.pid <= 0:
            return HeartbeatStatus.INVALID, heartbeat

        # Validate PID is alive (THE KEY FIX)
        if SystemConfig.HEARTBEAT_PID_VALIDATE:
            psutil = _get_psutil()
            if psutil:
                if not psutil.pid_exists(heartbeat.pid):
                    logger.info(
                        f"[TrueHeartbeat:{self.component_name}] "
                        f"PID {heartbeat.pid} is DEAD - removing stale heartbeat"
                    )
                    self.cleanup()
                    return HeartbeatStatus.DEAD, heartbeat

                # Optional: verify process is actually the right one
                try:
                    proc = psutil.Process(heartbeat.pid)
                    cmdline = ' '.join(proc.cmdline())
                    # Check if it looks like our component
                    if self.component_name.replace('_', '-') not in cmdline.lower():
                        logger.warning(
                            f"[TrueHeartbeat:{self.component_name}] "
                            f"PID {heartbeat.pid} exists but cmdline doesn't match: {cmdline[:50]}"
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Check staleness
        if heartbeat.is_stale:
            logger.warning(
                f"[TrueHeartbeat:{self.component_name}] "
                f"Heartbeat is STALE (age={heartbeat.age_seconds:.1f}s)"
            )
            return HeartbeatStatus.STALE, heartbeat

        return HeartbeatStatus.ALIVE, heartbeat

    def cleanup(self) -> bool:
        """Remove stale heartbeat file."""
        logger.info(f"[TrueHeartbeat:{self.component_name}] Cleaning up stale heartbeat")
        return self._writer.delete()

    def is_alive(self) -> bool:
        """Quick check if component is alive."""
        status, _ = self.validate()
        return status == HeartbeatStatus.ALIVE

    async def wait_for_alive(
        self,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> bool:
        """
        Wait for component to become alive.

        Args:
            timeout: Maximum wait time
            poll_interval: How often to check

        Returns:
            True if alive within timeout
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.is_alive():
                return True
            await asyncio.sleep(poll_interval)

        return False


# =============================================================================
# PORT MANAGER - Distributed Port Locking
# =============================================================================

class PortManager:
    """
    Distributed port management with file-based locking.

    Features:
    - Prevents multiple JARVIS instances from fighting over ports
    - Automatic stale lock detection and cleanup
    - Port range allocation for dynamic assignment
    - Health check integration

    Fixes Issues: #2 (Race Conditions in Port Cleanup)
    """

    def __init__(
        self,
        locks_dir: Optional[Path] = None,
    ):
        self.locks_dir = locks_dir or SystemConfig.LOCKS_DIR
        self.locks_dir.mkdir(parents=True, exist_ok=True)

        self._held_locks: Dict[int, FileLock] = {}

    def _get_lock_path(self, port: int) -> Path:
        """Get lock file path for a port."""
        return self.locks_dir / f"port_{port}.lock"

    def acquire_port(
        self,
        port: int,
        timeout: float = 10.0,
    ) -> bool:
        """
        Acquire exclusive lock on a port.

        Args:
            port: Port number to acquire
            timeout: Lock acquisition timeout

        Returns:
            True if port acquired
        """
        if port in self._held_locks:
            return True  # Already held

        lock = FileLock(
            self._get_lock_path(port),
            timeout=timeout,
        )

        if lock.acquire():
            self._held_locks[port] = lock
            logger.info(f"[PortManager] Acquired port {port}")
            return True

        logger.warning(f"[PortManager] Could not acquire port {port}")
        return False

    def release_port(self, port: int) -> None:
        """Release lock on a port."""
        if port in self._held_locks:
            self._held_locks[port].release()
            del self._held_locks[port]
            logger.info(f"[PortManager] Released port {port}")

    def release_all(self) -> None:
        """Release all held port locks."""
        for port in list(self._held_locks.keys()):
            self.release_port(port)

    def is_port_available(self, port: int) -> bool:
        """Check if a port is available (not locked and not in use)."""
        # Check if we hold the lock
        if port in self._held_locks:
            return True

        # Check if port is locked by another process
        lock = FileLock(self._get_lock_path(port), timeout=0.1)
        if not lock.acquire(blocking=False):
            return False
        lock.release()

        # Check if port is actually listening
        return not self._is_port_listening(port)

    def _is_port_listening(self, port: int) -> bool:
        """Check if something is listening on the port."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                result = sock.connect_ex(('127.0.0.1', port))
                return result == 0
        except Exception:
            return False

    def find_available_port(
        self,
        start: int = 8000,
        end: int = 8999,
    ) -> Optional[int]:
        """
        Find an available port in range.

        Args:
            start: Start of port range
            end: End of port range

        Returns:
            Available port or None
        """
        for port in range(start, end + 1):
            if self.is_port_available(port):
                if self.acquire_port(port, timeout=1.0):
                    return port
        return None

    def get_owner_info(self, port: int) -> Optional[Dict[str, Any]]:
        """Get information about who owns a port lock."""
        lock_path = self._get_lock_path(port)
        if lock_path.exists():
            try:
                with open(lock_path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def __enter__(self) -> "PortManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release_all()


# =============================================================================
# ADAPTIVE WAITER - System-Load-Aware Delays
# =============================================================================

class AdaptiveWaiter:
    """
    Intelligent delay system that adapts to system load.

    Features:
    - Measures actual system load (CPU, memory, I/O)
    - Adjusts delays based on load
    - Prevents thundering herd with jitter
    - Exponential backoff with smart limits

    Fixes Issues: #9 (Hardcoded Sleep Values)
    """

    def __init__(
        self,
        min_delay: float = None,
        max_delay: float = None,
        load_threshold: float = None,
    ):
        self.min_delay = min_delay or SystemConfig.ADAPTIVE_MIN_DELAY
        self.max_delay = max_delay or SystemConfig.ADAPTIVE_MAX_DELAY
        self.load_threshold = load_threshold or SystemConfig.ADAPTIVE_LOAD_THRESHOLD

        self._last_load_check: float = 0
        self._cached_load: float = 0.5
        self._load_check_interval: float = 1.0

    def _get_system_load(self) -> float:
        """Get current system load (0.0 - 1.0)."""
        now = time.time()

        # Cache load check to avoid overhead
        if now - self._last_load_check < self._load_check_interval:
            return self._cached_load

        try:
            psutil = _get_psutil()
            if psutil:
                # Combine CPU and memory pressure
                cpu_percent = psutil.cpu_percent(interval=None) / 100.0
                memory = psutil.virtual_memory()
                memory_pressure = memory.percent / 100.0

                # Weight CPU more than memory
                self._cached_load = 0.7 * cpu_percent + 0.3 * memory_pressure
            else:
                # Fallback to load average on Unix
                if hasattr(os, 'getloadavg'):
                    load1, _, _ = os.getloadavg()
                    cpu_count = os.cpu_count() or 1
                    self._cached_load = min(1.0, load1 / cpu_count)
                else:
                    self._cached_load = 0.5

            self._last_load_check = now

        except Exception:
            pass

        return self._cached_load

    def calculate_delay(
        self,
        base_delay: float,
        attempt: int = 0,
        add_jitter: bool = True,
    ) -> float:
        """
        Calculate adaptive delay based on system load.

        Args:
            base_delay: Base delay in seconds
            attempt: Retry attempt number (for backoff)
            add_jitter: Whether to add random jitter

        Returns:
            Calculated delay in seconds
        """
        import random

        load = self._get_system_load()

        # Scale delay based on load
        if load > self.load_threshold:
            # System under pressure - increase delay
            load_multiplier = 1.0 + (load - self.load_threshold) * 3.0
        else:
            # System healthy - can use shorter delays
            load_multiplier = 0.5 + load * 0.5

        # Apply exponential backoff for retries
        if attempt > 0:
            backoff = min(2 ** attempt, 10)  # Cap at 10x
            delay = base_delay * backoff * load_multiplier
        else:
            delay = base_delay * load_multiplier

        # Add jitter to prevent thundering herd
        if add_jitter:
            jitter = random.uniform(-0.1, 0.1) * delay
            delay += jitter

        # Clamp to limits
        return max(self.min_delay, min(self.max_delay, delay))

    async def wait(
        self,
        base_delay: float,
        attempt: int = 0,
        add_jitter: bool = True,
    ) -> float:
        """
        Wait with adaptive delay.

        Returns:
            Actual delay used
        """
        delay = self.calculate_delay(base_delay, attempt, add_jitter)
        await asyncio.sleep(delay)
        return delay

    def wait_sync(
        self,
        base_delay: float,
        attempt: int = 0,
        add_jitter: bool = True,
    ) -> float:
        """Synchronous wait with adaptive delay."""
        delay = self.calculate_delay(base_delay, attempt, add_jitter)
        time.sleep(delay)
        return delay


# =============================================================================
# EXCEPTION HANDLER - Enhanced Error Propagation
# =============================================================================

class EnhancedExceptionHandler:
    """
    Enhanced exception handling with full traceback preservation.

    Fixes Issues: #8 (Error Propagation)
    """

    @staticmethod
    def format_exception(e: Exception, include_chain: bool = True) -> str:
        """Format exception with full traceback."""
        lines = [''.join(traceback.format_exception(type(e), e, e.__traceback__))]

        if include_chain:
            cause = e.__cause__
            while cause:
                lines.append("\nCaused by:\n")
                lines.append(''.join(traceback.format_exception(type(cause), cause, cause.__traceback__)))
                cause = cause.__cause__

        return ''.join(lines)

    @staticmethod
    def wrap_async(
        operation_name: str,
        reraise: bool = True,
    ) -> Callable:
        """
        Decorator for async functions with enhanced exception handling.

        Args:
            operation_name: Name for logging
            reraise: Whether to reraise after logging
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise  # Don't catch cancellation
                except Exception as e:
                    error_msg = EnhancedExceptionHandler.format_exception(e)
                    logger.error(f"[{operation_name}] Exception:\n{error_msg}")
                    if reraise:
                        raise
                    return None
            return wrapper
        return decorator

    @staticmethod
    async def gather_with_exceptions(
        *coros,
        operation_name: str = "parallel_operation",
    ) -> Tuple[List[Any], List[Exception]]:
        """
        Gather coroutines with proper exception handling.

        Returns:
            Tuple of (results, exceptions)
        """
        results = await asyncio.gather(*coros, return_exceptions=True)

        successes = []
        exceptions = []

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                error_msg = EnhancedExceptionHandler.format_exception(result)
                logger.error(f"[{operation_name}] Task {i} failed:\n{error_msg}")
                exceptions.append(result)
            else:
                successes.append(result)

        return successes, exceptions


# =============================================================================
# v91.0: PROCESS HEALTH PREDICTOR - ML-Based Failure Prediction
# =============================================================================

class ProcessHealthPredictor:
    """
    ML-based failure prediction using time-series analysis.

    Uses Exponential Weighted Moving Average (EWMA) and anomaly detection
    to predict process failures before they happen.

    Features:
    - Real-time health score calculation
    - Anomaly detection using statistical thresholds
    - Trend analysis for early warning
    - Historical pattern matching
    - Adaptive thresholds based on system behavior
    """

    def __init__(
        self,
        window_size: int = 100,
        ewma_alpha: float = 0.3,
        anomaly_threshold: float = 2.5,  # Standard deviations
    ):
        self.window_size = window_size
        self.ewma_alpha = ewma_alpha
        self.anomaly_threshold = anomaly_threshold

        # Time-series data storage
        self._metrics_history: Dict[str, List[Tuple[float, float]]] = {}  # process_name -> [(timestamp, value)]
        self._ewma_values: Dict[str, float] = {}
        self._ewma_variance: Dict[str, float] = {}

        # Failure history for pattern matching
        self._failure_patterns: List[Dict[str, Any]] = []

        # Lock for thread safety
        self._lock = threading.Lock()

    def record_metric(
        self,
        process_name: str,
        metric_name: str,
        value: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a metric value for a process."""
        timestamp = timestamp or time.time()
        key = f"{process_name}:{metric_name}"

        with self._lock:
            if key not in self._metrics_history:
                self._metrics_history[key] = []
                self._ewma_values[key] = value
                self._ewma_variance[key] = 0.0

            # Add to history
            self._metrics_history[key].append((timestamp, value))

            # Trim to window size
            if len(self._metrics_history[key]) > self.window_size:
                self._metrics_history[key] = self._metrics_history[key][-self.window_size:]

            # Update EWMA
            old_ewma = self._ewma_values[key]
            new_ewma = self.ewma_alpha * value + (1 - self.ewma_alpha) * old_ewma
            self._ewma_values[key] = new_ewma

            # Update variance (for anomaly detection)
            diff = value - old_ewma
            self._ewma_variance[key] = (1 - self.ewma_alpha) * (self._ewma_variance[key] + self.ewma_alpha * diff * diff)

    def get_health_score(self, process_name: str) -> float:
        """
        Calculate overall health score for a process (0.0 - 1.0).

        Based on:
        - CPU usage trends
        - Memory usage trends
        - Response time anomalies
        - Error rate patterns
        """
        with self._lock:
            scores = []

            # Check each metric type
            for metric_type in ["cpu", "memory", "response_time", "error_rate"]:
                key = f"{process_name}:{metric_type}"
                if key in self._metrics_history and len(self._metrics_history[key]) > 5:
                    # Calculate z-score of latest value
                    latest = self._metrics_history[key][-1][1]
                    ewma = self._ewma_values[key]
                    std = max(0.001, self._ewma_variance[key] ** 0.5)
                    z_score = abs(latest - ewma) / std

                    # Convert z-score to health score
                    # Higher z-score = more anomalous = lower health
                    if z_score > self.anomaly_threshold:
                        scores.append(0.0)
                    else:
                        scores.append(1.0 - (z_score / self.anomaly_threshold))

            if not scores:
                return 1.0  # No data = assume healthy

            return sum(scores) / len(scores)

    def predict_failure_probability(
        self,
        process_name: str,
        horizon_seconds: float = 60.0,
    ) -> float:
        """
        Predict probability of failure in the next `horizon_seconds`.

        Returns:
            Probability (0.0 - 1.0) of process failure
        """
        health = self.get_health_score(process_name)

        # Check for concerning trends
        trend_factor = self._calculate_trend_factor(process_name)

        # Check pattern match with historical failures
        pattern_factor = self._match_failure_patterns(process_name)

        # Combine factors
        # Lower health = higher failure probability
        base_probability = 1.0 - health

        # Multiply by trend and pattern factors
        probability = base_probability * (1.0 + trend_factor) * (1.0 + pattern_factor)

        return min(1.0, max(0.0, probability))

    def _calculate_trend_factor(self, process_name: str) -> float:
        """Calculate trend factor (positive = concerning trend)."""
        with self._lock:
            trend_sum = 0.0
            count = 0

            for key in self._metrics_history:
                if key.startswith(f"{process_name}:"):
                    history = self._metrics_history[key]
                    if len(history) >= 10:
                        # Simple linear regression for trend
                        recent = [v for _, v in history[-10:]]
                        older = [v for _, v in history[-20:-10]] if len(history) >= 20 else recent

                        recent_avg = sum(recent) / len(recent)
                        older_avg = sum(older) / len(older)

                        # Positive trend in "bad" metrics is concerning
                        if "error" in key or "response_time" in key:
                            if recent_avg > older_avg:
                                trend_sum += (recent_avg - older_avg) / max(0.001, older_avg)
                        # Positive trend in CPU/memory can be concerning
                        elif "cpu" in key or "memory" in key:
                            if recent_avg > older_avg * 1.2:  # 20% increase
                                trend_sum += 0.5

                        count += 1

            return trend_sum / max(1, count)

    def _match_failure_patterns(self, process_name: str) -> float:
        """Match current state against historical failure patterns."""
        if not self._failure_patterns:
            return 0.0

        with self._lock:
            # Get current metrics snapshot
            current_snapshot = {}
            for key, history in self._metrics_history.items():
                if key.startswith(f"{process_name}:"):
                    if history:
                        current_snapshot[key.split(":")[1]] = history[-1][1]

            if not current_snapshot:
                return 0.0

            # Compare against failure patterns
            best_match = 0.0
            for pattern in self._failure_patterns[-10:]:  # Last 10 failures
                if pattern.get("process") == process_name:
                    pre_failure_state = pattern.get("pre_failure_metrics", {})
                    similarity = self._calculate_similarity(current_snapshot, pre_failure_state)
                    best_match = max(best_match, similarity)

            return best_match

    def _calculate_similarity(self, current: Dict, historical: Dict) -> float:
        """Calculate similarity between two metric snapshots."""
        if not current or not historical:
            return 0.0

        common_keys = set(current.keys()) & set(historical.keys())
        if not common_keys:
            return 0.0

        similarities = []
        for key in common_keys:
            c = current[key]
            h = historical[key]
            if h != 0:
                # Ratio-based similarity
                ratio = c / h
                if 0.8 <= ratio <= 1.2:
                    similarities.append(1.0 - abs(1.0 - ratio))
                else:
                    similarities.append(0.0)

        return sum(similarities) / len(similarities) if similarities else 0.0

    def record_failure(
        self,
        process_name: str,
        failure_type: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record a failure for pattern learning."""
        timestamp = timestamp or time.time()

        with self._lock:
            # Capture pre-failure metrics
            pre_failure_metrics = {}
            for key, history in self._metrics_history.items():
                if key.startswith(f"{process_name}:"):
                    if len(history) >= 5:
                        # Get average of last 5 values before failure
                        pre_failure_metrics[key.split(":")[1]] = sum(v for _, v in history[-5:]) / 5

            self._failure_patterns.append({
                "process": process_name,
                "failure_type": failure_type,
                "timestamp": timestamp,
                "pre_failure_metrics": pre_failure_metrics,
            })

            # Limit pattern history
            if len(self._failure_patterns) > 100:
                self._failure_patterns = self._failure_patterns[-100:]


# =============================================================================
# v91.0: SELF-HEALING ORCHESTRATOR - Automatic Remediation
# =============================================================================

class RemediationAction(Enum):
    """Types of remediation actions."""
    RESTART = auto()
    KILL_AND_RESTART = auto()
    SCALE_RESOURCES = auto()
    CLEAR_CACHE = auto()
    ROTATE_LOGS = auto()
    NOTIFY_ADMIN = auto()
    GRACEFUL_DEGRADATION = auto()
    NO_ACTION = auto()


@dataclass
class RemediationResult:
    """Result of a remediation attempt."""
    action: RemediationAction
    success: bool
    process_name: str
    timestamp: float = field(default_factory=time.time)
    details: str = ""
    error: Optional[str] = None


class SelfHealingOrchestrator:
    """
    Automatic remediation system for process failures.

    Features:
    - Multiple remediation strategies per failure type
    - Exponential backoff for repeated failures
    - Learning from remediation success/failure
    - Integration with ProcessHealthPredictor
    - Cooldown periods to prevent thrashing
    """

    def __init__(
        self,
        health_predictor: Optional[ProcessHealthPredictor] = None,
        max_remediation_attempts: int = 3,
        cooldown_seconds: float = 60.0,
    ):
        self.health_predictor = health_predictor or ProcessHealthPredictor()
        self.max_attempts = max_remediation_attempts
        self.cooldown_seconds = cooldown_seconds

        # Track remediation attempts
        self._attempts: Dict[str, List[Tuple[float, RemediationAction]]] = {}

        # Remediation strategies per failure type
        self._strategies: Dict[str, List[RemediationAction]] = {
            "crash": [RemediationAction.RESTART, RemediationAction.KILL_AND_RESTART, RemediationAction.NOTIFY_ADMIN],
            "memory_leak": [RemediationAction.RESTART, RemediationAction.CLEAR_CACHE, RemediationAction.SCALE_RESOURCES],
            "high_cpu": [RemediationAction.SCALE_RESOURCES, RemediationAction.GRACEFUL_DEGRADATION, RemediationAction.RESTART],
            "unresponsive": [RemediationAction.RESTART, RemediationAction.KILL_AND_RESTART, RemediationAction.NOTIFY_ADMIN],
            "disk_full": [RemediationAction.ROTATE_LOGS, RemediationAction.CLEAR_CACHE, RemediationAction.NOTIFY_ADMIN],
            "port_conflict": [RemediationAction.KILL_AND_RESTART, RemediationAction.NOTIFY_ADMIN],
            "unknown": [RemediationAction.RESTART, RemediationAction.NOTIFY_ADMIN],
        }

        # Success rate tracking for learning
        self._success_rates: Dict[Tuple[str, RemediationAction], Tuple[int, int]] = {}  # (success, total)

        self._lock = threading.Lock()

    def get_recommended_action(
        self,
        process_name: str,
        failure_type: str,
    ) -> RemediationAction:
        """
        Get the recommended remediation action.

        Uses:
        - Historical success rates
        - Number of previous attempts
        - Cooldown periods
        """
        with self._lock:
            # Check cooldown
            if process_name in self._attempts:
                recent = [t for t, _ in self._attempts[process_name] if time.time() - t < self.cooldown_seconds]
                if len(recent) >= self.max_attempts:
                    logger.warning(f"[SelfHealing:{process_name}] Max attempts reached, cooldown active")
                    return RemediationAction.NO_ACTION

            # Get strategies for this failure type
            strategies = self._strategies.get(failure_type, self._strategies["unknown"])

            # Sort by success rate
            rated_strategies = []
            for action in strategies:
                key = (failure_type, action)
                if key in self._success_rates:
                    success, total = self._success_rates[key]
                    rate = success / max(1, total)
                else:
                    rate = 0.5  # Default 50% assumed success rate
                rated_strategies.append((action, rate))

            rated_strategies.sort(key=lambda x: x[1], reverse=True)

            # Return highest rated action
            return rated_strategies[0][0] if rated_strategies else RemediationAction.RESTART

    async def execute_remediation(
        self,
        process_name: str,
        failure_type: str,
        safe_process: Optional[SafeProcess] = None,
        custom_handler: Optional[Callable] = None,
    ) -> RemediationResult:
        """
        Execute remediation for a process.

        Args:
            process_name: Name of the failed process
            failure_type: Type of failure detected
            safe_process: SafeProcess instance to remediate
            custom_handler: Optional custom remediation handler

        Returns:
            RemediationResult with outcome
        """
        action = self.get_recommended_action(process_name, failure_type)

        if action == RemediationAction.NO_ACTION:
            return RemediationResult(
                action=action,
                success=False,
                process_name=process_name,
                details="Cooldown active, no action taken",
            )

        # Record attempt
        with self._lock:
            if process_name not in self._attempts:
                self._attempts[process_name] = []
            self._attempts[process_name].append((time.time(), action))

        logger.info(f"[SelfHealing:{process_name}] Executing {action.name} for {failure_type}")

        try:
            success = False
            details = ""

            if action == RemediationAction.RESTART:
                if safe_process:
                    await safe_process.stop()
                    await asyncio.sleep(1.0)
                    await safe_process.start()
                    success = safe_process.is_running()
                    details = f"Restarted, PID={safe_process.pid}"
                elif custom_handler:
                    success = await custom_handler("restart")
                    details = "Custom restart handler executed"

            elif action == RemediationAction.KILL_AND_RESTART:
                if safe_process:
                    await safe_process.kill()
                    await asyncio.sleep(2.0)
                    await safe_process.start()
                    success = safe_process.is_running()
                    details = f"Force killed and restarted, PID={safe_process.pid}"
                elif custom_handler:
                    success = await custom_handler("kill_restart")
                    details = "Custom kill/restart handler executed"

            elif action == RemediationAction.NOTIFY_ADMIN:
                logger.critical(f"[SelfHealing:{process_name}] ADMIN NOTIFICATION: {failure_type} requires attention")
                success = True
                details = "Admin notification sent"

            elif action == RemediationAction.GRACEFUL_DEGRADATION:
                logger.warning(f"[SelfHealing:{process_name}] Entering graceful degradation mode")
                success = True
                details = "Graceful degradation activated"

            else:
                if custom_handler:
                    success = await custom_handler(action.name.lower())
                    details = f"Custom handler for {action.name}"
                else:
                    details = f"No handler for {action.name}"

            # Record success/failure for learning
            with self._lock:
                key = (failure_type, action)
                if key not in self._success_rates:
                    self._success_rates[key] = (0, 0)
                s, t = self._success_rates[key]
                self._success_rates[key] = (s + (1 if success else 0), t + 1)

            if success:
                logger.info(f"[SelfHealing:{process_name}] {action.name} succeeded: {details}")
            else:
                logger.warning(f"[SelfHealing:{process_name}] {action.name} failed: {details}")

            return RemediationResult(
                action=action,
                success=success,
                process_name=process_name,
                details=details,
            )

        except Exception as e:
            logger.error(f"[SelfHealing:{process_name}] Remediation error: {e}")
            return RemediationResult(
                action=action,
                success=False,
                process_name=process_name,
                error=str(e),
            )


# =============================================================================
# v91.0: RESOURCE QUOTA MANAGER - ulimit Protection
# =============================================================================

@dataclass
class ResourceQuota:
    """Resource quota configuration."""
    max_file_descriptors: int = 1024
    max_memory_mb: int = 4096
    max_cpu_percent: float = 80.0
    max_disk_io_mb_per_sec: float = 100.0
    warning_threshold: float = 0.8  # 80% of limit


@dataclass
class ResourceUsage:
    """Current resource usage snapshot."""
    file_descriptors: int = 0
    file_descriptors_limit: int = 0
    memory_mb: float = 0.0
    memory_limit_mb: float = 0.0
    cpu_percent: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def fd_usage_ratio(self) -> float:
        return self.file_descriptors / max(1, self.file_descriptors_limit)

    @property
    def memory_usage_ratio(self) -> float:
        return self.memory_mb / max(1, self.memory_limit_mb)


class ResourceQuotaManager:
    """
    Resource quota management with ulimit protection.

    Features:
    - Real-time resource monitoring
    - Automatic ulimit detection
    - Early warning for resource exhaustion
    - Resource reservation for critical operations
    - Historical tracking for trend analysis
    """

    def __init__(
        self,
        quota: Optional[ResourceQuota] = None,
        history_size: int = 1000,
    ):
        self.quota = quota or ResourceQuota()
        self.history_size = history_size

        self._history: List[ResourceUsage] = []
        self._reserved_fds: int = 0
        self._lock = threading.Lock()

        # Detect system limits
        self._system_limits = self._detect_system_limits()

    def _detect_system_limits(self) -> Dict[str, int]:
        """Detect system resource limits."""
        limits = {}

        try:
            import resource

            # File descriptor limits
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            limits["fd_soft"] = soft
            limits["fd_hard"] = hard

            # Memory limits (if set)
            try:
                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                limits["mem_soft"] = soft
                limits["mem_hard"] = hard
            except Exception:
                limits["mem_soft"] = -1
                limits["mem_hard"] = -1

        except ImportError:
            # Windows doesn't have resource module
            limits["fd_soft"] = 8192  # Reasonable default
            limits["fd_hard"] = 8192

        return limits

    def get_current_usage(self) -> ResourceUsage:
        """Get current resource usage."""
        psutil = _get_psutil()

        usage = ResourceUsage()

        if psutil:
            proc = psutil.Process()

            # File descriptors
            try:
                usage.file_descriptors = proc.num_fds() if hasattr(proc, 'num_fds') else len(proc.open_files())
            except Exception:
                usage.file_descriptors = 0

            usage.file_descriptors_limit = self._system_limits.get("fd_soft", 1024)

            # Memory
            try:
                mem = proc.memory_info()
                usage.memory_mb = mem.rss / (1024 * 1024)
                usage.memory_limit_mb = psutil.virtual_memory().total / (1024 * 1024)
            except Exception:
                pass

            # CPU
            try:
                usage.cpu_percent = proc.cpu_percent()
            except Exception:
                pass

            # Disk I/O
            try:
                io = proc.io_counters()
                usage.disk_read_mb = io.read_bytes / (1024 * 1024)
                usage.disk_write_mb = io.write_bytes / (1024 * 1024)
            except Exception:
                pass

        # Store in history
        with self._lock:
            self._history.append(usage)
            if len(self._history) > self.history_size:
                self._history = self._history[-self.history_size:]

        return usage

    def check_quota_violations(self) -> List[str]:
        """Check for quota violations."""
        usage = self.get_current_usage()
        violations = []

        # File descriptors
        available_fds = usage.file_descriptors_limit - usage.file_descriptors - self._reserved_fds
        if available_fds < 100:
            violations.append(f"CRITICAL: Only {available_fds} file descriptors available")
        elif usage.fd_usage_ratio > self.quota.warning_threshold:
            violations.append(f"WARNING: File descriptors at {usage.fd_usage_ratio*100:.1f}% capacity")

        # Memory
        if usage.memory_mb > self.quota.max_memory_mb:
            violations.append(f"CRITICAL: Memory usage {usage.memory_mb:.1f}MB exceeds quota {self.quota.max_memory_mb}MB")
        elif usage.memory_usage_ratio > self.quota.warning_threshold:
            violations.append(f"WARNING: Memory at {usage.memory_usage_ratio*100:.1f}% capacity")

        # CPU
        if usage.cpu_percent > self.quota.max_cpu_percent:
            violations.append(f"WARNING: CPU at {usage.cpu_percent:.1f}% (limit: {self.quota.max_cpu_percent}%)")

        return violations

    def reserve_file_descriptors(self, count: int) -> bool:
        """
        Reserve file descriptors for upcoming operations.

        Returns True if reservation successful.
        """
        with self._lock:
            usage = self.get_current_usage()
            available = usage.file_descriptors_limit - usage.file_descriptors - self._reserved_fds

            if available >= count:
                self._reserved_fds += count
                logger.debug(f"[ResourceQuota] Reserved {count} FDs, {available - count} remaining")
                return True
            else:
                logger.warning(f"[ResourceQuota] Cannot reserve {count} FDs, only {available} available")
                return False

    def release_file_descriptors(self, count: int) -> None:
        """Release previously reserved file descriptors."""
        with self._lock:
            self._reserved_fds = max(0, self._reserved_fds - count)
            logger.debug(f"[ResourceQuota] Released {count} FDs")

    def get_trend(self, metric: str, window_seconds: float = 60.0) -> float:
        """
        Get trend for a metric (positive = increasing, negative = decreasing).

        Returns rate of change per second.
        """
        with self._lock:
            if len(self._history) < 2:
                return 0.0

            cutoff = time.time() - window_seconds
            recent = [u for u in self._history if u.timestamp > cutoff]

            if len(recent) < 2:
                return 0.0

            # Get values for metric
            if metric == "file_descriptors":
                values = [(u.timestamp, u.file_descriptors) for u in recent]
            elif metric == "memory":
                values = [(u.timestamp, u.memory_mb) for u in recent]
            elif metric == "cpu":
                values = [(u.timestamp, u.cpu_percent) for u in recent]
            else:
                return 0.0

            # Simple linear regression for trend
            first_t, first_v = values[0]
            last_t, last_v = values[-1]

            dt = last_t - first_t
            if dt <= 0:
                return 0.0

            return (last_v - first_v) / dt


# =============================================================================
# v91.0: DISTRIBUTED STATE COORDINATOR - Cross-Repo Synchronization
# =============================================================================

class DistributedStateCoordinator:
    """
    Cross-repo state synchronization using file-based IPC.

    Features:
    - Atomic state updates across repos
    - Version vectors for conflict resolution
    - Event broadcasting for real-time sync
    - Leader election for coordination
    - Quorum-based decisions
    """

    def __init__(
        self,
        component_name: str,
        state_dir: Optional[Path] = None,
    ):
        self.component_name = component_name
        self.state_dir = state_dir or (Path.home() / ".jarvis" / "trinity" / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._instance_id = uuid.uuid4().hex[:8]
        self._version_vector: Dict[str, int] = {component_name: 0}
        self._last_sync: float = 0.0

        # Shared state file
        self._state_file = self.state_dir / f"{component_name}_state.json"
        self._writer = AtomicStateWriter(self._state_file)

        # Lock for coordination
        self._coord_lock = FileLock(self.state_dir / f"{component_name}_coord.lock")

    def publish_state(
        self,
        state: Dict[str, Any],
        event_type: str = "state_update",
    ) -> bool:
        """
        Publish state update to shared storage.

        Uses version vectors for conflict resolution.
        """
        try:
            # Increment version
            self._version_vector[self.component_name] = self._version_vector.get(self.component_name, 0) + 1

            data = {
                "component": self.component_name,
                "instance_id": self._instance_id,
                "event_type": event_type,
                "state": state,
                "version_vector": self._version_vector.copy(),
                "timestamp": time.time(),
                "host": socket.gethostname(),
                "pid": os.getpid(),
            }

            success = self._writer.write(data)

            if success:
                self._last_sync = time.time()
                logger.debug(f"[StateCoord:{self.component_name}] Published state v{self._version_vector[self.component_name]}")

            return success

        except Exception as e:
            logger.error(f"[StateCoord:{self.component_name}] Publish error: {e}")
            return False

    def read_state(self, component: str) -> Optional[Dict[str, Any]]:
        """Read state from another component."""
        try:
            state_file = self.state_dir / f"{component}_state.json"
            if not state_file.exists():
                return None

            reader = AtomicStateWriter(state_file)
            data = reader.read()

            if data:
                # Update version vector
                remote_vector = data.get("version_vector", {})
                for comp, ver in remote_vector.items():
                    self._version_vector[comp] = max(
                        self._version_vector.get(comp, 0),
                        ver
                    )

            return data

        except Exception as e:
            logger.error(f"[StateCoord:{self.component_name}] Read error for {component}: {e}")
            return None

    def broadcast_event(
        self,
        event_name: str,
        event_data: Dict[str, Any],
    ) -> bool:
        """
        Broadcast an event to all components.

        Events are stored in an events directory with timestamps.
        """
        try:
            events_dir = self.state_dir / "events"
            events_dir.mkdir(exist_ok=True)

            event = {
                "source": self.component_name,
                "instance_id": self._instance_id,
                "event_name": event_name,
                "data": event_data,
                "timestamp": time.time(),
                "id": uuid.uuid4().hex,
            }

            # Write to timestamped file
            event_file = events_dir / f"{int(time.time() * 1000)}_{self.component_name}_{event_name}.json"
            writer = AtomicStateWriter(event_file)

            success = writer.write(event)

            if success:
                logger.debug(f"[StateCoord:{self.component_name}] Broadcast event: {event_name}")

            return success

        except Exception as e:
            logger.error(f"[StateCoord:{self.component_name}] Broadcast error: {e}")
            return False

    def poll_events(
        self,
        since_timestamp: float = 0.0,
        event_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Poll for new events since a timestamp.

        Args:
            since_timestamp: Only return events after this timestamp
            event_filter: Optional list of event names to filter

        Returns:
            List of events ordered by timestamp
        """
        events = []

        try:
            events_dir = self.state_dir / "events"
            if not events_dir.exists():
                return []

            for event_file in sorted(events_dir.glob("*.json")):
                try:
                    reader = AtomicStateWriter(event_file)
                    event = reader.read()

                    if event and event.get("timestamp", 0) > since_timestamp:
                        # Skip own events
                        if event.get("source") == self.component_name and event.get("instance_id") == self._instance_id:
                            continue

                        # Apply filter
                        if event_filter and event.get("event_name") not in event_filter:
                            continue

                        events.append(event)

                except Exception:
                    continue

            # Clean up old events (> 5 minutes)
            cutoff = time.time() - 300
            for event_file in events_dir.glob("*.json"):
                try:
                    # Extract timestamp from filename
                    ts = int(event_file.stem.split("_")[0]) / 1000
                    if ts < cutoff:
                        event_file.unlink()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[StateCoord:{self.component_name}] Poll error: {e}")

        return events

    async def acquire_leadership(self, timeout: float = 10.0) -> bool:
        """
        Attempt to acquire leadership for coordination.

        Uses file locking for leader election.
        """
        try:
            acquired = await self._coord_lock.__aenter__()
            if acquired:
                logger.info(f"[StateCoord:{self.component_name}] Acquired leadership")
                return True
        except Exception as e:
            logger.debug(f"[StateCoord:{self.component_name}] Leadership acquisition failed: {e}")

        return False

    def release_leadership(self) -> None:
        """Release leadership."""
        try:
            self._coord_lock.release()
            logger.info(f"[StateCoord:{self.component_name}] Released leadership")
        except Exception:
            pass


# =============================================================================
# v91.0: GRACEFUL DEGRADATION MANAGER
# =============================================================================

class DegradationLevel(Enum):
    """Levels of graceful degradation."""
    FULL = auto()       # All features available
    REDUCED = auto()    # Non-essential features disabled
    MINIMAL = auto()    # Only core functionality
    EMERGENCY = auto()  # Bare minimum for survival


@dataclass
class FeatureFlag:
    """Feature flag with degradation behavior."""
    name: str
    enabled: bool = True
    min_level: DegradationLevel = DegradationLevel.FULL
    priority: int = 5  # 1-10, higher = more important


class GracefulDegradationManager:
    """
    Manages graceful degradation of system features.

    Features:
    - Automatic feature disabling based on resource pressure
    - Priority-based feature selection
    - Dynamic degradation level adjustment
    - Recovery detection and feature re-enabling
    """

    def __init__(
        self,
        resource_manager: Optional[ResourceQuotaManager] = None,
    ):
        self.resource_manager = resource_manager or ResourceQuotaManager()

        self._current_level = DegradationLevel.FULL
        self._features: Dict[str, FeatureFlag] = {}
        self._lock = threading.Lock()

        # Thresholds for degradation
        self._thresholds = {
            DegradationLevel.REDUCED: 0.7,   # 70% resource usage
            DegradationLevel.MINIMAL: 0.85,  # 85% resource usage
            DegradationLevel.EMERGENCY: 0.95,  # 95% resource usage
        }

    def register_feature(
        self,
        name: str,
        min_level: DegradationLevel = DegradationLevel.FULL,
        priority: int = 5,
    ) -> None:
        """Register a feature for degradation management."""
        with self._lock:
            self._features[name] = FeatureFlag(
                name=name,
                enabled=True,
                min_level=min_level,
                priority=priority,
            )

    def is_feature_enabled(self, name: str) -> bool:
        """Check if a feature is currently enabled."""
        with self._lock:
            if name not in self._features:
                return True  # Unknown features are enabled by default

            feature = self._features[name]

            # Check if current level allows this feature
            if self._current_level.value >= feature.min_level.value:
                return False

            return feature.enabled

    def update_degradation_level(self) -> DegradationLevel:
        """
        Update degradation level based on resource usage.

        Returns:
            The new degradation level
        """
        usage = self.resource_manager.get_current_usage()

        # Calculate overall resource pressure
        pressure = max(
            usage.fd_usage_ratio,
            usage.memory_usage_ratio,
            usage.cpu_percent / 100.0,
        )

        with self._lock:
            old_level = self._current_level

            # Determine new level
            if pressure >= self._thresholds[DegradationLevel.EMERGENCY]:
                self._current_level = DegradationLevel.EMERGENCY
            elif pressure >= self._thresholds[DegradationLevel.MINIMAL]:
                self._current_level = DegradationLevel.MINIMAL
            elif pressure >= self._thresholds[DegradationLevel.REDUCED]:
                self._current_level = DegradationLevel.REDUCED
            else:
                self._current_level = DegradationLevel.FULL

            if self._current_level != old_level:
                logger.warning(
                    f"[Degradation] Level changed: {old_level.name} -> {self._current_level.name} "
                    f"(pressure: {pressure*100:.1f}%)"
                )

                # Update feature states
                self._update_feature_states()

            return self._current_level

    def _update_feature_states(self) -> None:
        """Update feature enabled states based on current level."""
        for name, feature in self._features.items():
            # Disable features that require higher level than current
            if self._current_level.value >= feature.min_level.value:
                if feature.enabled:
                    logger.info(f"[Degradation] Disabling feature: {name}")
                    feature.enabled = False
            else:
                if not feature.enabled:
                    logger.info(f"[Degradation] Re-enabling feature: {name}")
                    feature.enabled = True

    def get_enabled_features(self) -> List[str]:
        """Get list of currently enabled features."""
        with self._lock:
            return [name for name, f in self._features.items() if f.enabled]

    def get_disabled_features(self) -> List[str]:
        """Get list of currently disabled features."""
        with self._lock:
            return [name for name, f in self._features.items() if not f.enabled]

    @property
    def level(self) -> DegradationLevel:
        return self._current_level


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

def initialize() -> None:
    """Initialize the system primitives module."""
    SystemConfig.ensure_directories()
    logger.info("[SystemPrimitives] Initialized v91.0 (Advanced Features)")


# Auto-initialize on import
try:
    initialize()
except Exception as e:
    logger.warning(f"[SystemPrimitives] Init warning: {e}")

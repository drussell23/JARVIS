"""
Safe File Descriptor Management v109.3

Enterprise-grade file descriptor handling that prevents EXC_GUARD crashes on macOS.

Problem:
    macOS guards certain file descriptors (libdispatch/GCD, HTTP clients, system frameworks).
    When code tries to os.close() a guarded FD, macOS terminates the process with EXC_GUARD.
    This is a Mach exception - NOT catchable by Python's try/except.

Solution:
    - Track which FDs we explicitly opened (safe to close)
    - Validate FDs before closing using fcntl.F_GETFD
    - Detect guarded FDs by their failure patterns
    - Provide safe_close() that never crashes
    - Thread-safe, async-compatible, zero hardcoding

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                   Safe FD Management Layer                      │
    │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐        │
    │  │   FD Tracker  │  │  FD Validator │  │  Safe Closer  │        │
    │  │ (owned FDs)   │  │ (fcntl check) │  │ (guard-aware) │        │
    │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘        │
    │          │                  │                  │                │
    │          └──────────────────┼──────────────────┘                │
    │                             │                                   │
    │                    ┌────────▼────────┐                          │
    │                    │   SafeFDManager │                          │
    │                    │  - open_safe()  │                          │
    │                    │  - close_safe() │                          │
    │                    │  - with_fd()    │                          │
    │                    └─────────────────┘                          │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from backend.core.safe_fd import safe_close, safe_open, SafeFDManager

    # Drop-in replacement for os.close()
    safe_close(fd)  # Never crashes, even on guarded FDs

    # Safe file operations
    fd = safe_open('/path/to/file', os.O_RDONLY)
    try:
        # ... use fd ...
    finally:
        safe_close(fd)

    # Context manager for guaranteed cleanup
    async with SafeFDManager.open_safe('/path', os.O_RDONLY) as fd:
        os.read(fd, 1024)

Author: JARVIS Development Team
Version: 109.3.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import platform
import threading
import time
from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    ClassVar,
    Dict,
    Final,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Configuration (Environment-Driven, Zero Hardcoding)
# =============================================================================

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class SafeFDConfig:
    """Environment-driven configuration for safe FD management."""

    # Enable FD tracking (slight overhead but maximum safety)
    ENABLE_TRACKING: Final[bool] = _env_bool("SAFE_FD_ENABLE_TRACKING", True)

    # Log FD operations for debugging
    DEBUG_LOGGING: Final[bool] = _env_bool("SAFE_FD_DEBUG_LOGGING", False)

    # Maximum tracked FDs before cleanup
    MAX_TRACKED_FDS: Final[int] = _env_int("SAFE_FD_MAX_TRACKED", 10000)

    # Cleanup interval in seconds
    CLEANUP_INTERVAL: Final[int] = _env_int("SAFE_FD_CLEANUP_INTERVAL", 300)

    # Whether to attempt close on potentially guarded FDs
    STRICT_MODE: Final[bool] = _env_bool("SAFE_FD_STRICT_MODE", True)


# =============================================================================
# FD Status & Classification
# =============================================================================

class FDStatus(Enum):
    """Status of a file descriptor."""
    VALID = auto()           # FD is valid and safe to close
    INVALID = auto()         # FD is not open (already closed or never opened)
    GUARDED = auto()         # FD is guarded by macOS (DO NOT CLOSE)
    UNKNOWN = auto()         # Status cannot be determined
    OWNED = auto()           # FD was opened by us (safe to close)
    FOREIGN = auto()         # FD was not opened by us (potentially guarded)


@dataclass(frozen=True)
class FDInfo:
    """Information about a file descriptor."""
    fd: int
    status: FDStatus
    path: Optional[str] = None
    opened_at: Optional[float] = None
    opened_by: Optional[str] = None  # Stack trace of opener
    flags: Optional[int] = None
    error_code: Optional[int] = None
    error_msg: Optional[str] = None


# =============================================================================
# Platform Detection
# =============================================================================

IS_MACOS: Final[bool] = platform.system() == "Darwin"
IS_LINUX: Final[bool] = platform.system() == "Linux"
MACOS_VERSION: Optional[Tuple[int, int, int]] = None

if IS_MACOS:
    try:
        version_str = platform.mac_ver()[0]
        if version_str:
            parts = version_str.split(".")
            MACOS_VERSION = tuple(int(p) for p in parts[:3])  # type: ignore
    except Exception:
        MACOS_VERSION = None


# =============================================================================
# FD Tracker (Thread-Safe)
# =============================================================================

class _FDTracker:
    """
    Thread-safe tracker for file descriptors we've opened.

    This allows us to distinguish between:
    - FDs we opened (safe to close)
    - FDs from system/libraries (potentially guarded, dangerous to close)
    """

    _instance: ClassVar[Optional["_FDTracker"]] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls) -> "_FDTracker":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._lock = threading.RLock()
        self._owned_fds: Dict[int, FDInfo] = {}
        self._closed_fds: Set[int] = set()  # Track recently closed to prevent double-close
        self._last_cleanup = time.time()
        # v276.0 Phase 11: Monotonic cleanup interval
        try:
            from backend.core.monotonic_clock import monotonic_now as _fd_mono
            self._last_cleanup_mono = _fd_mono()
        except ImportError:
            self._last_cleanup_mono = time.time()
        self._initialized = True

        logger.debug("[SafeFD] FD Tracker initialized")

    def register(
        self,
        fd: int,
        path: Optional[str] = None,
        flags: Optional[int] = None,
    ) -> None:
        """Register an FD as owned by us."""
        if not SafeFDConfig.ENABLE_TRACKING:
            return

        with self._lock:
            # Cleanup if we have too many tracked FDs
            if len(self._owned_fds) >= SafeFDConfig.MAX_TRACKED_FDS:
                self._cleanup()

            info = FDInfo(
                fd=fd,
                status=FDStatus.OWNED,
                path=path,
                opened_at=time.time(),
                opened_by=self._get_caller_info() if SafeFDConfig.DEBUG_LOGGING else None,
                flags=flags,
            )
            self._owned_fds[fd] = info

            if SafeFDConfig.DEBUG_LOGGING:
                logger.debug(f"[SafeFD] Registered FD {fd} (path={path})")

    def unregister(self, fd: int) -> Optional[FDInfo]:
        """Unregister an FD after closing."""
        if not SafeFDConfig.ENABLE_TRACKING:
            return None

        with self._lock:
            info = self._owned_fds.pop(fd, None)
            self._closed_fds.add(fd)

            # Limit closed FDs set size
            if len(self._closed_fds) > 1000:
                self._closed_fds.clear()

            if SafeFDConfig.DEBUG_LOGGING and info:
                logger.debug(f"[SafeFD] Unregistered FD {fd}")

            return info

    def is_owned(self, fd: int) -> bool:
        """Check if an FD was opened by us."""
        if not SafeFDConfig.ENABLE_TRACKING:
            return True  # Assume owned if tracking disabled

        with self._lock:
            return fd in self._owned_fds

    def was_closed(self, fd: int) -> bool:
        """Check if an FD was recently closed by us."""
        with self._lock:
            return fd in self._closed_fds

    def get_info(self, fd: int) -> Optional[FDInfo]:
        """Get info about a tracked FD."""
        with self._lock:
            return self._owned_fds.get(fd)

    def get_stats(self) -> Dict[str, Any]:
        """Get tracking statistics."""
        with self._lock:
            return {
                "owned_fds": len(self._owned_fds),
                "closed_fds": len(self._closed_fds),
                "tracking_enabled": SafeFDConfig.ENABLE_TRACKING,
            }

    def _cleanup(self) -> None:
        """Clean up stale entries."""
        # v276.0 Phase 11: Prefer monotonic for cleanup interval
        try:
            from backend.core.monotonic_clock import monotonic_now as _fd_mono_c
            _now_mono = _fd_mono_c()
        except ImportError:
            _now_mono = time.time()
        if _now_mono - getattr(self, '_last_cleanup_mono', 0) < SafeFDConfig.CLEANUP_INTERVAL:
            return
        self._last_cleanup_mono = _now_mono
        now = time.time()  # kept for backward-compat
        self._last_cleanup = now
        stale_fds = []

        for fd in self._owned_fds:
            # Check if FD is still valid
            if not _is_fd_valid(fd):
                stale_fds.append(fd)

        for fd in stale_fds:
            self._owned_fds.pop(fd, None)

        if stale_fds:
            logger.debug(f"[SafeFD] Cleaned up {len(stale_fds)} stale FD entries")

    @staticmethod
    def _get_caller_info() -> str:
        """Get caller stack trace for debugging."""
        import traceback
        frames = traceback.extract_stack()
        # Skip internal frames
        relevant = [f for f in frames[:-3] if "safe_fd" not in f.filename]
        if relevant:
            f = relevant[-1]
            return f"{f.filename}:{f.lineno} in {f.name}"
        return "unknown"


# Global tracker instance
_fd_tracker = _FDTracker()


# =============================================================================
# FD Validation Functions
# =============================================================================

def _is_fd_valid(fd: int) -> bool:
    """
    Check if a file descriptor is valid (open and usable).

    Uses fcntl.F_GETFD which:
    - Returns file descriptor flags if FD is valid
    - Raises OSError with EBADF if FD is invalid/closed

    This is safe and won't trigger EXC_GUARD.
    """
    if fd < 0:
        return False

    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
        return True
    except OSError as e:
        if e.errno == errno.EBADF:
            return False  # FD is not valid
        # Other errors (shouldn't happen) - assume invalid
        return False


def _get_fd_flags(fd: int) -> Optional[int]:
    """Get file descriptor flags, or None if invalid."""
    if fd < 0:
        return None

    try:
        return fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError:
        return None


def _is_fd_guarded(fd: int) -> bool:
    """
    Heuristically detect if an FD might be guarded by macOS.

    macOS guards FDs for:
    - libdispatch/GCD internals
    - HTTP client connections (URLSession, etc.)
    - System framework internals
    - Mach ports exposed as FDs

    Detection strategy:
    1. If we opened it, it's not guarded
    2. If it fails F_GETFD in a specific way, it might be guarded
    3. High FD numbers (> 1000) that we didn't open are suspicious
    """
    if not IS_MACOS:
        return False

    # If we own it, it's safe
    if _fd_tracker.is_owned(fd):
        return False

    # If we already closed it, don't try again
    if _fd_tracker.was_closed(fd):
        return True  # Treat as guarded to prevent double-close

    # Check if FD is valid at all
    if not _is_fd_valid(fd):
        return False  # Not guarded, just invalid

    # Heuristic: High FD numbers we didn't open are suspicious
    # System FDs are typically < 10, our FDs < 1000
    # Guarded FDs from libdispatch often have high numbers
    if fd > 1000 and not _fd_tracker.is_owned(fd):
        if SafeFDConfig.DEBUG_LOGGING:
            logger.debug(f"[SafeFD] FD {fd} is high-numbered and unowned - treating as potentially guarded")
        return SafeFDConfig.STRICT_MODE

    return False


def get_fd_status(fd: int) -> FDInfo:
    """
    Get comprehensive status of a file descriptor.

    Returns FDInfo with:
    - status: VALID, INVALID, GUARDED, OWNED, or FOREIGN
    - Additional diagnostic information
    """
    if fd < 0:
        return FDInfo(
            fd=fd,
            status=FDStatus.INVALID,
            error_msg="Negative file descriptor",
        )

    # Check if we own it
    owned_info = _fd_tracker.get_info(fd)
    if owned_info:
        # Verify it's still valid
        if _is_fd_valid(fd):
            return FDInfo(
                fd=fd,
                status=FDStatus.OWNED,
                path=owned_info.path,
                opened_at=owned_info.opened_at,
                flags=_get_fd_flags(fd),
            )
        else:
            return FDInfo(
                fd=fd,
                status=FDStatus.INVALID,
                error_msg="FD was owned but is no longer valid",
            )

    # Check if we closed it
    if _fd_tracker.was_closed(fd):
        return FDInfo(
            fd=fd,
            status=FDStatus.INVALID,
            error_msg="FD was previously closed",
        )

    # Check basic validity
    if not _is_fd_valid(fd):
        return FDInfo(
            fd=fd,
            status=FDStatus.INVALID,
            error_msg="FD is not open",
        )

    # Check if potentially guarded
    if _is_fd_guarded(fd):
        return FDInfo(
            fd=fd,
            status=FDStatus.GUARDED,
            error_msg="FD appears to be guarded by macOS",
        )

    # Valid but not owned by us
    return FDInfo(
        fd=fd,
        status=FDStatus.FOREIGN,
        flags=_get_fd_flags(fd),
    )


# =============================================================================
# Safe Close Function (The Core Fix)
# =============================================================================

def safe_close(
    fd: int,
    suppress_errors: bool = True,
    force: bool = False,
) -> bool:
    """
    Safely close a file descriptor without risking EXC_GUARD crash.

    This is the drop-in replacement for os.close() that prevents crashes.

    Args:
        fd: File descriptor to close
        suppress_errors: If True, don't raise on errors (default: True)
        force: If True, attempt close even on potentially guarded FDs (dangerous!)

    Returns:
        True if FD was successfully closed, False otherwise

    Example:
        # Instead of:
        os.close(fd)  # Can crash with EXC_GUARD

        # Use:
        safe_close(fd)  # Never crashes
    """
    if fd < 0:
        if SafeFDConfig.DEBUG_LOGGING:
            logger.debug(f"[SafeFD] Ignoring negative FD {fd}")
        return False

    # Get status
    status = get_fd_status(fd)

    # Handle based on status
    if status.status == FDStatus.INVALID:
        if SafeFDConfig.DEBUG_LOGGING:
            logger.debug(f"[SafeFD] FD {fd} already closed or invalid")
        _fd_tracker.unregister(fd)
        return True  # Already closed is success

    if status.status == FDStatus.GUARDED and not force:
        logger.warning(
            f"[SafeFD] v109.3: REFUSING to close potentially guarded FD {fd} - "
            f"this prevents EXC_GUARD crash. Use force=True to override (dangerous!)."
        )
        return False

    if status.status == FDStatus.FOREIGN and SafeFDConfig.STRICT_MODE and not force:
        # In strict mode, refuse to close FDs we didn't open
        logger.warning(
            f"[SafeFD] v109.3: FD {fd} was not opened by us - skipping close in strict mode"
        )
        return False

    # Attempt the close
    try:
        os.close(fd)
        _fd_tracker.unregister(fd)

        if SafeFDConfig.DEBUG_LOGGING:
            logger.debug(f"[SafeFD] Successfully closed FD {fd}")

        return True

    except OSError as e:
        _fd_tracker.unregister(fd)

        if e.errno == errno.EBADF:
            # Already closed - this is fine
            if SafeFDConfig.DEBUG_LOGGING:
                logger.debug(f"[SafeFD] FD {fd} was already closed (EBADF)")
            return True

        # Log unexpected error
        logger.warning(f"[SafeFD] Unexpected error closing FD {fd}: {e}")

        if not suppress_errors:
            raise

        return False

    except Exception as e:
        # Catch-all for unexpected errors
        _fd_tracker.unregister(fd)
        logger.error(f"[SafeFD] Unexpected exception closing FD {fd}: {e}")

        if not suppress_errors:
            raise

        return False


# =============================================================================
# Safe Open Functions
# =============================================================================

def safe_open(
    path: Union[str, Path],
    flags: int,
    mode: int = 0o600,
) -> int:
    """
    Open a file and register the FD for safe closing.

    This is a drop-in replacement for os.open() that tracks the FD.

    Args:
        path: Path to open
        flags: os.O_* flags
        mode: File mode for creation (default: 0o600)

    Returns:
        File descriptor (registered for safe closing)

    Example:
        fd = safe_open('/path/to/file', os.O_RDONLY)
        try:
            data = os.read(fd, 1024)
        finally:
            safe_close(fd)
    """
    path_str = str(path)
    fd = os.open(path_str, flags, mode)
    _fd_tracker.register(fd, path=path_str, flags=flags)
    return fd


def safe_dup(fd: int) -> int:
    """
    Duplicate an FD and register the new one.

    Args:
        fd: File descriptor to duplicate

    Returns:
        New file descriptor (registered for safe closing)
    """
    new_fd = os.dup(fd)
    _fd_tracker.register(new_fd)
    return new_fd


def safe_dup2(fd: int, fd2: int) -> int:
    """
    Duplicate FD to a specific number.

    Args:
        fd: Source file descriptor
        fd2: Target file descriptor number

    Returns:
        The new file descriptor (fd2)
    """
    result = os.dup2(fd, fd2)
    _fd_tracker.register(fd2)
    return result


# =============================================================================
# Context Managers
# =============================================================================

@contextmanager
def safe_fd_context(
    path: Union[str, Path],
    flags: int,
    mode: int = 0o600,
) -> Generator[int, None, None]:
    """
    Context manager for safe FD handling.

    Usage:
        with safe_fd_context('/path/to/file', os.O_RDONLY) as fd:
            data = os.read(fd, 1024)
        # FD is safely closed here
    """
    fd = safe_open(path, flags, mode)
    try:
        yield fd
    finally:
        safe_close(fd)


@asynccontextmanager
async def async_safe_fd_context(
    path: Union[str, Path],
    flags: int,
    mode: int = 0o600,
) -> AsyncGenerator[int, None]:
    """
    Async context manager for safe FD handling.

    Usage:
        async with async_safe_fd_context('/path/to/file', os.O_RDONLY) as fd:
            data = os.read(fd, 1024)
        # FD is safely closed here
    """
    fd = safe_open(path, flags, mode)
    try:
        yield fd
    finally:
        safe_close(fd)


# =============================================================================
# SafeFDManager Class
# =============================================================================

class SafeFDManager:
    """
    Comprehensive safe file descriptor manager.

    Provides:
    - Safe open/close operations
    - FD lifecycle tracking
    - Statistics and debugging
    - Batch operations

    Usage:
        manager = SafeFDManager()

        # Open and track
        fd = manager.open('/path', os.O_RDONLY)

        # Safe close
        manager.close(fd)

        # Or use context manager
        async with manager.open_context('/path', os.O_RDONLY) as fd:
            data = os.read(fd, 1024)
    """

    def __init__(self):
        self._local_fds: Set[int] = set()
        self._lock = threading.Lock()

    def open(
        self,
        path: Union[str, Path],
        flags: int,
        mode: int = 0o600,
    ) -> int:
        """Open a file and track it."""
        fd = safe_open(path, flags, mode)
        with self._lock:
            self._local_fds.add(fd)
        return fd

    def close(self, fd: int, **kwargs) -> bool:
        """Safely close a file descriptor."""
        with self._lock:
            self._local_fds.discard(fd)
        return safe_close(fd, **kwargs)

    def close_all(self) -> int:
        """Close all FDs opened through this manager."""
        with self._lock:
            fds = list(self._local_fds)
            self._local_fds.clear()

        closed = 0
        for fd in fds:
            if safe_close(fd):
                closed += 1

        return closed

    @contextmanager
    def open_context(
        self,
        path: Union[str, Path],
        flags: int,
        mode: int = 0o600,
    ) -> Generator[int, None, None]:
        """Sync context manager for FD operations."""
        fd = self.open(path, flags, mode)
        try:
            yield fd
        finally:
            self.close(fd)

    @asynccontextmanager
    async def async_open_context(
        self,
        path: Union[str, Path],
        flags: int,
        mode: int = 0o600,
    ) -> AsyncGenerator[int, None]:
        """Async context manager for FD operations."""
        fd = self.open(path, flags, mode)
        try:
            yield fd
        finally:
            self.close(fd)

    @staticmethod
    def open_safe(path: Union[str, Path], flags: int, mode: int = 0o600) -> "SafeFDHandle":
        """
        Create a SafeFDHandle context manager.

        Usage:
            async with SafeFDManager.open_safe('/path', os.O_RDONLY) as fd:
                data = os.read(fd, 1024)
        """
        return SafeFDHandle(path, flags, mode)

    def get_open_fds(self) -> List[int]:
        """Get list of currently open FDs from this manager."""
        with self._lock:
            return list(self._local_fds)

    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        with self._lock:
            local_count = len(self._local_fds)

        global_stats = _fd_tracker.get_stats()

        return {
            "local_fds": local_count,
            "global_owned_fds": global_stats["owned_fds"],
            "global_closed_fds": global_stats["closed_fds"],
            "tracking_enabled": SafeFDConfig.ENABLE_TRACKING,
            "strict_mode": SafeFDConfig.STRICT_MODE,
            "is_macos": IS_MACOS,
            "macos_version": MACOS_VERSION,
        }


class SafeFDHandle:
    """
    Context manager handle for safe FD operations.

    Supports both sync and async context managers.
    """

    def __init__(
        self,
        path: Union[str, Path],
        flags: int,
        mode: int = 0o600,
    ):
        self.path = path
        self.flags = flags
        self.mode = mode
        self._fd: Optional[int] = None

    def __enter__(self) -> int:
        self._fd = safe_open(self.path, self.flags, self.mode)
        return self._fd

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        if self._fd is not None:
            safe_close(self._fd)
            self._fd = None

    async def __aenter__(self) -> int:
        self._fd = safe_open(self.path, self.flags, self.mode)
        return self._fd

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        if self._fd is not None:
            safe_close(self._fd)
            self._fd = None


# =============================================================================
# Utility Functions for Migration
# =============================================================================

def migrate_fd_close(fd: int) -> bool:
    """
    Migration helper: Replace direct os.close() calls.

    This function is meant to be a search-and-replace target.

    Before: os.close(fd)
    After:  migrate_fd_close(fd)

    Or better: safe_close(fd)
    """
    return safe_close(fd)


def wrap_fd(fd: int, path: Optional[str] = None) -> int:
    """
    Register an externally-opened FD for safe closing.

    Use this when you receive an FD from a library/system call
    and want to track it for safe closing.

    Args:
        fd: File descriptor to track
        path: Optional path for debugging

    Returns:
        The same FD (for chaining)

    Example:
        # FD from library
        fd = some_library_function()
        wrap_fd(fd, "from_library")
        # Now safe_close(fd) will work correctly
    """
    _fd_tracker.register(fd, path=path)
    return fd


def is_safe_to_close(fd: int) -> bool:
    """
    Check if an FD is safe to close.

    Returns True if the FD can be closed without risking EXC_GUARD.
    """
    status = get_fd_status(fd)
    return status.status in (FDStatus.VALID, FDStatus.OWNED, FDStatus.INVALID)


# =============================================================================
# Fsync with Safe FD
# =============================================================================

def safe_fsync(fd: int) -> bool:
    """
    Safely fsync a file descriptor.

    Args:
        fd: File descriptor to sync

    Returns:
        True if sync succeeded, False otherwise
    """
    if fd < 0:
        return False

    try:
        os.fsync(fd)
        return True
    except OSError as e:
        if e.errno == errno.EBADF:
            logger.warning(f"[SafeFD] Cannot fsync invalid FD {fd}")
        else:
            logger.warning(f"[SafeFD] Fsync error on FD {fd}: {e}")
        return False


def safe_sync_file(path: Union[str, Path]) -> bool:
    """
    Safely sync a file to disk.

    Opens, fsyncs, and safely closes the file.

    Args:
        path: Path to the file

    Returns:
        True if sync succeeded, False otherwise
    """
    try:
        with safe_fd_context(path, os.O_RDONLY) as fd:
            return safe_fsync(fd)
    except OSError as e:
        logger.warning(f"[SafeFD] Cannot sync file {path}: {e}")
        return False


# =============================================================================
# Async File Sync (Used by native_integration.py)
# =============================================================================

async def async_safe_sync_file(path: Union[str, Path]) -> bool:
    """
    Async version of safe_sync_file.

    Runs the sync in a thread pool to avoid blocking.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, safe_sync_file, path)


# =============================================================================
# Global Statistics
# =============================================================================

def get_fd_statistics() -> Dict[str, Any]:
    """Get comprehensive FD statistics."""
    return {
        "tracker": _fd_tracker.get_stats(),
        "config": {
            "tracking_enabled": SafeFDConfig.ENABLE_TRACKING,
            "debug_logging": SafeFDConfig.DEBUG_LOGGING,
            "max_tracked": SafeFDConfig.MAX_TRACKED_FDS,
            "strict_mode": SafeFDConfig.STRICT_MODE,
        },
        "platform": {
            "is_macos": IS_MACOS,
            "is_linux": IS_LINUX,
            "macos_version": MACOS_VERSION,
        },
    }


# =============================================================================
# Module Initialization
# =============================================================================

def _init_module():
    """Initialize the module on import."""
    if SafeFDConfig.DEBUG_LOGGING:
        logger.info(
            f"[SafeFD] v109.3 initialized - "
            f"tracking={SafeFDConfig.ENABLE_TRACKING}, "
            f"strict={SafeFDConfig.STRICT_MODE}, "
            f"macos={IS_MACOS}"
        )


_init_module()


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Core functions
    "safe_close",
    "safe_open",
    "safe_dup",
    "safe_dup2",
    "safe_fsync",
    "safe_sync_file",
    "async_safe_sync_file",

    # Status checking
    "get_fd_status",
    "is_safe_to_close",

    # Context managers
    "safe_fd_context",
    "async_safe_fd_context",

    # Classes
    "SafeFDManager",
    "SafeFDHandle",
    "FDStatus",
    "FDInfo",
    "SafeFDConfig",

    # Migration helpers
    "migrate_fd_close",
    "wrap_fd",

    # Statistics
    "get_fd_statistics",
]

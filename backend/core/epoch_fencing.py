"""
Epoch Fencing v2.0 — Split-brain prevention for the Trinity ecosystem.

When the supervisor crashes mid-operation, the three repos (JARVIS, JARVIS Prime,
Reactor Core) can disagree on system state. This module provides epoch-based
fencing to detect and resolve split-brain scenarios.

Architecture:
    ┌─────────────────────────────────────────────────────────┐
    │                  Epoch Fencing v2.0                      │
    │                                                         │
    │  ┌──────────────┐   ┌──────────────┐   ┌────────────┐  │
    │  │ EpochManager │──→│ EpochInfo    │   │ Fencing    │  │
    │  │ (singleton)  │   │ (dataclass)  │   │ Token      │  │
    │  │              │   │              │   │ (ctx mgr)  │  │
    │  │ • increment  │   │ • epoch      │   │            │  │
    │  │ • validate   │   │ • started_at │   │ • is_valid  │  │
    │  │ • get_info   │   │ • pid        │   │ • __enter__ │  │
    │  │ • fcntl lock │   │ • reason     │   │ • __aenter__│  │
    │  └──────────────┘   └──────────────┘   └────────────┘  │
    │         │                                               │
    │         ▼                                               │
    │  ~/.jarvis/trinity/epoch.json  (fcntl.flock guarded)    │
    └─────────────────────────────────────────────────────────┘

Components:
    1. EpochManager — Manages the epoch counter with file-locked atomic updates.
    2. EpochInfo — Immutable dataclass with epoch metadata.
    3. FencingToken — Context manager (sync + async) that validates epoch currency.
    4. StaleEpochError — Raised when an operation carries a stale epoch.

Integration Points (implement in respective modules):
    - Supervisor increments epoch on startup: new_epoch("supervisor_restart")
    - DLM includes fencing token in lock acquisitions
    - IPC messages include epoch number for staleness detection
    - Each repo checks epoch on reconnect

Epoch File Format (~/.jarvis/trinity/epoch.json):
    {
        "epoch": 42,
        "started_at": 1700000000.0,
        "started_by_pid": 12345,
        "reason": "supervisor_restart",
        "previous_epoch": 41,
        "history": [
            {"epoch": 41, "started_at": ..., "reason": "..."},
            {"epoch": 40, "started_at": ..., "reason": "..."}
        ]
    }

Usage:
    from backend.core.epoch_fencing import (
        get_epoch_manager, new_epoch, get_fencing_token, validate_or_resync,
        EpochManager, EpochInfo, FencingToken, StaleEpochError,
    )

    # Supervisor startup
    epoch = new_epoch("supervisor_restart")

    # Create and use a fencing token (sync)
    token = get_fencing_token()
    with token:
        perform_critical_operation()

    # Create and use a fencing token (async)
    token = get_fencing_token()
    async with token:
        await perform_critical_operation_async()

    # Validate an incoming epoch
    is_current = validate_or_resync(incoming_epoch)

    # IPC message stamping
    msg = stamp_message({"type": "deploy", "target": "prime"})
    if validate_message(incoming_msg):
        process(incoming_msg)

Upgrade from v1.0:
    - EpochManager singleton replaces module-level functions (backwards-compat shims kept)
    - fcntl.flock() replaces threading.Lock for cross-process atomicity
    - EpochInfo dataclass replaces raw dict
    - FencingToken context manager (sync + async) with epoch validation
    - Configurable via env vars (epoch file path, max history)
    - Richer history with reason, pid, previous_epoch per entry
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# Platform check
# =============================================================================

if sys.platform == "win32":
    raise RuntimeError(
        "epoch_fencing.py requires POSIX fcntl.flock(). "
        "Windows is not supported."
    )

# =============================================================================
# Configuration — all from env vars with sensible defaults
# =============================================================================

_DEFAULT_EPOCH_FILE = os.path.expanduser("~/.jarvis/trinity/epoch.json")
_EPOCH_FILE_PATH: str = os.getenv("JARVIS_EPOCH_FILE", _DEFAULT_EPOCH_FILE)
_MAX_HISTORY: int = int(os.getenv("JARVIS_EPOCH_MAX_HISTORY", "10"))


# =============================================================================
# StaleEpochError
# =============================================================================

class StaleEpochError(Exception):
    """Raised when an operation carries an epoch that is no longer current.

    Attributes:
        stale_epoch: The epoch that was found to be stale.
        current_epoch: The epoch that is actually current.
    """

    def __init__(self, stale_epoch: int, current_epoch: int) -> None:
        self.stale_epoch = stale_epoch
        self.current_epoch = current_epoch
        super().__init__(
            f"Stale epoch: operation has epoch {stale_epoch}, "
            f"but current epoch is {current_epoch}"
        )


# =============================================================================
# EpochInfo dataclass
# =============================================================================

@dataclass(frozen=True)
class EpochInfo:
    """Immutable snapshot of epoch metadata.

    Attributes:
        epoch: The current epoch number (monotonically increasing).
        started_at: Unix timestamp when this epoch was started.
        started_by_pid: PID of the process that started this epoch.
        reason: Human-readable reason for the epoch increment.
        previous_epoch: The epoch number that was superseded.
    """
    epoch: int
    started_at: float
    started_by_pid: int
    reason: str
    previous_epoch: int

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict for JSON storage / IPC."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EpochInfo:
        """Deserialize from a dict."""
        return cls(
            epoch=int(data.get("epoch", 0)),
            started_at=float(data.get("started_at", 0.0)),
            started_by_pid=int(data.get("started_by_pid", 0)),
            reason=str(data.get("reason", "")),
            previous_epoch=int(data.get("previous_epoch", 0)),
        )

    @classmethod
    def empty(cls) -> EpochInfo:
        """Return a zeroed-out EpochInfo for when no epoch file exists yet."""
        return cls(
            epoch=0,
            started_at=0.0,
            started_by_pid=0,
            reason="",
            previous_epoch=0,
        )


# =============================================================================
# EpochManager — singleton that manages the epoch counter
# =============================================================================

class EpochManager:
    """Manages a monotonically increasing epoch counter stored on disk.

    Uses ``fcntl.flock()`` for cross-process atomic access and a
    ``threading.Lock`` for intra-process thread safety.

    The epoch file is stored at the path specified by the ``JARVIS_EPOCH_FILE``
    environment variable (default: ``~/.jarvis/trinity/epoch.json``).

    Graceful degradation: if file operations fail (permissions, disk full, etc.),
    methods log a warning and return epoch 0 / ``EpochInfo.empty()`` rather than
    raising.
    """

    def __init__(
        self,
        epoch_file: Optional[str] = None,
        max_history: Optional[int] = None,
    ) -> None:
        self._epoch_file = Path(epoch_file or _EPOCH_FILE_PATH)
        self._max_history = max_history if max_history is not None else _MAX_HISTORY
        self._thread_lock = threading.Lock()
        # In-memory cache for fast reads (invalidated on writes)
        self._cached_epoch: Optional[int] = None
        self._cached_info: Optional[EpochInfo] = None
        self._cache_time: float = 0.0
        # How long the in-memory cache is trusted (seconds)
        self._cache_ttl: float = float(
            os.getenv("JARVIS_EPOCH_CACHE_TTL", "1.0")
        )

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #

    def _ensure_directory(self) -> None:
        """Create the parent directory if it doesn't exist."""
        try:
            self._epoch_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[EpochManager] Failed to create epoch directory %s: %s",
                self._epoch_file.parent,
                exc,
            )

    def _read_locked(self) -> Dict[str, Any]:
        """Read the epoch file while holding an ``fcntl`` shared (read) lock.

        Returns the parsed JSON data, or a default dict if the file does not
        exist or is corrupt.
        """
        self._ensure_directory()

        default: Dict[str, Any] = {
            "epoch": 0,
            "started_at": 0.0,
            "started_by_pid": 0,
            "reason": "",
            "previous_epoch": 0,
            "history": [],
        }

        if not self._epoch_file.exists():
            return default

        fd: Optional[int] = None
        try:
            fd = os.open(str(self._epoch_file), os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_SH)
            raw = os.read(fd, 1_048_576)  # 1 MiB — more than enough
            if not raw:
                return default
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                logger.warning(
                    "[EpochManager] Epoch file contains non-dict data, resetting"
                )
                return default
            return data
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("[EpochManager] Failed to read epoch file: %s", exc)
            return default
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass

    def _write_locked(self, data: Dict[str, Any]) -> bool:
        """Atomically write epoch data while holding an ``fcntl`` exclusive lock.

        Strategy:
            1. Open (or create) the epoch file with O_RDWR | O_CREAT.
            2. Acquire an exclusive flock.
            3. Write updated JSON.
            4. Truncate any leftover bytes (if new content is shorter).
            5. Release the lock.

        Returns True on success, False on failure.
        """
        self._ensure_directory()

        fd: Optional[int] = None
        try:
            fd = os.open(
                str(self._epoch_file),
                os.O_RDWR | os.O_CREAT,
                0o600,  # v242.1: Owner-only (was 0o644 — CWE-732 overly permissive)
            )
            fcntl.flock(fd, fcntl.LOCK_EX)

            serialized = json.dumps(data, indent=2).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, serialized)
            os.ftruncate(fd, len(serialized))
            os.fsync(fd)

            return True
        except OSError as exc:
            logger.error("[EpochManager] Failed to write epoch file: %s", exc)
            return False
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass

    def _invalidate_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cached_epoch = None
        self._cached_info = None
        self._cache_time = 0.0

    def _is_cache_fresh(self) -> bool:
        """Check if the in-memory cache is still within TTL."""
        return (
            self._cached_epoch is not None
            and (time.monotonic() - self._cache_time) < self._cache_ttl
        )

    def _data_to_epoch_info(self, data: Dict[str, Any]) -> EpochInfo:
        """Convert raw file data to an ``EpochInfo`` instance."""
        return EpochInfo(
            epoch=int(data.get("epoch", 0)),
            started_at=float(data.get("started_at", 0.0)),
            started_by_pid=int(data.get("started_by_pid", 0)),
            reason=str(data.get("reason", "")),
            previous_epoch=int(data.get("previous_epoch", 0)),
        )

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    def get_current_epoch(self) -> int:
        """Read and return the current epoch number.

        Uses an in-memory cache with a configurable TTL
        (``JARVIS_EPOCH_CACHE_TTL``, default 1 s) to avoid excessive disk I/O
        on rapid consecutive calls.  Falls back to epoch 0 on failure.
        """
        with self._thread_lock:
            if self._is_cache_fresh():
                return self._cached_epoch  # type: ignore[return-value]
            try:
                data = self._read_locked()
                epoch = int(data.get("epoch", 0))
                self._cached_epoch = epoch
                self._cache_time = time.monotonic()
                return epoch
            except Exception as exc:
                logger.warning(
                    "[EpochManager] get_current_epoch failed, returning 0: %s", exc
                )
                return 0

    def get_epoch_info(self) -> EpochInfo:
        """Return full metadata for the current epoch.

        Falls back to ``EpochInfo.empty()`` on failure.
        """
        with self._thread_lock:
            # Check cached info
            if self._is_cache_fresh() and self._cached_info is not None:
                return self._cached_info
            try:
                data = self._read_locked()
                info = self._data_to_epoch_info(data)
                # Update cache
                self._cached_epoch = info.epoch
                self._cached_info = info
                self._cache_time = time.monotonic()
                return info
            except Exception as exc:
                logger.warning(
                    "[EpochManager] get_epoch_info failed: %s", exc
                )
                return EpochInfo.empty()

    def increment_epoch(self, reason: str = "unspecified") -> int:
        """Atomically increment the epoch counter and return the new epoch.

        This operation:
            1. Acquires an exclusive file lock via ``fcntl.flock()``.
            2. Reads the current epoch data.
            3. Increments the epoch number.
            4. Records PID, timestamp, reason, and previous epoch.
            5. Appends to bounded history (max ``JARVIS_EPOCH_MAX_HISTORY``).
            6. Writes back and releases the lock.

        Falls back to epoch 0 on failure (with a logged error).

        Args:
            reason: Human-readable reason for the increment (e.g.,
                    ``"supervisor_restart"``, ``"manual_reset"``).

        Returns:
            The new epoch number.
        """
        with self._thread_lock:
            try:
                # We need to do a combined read-modify-write under exclusive lock
                # so we handle the locking inline rather than using _read_locked
                self._ensure_directory()

                fd: Optional[int] = None
                try:
                    fd = os.open(
                        str(self._epoch_file),
                        os.O_RDWR | os.O_CREAT,
                        0o644,
                    )
                    fcntl.flock(fd, fcntl.LOCK_EX)

                    # Read current data
                    raw = os.read(fd, 1_048_576)
                    if raw:
                        try:
                            data = json.loads(raw.decode("utf-8"))
                            if not isinstance(data, dict):
                                data = {}
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            data = {}
                    else:
                        data = {}

                    old_epoch = int(data.get("epoch", 0))
                    new_epoch_val = old_epoch + 1
                    now = time.time()
                    pid = os.getpid()

                    # Build history entry for the OLD epoch (if it had data)
                    history: List[Dict[str, Any]] = data.get("history", [])
                    if old_epoch > 0:
                        history_entry = {
                            "epoch": old_epoch,
                            "started_at": float(data.get("started_at", 0.0)),
                            "started_by_pid": int(data.get("started_by_pid", 0)),
                            "reason": str(data.get("reason", "")),
                        }
                        history.append(history_entry)

                    # Trim history to max size
                    if len(history) > self._max_history:
                        history = history[-self._max_history:]

                    # Build new data
                    new_data: Dict[str, Any] = {
                        "epoch": new_epoch_val,
                        "started_at": now,
                        "started_by_pid": pid,
                        "reason": reason,
                        "previous_epoch": old_epoch,
                        "history": history,
                    }

                    # Write back
                    serialized = json.dumps(new_data, indent=2).encode("utf-8")
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, serialized)
                    os.ftruncate(fd, len(serialized))
                    os.fsync(fd)

                    # Update cache
                    self._cached_epoch = new_epoch_val
                    self._cached_info = self._data_to_epoch_info(new_data)
                    self._cache_time = time.monotonic()

                    logger.info(
                        "[EpochManager] Epoch incremented: %d -> %d (reason=%s, pid=%d)",
                        old_epoch,
                        new_epoch_val,
                        reason,
                        pid,
                    )
                    return new_epoch_val

                finally:
                    if fd is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                            os.close(fd)
                        except OSError:
                            pass

            except Exception as exc:
                logger.error(
                    "[EpochManager] increment_epoch failed: %s", exc
                )
                self._invalidate_cache()
                return 0

    def validate_epoch(self, epoch: int) -> bool:
        """Check if the given epoch matches the current epoch.

        Args:
            epoch: The epoch number to validate.

        Returns:
            True if ``epoch`` equals the current epoch, False otherwise.
        """
        current = self.get_current_epoch()
        if epoch != current:
            logger.debug(
                "[EpochManager] Epoch validation failed: got %d, current is %d",
                epoch,
                current,
            )
            return False
        return True

    def get_history(self) -> List[Dict[str, Any]]:
        """Return the bounded history of previous epochs.

        Returns:
            List of dicts, each with ``epoch``, ``started_at``,
            ``started_by_pid``, and ``reason`` keys.
        """
        with self._thread_lock:
            try:
                data = self._read_locked()
                return list(data.get("history", []))
            except Exception as exc:
                logger.warning("[EpochManager] get_history failed: %s", exc)
                return []


# =============================================================================
# FencingToken — context manager wrapping an epoch for fenced operations
# =============================================================================

class FencingToken:
    """Wraps an epoch number for use as a fencing token.

    A fencing token ensures that an operation was started in the current
    epoch and has not been superseded by a new epoch.  It can be used as
    both a synchronous and asynchronous context manager.

    Usage (sync):
        token = FencingToken(manager)
        with token:
            do_something()

    Usage (async):
        token = FencingToken(manager)
        async with token:
            await do_something_async()

    Raises ``StaleEpochError`` on ``__enter__`` / ``__aenter__`` if the
    token's epoch is already stale, and again on ``__exit__`` /
    ``__aexit__`` if the epoch changed during the operation.
    """

    def __init__(
        self,
        manager: EpochManager,
        *,
        epoch: Optional[int] = None,
        operation_name: str = "",
    ) -> None:
        self._manager = manager
        self.epoch = epoch if epoch is not None else manager.get_current_epoch()
        self.operation_name = operation_name
        self.operation_id: str = uuid.uuid4().hex[:12]
        self.created_at: float = time.time()

    def is_valid(self) -> bool:
        """Check if this token's epoch matches the current epoch."""
        return self._manager.validate_epoch(self.epoch)

    def validate_or_raise(self) -> None:
        """Raise ``StaleEpochError`` if this token's epoch is stale."""
        current = self._manager.get_current_epoch()
        if self.epoch != current:
            raise StaleEpochError(stale_epoch=self.epoch, current_epoch=current)

    # -- Sync context manager ------------------------------------------------

    def __enter__(self) -> FencingToken:
        self.validate_or_raise()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        # Only validate on clean exit (no exception already in flight)
        if exc_type is None:
            self.validate_or_raise()

    # -- Async context manager -----------------------------------------------

    async def __aenter__(self) -> FencingToken:
        self.validate_or_raise()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        if exc_type is None:
            self.validate_or_raise()

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict for IPC / lock metadata."""
        return {
            "epoch": self.epoch,
            "operation_id": self.operation_id,
            "operation_name": self.operation_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        manager: Optional[EpochManager] = None,
    ) -> FencingToken:
        """Deserialize from a dict.

        If *manager* is ``None``, the module-level singleton is used.
        """
        mgr = manager or get_epoch_manager()
        token = cls(
            manager=mgr,
            epoch=int(data.get("epoch", 0)),
            operation_name=str(data.get("operation_name", "")),
        )
        token.operation_id = str(data.get("operation_id", token.operation_id))
        token.created_at = float(data.get("created_at", token.created_at))
        return token

    def __repr__(self) -> str:
        return (
            f"FencingToken(epoch={self.epoch}, "
            f"op={self.operation_name!r}, "
            f"id={self.operation_id})"
        )


# =============================================================================
# Module-level singleton
# =============================================================================

_singleton_lock = threading.Lock()
_singleton: Optional[EpochManager] = None


def get_epoch_manager() -> EpochManager:
    """Return the module-level ``EpochManager`` singleton.

    Thread-safe lazy initialization.  The singleton reads its configuration
    from environment variables on first call.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        # Double-checked locking
        if _singleton is not None:
            return _singleton
        _singleton = EpochManager()
        return _singleton


# =============================================================================
# Module-level convenience functions
# =============================================================================

def new_epoch(reason: str) -> int:
    """Increment the epoch with the given reason and return the new epoch.

    Convenience wrapper around ``get_epoch_manager().increment_epoch(reason)``.

    Args:
        reason: Human-readable reason (e.g., ``"supervisor_restart"``).

    Returns:
        The new epoch number.
    """
    return get_epoch_manager().increment_epoch(reason)


def get_fencing_token(operation_name: str = "") -> FencingToken:
    """Get a ``FencingToken`` for the current epoch.

    Args:
        operation_name: Optional label for the operation being fenced.

    Returns:
        A ``FencingToken`` bound to the current epoch.
    """
    return FencingToken(get_epoch_manager(), operation_name=operation_name)


def validate_or_resync(epoch: int) -> bool:
    """Validate that *epoch* is current; return False if stale.

    This is the "check before acting" primitive.  If the epoch is stale,
    the caller should initiate a resync from the supervisor.

    Args:
        epoch: The epoch number to validate.

    Returns:
        True if *epoch* matches the current epoch, False otherwise.
    """
    mgr = get_epoch_manager()
    is_valid = mgr.validate_epoch(epoch)
    if not is_valid:
        current = mgr.get_current_epoch()
        logger.warning(
            "[EpochFencing] validate_or_resync: epoch %d is stale "
            "(current=%d) — caller should resync",
            epoch,
            current,
        )
    return is_valid


# =============================================================================
# Backwards-compatible shims (v1.0 API)
# =============================================================================
# These preserve the v1.0 module-level function signatures so that existing
# callers (e.g., IPC helpers, supervisor startup) continue to work without
# modification.


def get_current_epoch() -> int:
    """Return the current epoch number (v1.0 compat)."""
    return get_epoch_manager().get_current_epoch()


def get_epoch_info() -> Dict[str, Any]:
    """Return full epoch metadata as a dict (v1.0 compat).

    Note: v2.0 consumers should prefer ``get_epoch_manager().get_epoch_info()``
    which returns an ``EpochInfo`` dataclass.
    """
    info = get_epoch_manager().get_epoch_info()
    return info.to_dict()


def increment_epoch(supervisor_id: Optional[str] = None) -> int:
    """Increment the epoch (v1.0 compat).

    Maps the old ``supervisor_id`` parameter to ``reason``.
    """
    reason = f"supervisor_restart (id={supervisor_id})" if supervisor_id else "supervisor_restart"
    return get_epoch_manager().increment_epoch(reason)


def validate_epoch(epoch: int) -> bool:
    """Check if the given epoch matches the current epoch (v1.0 compat)."""
    return get_epoch_manager().validate_epoch(epoch)


def validate_epoch_or_raise(epoch: int) -> None:
    """Raise ``StaleEpochError`` if epoch doesn't match (v1.0 compat).

    Note: v1.0 ``StaleEpochError(current, received)`` used positional args
    with different names.  v2.0 uses ``stale_epoch`` and ``current_epoch``.
    """
    current = get_epoch_manager().get_current_epoch()
    if epoch != current:
        raise StaleEpochError(stale_epoch=epoch, current_epoch=current)


# =============================================================================
# IPC message helpers (v1.0 compat, enhanced)
# =============================================================================

def stamp_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Add epoch and message metadata to an IPC message.

    Stamps the message in-place and returns it for chaining.

    Keys added:
        ``_epoch``: current epoch number
        ``_msg_id``: unique message ID (12-char hex)
        ``_ts``: unix timestamp
    """
    mgr = get_epoch_manager()
    msg["_epoch"] = mgr.get_current_epoch()
    msg["_msg_id"] = uuid.uuid4().hex[:12]
    msg["_ts"] = time.time()
    return msg


def validate_message(
    msg: Dict[str, Any],
    *,
    allow_stale: bool = False,
) -> bool:
    """Validate epoch in an incoming IPC message.

    Args:
        msg: The message dict.  Expected to have an ``_epoch`` key.
        allow_stale: If True, stale messages are accepted with a warning
                     instead of being rejected.

    Returns:
        True if the message is valid (current epoch or ``allow_stale=True``),
        False if the message is stale and ``allow_stale=False``.
    """
    msg_epoch = msg.get("_epoch")
    if msg_epoch is None:
        # Legacy message without epoch — accept but warn
        logger.debug(
            "[EpochFencing] Message without epoch stamp — legacy format"
        )
        return True

    mgr = get_epoch_manager()
    current = mgr.get_current_epoch()
    if msg_epoch != current:
        if allow_stale:
            logger.warning(
                "[EpochFencing] Stale message accepted (allow_stale=True): "
                "epoch %d (current %d), msg_id=%s",
                msg_epoch,
                current,
                msg.get("_msg_id", "?"),
            )
            return True
        logger.warning(
            "[EpochFencing] Rejected stale message: "
            "epoch %d (current %d), msg_id=%s",
            msg_epoch,
            current,
            msg.get("_msg_id", "?"),
        )
        return False
    return True


# =============================================================================
# v1.0 EpochFencingToken compat shim
# =============================================================================
# The v1.0 module exported ``EpochFencingToken`` as a frozen dataclass.
# We provide a thin wrapper that translates to v2.0 ``FencingToken``.

class EpochFencingToken:
    """v1.0-compatible fencing token wrapper.

    Provides the same ``create()``, ``to_dict()``, ``from_dict()``,
    ``validate()``, and ``validate_or_raise()`` API as v1.0.

    New code should use ``FencingToken`` directly.
    """

    def __init__(
        self,
        epoch: int,
        operation_id: str,
        operation_name: str,
        created_at: float,
    ) -> None:
        self.epoch = epoch
        self.operation_id = operation_id
        self.operation_name = operation_name
        self.created_at = created_at

    @classmethod
    def create(cls, operation_name: str) -> EpochFencingToken:
        """Create a new fencing token with the current epoch."""
        return cls(
            epoch=get_current_epoch(),
            operation_id=uuid.uuid4().hex[:12],
            operation_name=operation_name,
            created_at=time.time(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "operation_id": self.operation_id,
            "operation_name": self.operation_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EpochFencingToken:
        return cls(
            epoch=data["epoch"],
            operation_id=data["operation_id"],
            operation_name=data.get("operation_name", ""),
            created_at=data.get("created_at", 0.0),
        )

    def validate(self) -> bool:
        """Check if this token's epoch matches the current epoch."""
        return self.epoch == get_current_epoch()

    def validate_or_raise(self) -> None:
        """Raise ``StaleEpochError`` if epoch doesn't match."""
        current = get_current_epoch()
        if self.epoch != current:
            raise StaleEpochError(stale_epoch=self.epoch, current_epoch=current)

"""
JARVIS Idempotency Registry v1.0
==================================
Bounded in-memory registry for exactly-once command/event semantics.

Root causes cured:
  - No idempotency keys anywhere in the codebase
  - Cost tracker records have no dedup — timeout+retry creates duplicate billing
  - GCP API calls have no operation tracking — timeout+retry duplicates side effects
  - Event emission has no dedup window — same event emitted multiple times
  - Two independent code paths can promote the same GCP endpoint concurrently

Thread-safe via threading.Lock (used from both sync and async contexts).
NOT persistent across restarts (in-memory, same pattern as decision_log.py).

v272.x: Created as part of Phase 10 — exactly-once command semantics.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (all configurable via env vars — no hardcoding)
# ---------------------------------------------------------------------------

_DEFAULT_DEDUP_WINDOW_S = float(
    os.environ.get("JARVIS_IDEMPOTENCY_WINDOW", "300.0")
)
_DEFAULT_MAX_ENTRIES = int(
    os.environ.get("JARVIS_IDEMPOTENCY_MAX_ENTRIES", "1000")
)
_DEFAULT_OPERATION_TIMEOUT_S = float(
    os.environ.get("JARVIS_OPERATION_TRACKER_TIMEOUT", "300.0")
)


# ===========================================================================
# Data Model
# ===========================================================================


@dataclass(frozen=True)
class IdempotencyKey:
    """Unique key for an idempotent operation.

    Frozen so keys are hashable and safe as dict keys.
    """
    operation_type: str   # e.g., "create_vm", "terminate_vm", "promote_gcp"
    resource_id: str      # e.g., VM name, endpoint "host:port"
    nonce: str = ""       # Optional caller-provided dedup token


@dataclass
class _IdempotencyEntry:
    """Internal: tracks when a key was first recorded."""
    key: IdempotencyKey
    recorded_at: float = field(default_factory=time.time)


@dataclass
class _OperationRecord:
    """Internal: tracks an in-flight operation."""
    key: IdempotencyKey
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    timeout_s: float = _DEFAULT_OPERATION_TIMEOUT_S


# ===========================================================================
# IdempotencyRegistry — bounded dedup window
# ===========================================================================


class IdempotencyRegistry:
    """Bounded in-memory registry preventing duplicate operations within a
    configurable time window.

    Thread-safe.  Singleton via ``get_instance()``.
    """

    _instance: Optional[IdempotencyRegistry] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        dedup_window_s: float = _DEFAULT_DEDUP_WINDOW_S,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._entries: Dict[IdempotencyKey, _IdempotencyEntry] = {}
        self._lock = threading.Lock()
        self.dedup_window_s = dedup_window_s
        self.max_entries = max_entries

    @classmethod
    def get_instance(cls) -> IdempotencyRegistry:
        """Singleton accessor (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -------------------------------------------------------------------
    # Core API
    # -------------------------------------------------------------------

    def check_and_record(self, key: IdempotencyKey) -> bool:
        """Return ``True`` if this is a NEW operation (caller should proceed).

        Return ``False`` if this is a DUPLICATE within the dedup window
        (caller should skip).

        Expired entries are evicted on each call.
        """
        with self._lock:
            self._evict_expired()

            existing = self._entries.get(key)
            if existing is not None:
                age = time.time() - existing.recorded_at
                if age < self.dedup_window_s:
                    logger.debug(
                        "[Idempotency] Duplicate suppressed: %s/%s (age=%.1fs)",
                        key.operation_type, key.resource_id, age,
                    )
                    return False
                # Expired — remove and allow re-record below

            # Record new entry
            self._entries[key] = _IdempotencyEntry(key=key)

            # Enforce max entries (evict oldest if over limit)
            if len(self._entries) > self.max_entries:
                self._evict_oldest()

            return True

    def is_duplicate(self, key: IdempotencyKey) -> bool:
        """Read-only check.  Returns ``True`` if key is in the dedup window."""
        with self._lock:
            existing = self._entries.get(key)
            if existing is None:
                return False
            return (time.time() - existing.recorded_at) < self.dedup_window_s

    def clear(self, key: IdempotencyKey) -> None:
        """Explicitly remove a key (for error recovery)."""
        with self._lock:
            self._entries.pop(key, None)

    def stats(self) -> Dict[str, Any]:
        """Return diagnostic statistics."""
        with self._lock:
            if not self._entries:
                return {"count": 0, "oldest_age_s": 0.0}
            now = time.time()
            ages = [now - e.recorded_at for e in self._entries.values()]
            return {
                "count": len(self._entries),
                "oldest_age_s": max(ages),
                "newest_age_s": min(ages),
                "dedup_window_s": self.dedup_window_s,
                "max_entries": self.max_entries,
            }

    def reset(self) -> None:
        """Clear all entries.  Used for testing."""
        with self._lock:
            self._entries.clear()

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove entries older than the dedup window."""
        cutoff = time.time() - self.dedup_window_s
        expired = [
            k for k, e in self._entries.items()
            if e.recorded_at < cutoff
        ]
        for k in expired:
            del self._entries[k]

    def _evict_oldest(self) -> None:
        """Remove the oldest entry to stay within max_entries."""
        if not self._entries:
            return
        oldest_key = min(
            self._entries, key=lambda k: self._entries[k].recorded_at
        )
        del self._entries[oldest_key]


# ===========================================================================
# OperationTracker — in-flight operation guard
# ===========================================================================


class OperationTracker:
    """Tracks in-flight operations to prevent concurrent duplicates.

    ``start_operation()`` returns a unique token if the operation is new,
    or ``None`` if the same operation is already in-flight.  The caller
    must call ``complete_operation()`` or ``fail_operation()`` when done.

    Timed-out operations are automatically reaped.

    Thread-safe.  Singleton via ``get_instance()``.
    """

    _instance: Optional[OperationTracker] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._in_flight: Dict[IdempotencyKey, _OperationRecord] = {}
        self._token_to_key: Dict[str, IdempotencyKey] = {}
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> OperationTracker:
        """Singleton accessor (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -------------------------------------------------------------------
    # Core API
    # -------------------------------------------------------------------

    def start_operation(
        self,
        key: IdempotencyKey,
        timeout_s: float = _DEFAULT_OPERATION_TIMEOUT_S,
    ) -> Optional[str]:
        """Start tracking an operation.

        Returns a unique token if started (caller should proceed).
        Returns ``None`` if the same operation is already in-flight
        (caller should skip or wait).
        """
        with self._lock:
            self._reap_timed_out()

            existing = self._in_flight.get(key)
            if existing is not None:
                logger.debug(
                    "[OpTracker] Operation already in-flight: %s/%s (token=%s…)",
                    key.operation_type, key.resource_id, existing.token[:8],
                )
                return None

            record = _OperationRecord(key=key, timeout_s=timeout_s)
            self._in_flight[key] = record
            self._token_to_key[record.token] = key

            logger.debug(
                "[OpTracker] Operation started: %s/%s (token=%s…)",
                key.operation_type, key.resource_id, record.token[:8],
            )
            return record.token

    def complete_operation(self, token: str) -> None:
        """Mark an operation as successfully completed."""
        with self._lock:
            key = self._token_to_key.pop(token, None)
            if key is not None:
                self._in_flight.pop(key, None)
                logger.debug(
                    "[OpTracker] Operation completed: %s/%s",
                    key.operation_type, key.resource_id,
                )

    def fail_operation(self, token: str) -> None:
        """Mark an operation as failed (allows retry)."""
        with self._lock:
            key = self._token_to_key.pop(token, None)
            if key is not None:
                self._in_flight.pop(key, None)
                logger.debug(
                    "[OpTracker] Operation failed: %s/%s",
                    key.operation_type, key.resource_id,
                )

    def is_in_flight(self, key: IdempotencyKey) -> bool:
        """Check if an operation is currently in-flight (also reaps stale)."""
        with self._lock:
            self._reap_timed_out()
            return key in self._in_flight

    def active_count(self) -> int:
        """Return the number of currently in-flight operations."""
        with self._lock:
            self._reap_timed_out()
            return len(self._in_flight)

    def reset(self) -> None:
        """Clear all tracked operations.  Used for testing."""
        with self._lock:
            self._in_flight.clear()
            self._token_to_key.clear()

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _reap_timed_out(self) -> None:
        """Remove operations that have exceeded their timeout."""
        now = time.time()
        timed_out = [
            (k, r) for k, r in self._in_flight.items()
            if (now - r.started_at) > r.timeout_s
        ]
        for key, record in timed_out:
            self._in_flight.pop(key, None)
            self._token_to_key.pop(record.token, None)
            logger.warning(
                "[OpTracker] Operation timed out and reaped: %s/%s "
                "(started %.1fs ago, timeout=%.1fs)",
                key.operation_type, key.resource_id,
                now - record.started_at, record.timeout_s,
            )


# ===========================================================================
# Module-level convenience functions
# ===========================================================================


def check_idempotent(op_type: str, resource_id: str, nonce: str = "") -> bool:
    """Return ``True`` if new operation, ``False`` if duplicate.

    Never raises — returns ``True`` on any internal error (fail-open).
    """
    try:
        return IdempotencyRegistry.get_instance().check_and_record(
            IdempotencyKey(op_type, resource_id, nonce)
        )
    except Exception:
        return True


def start_tracked_operation(
    op_type: str,
    resource_id: str,
    timeout_s: float = _DEFAULT_OPERATION_TIMEOUT_S,
) -> Optional[str]:
    """Return operation token if started, ``None`` if already in-flight.

    Never raises — returns a generated token on any internal error (fail-open).
    """
    try:
        return OperationTracker.get_instance().start_operation(
            IdempotencyKey(op_type, resource_id), timeout_s
        )
    except Exception:
        return uuid.uuid4().hex


def complete_tracked_operation(token: str) -> None:
    """Mark a tracked operation as complete.  Never raises."""
    try:
        OperationTracker.get_instance().complete_operation(token)
    except Exception:
        pass


def fail_tracked_operation(token: str) -> None:
    """Mark a tracked operation as failed.  Never raises."""
    try:
        OperationTracker.get_instance().fail_operation(token)
    except Exception:
        pass

"""
Governance Lock Manager -- Hierarchical Read/Write Leases
=========================================================

Wraps the existing DLM (``distributed_lock_manager.py``) with governance-
specific semantics:

1. **8-level lock hierarchy** enforced at runtime (ascending order only).
2. **Shared-read / exclusive-write** semantics per level.
3. **Fencing token validation** for every write operation.
4. **Fairness tracking** -- max wait time exposed for monitoring.

Lock Levels (acquire in ascending order ONLY)::

    0: FILE_LOCK        per-file, shared-read / exclusive-write
    1: REPO_LOCK        per-repo exclusive write
    2: CROSS_REPO_TX    multi-repo transaction envelope
    3: POLICY_LOCK      short-lived, around classification + gating
    4: LEDGER_APPEND    fencing token for exactly-once state transitions
    5: BUILD_LOCK       build gate
    6: STAGING_LOCK     staging apply
    7: PROD_LOCK        production apply
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("Ouroboros.LockManager")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class LockLevel(enum.IntEnum):
    """Hierarchical lock levels.  Always acquire in ascending order."""

    FILE_LOCK = 0
    REPO_LOCK = 1
    CROSS_REPO_TX = 2
    POLICY_LOCK = 3
    LEDGER_APPEND = 4
    BUILD_LOCK = 5
    STAGING_LOCK = 6
    PROD_LOCK = 7


class LockMode(enum.Enum):
    """Lock acquisition mode."""

    SHARED_READ = "shared_read"
    EXCLUSIVE_WRITE = "exclusive_write"


# ---------------------------------------------------------------------------
# TTL configuration
# ---------------------------------------------------------------------------

LOCK_TTLS: Dict[LockLevel, float] = {
    LockLevel.FILE_LOCK: 60.0,
    LockLevel.REPO_LOCK: 120.0,
    LockLevel.CROSS_REPO_TX: 300.0,
    LockLevel.POLICY_LOCK: 30.0,
    LockLevel.LEDGER_APPEND: 30.0,
    LockLevel.BUILD_LOCK: 300.0,
    LockLevel.STAGING_LOCK: 600.0,
    LockLevel.PROD_LOCK: 600.0,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LockOrderViolation(RuntimeError):
    """Raised when a lower-level lock is requested while holding a higher one."""


class FencingTokenError(RuntimeError):
    """Raised when a write uses a stale fencing token."""


# ---------------------------------------------------------------------------
# LeaseHandle
# ---------------------------------------------------------------------------


@dataclass
class LeaseHandle:
    """Handle returned when a lock is successfully acquired.

    Parameters
    ----------
    level:
        The lock level that was acquired.
    resource:
        The resource identifier (file path, repo name, etc.).
    mode:
        Whether this is a shared-read or exclusive-write lease.
    fencing_token:
        Monotonically increasing token for ordering writes.
    acquired_at:
        Monotonic clock timestamp when the lock was acquired.
    ttl:
        Time-to-live in seconds for this lease.
    """

    level: LockLevel
    resource: str
    mode: LockMode
    fencing_token: int
    acquired_at: float = field(default_factory=time.monotonic)
    ttl: float = 60.0


# ---------------------------------------------------------------------------
# GovernanceLockManager
# ---------------------------------------------------------------------------


class GovernanceLockManager:
    """Hierarchical lock manager with read/write lease semantics.

    Enforces:
    - Strict ascending acquisition order (level 0 -> 7).
    - Shared-read allows multiple concurrent readers.
    - Exclusive-write blocks all other writers.
    - Fencing tokens are monotonically increasing per (level, resource).
    - Fairness: tracks max wait time for contention monitoring.
    """

    def __init__(self) -> None:
        # Per-task held lock levels for ordering enforcement
        self._task_held_levels: Dict[int, List[int]] = {}

        # Read/write state: (level, resource) -> set of reader task IDs
        self._readers: Dict[Tuple[LockLevel, str], Set[int]] = {}
        # (level, resource) -> writer task ID or None
        self._writers: Dict[Tuple[LockLevel, str], Optional[int]] = {}
        # Condition variable for waiting on write release
        self._lock_conditions: Dict[Tuple[LockLevel, str], asyncio.Condition] = {}

        # Fencing tokens: (level, resource) -> current token
        self._fencing_tokens: Dict[Tuple[LockLevel, str], int] = {}
        self._fencing_lock = asyncio.Lock()

        # Re-entrancy tracking: (task_id, level, resource) -> count
        self._reentrant_counts: Dict[Tuple[int, LockLevel, str], int] = {}

        # Fairness metrics
        self._max_wait_ms: float = 0.0
        self._total_acquisitions: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _task_id(self) -> int:
        """Get a unique identifier for the current asyncio task."""
        try:
            task = asyncio.current_task()
            return id(task) if task else id(asyncio.get_running_loop())
        except RuntimeError:
            return 0

    def _get_condition(
        self, level: LockLevel, resource: str
    ) -> Tuple[Tuple[LockLevel, str], asyncio.Condition]:
        """Get or create a condition variable for a (level, resource) pair."""
        key = (level, resource)
        if key not in self._lock_conditions:
            self._lock_conditions[key] = asyncio.Condition()
        return key, self._lock_conditions[key]

    async def _next_fencing_token(
        self, level: LockLevel, resource: str
    ) -> int:
        """Increment and return the next fencing token."""
        async with self._fencing_lock:
            key = (level, resource)
            current = self._fencing_tokens.get(key, 0)
            next_val = current + 1
            self._fencing_tokens[key] = next_val
            return next_val

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        level: LockLevel,
        resource: str,
        mode: LockMode,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[LeaseHandle]:
        """Acquire a governance lock with hierarchy and read/write enforcement.

        Parameters
        ----------
        level:
            The lock level to acquire.
        resource:
            Resource identifier (file path, repo name, etc.).
        mode:
            SHARED_READ or EXCLUSIVE_WRITE.
        timeout:
            Max time to wait (defaults to level TTL).

        Yields
        ------
        LeaseHandle
            Handle with fencing token and lease metadata.

        Raises
        ------
        LockOrderViolation
            If a lower-level lock is requested while a higher one is held.
        asyncio.TimeoutError
            If the lock cannot be acquired within the timeout.
        """
        tid = self._task_id()
        ttl = LOCK_TTLS.get(level, 60.0)
        timeout = timeout or ttl
        key = (level, resource)
        reentrant_key = (tid, level, resource)

        # -- Re-entrancy check --
        if self._reentrant_counts.get(reentrant_key, 0) > 0:
            self._reentrant_counts[reentrant_key] += 1
            # Return same fencing token for re-entrant acquisition
            existing_token = self._fencing_tokens.get(key, 0)
            yield LeaseHandle(
                level=level,
                resource=resource,
                mode=mode,
                fencing_token=existing_token,
                ttl=ttl,
            )
            self._reentrant_counts[reentrant_key] -= 1
            return

        # -- Ascending order check --
        held = self._task_held_levels.get(tid, [])
        if held:
            max_held = max(held)
            if level.value < max_held:
                raise LockOrderViolation(
                    f"Cannot acquire {level.name} (level {level.value}) "
                    f"while holding level {max_held}. "
                    f"Locks must be acquired in ascending order."
                )

        # -- Acquire based on mode --
        _, condition = self._get_condition(level, resource)
        wait_start = time.monotonic()

        async with condition:
            if mode is LockMode.SHARED_READ:
                # Wait until no exclusive writer holds the lock
                while self._writers.get(key) is not None:
                    await asyncio.wait_for(
                        condition.wait(), timeout=timeout
                    )
                readers = self._readers.setdefault(key, set())
                readers.add(tid)

            elif mode is LockMode.EXCLUSIVE_WRITE:
                # Wait until no writer AND no readers
                while (
                    self._writers.get(key) is not None
                    or len(self._readers.get(key, set())) > 0
                ):
                    await asyncio.wait_for(
                        condition.wait(), timeout=timeout
                    )
                self._writers[key] = tid

        # Track wait time for fairness
        wait_ms = (time.monotonic() - wait_start) * 1000
        self._max_wait_ms = max(self._max_wait_ms, wait_ms)
        self._total_acquisitions += 1

        # Get fencing token
        fencing_token = await self._next_fencing_token(level, resource)

        # Track held levels for this task
        self._task_held_levels.setdefault(tid, []).append(level.value)
        self._reentrant_counts[reentrant_key] = 1

        handle = LeaseHandle(
            level=level,
            resource=resource,
            mode=mode,
            fencing_token=fencing_token,
            ttl=ttl,
        )

        try:
            yield handle
        finally:
            # -- Release --
            self._reentrant_counts.pop(reentrant_key, None)

            _, condition = self._get_condition(level, resource)
            async with condition:
                if mode is LockMode.SHARED_READ:
                    readers = self._readers.get(key, set())
                    readers.discard(tid)
                    if not readers:
                        self._readers.pop(key, None)
                elif mode is LockMode.EXCLUSIVE_WRITE:
                    if self._writers.get(key) == tid:
                        self._writers.pop(key, None)
                condition.notify_all()

            # Remove from held levels
            held = self._task_held_levels.get(tid, [])
            if level.value in held:
                held.remove(level.value)
            if not held:
                self._task_held_levels.pop(tid, None)

    def validate_fencing_token(
        self,
        level: LockLevel,
        resource: str,
        token: int,
    ) -> None:
        """Validate that a fencing token is current (not stale).

        Raises
        ------
        FencingTokenError
            If the token is less than the current fencing token.
        """
        key = (level, resource)
        current = self._fencing_tokens.get(key, 0)
        if token < current:
            raise FencingTokenError(
                f"Stale fencing token {token} for {level.name}:{resource} "
                f"(current is {current})"
            )

    def get_contention_stats(self) -> Dict[str, Any]:
        """Return contention and fairness statistics."""
        active_readers = sum(len(s) for s in self._readers.values())
        active_writers = sum(1 for w in self._writers.values() if w is not None)
        return {
            "max_wait_ms": self._max_wait_ms,
            "active_locks": active_readers + active_writers,
            "active_readers": active_readers,
            "active_writers": active_writers,
            "total_acquisitions": self._total_acquisitions,
        }

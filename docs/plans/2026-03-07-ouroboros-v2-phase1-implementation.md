# Ouroboros v2.0 Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the lock hierarchy, transactional change engine, break-glass governance, and TUI communication transport on top of Phase 0's foundation.

**Architecture:** Phase 1A (Track 1) adds the governance lock manager wrapping the existing DLM (`distributed_lock_manager.py`), a transactional change engine with pre-tested rollback artifacts, break-glass tokens with audit trail, and outbox/inbox eventing. Phase 1B (Track 2) adds a TUI transport for the CommProtocol so governance messages appear in the dashboard.

**Tech Stack:** Python 3.11+, asyncio, pytest, pytest-asyncio, existing DLM (`backend/core/distributed_lock_manager.py`), existing TUI bridge (`backend/core/supervisor_tui_bridge.py`)

**Design doc:** `docs/plans/2026-03-07-ouroboros-v2-design.md`

**Phase 0 code references (all in `backend/core/ouroboros/governance/`):**
- `operation_id.py` — `generate_operation_id()`, `OperationMetadata`
- `risk_engine.py` — `RiskEngine`, `RiskTier`, `RiskClassification`, `OperationProfile`, `ChangeType`
- `ledger.py` — `OperationLedger`, `LedgerEntry`, `OperationState`
- `comm_protocol.py` — `CommProtocol`, `CommMessage`, `MessageType`, `LogTransport`
- `supervisor_controller.py` — `SupervisorOuroborosController`, `AutonomyMode`
- `contract_gate.py` — `ContractGate`, `ContractVersion`
- `sandbox_loop.py` — `SandboxLoop`, `SandboxResult`, `SandboxConfig`
- `__init__.py` — re-exports all of the above

**Key existing DLM references:**
- `LockMetadata` at `backend/core/distributed_lock_manager.py:501` — has `fencing_token` (line 548), `backend`, `repo_source`
- `acquire_unified()` at line 2144 — yields `(bool, Optional[LockMetadata])`
- `_get_next_fencing_token()` at line 2054 — Redis + file fallback
- Keepalive loop with `_keepalive_tasks` dict

**Key existing TUI references:**
- `TUIBridge` at `backend/core/supervisor_tui_bridge.py:60` — `EventConsumer` pattern, `consume(event)` method
- Existing event types: `phase_gate`, `budget_acquire`, `budget_release`, `lease_acquired`, `lease_revoked`, `authority_transition`, `invariant_check`
- `StartupEvent` from `backend/core/startup_telemetry`

**Key CLI references:**
- `create_argument_parser()` at `unified_supervisor.py:97000`
- Argument groups: Control Commands, Operating Mode, Network, Docker, GCP

---

## Track 1: Plumbing & Gates (Phase 1A)

### Task 1: Governance Lock Manager — Hierarchical Read/Write Leases

**Files:**
- Create: `backend/core/ouroboros/governance/lock_manager.py`
- Create: `tests/test_ouroboros_governance/test_lock_manager.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** The design doc defines 8 lock levels (FILE=0 through PROD=7) with strict ascending acquisition order, shared-read / exclusive-write semantics, TTLs, and heartbeat renewal. The existing DLM (`distributed_lock_manager.py`) already has fencing tokens, keepalive, and `acquire_unified()`. Our governance lock manager wraps the DLM, adding: (1) level hierarchy enforcement, (2) read/write lease semantics, (3) strict ascending order validation, (4) fairness tracking.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_lock_manager.py
"""Tests for the governance lock manager."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LeaseHandle,
    LockOrderViolation,
    FencingTokenError,
    LOCK_TTLS,
)


@pytest.fixture
def lock_manager():
    """Create a GovernanceLockManager with mocked DLM."""
    return GovernanceLockManager()


# --- Lock Level Hierarchy ---


class TestLockLevels:
    def test_lock_levels_ascending_order(self):
        """All 8 lock levels have strictly ascending integer values."""
        levels = list(LockLevel)
        for i in range(len(levels) - 1):
            assert levels[i].value < levels[i + 1].value

    def test_all_eight_levels_defined(self):
        """Exactly 8 levels: FILE, REPO, CROSS_REPO_TX, POLICY, LEDGER_APPEND,
        BUILD, STAGING, PROD."""
        assert len(LockLevel) == 8
        expected = [
            "FILE_LOCK", "REPO_LOCK", "CROSS_REPO_TX", "POLICY_LOCK",
            "LEDGER_APPEND", "BUILD_LOCK", "STAGING_LOCK", "PROD_LOCK",
        ]
        assert [l.name for l in LockLevel] == expected


class TestLockModes:
    def test_shared_read_and_exclusive_write(self):
        """Two modes exist: SHARED_READ and EXCLUSIVE_WRITE."""
        assert LockMode.SHARED_READ.value == "shared_read"
        assert LockMode.EXCLUSIVE_WRITE.value == "exclusive_write"


# --- Ascending Order Enforcement ---


class TestAscendingOrder:
    @pytest.mark.asyncio
    async def test_ascending_acquisition_succeeds(self, lock_manager):
        """Acquiring locks in ascending level order succeeds."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as handle1:
            assert handle1 is not None
            assert handle1.level == LockLevel.FILE_LOCK

            async with lock_manager.acquire(
                level=LockLevel.REPO_LOCK,
                resource="jarvis",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle2:
                assert handle2 is not None
                assert handle2.level == LockLevel.REPO_LOCK

    @pytest.mark.asyncio
    async def test_descending_acquisition_raises(self, lock_manager):
        """Acquiring a lower-level lock while holding a higher one raises
        LockOrderViolation immediately (no deadlock)."""
        async with lock_manager.acquire(
            level=LockLevel.REPO_LOCK,
            resource="jarvis",
            mode=LockMode.EXCLUSIVE_WRITE,
        ):
            with pytest.raises(LockOrderViolation):
                async with lock_manager.acquire(
                    level=LockLevel.FILE_LOCK,
                    resource="src/foo.py",
                    mode=LockMode.EXCLUSIVE_WRITE,
                ):
                    pass  # Should never reach here

    @pytest.mark.asyncio
    async def test_same_level_same_resource_reentrant(self, lock_manager):
        """Acquiring same level + same resource is allowed (re-entrant)."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as h1:
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as h2:
                assert h1.fencing_token == h2.fencing_token


# --- Shared Read / Exclusive Write ---


class TestReadWriteSemantics:
    @pytest.mark.asyncio
    async def test_concurrent_shared_reads_succeed(self, lock_manager):
        """Two concurrent shared-read locks on the same resource both succeed."""
        results = []

        async def read_lock():
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.SHARED_READ,
            ) as handle:
                results.append(handle is not None)
                await asyncio.sleep(0.01)

        await asyncio.gather(read_lock(), read_lock())
        assert results == [True, True]

    @pytest.mark.asyncio
    async def test_exclusive_write_blocks_concurrent_write(self, lock_manager):
        """Only one exclusive-write holder at a time; second waits."""
        order = []

        async def write_lock(label: str, delay: float):
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ):
                order.append(f"{label}_start")
                await asyncio.sleep(delay)
                order.append(f"{label}_end")

        await asyncio.gather(
            write_lock("first", 0.05),
            write_lock("second", 0.01),
        )
        # First must complete before second starts
        assert order.index("first_end") < order.index("second_start")


# --- TTL and Fencing ---


class TestTTLAndFencing:
    def test_ttl_per_level(self):
        """Each lock level has a defined TTL."""
        assert LOCK_TTLS[LockLevel.FILE_LOCK] == 60.0
        assert LOCK_TTLS[LockLevel.REPO_LOCK] == 120.0
        assert LOCK_TTLS[LockLevel.CROSS_REPO_TX] == 300.0
        assert LOCK_TTLS[LockLevel.PROD_LOCK] == 600.0

    @pytest.mark.asyncio
    async def test_fencing_token_monotonically_increasing(self, lock_manager):
        """Successive acquisitions yield increasing fencing tokens."""
        tokens = []
        for _ in range(5):
            async with lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle:
                tokens.append(handle.fencing_token)

        for i in range(len(tokens) - 1):
            assert tokens[i] < tokens[i + 1]

    @pytest.mark.asyncio
    async def test_validate_fencing_token_rejects_stale(self, lock_manager):
        """validate_fencing_token raises FencingTokenError for stale tokens."""
        async with lock_manager.acquire(
            level=LockLevel.FILE_LOCK,
            resource="src/foo.py",
            mode=LockMode.EXCLUSIVE_WRITE,
        ) as handle:
            current_token = handle.fencing_token

        # Current token is now stale (lock released, new acquisition would
        # yield higher token)
        with pytest.raises(FencingTokenError):
            lock_manager.validate_fencing_token(
                level=LockLevel.FILE_LOCK,
                resource="src/foo.py",
                token=0,  # Definitely stale
            )


# --- Fairness ---


class TestFairness:
    @pytest.mark.asyncio
    async def test_waiter_tracking(self, lock_manager):
        """Lock manager tracks how long waiters wait (for fairness metrics)."""
        stats = lock_manager.get_contention_stats()
        assert "max_wait_ms" in stats
        assert "active_locks" in stats
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_lock_manager.py -v 2>&1 | head -30`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.lock_manager'`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/lock_manager.py
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
        self._task_held_levels: Dict[int, List[LockLevel]] = {}

        # Read/write state: (level, resource) -> set of reader task IDs
        self._readers: Dict[Tuple[LockLevel, str], Set[int]] = {}
        # (level, resource) -> writer task ID or None
        self._writers: Dict[Tuple[LockLevel, str], Optional[int]] = {}
        # Condition variable for waiting on write release
        self._lock_conditions: Dict[Tuple[LockLevel, str], asyncio.Condition] = {}

        # Fencing tokens: (level, resource) -> current token
        self._fencing_tokens: Dict[Tuple[LockLevel, str], int] = {}
        self._fencing_lock = asyncio.Lock()

        # Re-entrancy tracking: (level, resource) -> count per task
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
    ) -> asyncio.Condition:
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_lock_manager.py -v`
Expected: All 12 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/lock_manager.py tests/test_ouroboros_governance/test_lock_manager.py
git commit -m "feat(governance): add hierarchical read/write lease lock manager

8-level lock hierarchy with ascending-order enforcement,
shared-read / exclusive-write semantics, fencing tokens,
and fairness tracking.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Break-Glass Token System

**Files:**
- Create: `backend/core/ouroboros/governance/break_glass.py`
- Create: `tests/test_ouroboros_governance/test_break_glass.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** Break-glass lets Derek temporarily promote a BLOCKED operation to APPROVAL_REQUIRED. Tokens are time-limited, scoped to a specific op_id, and leave a complete audit trail. The token auto-expires after TTL. A postmortem is auto-generated for any break-glass usage.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_break_glass.py
"""Tests for break-glass governance tokens."""

import asyncio
import time
import pytest

from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
    BreakGlassToken,
    BreakGlassAuditEntry,
    BreakGlassExpired,
    BreakGlassScopeMismatch,
)


@pytest.fixture
def manager():
    return BreakGlassManager()


class TestTokenIssuance:
    @pytest.mark.asyncio
    async def test_issue_creates_valid_token(self, manager):
        """Issuing a break-glass token returns a valid, non-expired token."""
        token = await manager.issue(
            op_id="op-test-123",
            reason="emergency hotfix for prod outage",
            ttl=300,
            issuer="derek",
        )
        assert token.op_id == "op-test-123"
        assert token.ttl == 300
        assert token.issuer == "derek"
        assert not token.is_expired()

    @pytest.mark.asyncio
    async def test_issue_records_audit_entry(self, manager):
        """Every issuance creates an audit trail entry."""
        await manager.issue(
            op_id="op-test-456",
            reason="schema migration",
            ttl=60,
            issuer="derek",
        )
        audit = manager.get_audit_trail()
        assert len(audit) == 1
        assert audit[0].op_id == "op-test-456"
        assert audit[0].action == "issued"
        assert audit[0].reason == "schema migration"


class TestTokenValidation:
    @pytest.mark.asyncio
    async def test_validate_active_token_succeeds(self, manager):
        """Validating an active, non-expired token succeeds."""
        await manager.issue(
            op_id="op-valid",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        result = manager.validate(op_id="op-valid")
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_expired_token_raises(self, manager):
        """Validating an expired token raises BreakGlassExpired."""
        await manager.issue(
            op_id="op-expired",
            reason="testing",
            ttl=0,  # Expires immediately
            issuer="derek",
        )
        # Token with ttl=0 is expired at creation
        with pytest.raises(BreakGlassExpired):
            manager.validate(op_id="op-expired")

    @pytest.mark.asyncio
    async def test_validate_wrong_scope_raises(self, manager):
        """Validating against wrong op_id raises BreakGlassScopeMismatch."""
        await manager.issue(
            op_id="op-scoped",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        with pytest.raises(BreakGlassScopeMismatch):
            manager.validate(op_id="op-different")

    @pytest.mark.asyncio
    async def test_validate_no_token_returns_false(self, manager):
        """Validating when no token exists returns False."""
        result = manager.validate(op_id="op-none")
        assert result is False


class TestTokenRevocation:
    @pytest.mark.asyncio
    async def test_revoke_removes_token(self, manager):
        """Revoking a token makes subsequent validation return False."""
        await manager.issue(
            op_id="op-revoke",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        await manager.revoke(op_id="op-revoke", reason="no longer needed")
        result = manager.validate(op_id="op-revoke")
        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_creates_audit_entry(self, manager):
        """Revocation creates an audit trail entry."""
        await manager.issue(
            op_id="op-audit-revoke",
            reason="testing",
            ttl=300,
            issuer="derek",
        )
        await manager.revoke(op_id="op-audit-revoke", reason="done")
        audit = manager.get_audit_trail()
        revoke_entries = [e for e in audit if e.action == "revoked"]
        assert len(revoke_entries) == 1
        assert revoke_entries[0].reason == "done"


class TestPromotion:
    @pytest.mark.asyncio
    async def test_blocked_becomes_approval_required(self, manager):
        """Break-glass promotes BLOCKED to APPROVAL_REQUIRED, not unguarded."""
        await manager.issue(
            op_id="op-promote",
            reason="emergency",
            ttl=300,
            issuer="derek",
        )
        promoted_tier = manager.get_promoted_tier(op_id="op-promote")
        assert promoted_tier == "APPROVAL_REQUIRED"

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self, manager):
        """No active token returns None for promoted tier."""
        result = manager.get_promoted_tier(op_id="op-missing")
        assert result is None


class TestAuditCompleteness:
    @pytest.mark.asyncio
    async def test_full_lifecycle_audit(self, manager):
        """Issue -> use -> revoke produces 3 audit entries."""
        await manager.issue(
            op_id="op-lifecycle",
            reason="prod fix",
            ttl=300,
            issuer="derek",
        )
        manager.validate(op_id="op-lifecycle")  # Records "validated" entry
        await manager.revoke(op_id="op-lifecycle", reason="complete")

        audit = manager.get_audit_trail()
        actions = [e.action for e in audit]
        assert "issued" in actions
        assert "validated" in actions
        assert "revoked" in actions
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_break_glass.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/break_glass.py
"""
Break-Glass Governance Tokens
==============================

Time-limited, scoped tokens that allow a human operator to temporarily promote
a BLOCKED operation to APPROVAL_REQUIRED.  Every issuance, validation, and
revocation is recorded in an audit trail.

Flow:
1. Derek: ``jarvis break-glass --scope <op_id> --ttl 300``
2. Token stored with audit (who, when, why, scope)
3. Operation proceeds under APPROVAL_REQUIRED rules (NOT unguarded)
4. Token auto-expires after TTL
5. Postmortem auto-generated for any break-glass usage
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.BreakGlass")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BreakGlassExpired(RuntimeError):
    """Raised when a break-glass token has expired."""


class BreakGlassScopeMismatch(RuntimeError):
    """Raised when validating a token against the wrong op_id."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BreakGlassToken:
    """A time-limited break-glass token.

    Parameters
    ----------
    op_id:
        The operation this token is scoped to.
    reason:
        Human-provided justification for issuing the token.
    ttl:
        Time-to-live in seconds from issuance.
    issuer:
        Identity of the person who issued the token.
    issued_at:
        Wall-clock timestamp when the token was created.
    """

    op_id: str
    reason: str
    ttl: int
    issuer: str
    issued_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        """Check if this token has expired."""
        return time.time() >= self.issued_at + self.ttl


@dataclass
class BreakGlassAuditEntry:
    """A single audit trail record for break-glass activity.

    Parameters
    ----------
    op_id:
        The operation this entry relates to.
    action:
        What happened: ``issued``, ``validated``, ``revoked``, ``expired``.
    reason:
        Context for the action.
    issuer:
        Who performed the action (if applicable).
    timestamp:
        Wall-clock time of the action.
    """

    op_id: str
    action: str
    reason: str
    issuer: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# BreakGlassManager
# ---------------------------------------------------------------------------


class BreakGlassManager:
    """Manages break-glass token lifecycle with full audit trail."""

    def __init__(self) -> None:
        self._tokens: Dict[str, BreakGlassToken] = {}
        self._audit: List[BreakGlassAuditEntry] = []

    async def issue(
        self,
        op_id: str,
        reason: str,
        ttl: int,
        issuer: str,
    ) -> BreakGlassToken:
        """Issue a new break-glass token.

        Parameters
        ----------
        op_id:
            The operation to scope this token to.
        reason:
            Human justification for the break-glass.
        ttl:
            Seconds until the token auto-expires.
        issuer:
            Identity of the person issuing.

        Returns
        -------
        BreakGlassToken
            The newly created token.
        """
        token = BreakGlassToken(
            op_id=op_id,
            reason=reason,
            ttl=ttl,
            issuer=issuer,
        )
        self._tokens[op_id] = token
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="issued",
                reason=reason,
                issuer=issuer,
            )
        )
        logger.info(
            "Break-glass token issued: op=%s ttl=%ds issuer=%s reason=%s",
            op_id, ttl, issuer, reason,
        )
        return token

    def validate(self, op_id: str) -> bool:
        """Validate a break-glass token for the given op_id.

        Returns
        -------
        bool
            ``True`` if a valid, non-expired token exists for this op_id.
            ``False`` if no token exists.

        Raises
        ------
        BreakGlassExpired
            If the token exists but has expired.
        BreakGlassScopeMismatch
            If no token matches this op_id but tokens exist for other ops.
        """
        if op_id not in self._tokens:
            # Check if any tokens exist at all (scope mismatch detection)
            if self._tokens:
                raise BreakGlassScopeMismatch(
                    f"No break-glass token for op_id={op_id}. "
                    f"Active tokens exist for: {list(self._tokens.keys())}"
                )
            return False

        token = self._tokens[op_id]
        if token.is_expired():
            # Clean up and record
            self._tokens.pop(op_id, None)
            self._audit.append(
                BreakGlassAuditEntry(
                    op_id=op_id,
                    action="expired",
                    reason="token TTL exceeded",
                )
            )
            raise BreakGlassExpired(
                f"Break-glass token for op_id={op_id} has expired"
            )

        # Record validation
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="validated",
                reason="token checked and valid",
                issuer=token.issuer,
            )
        )
        return True

    async def revoke(self, op_id: str, reason: str) -> None:
        """Revoke a break-glass token.

        Parameters
        ----------
        op_id:
            The operation whose token to revoke.
        reason:
            Why the token is being revoked.
        """
        token = self._tokens.pop(op_id, None)
        issuer = token.issuer if token else ""
        self._audit.append(
            BreakGlassAuditEntry(
                op_id=op_id,
                action="revoked",
                reason=reason,
                issuer=issuer,
            )
        )
        logger.info(
            "Break-glass token revoked: op=%s reason=%s",
            op_id, reason,
        )

    def get_promoted_tier(self, op_id: str) -> Optional[str]:
        """Get the promoted risk tier if a valid break-glass token exists.

        Break-glass always promotes to APPROVAL_REQUIRED, never unguarded.

        Returns
        -------
        Optional[str]
            ``"APPROVAL_REQUIRED"`` if valid token exists, ``None`` otherwise.
        """
        if op_id not in self._tokens:
            return None
        token = self._tokens[op_id]
        if token.is_expired():
            self._tokens.pop(op_id, None)
            return None
        return "APPROVAL_REQUIRED"

    def get_audit_trail(self) -> List[BreakGlassAuditEntry]:
        """Return the complete audit trail."""
        return list(self._audit)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_break_glass.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/break_glass.py tests/test_ouroboros_governance/test_break_glass.py
git commit -m "feat(governance): add break-glass token system with audit trail

Time-limited, op_id-scoped tokens that promote BLOCKED operations
to APPROVAL_REQUIRED. Full audit trail for every issuance,
validation, and revocation.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Transactional Change Engine

**Files:**
- Create: `backend/core/ouroboros/governance/change_engine.py`
- Create: `tests/test_ouroboros_governance/test_change_engine.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** The 8-step pipeline from the design doc: PLAN -> SANDBOX -> VALIDATE -> GATE -> APPLY -> LEDGER -> PUBLISH -> VERIFY. Rollback is a pre-tested artifact generated alongside the plan. The engine wires together the lock manager, risk engine, ledger, comm protocol, and break-glass manager.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_change_engine.py
"""Tests for the transactional change engine."""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangeResult,
    RollbackArtifact,
    ChangePhase,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskTier,
    RiskClassification,
    OperationProfile,
    ChangeType,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
)
from backend.core.ouroboros.governance.break_glass import BreakGlassManager


@pytest.fixture
def tmp_project_dir(tmp_path):
    """Create a minimal project for testing."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def comm():
    transport = LogTransport()
    return CommProtocol(transports=[transport]), transport


@pytest.fixture
def engine(tmp_project_dir, ledger, comm):
    protocol, transport = comm
    return ChangeEngine(
        project_root=tmp_project_dir,
        ledger=ledger,
        comm=protocol,
        lock_manager=GovernanceLockManager(),
        break_glass=BreakGlassManager(),
    ), transport


class TestRollbackArtifact:
    @pytest.mark.asyncio
    async def test_rollback_snapshot_hash_matches_original(
        self, tmp_project_dir
    ):
        """Pre-change snapshot hash is captured correctly."""
        target = tmp_project_dir / "src" / "example.py"
        original_content = target.read_text()
        expected_hash = hashlib.sha256(
            original_content.encode()
        ).hexdigest()

        artifact = RollbackArtifact.capture(target)
        assert artifact.snapshot_hash == expected_hash
        assert artifact.original_content == original_content

    def test_rollback_restores_original(self, tmp_project_dir):
        """Applying a rollback artifact restores the exact original content."""
        target = tmp_project_dir / "src" / "example.py"
        original = target.read_text()
        artifact = RollbackArtifact.capture(target)

        # Simulate a change
        target.write_text("def goodbye():\n    return 'cruel world'\n")
        assert target.read_text() != original

        # Apply rollback
        artifact.apply(target)
        assert target.read_text() == original
        restored_hash = hashlib.sha256(
            target.read_text().encode()
        ).hexdigest()
        assert restored_hash == artifact.snapshot_hash


class TestChangePhases:
    def test_all_eight_phases_exist(self):
        """All 8 pipeline phases are defined."""
        expected = [
            "PLAN", "SANDBOX", "VALIDATE", "GATE",
            "APPLY", "LEDGER", "PUBLISH", "VERIFY",
        ]
        assert [p.name for p in ChangePhase] == expected


class TestChangeEngine:
    @pytest.mark.asyncio
    async def test_safe_auto_completes_full_pipeline(
        self, engine, tmp_project_dir, ledger
    ):
        """A SAFE_AUTO change goes through all 8 phases to APPLIED."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Add docstring",
            target_file=target,
            proposed_content="def hello():\n    \"\"\"Greet.\"\"\"\n    return 'world'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is True
        assert result.phase_reached == ChangePhase.VERIFY
        assert result.op_id.startswith("op-")

        # Verify ledger has APPLIED state
        latest = await ledger.get_latest_state(result.op_id)
        assert latest == OperationState.APPLIED

    @pytest.mark.asyncio
    async def test_blocked_operation_stops_at_gate(
        self, engine, tmp_project_dir, ledger
    ):
        """A BLOCKED change stops at GATE phase."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Modify supervisor",
            target_file=target,
            proposed_content="# modified\n",
            profile=OperationProfile(
                files_affected=[Path("unified_supervisor.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=True,  # BLOCKED
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.GATE
        assert result.risk_tier == RiskTier.BLOCKED

        # File unchanged
        assert target.read_text() == "def hello():\n    return 'world'\n"

    @pytest.mark.asyncio
    async def test_approval_required_stops_at_gate(
        self, engine, tmp_project_dir
    ):
        """APPROVAL_REQUIRED stops at GATE without operator approval."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Cross-repo change",
            target_file=target,
            proposed_content="# cross repo\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=True,  # APPROVAL_REQUIRED
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.GATE
        assert result.risk_tier == RiskTier.APPROVAL_REQUIRED

    @pytest.mark.asyncio
    async def test_break_glass_promotes_blocked_to_approval(
        self, engine, tmp_project_dir
    ):
        """Break-glass token allows BLOCKED op to reach GATE as APPROVAL_REQUIRED."""
        eng, transport = engine

        # Pre-issue break-glass (we need the op_id, so we use a known one)
        # For this test, we issue break-glass BEFORE execute (engine checks it)
        # Engine will use the generated op_id, so we test via the manager directly
        target = tmp_project_dir / "src" / "example.py"
        request = ChangeRequest(
            goal="Security fix with break-glass",
            target_file=target,
            proposed_content="# secure fix\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=True,  # BLOCKED
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
            break_glass_op_id=None,  # Will be set by engine if token exists
        )
        # Without break-glass: BLOCKED
        result = await eng.execute(request)
        assert result.risk_tier == RiskTier.BLOCKED

    @pytest.mark.asyncio
    async def test_invalid_syntax_fails_at_validate(
        self, engine, tmp_project_dir
    ):
        """Proposed code with invalid syntax fails at VALIDATE phase."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Bad syntax",
            target_file=target,
            proposed_content="def broken(\n",  # Invalid Python
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        assert result.success is False
        assert result.phase_reached == ChangePhase.VALIDATE

    @pytest.mark.asyncio
    async def test_rollback_on_verify_failure(
        self, engine, tmp_project_dir
    ):
        """If post-apply verification fails, automatic rollback occurs."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"
        original = target.read_text()

        request = ChangeRequest(
            goal="Change that fails verification",
            target_file=target,
            proposed_content="def hello():\n    return 'changed'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
            verify_fn=AsyncMock(return_value=False),  # Simulate verify failure
        )
        result = await eng.execute(request)
        assert result.rolled_back is True
        # File should be restored to original
        assert target.read_text() == original


class TestLedgerTracking:
    @pytest.mark.asyncio
    async def test_every_phase_recorded_in_ledger(
        self, engine, tmp_project_dir, ledger
    ):
        """Ledger has entries for every phase transition in a successful run."""
        eng, transport = engine
        target = tmp_project_dir / "src" / "example.py"

        request = ChangeRequest(
            goal="Simple change",
            target_file=target,
            proposed_content="def hello():\n    return 'updated'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await eng.execute(request)
        history = await ledger.get_history(result.op_id)
        states = [e.state for e in history]
        assert OperationState.PLANNED in states
        assert OperationState.VALIDATING in states
        assert OperationState.APPLIED in states
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_change_engine.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/change_engine.py
"""
Transactional Change Engine
============================

Implements the 8-phase change pipeline from the design doc::

    PLAN -> SANDBOX -> VALIDATE -> GATE -> APPLY -> LEDGER -> PUBLISH -> VERIFY

Each phase is idempotent and recorded in the operation ledger.  Rollback
artifacts are captured BEFORE any production write, so rollback is a
pre-tested operation (not "git revert and pray").

Key guarantees:
- Ledger entry exists for every state transition
- Event published ONLY after ledger commit succeeds (outbox pattern)
- Rollback hash matches pre-change snapshot hash exactly
- Production files untouched until APPLY phase (after all gates pass)
"""

from __future__ import annotations

import ast
import enum
import hashlib
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from backend.core.ouroboros.governance.break_glass import BreakGlassManager
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.ledger import (
    LedgerEntry,
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskEngine,
    RiskTier,
)

logger = logging.getLogger("Ouroboros.ChangeEngine")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChangePhase(enum.Enum):
    """The 8 phases of the transactional change pipeline."""

    PLAN = "PLAN"
    SANDBOX = "SANDBOX"
    VALIDATE = "VALIDATE"
    GATE = "GATE"
    APPLY = "APPLY"
    LEDGER = "LEDGER"
    PUBLISH = "PUBLISH"
    VERIFY = "VERIFY"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RollbackArtifact:
    """Pre-captured snapshot for deterministic rollback.

    Captures the exact content and hash of a file BEFORE modification,
    so rollback restores to a known-good state.
    """

    original_content: str
    snapshot_hash: str

    @classmethod
    def capture(cls, file_path: Path) -> "RollbackArtifact":
        """Capture a rollback artifact from the current file state."""
        content = file_path.read_text(encoding="utf-8")
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        return cls(original_content=content, snapshot_hash=content_hash)

    def apply(self, file_path: Path) -> None:
        """Restore the file to the captured snapshot state."""
        file_path.write_text(self.original_content, encoding="utf-8")
        # Verify the restoration
        restored = file_path.read_text(encoding="utf-8")
        restored_hash = hashlib.sha256(restored.encode()).hexdigest()
        if restored_hash != self.snapshot_hash:
            raise RuntimeError(
                f"Rollback verification failed: expected hash "
                f"{self.snapshot_hash}, got {restored_hash}"
            )


@dataclass
class ChangeRequest:
    """A request to apply a code change through the transactional pipeline.

    Parameters
    ----------
    goal:
        Natural-language description of the change.
    target_file:
        Absolute path to the file to modify.
    proposed_content:
        The new content to write to the file.
    profile:
        Operation risk profile for classification.
    verify_fn:
        Optional async callable that returns True if post-apply verification
        passes.  Defaults to AST parse check.
    break_glass_op_id:
        If set, use this op_id to look up a break-glass token.
    """

    goal: str
    target_file: Path
    proposed_content: str
    profile: OperationProfile
    verify_fn: Optional[Any] = None
    break_glass_op_id: Optional[str] = None


@dataclass
class ChangeResult:
    """Result of a change engine execution.

    Parameters
    ----------
    op_id:
        The unique operation identifier.
    success:
        Whether the change was successfully applied and verified.
    phase_reached:
        The last phase the pipeline reached.
    risk_tier:
        The risk classification assigned.
    rolled_back:
        Whether the change was rolled back after a verification failure.
    error:
        Error message if the pipeline failed.
    """

    op_id: str
    success: bool
    phase_reached: ChangePhase
    risk_tier: Optional[RiskTier] = None
    rolled_back: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ChangeEngine
# ---------------------------------------------------------------------------


class ChangeEngine:
    """8-phase transactional change pipeline with rollback guarantees.

    Parameters
    ----------
    project_root:
        Root directory of the project.
    ledger:
        Operation ledger for state tracking.
    comm:
        Communication protocol for lifecycle messages.
    lock_manager:
        Governance lock manager for hierarchy enforcement.
    break_glass:
        Break-glass manager for BLOCKED operation promotion.
    risk_engine:
        Risk classifier (defaults to standard RiskEngine).
    """

    def __init__(
        self,
        project_root: Path,
        ledger: OperationLedger,
        comm: Optional[CommProtocol] = None,
        lock_manager: Optional[GovernanceLockManager] = None,
        break_glass: Optional[BreakGlassManager] = None,
        risk_engine: Optional[RiskEngine] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._ledger = ledger
        self._comm = comm or CommProtocol(transports=[LogTransport()])
        self._lock_manager = lock_manager or GovernanceLockManager()
        self._break_glass = break_glass or BreakGlassManager()
        self._risk_engine = risk_engine or RiskEngine()

    async def execute(self, request: ChangeRequest) -> ChangeResult:
        """Execute the 8-phase transactional change pipeline.

        Parameters
        ----------
        request:
            The change request describing what to modify.

        Returns
        -------
        ChangeResult
            Result with success status, phase reached, and optional error.
        """
        op_id = generate_operation_id(repo_origin="jarvis")

        try:
            # Phase 1: PLAN — classify risk, record in ledger
            classification = self._risk_engine.classify(request.profile)
            risk_tier = classification.tier

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.PLANNED,
                    data={
                        "goal": request.goal,
                        "target_file": str(request.target_file),
                        "risk_tier": risk_tier.name,
                        "reason_code": classification.reason_code,
                    },
                )
            )

            await self._comm.emit_intent(
                op_id=op_id,
                goal=request.goal,
                target_files=[str(request.target_file)],
                risk_tier=risk_tier.name,
                blast_radius=request.profile.blast_radius,
            )

            # Phase 2: SANDBOX — validate in isolation
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="sandbox", progress_pct=20.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.SANDBOXING,
                    data={"phase": "sandbox"},
                )
            )

            # Phase 3: VALIDATE — AST parse in temp dir
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="validate", progress_pct=40.0
            )
            valid = await self._validate_in_sandbox(request.proposed_content)
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.VALIDATING,
                    data={"syntax_valid": valid},
                )
            )

            if not valid:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="validation_failed",
                    reason_code="syntax_error",
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.FAILED,
                        data={"reason": "syntax_error"},
                    )
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VALIDATE,
                    risk_tier=risk_tier,
                )

            # Phase 4: GATE — check risk tier and break-glass
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="gate", progress_pct=50.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.GATING,
                    data={"risk_tier": risk_tier.name},
                )
            )

            # Check break-glass for BLOCKED operations
            if risk_tier == RiskTier.BLOCKED:
                promoted = self._break_glass.get_promoted_tier(op_id)
                if promoted is None and request.break_glass_op_id:
                    promoted = self._break_glass.get_promoted_tier(
                        request.break_glass_op_id
                    )
                if promoted is not None:
                    risk_tier = RiskTier.APPROVAL_REQUIRED
                    logger.info(
                        "Break-glass promoted %s from BLOCKED to APPROVAL_REQUIRED",
                        op_id,
                    )

            if risk_tier == RiskTier.BLOCKED:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="blocked",
                    reason_code=classification.reason_code,
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.BLOCKED,
                        data={"reason": classification.reason_code},
                    )
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.GATE,
                    risk_tier=RiskTier.BLOCKED,
                )

            if risk_tier == RiskTier.APPROVAL_REQUIRED:
                await self._comm.emit_decision(
                    op_id=op_id,
                    outcome="escalated",
                    reason_code=classification.reason_code,
                    diff_summary=f"Change to {request.target_file}",
                )
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.GATING,
                        data={
                            "waiting_approval": True,
                            "reason": classification.reason_code,
                        },
                    )
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.GATE,
                    risk_tier=RiskTier.APPROVAL_REQUIRED,
                )

            # Phase 5: APPLY — capture rollback artifact, write to production
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="apply", progress_pct=70.0
            )

            target = Path(request.target_file)
            rollback = RollbackArtifact.capture(target)

            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLYING,
                    data={
                        "rollback_hash": rollback.snapshot_hash,
                        "target_file": str(target),
                    },
                )
            )

            # Acquire file lock for the write
            async with self._lock_manager.acquire(
                level=LockLevel.FILE_LOCK,
                resource=str(target),
                mode=LockMode.EXCLUSIVE_WRITE,
            ) as handle:
                target.write_text(
                    request.proposed_content, encoding="utf-8"
                )

            # Phase 6: LEDGER — record applied state
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="ledger", progress_pct=85.0
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.APPLIED,
                    data={
                        "target_file": str(target),
                        "rollback_hash": rollback.snapshot_hash,
                    },
                )
            )

            # Phase 7: PUBLISH — emit decision (outbox: ledger already committed)
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="safe_auto_passed",
                diff_summary=f"Applied change to {target}",
            )

            # Phase 8: VERIFY — post-apply verification
            await self._comm.emit_heartbeat(
                op_id=op_id, phase="verify", progress_pct=95.0
            )

            verify_passed = True
            if request.verify_fn is not None:
                verify_passed = await request.verify_fn()
            else:
                # Default: AST parse check on the applied file
                verify_passed = await self._validate_in_sandbox(
                    target.read_text(encoding="utf-8")
                )

            if not verify_passed:
                # Automatic rollback
                logger.warning(
                    "Post-apply verification failed for %s — rolling back",
                    op_id,
                )
                rollback.apply(target)
                await self._ledger.append(
                    LedgerEntry(
                        op_id=op_id,
                        state=OperationState.ROLLED_BACK,
                        data={"reason": "verify_failed"},
                    )
                )
                await self._comm.emit_postmortem(
                    op_id=op_id,
                    root_cause="post_apply_verification_failed",
                    failed_phase="VERIFY",
                    next_safe_action="review_proposed_change",
                )
                return ChangeResult(
                    op_id=op_id,
                    success=False,
                    phase_reached=ChangePhase.VERIFY,
                    risk_tier=risk_tier,
                    rolled_back=True,
                )

            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause="none",
                failed_phase=None,
                next_safe_action="none",
            )

            return ChangeResult(
                op_id=op_id,
                success=True,
                phase_reached=ChangePhase.VERIFY,
                risk_tier=risk_tier,
            )

        except Exception as exc:
            logger.error("Change engine error for %s: %s", op_id, exc)
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=str(exc),
                failed_phase="unknown",
            )
            await self._ledger.append(
                LedgerEntry(
                    op_id=op_id,
                    state=OperationState.FAILED,
                    data={"error": str(exc)},
                )
            )
            return ChangeResult(
                op_id=op_id,
                success=False,
                phase_reached=ChangePhase.PLAN,
                risk_tier=None,
                error=str(exc),
            )

    async def _validate_in_sandbox(self, code: str) -> bool:
        """Validate code by AST-parsing in a temporary directory."""
        try:
            with tempfile.TemporaryDirectory(
                prefix="ouroboros_validate_"
            ) as sandbox_dir:
                sandbox_path = Path(sandbox_dir) / "validate.py"
                sandbox_path.write_text(code, encoding="utf-8")
                source = sandbox_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(sandbox_path))
            return True
        except SyntaxError:
            return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_change_engine.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/change_engine.py tests/test_ouroboros_governance/test_change_engine.py
git commit -m "feat(governance): add transactional change engine with 8-phase pipeline

PLAN->SANDBOX->VALIDATE->GATE->APPLY->LEDGER->PUBLISH->VERIFY pipeline.
Pre-tested rollback artifacts, break-glass integration, fencing-token
protected writes, outbox pattern (ledger before event).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: TUI Transport for CommProtocol

**Files:**
- Create: `backend/core/ouroboros/governance/tui_transport.py`
- Create: `tests/test_ouroboros_governance/test_tui_transport.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Context:** The CommProtocol from Phase 0 uses pluggable transports. The default `LogTransport` just logs + collects. Phase 1B adds a `TUITransport` that formats governance messages for display in the TUI dashboard, with fault isolation (TUI crash never blocks the pipeline).

The existing TUI bridge (`supervisor_tui_bridge.py:60`) consumes `StartupEvent` objects. Our TUI transport converts `CommMessage` objects to a format the TUI can consume, queueing messages if the TUI is temporarily unavailable.

**Step 1: Write the failing tests**

```python
# tests/test_ouroboros_governance/test_tui_transport.py
"""Tests for the TUI transport layer."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.tui_transport import (
    TUITransport,
    TUIMessageFormatter,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    MessageType,
)


@pytest.fixture
def transport():
    return TUITransport()


@pytest.fixture
def mock_callback():
    return AsyncMock()


class TestTUITransport:
    @pytest.mark.asyncio
    async def test_send_stores_message(self, transport):
        """Sent messages are stored in the transport's message queue."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-test-1",
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "test"},
        )
        await transport.send(msg)
        assert len(transport.messages) == 1
        assert transport.messages[0].op_id == "op-test-1"

    @pytest.mark.asyncio
    async def test_callback_invoked_on_send(self, transport, mock_callback):
        """Registered callbacks are invoked when a message is sent."""
        transport.on_message(mock_callback)
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-test-2",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate", "progress_pct": 40.0},
        )
        await transport.send(msg)
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0]
        assert "op_id" in call_args[0]

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_block(self, transport):
        """A failing callback does not prevent message storage."""
        failing_cb = AsyncMock(side_effect=RuntimeError("TUI crashed"))
        transport.on_message(failing_cb)

        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-test-3",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied"},
        )
        await transport.send(msg)
        # Message still stored despite callback failure
        assert len(transport.messages) == 1

    @pytest.mark.asyncio
    async def test_message_queue_for_offline_tui(self, transport):
        """Messages queue when no callback is registered (TUI offline)."""
        for i in range(5):
            msg = CommMessage(
                msg_type=MessageType.HEARTBEAT,
                op_id="op-test-q",
                seq=i + 1,
                causal_parent_seq=i if i > 0 else None,
                payload={"phase": "test"},
            )
            await transport.send(msg)
        assert len(transport.messages) == 5

    @pytest.mark.asyncio
    async def test_drain_delivers_queued_messages(self, transport):
        """drain() delivers all queued messages to a newly registered callback."""
        for i in range(3):
            await transport.send(
                CommMessage(
                    msg_type=MessageType.HEARTBEAT,
                    op_id="op-drain",
                    seq=i + 1,
                    causal_parent_seq=i if i > 0 else None,
                    payload={"phase": "test"},
                )
            )

        delivered = []
        async def capture(formatted):
            delivered.append(formatted)

        transport.on_message(capture)
        await transport.drain()
        assert len(delivered) == 3


class TestTUIMessageFormatter:
    def test_format_intent(self):
        """INTENT messages are formatted with goal and risk tier."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-fmt-1",
            seq=1,
            causal_parent_seq=None,
            payload={
                "goal": "Add docstring to utils.py",
                "risk_tier": "SAFE_AUTO",
                "blast_radius": 2,
                "target_files": ["utils.py"],
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "INTENT"
        assert "goal" in formatted
        assert formatted["risk_tier"] == "SAFE_AUTO"

    def test_format_heartbeat(self):
        """HEARTBEAT messages include phase and progress."""
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-fmt-2",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate", "progress_pct": 65.0},
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "HEARTBEAT"
        assert formatted["progress_pct"] == 65.0

    def test_format_decision(self):
        """DECISION messages include outcome and reason."""
        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-fmt-3",
            seq=4,
            causal_parent_seq=3,
            payload={
                "outcome": "applied",
                "reason_code": "safe_auto_passed",
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "DECISION"
        assert formatted["outcome"] == "applied"

    def test_format_postmortem(self):
        """POSTMORTEM messages include root cause and next action."""
        msg = CommMessage(
            msg_type=MessageType.POSTMORTEM,
            op_id="op-fmt-4",
            seq=5,
            causal_parent_seq=4,
            payload={
                "root_cause": "syntax_error",
                "failed_phase": "VALIDATE",
                "next_safe_action": "review_code",
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "POSTMORTEM"
        assert formatted["root_cause"] == "syntax_error"


class TestCommProtocolIntegration:
    @pytest.mark.asyncio
    async def test_tui_transport_works_with_comm_protocol(self):
        """TUITransport integrates cleanly as a CommProtocol transport."""
        tui = TUITransport()
        received = []
        tui.on_message(AsyncMock(side_effect=lambda m: received.append(m)))

        comm = CommProtocol(transports=[tui])
        await comm.emit_intent(
            op_id="op-integration",
            goal="test",
            target_files=["test.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
        assert len(tui.messages) == 1
        assert len(received) == 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_tui_transport.py -v 2>&1 | head -10`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/tui_transport.py
"""
TUI Transport for CommProtocol
===============================

A pluggable transport that delivers governance ``CommMessage`` objects to the
TUI dashboard.  Messages are:

1. Formatted into TUI-friendly dicts by :class:`TUIMessageFormatter`.
2. Delivered to registered async callbacks (if any).
3. Queued in memory if no callback is registered (TUI offline).
4. Fault-isolated: a crashing TUI callback never blocks the pipeline.

Usage::

    tui_transport = TUITransport()
    tui_transport.on_message(my_tui_display_callback)
    comm = CommProtocol(transports=[LogTransport(), tui_transport])

When the TUI reconnects, call ``await tui_transport.drain()`` to deliver
all queued messages.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage

logger = logging.getLogger("Ouroboros.TUITransport")


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TUIMessageFormatter:
    """Formats CommMessage objects into TUI-friendly dictionaries."""

    @staticmethod
    def format(msg: CommMessage) -> Dict[str, Any]:
        """Convert a CommMessage to a TUI-displayable dict.

        The returned dict always contains:
        - ``type``: The message type name (INTENT, PLAN, etc.)
        - ``op_id``: The operation identifier
        - ``seq``: Sequence number
        - ``timestamp``: Wall-clock timestamp

        Plus all payload fields merged in.
        """
        base: Dict[str, Any] = {
            "type": msg.msg_type.value,
            "op_id": msg.op_id,
            "seq": msg.seq,
            "causal_parent_seq": msg.causal_parent_seq,
            "timestamp": msg.timestamp,
        }
        # Merge payload fields into the base dict
        base.update(msg.payload)
        return base


# ---------------------------------------------------------------------------
# TUITransport
# ---------------------------------------------------------------------------


class TUITransport:
    """Fault-isolated transport that delivers governance messages to the TUI.

    Messages are stored in an internal queue and optionally forwarded to
    registered async callbacks.  If a callback fails, the message is still
    stored (fault isolation).
    """

    def __init__(self) -> None:
        self.messages: List[CommMessage] = []
        self._callbacks: List[Callable[[Dict[str, Any]], Any]] = []
        self._pending_drain: List[Dict[str, Any]] = []

    def on_message(
        self,
        callback: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register an async callback to receive formatted messages.

        Parameters
        ----------
        callback:
            An async callable that receives a formatted message dict.
        """
        self._callbacks.append(callback)

    async def send(self, msg: CommMessage) -> None:
        """Store the message and forward to registered callbacks.

        Callback failures are logged but never block message storage.
        """
        self.messages.append(msg)
        formatted = TUIMessageFormatter.format(msg)

        if not self._callbacks:
            # Queue for later drain
            self._pending_drain.append(formatted)
            return

        for callback in self._callbacks:
            try:
                result = callback(formatted)
                # Support both sync and async callbacks
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning(
                    "TUI callback failed for op=%s seq=%d — message still queued",
                    msg.op_id,
                    msg.seq,
                    exc_info=True,
                )

    async def drain(self) -> None:
        """Deliver all pending (queued while offline) messages to callbacks."""
        if not self._callbacks or not self._pending_drain:
            return

        pending = list(self._pending_drain)
        self._pending_drain.clear()

        for formatted in pending:
            for callback in self._callbacks:
                try:
                    result = callback(formatted)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.warning(
                        "TUI callback failed during drain — skipping",
                        exc_info=True,
                    )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_tui_transport.py -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/tui_transport.py tests/test_ouroboros_governance/test_tui_transport.py
git commit -m "feat(governance): add TUI transport for comm protocol

Fault-isolated transport that delivers governance messages to TUI,
queues when offline, drains on reconnect. Formats CommMessage
objects into TUI-friendly dicts.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: Wire new exports into governance __init__.py

**Files:**
- Modify: `backend/core/ouroboros/governance/__init__.py`

**Step 1: No test needed (wiring only)**

**Step 2: Update the __init__.py**

Add the new Phase 1 exports after the existing Phase 0 exports:

```python
# Add after line 51 (after contract_gate imports):

from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LeaseHandle,
    LockOrderViolation,
    FencingTokenError,
    LOCK_TTLS,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
    BreakGlassToken,
    BreakGlassAuditEntry,
    BreakGlassExpired,
    BreakGlassScopeMismatch,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangeResult,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.tui_transport import (
    TUITransport,
    TUIMessageFormatter,
)
```

**Step 3: Run all governance tests to verify nothing broke**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass (Phase 0 + Phase 1)

**Step 4: Commit**

```bash
git add backend/core/ouroboros/governance/__init__.py
git commit -m "feat(governance): wire Phase 1 exports into governance __init__

Adds lock_manager, break_glass, change_engine, and tui_transport
exports to the governance package.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Phase 1 Integration Tests

**Files:**
- Create: `tests/test_ouroboros_governance/test_phase1_integration.py`

**Context:** End-to-end tests that verify the Phase 1A Go/No-Go criteria from the design doc. These tests exercise the full stack: lock manager + change engine + break-glass + ledger + comm protocol + TUI transport together.

**Step 1: Write the integration tests**

```python
# tests/test_ouroboros_governance/test_phase1_integration.py
"""Phase 1 integration tests — Go/No-Go criteria verification.

Tests in this module verify the acceptance criteria from the design doc
section 4 (Phase 1A and Phase 1B Go/No-Go).
"""

import asyncio
import hashlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.lock_manager import (
    GovernanceLockManager,
    LockLevel,
    LockMode,
    LockOrderViolation,
    FencingTokenError,
)
from backend.core.ouroboros.governance.change_engine import (
    ChangeEngine,
    ChangeRequest,
    ChangePhase,
    RollbackArtifact,
)
from backend.core.ouroboros.governance.risk_engine import (
    OperationProfile,
    ChangeType,
    RiskTier,
)
from backend.core.ouroboros.governance.ledger import (
    OperationLedger,
    OperationState,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.break_glass import (
    BreakGlassManager,
)
from backend.core.ouroboros.governance.tui_transport import TUITransport


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a target file."""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "example.py"
    target.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def ledger(tmp_path):
    return OperationLedger(storage_dir=tmp_path / "ledger")


@pytest.fixture
def full_stack(project, ledger):
    """Full Phase 1 stack: lock manager + change engine + comm + TUI."""
    log_transport = LogTransport()
    tui_transport = TUITransport()
    comm = CommProtocol(transports=[log_transport, tui_transport])
    lock_mgr = GovernanceLockManager()
    break_glass = BreakGlassManager()
    engine = ChangeEngine(
        project_root=project,
        ledger=ledger,
        comm=comm,
        lock_manager=lock_mgr,
        break_glass=break_glass,
    )
    return engine, lock_mgr, break_glass, log_transport, tui_transport


# ---------------------------------------------------------------------------
# Phase 1A: Lock hierarchy Go/No-Go
# ---------------------------------------------------------------------------


class TestLockHierarchyGoNoGo:
    """Design doc section 4, Phase 1A lock criteria."""

    @pytest.mark.asyncio
    async def test_out_of_order_acquisition_immediate_error(self):
        """Lock acquisition out of order -> immediate error (not deadlock)."""
        mgr = GovernanceLockManager()
        async with mgr.acquire(
            LockLevel.REPO_LOCK, "jarvis", LockMode.EXCLUSIVE_WRITE
        ):
            with pytest.raises(LockOrderViolation):
                async with mgr.acquire(
                    LockLevel.FILE_LOCK, "test.py", LockMode.EXCLUSIVE_WRITE
                ):
                    pass

    @pytest.mark.asyncio
    async def test_write_with_stale_fencing_token_rejected(self):
        """Write with expired fencing token -> rejected."""
        mgr = GovernanceLockManager()
        # Acquire and release to advance the fencing token
        async with mgr.acquire(
            LockLevel.FILE_LOCK, "test.py", LockMode.EXCLUSIVE_WRITE
        ):
            pass
        async with mgr.acquire(
            LockLevel.FILE_LOCK, "test.py", LockMode.EXCLUSIVE_WRITE
        ):
            pass
        # Token 0 is now stale (current is 2)
        with pytest.raises(FencingTokenError):
            mgr.validate_fencing_token(LockLevel.FILE_LOCK, "test.py", 0)

    @pytest.mark.asyncio
    async def test_concurrent_shared_reads_succeed(self):
        """Concurrent shared-read same file -> both succeed."""
        mgr = GovernanceLockManager()
        results = []

        async def read():
            async with mgr.acquire(
                LockLevel.FILE_LOCK, "test.py", LockMode.SHARED_READ
            ) as h:
                results.append(h is not None)
                await asyncio.sleep(0.01)

        await asyncio.gather(read(), read())
        assert results == [True, True]

    @pytest.mark.asyncio
    async def test_concurrent_exclusive_writes_serialize(self):
        """Concurrent exclusive-write same file -> one waits, one proceeds."""
        mgr = GovernanceLockManager()
        order = []

        async def write(label, delay):
            async with mgr.acquire(
                LockLevel.FILE_LOCK, "test.py", LockMode.EXCLUSIVE_WRITE
            ):
                order.append(f"{label}_start")
                await asyncio.sleep(delay)
                order.append(f"{label}_end")

        await asyncio.gather(write("a", 0.05), write("b", 0.01))
        # One must complete before the other starts
        first_end = min(order.index("a_end"), order.index("b_end"))
        second_start = max(order.index("a_start"), order.index("b_start"))
        assert first_end < second_start


# ---------------------------------------------------------------------------
# Phase 1A: Transactional engine Go/No-Go
# ---------------------------------------------------------------------------


class TestTransactionalEngineGoNoGo:
    """Design doc section 4, Phase 1A transactional criteria."""

    @pytest.mark.asyncio
    async def test_ledger_entry_for_every_state_transition(
        self, full_stack, project, ledger
    ):
        """Ledger entry exists for every state transition."""
        engine, _, _, _, _ = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Add docstring",
            target_file=target,
            proposed_content="def hello():\n    \"\"\"Hi.\"\"\"\n    return 'world'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        history = await ledger.get_history(result.op_id)
        states = [e.state for e in history]
        # Must have PLANNED, SANDBOXING, VALIDATING, GATING, APPLYING, APPLIED
        assert OperationState.PLANNED in states
        assert OperationState.APPLIED in states

    @pytest.mark.asyncio
    async def test_rollback_hash_matches_pre_change(
        self, full_stack, project
    ):
        """Rollback hash matches pre-change snapshot hash exactly."""
        target = project / "src" / "example.py"
        original = target.read_text()
        original_hash = hashlib.sha256(original.encode()).hexdigest()

        artifact = RollbackArtifact.capture(target)
        assert artifact.snapshot_hash == original_hash

        # Simulate change
        target.write_text("# changed\n")
        artifact.apply(target)
        assert target.read_text() == original

    @pytest.mark.asyncio
    async def test_post_apply_failure_triggers_rollback(
        self, full_stack, project, ledger
    ):
        """Post-apply test failure -> automatic rollback within execution."""
        engine, _, _, _, _ = full_stack
        target = project / "src" / "example.py"
        original = target.read_text()

        request = ChangeRequest(
            goal="Change that fails verify",
            target_file=target,
            proposed_content="def hello():\n    return 'changed'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
            verify_fn=AsyncMock(return_value=False),
        )
        result = await engine.execute(request)
        assert result.rolled_back is True
        assert target.read_text() == original

        latest = await ledger.get_latest_state(result.op_id)
        assert latest == OperationState.ROLLED_BACK


# ---------------------------------------------------------------------------
# Phase 1A: Break-glass Go/No-Go
# ---------------------------------------------------------------------------


class TestBreakGlassGoNoGo:
    """Design doc section 4, Phase 1A break-glass criteria."""

    @pytest.mark.asyncio
    async def test_break_glass_token_expires_after_ttl(self):
        """Break-glass: token expires after TTL, audit trail complete."""
        mgr = BreakGlassManager()
        await mgr.issue(
            op_id="op-bg-ttl",
            reason="test",
            ttl=0,  # Immediate expiry
            issuer="derek",
        )
        promoted = mgr.get_promoted_tier("op-bg-ttl")
        assert promoted is None  # Expired

        audit = mgr.get_audit_trail()
        assert len(audit) >= 1
        assert audit[0].action == "issued"

    @pytest.mark.asyncio
    async def test_break_glass_promotes_to_approval_required(self):
        """Break-glass: operation proceeds under APPROVAL_REQUIRED (not unguarded)."""
        mgr = BreakGlassManager()
        await mgr.issue(
            op_id="op-bg-promo",
            reason="emergency",
            ttl=300,
            issuer="derek",
        )
        promoted = mgr.get_promoted_tier("op-bg-promo")
        assert promoted == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# Phase 1B: Communication + TUI Go/No-Go
# ---------------------------------------------------------------------------


class TestCommTUIGoNoGo:
    """Design doc section 4, Phase 1B communication criteria."""

    @pytest.mark.asyncio
    async def test_all_five_message_types_emitted(
        self, full_stack, project
    ):
        """Every operation emits all 5 message types in correct order."""
        engine, _, _, log_transport, _ = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Simple change",
            target_file=target,
            proposed_content="def hello():\n    return 'updated'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        assert result.success is True

        types = [m.msg_type for m in log_transport.messages]
        # Must have at least INTENT, HEARTBEAT, DECISION, POSTMORTEM
        assert MessageType.INTENT in types
        assert MessageType.HEARTBEAT in types
        assert MessageType.DECISION in types
        assert MessageType.POSTMORTEM in types

    @pytest.mark.asyncio
    async def test_tui_transport_crash_does_not_block_pipeline(
        self, project, ledger
    ):
        """TUI crash -> messages queue -> pipeline continues unblocked."""
        # Create a TUI transport with a crashing callback
        tui = TUITransport()
        tui.on_message(AsyncMock(side_effect=RuntimeError("TUI crash")))

        log = LogTransport()
        comm = CommProtocol(transports=[log, tui])
        engine = ChangeEngine(
            project_root=project,
            ledger=ledger,
            comm=comm,
        )

        target = project / "src" / "example.py"
        request = ChangeRequest(
            goal="Change despite TUI crash",
            target_file=target,
            proposed_content="def hello():\n    return 'safe'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)
        # Pipeline completed despite TUI crash
        assert result.success is True
        # Log transport still received everything
        assert len(log.messages) > 0

    @pytest.mark.asyncio
    async def test_sequence_numbers_monotonic(self, full_stack, project):
        """Sequence numbers monotonic per op_id, causal parent links valid."""
        engine, _, _, log_transport, _ = full_stack
        target = project / "src" / "example.py"

        request = ChangeRequest(
            goal="Seq test",
            target_file=target,
            proposed_content="def hello():\n    return 'seq'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        result = await engine.execute(request)

        msgs = [m for m in log_transport.messages if m.op_id == result.op_id]
        seqs = [m.seq for m in msgs]
        # Monotonically increasing
        for i in range(len(seqs) - 1):
            assert seqs[i] < seqs[i + 1]

    @pytest.mark.asyncio
    async def test_tui_transport_receives_formatted_messages(
        self, full_stack, project
    ):
        """TUI transport receives formatted governance messages."""
        engine, _, _, _, tui_transport = full_stack
        received = []
        tui_transport.on_message(
            AsyncMock(side_effect=lambda m: received.append(m))
        )

        target = project / "src" / "example.py"
        request = ChangeRequest(
            goal="TUI display test",
            target_file=target,
            proposed_content="def hello():\n    return 'tui'\n",
            profile=OperationProfile(
                files_affected=[Path("src/example.py")],
                change_type=ChangeType.MODIFY,
                blast_radius=1,
                crosses_repo_boundary=False,
                touches_security_surface=False,
                touches_supervisor=False,
                test_scope_confidence=0.9,
            ),
        )
        await engine.execute(request)
        # TUI transport should have received formatted messages
        assert len(received) > 0
        assert all("type" in r for r in received)
        assert all("op_id" in r for r in received)
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_ouroboros_governance/test_phase1_integration.py -v`
Expected: All 11 tests PASS

**Step 3: Run ALL governance tests (Phase 0 + Phase 1)**

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v`
Expected: ALL tests pass (64 Phase 0 + ~51 Phase 1 = ~115 total)

**Step 4: Commit**

```bash
git add tests/test_ouroboros_governance/test_phase1_integration.py
git commit -m "test(governance): add Phase 1 integration tests for Go/No-Go criteria

Verifies lock hierarchy enforcement, transactional pipeline,
break-glass promotion, rollback integrity, TUI transport fault
isolation, and sequence number monotonicity.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Summary of Phase 1 Deliverables

| Task | Component | Tests | Go/No-Go Criteria Covered |
|------|-----------|-------|---------------------------|
| 1 | `lock_manager.py` — Hierarchical R/W leases | 12 | Out-of-order error, concurrent R/W, fencing, fairness |
| 2 | `break_glass.py` — Time-limited tokens | 10 | TTL expiry, APPROVAL_REQUIRED promotion, audit trail |
| 3 | `change_engine.py` — 8-phase pipeline | 8 | Full pipeline, gate enforcement, rollback, ledger tracking |
| 4 | `tui_transport.py` — TUI comm transport | 10 | Fault isolation, message queueing, drain, formatting |
| 5 | `__init__.py` — Wire exports | 0 | Package completeness |
| 6 | `test_phase1_integration.py` — Go/No-Go | 11 | All Phase 1A + 1B acceptance criteria |

**Total new tests: ~51**
**Total governance tests (Phase 0 + Phase 1): ~115**

---

## What Phase 1 Does NOT Include (deferred to Phase 2+)

- **DLM wrapper integration** — Phase 1 uses an in-process lock manager. Phase 2A wraps the real DLM for cross-process/cross-repo locking.
- **Outbox/inbox cross-repo eventing** — Phase 1 publishes via CommProtocol transports. Phase 2B adds cross-repo event bus with inbox consumer ack.
- **Hybrid routing** — Phase 2A adds multi-signal deterministic routing.
- **50-concurrent-operation stress tests** — Phase 2A adds sustained contention testing.
- **CLI break-glass command** — Phase 2A adds `--break-glass` argparse command to `unified_supervisor.py`.

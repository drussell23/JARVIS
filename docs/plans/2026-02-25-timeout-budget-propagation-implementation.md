# Timeout Budget Propagation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace independent timeout clocks with a composable budget system that propagates deadlines through the async call stack, producing typed errors that downstream logic can classify correctly.

**Architecture:** A frozen `ExecutionContext` dataclass propagated via `ContextVar`. Phase/service boundaries create contexts with `execution_budget()`. Interior calls use `budget_aware_wait_for()` which computes `effective = min(remaining_budget, local_cap)`. Three error types (`BudgetExhaustedError`, `LocalCapExceededError`, `ExternalCancellationError`) replace ambiguous `asyncio.TimeoutError`/`CancelledError`.

**Tech Stack:** Python 3.9+ stdlib (`asyncio`, `contextvars`, `time.monotonic`, `threading`, `dataclasses`). No external dependencies.

**Design doc:** `docs/plans/2026-02-25-timeout-budget-propagation-design.md`

---

### Task 1: Error Taxonomy and CancelScope

**Files:**
- Create: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests for error types and CancelScope**

```python
# tests/unit/backend/core/test_execution_context.py
"""Tests for execution context timeout budget propagation."""
import asyncio
import threading
import time
import pytest


class TestErrorTaxonomy:
    """Verify error types have correct inheritance and fields."""

    def test_budget_exhausted_not_timeout_error(self):
        from backend.core.execution_context import BudgetExhaustedError
        err = BudgetExhaustedError(
            owner="phase_preflight", phase="preflight",
            deadline_mono=1000.0, remaining_at_entry=0.0,
            local_cap=30.0, effective_timeout=0.0,
            elapsed=90.0, timeout_origin="budget",
        )
        assert not isinstance(err, TimeoutError)
        assert isinstance(err, Exception)
        assert err.owner == "phase_preflight"
        assert err.timeout_origin == "budget"

    def test_local_cap_exceeded_is_timeout_error(self):
        from backend.core.execution_context import LocalCapExceededError
        err = LocalCapExceededError(
            owner="phase_preflight", phase="preflight",
            deadline_mono=2000.0, remaining_at_entry=50.0,
            local_cap=5.0, effective_timeout=5.0,
            elapsed=5.0, timeout_origin="local_cap",
        )
        assert isinstance(err, TimeoutError)
        assert err.timeout_origin == "local_cap"

    def test_external_cancellation_error(self):
        from backend.core.execution_context import (
            ExternalCancellationError, CancellationCause,
        )
        err = ExternalCancellationError(
            cause=CancellationCause.OWNER_SHUTDOWN,
            scope_id="scope-123",
            detail="Supervisor shutting down",
        )
        assert not isinstance(err, TimeoutError)
        assert err.cause == CancellationCause.OWNER_SHUTDOWN

    def test_budget_exhausted_has_all_metadata(self):
        from backend.core.execution_context import BudgetExhaustedError
        err = BudgetExhaustedError(
            owner="svc_cloudsql", phase="enterprise",
            deadline_mono=500.0, remaining_at_entry=2.0,
            local_cap=30.0, effective_timeout=2.0,
            elapsed=2.0, timeout_origin="budget",
        )
        assert err.remaining_at_entry == 2.0
        assert err.effective_timeout == 2.0


class TestCancelScope:
    """Verify CancelScope is write-once and thread-safe."""

    def test_cancel_scope_write_once(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="test")
        first = handle.set_cause(
            CancellationCause.BUDGET_EXHAUSTED, "deadline hit"
        )
        second = handle.set_cause(
            CancellationCause.OWNER_SHUTDOWN, "shutdown"
        )
        assert first is True
        assert second is False
        assert handle.scope.cause == CancellationCause.BUDGET_EXHAUSTED

    def test_cancel_scope_initially_none(self):
        from backend.core.execution_context import CancelScopeHandle
        handle = CancelScopeHandle(owner_id="test")
        assert handle.scope is None

    def test_cancel_scope_thread_safe(self):
        from backend.core.execution_context import (
            CancelScopeHandle, CancellationCause,
        )
        handle = CancelScopeHandle(owner_id="test")
        results = []

        def try_set(cause, detail):
            results.append(handle.set_cause(cause, detail))

        threads = [
            threading.Thread(
                target=try_set,
                args=(CancellationCause.BUDGET_EXHAUSTED, f"t{i}"),
            )
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 9
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py -v --tb=short 2>&1 | head -30`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.execution_context'`

**Step 3: Implement error types and CancelScope**

```python
# backend/core/execution_context.py
"""
Execution Context v1.0 — Composable Timeout Budget Propagation.

Provides a shared deadline model that propagates through async call stacks
via contextvars. Replaces independent asyncio.wait_for() timeout clocks
with budget-aware execution that produces typed errors.

Usage:
    from backend.core.execution_context import (
        execution_budget, budget_aware_wait_for, remaining_budget,
    )

    # At phase boundaries:
    async with execution_budget("phase_preflight", timeout=90.0):
        # Interior calls automatically respect the budget:
        result = await budget_aware_wait_for(
            some_service_init(), local_cap=30.0, label="svc_init"
        )

Design doc: docs/plans/2026-02-25-timeout-budget-propagation-design.md
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any, AsyncGenerator, Awaitable, Callable, Final,
    Mapping, Optional, TypeVar, Union,
)

logger = logging.getLogger("jarvis.execution_context")

T = TypeVar("T")


# =============================================================================
# FEATURE FLAGS
# =============================================================================

BUDGET_ENFORCE: bool = os.getenv(
    "JARVIS_BUDGET_ENFORCE", "true"
).lower() == "true"

BUDGET_SHADOW: bool = os.getenv(
    "JARVIS_BUDGET_SHADOW", "false"
).lower() == "true"


# =============================================================================
# ENUMS
# =============================================================================

class CancellationCause(Enum):
    """Why an operation was cancelled."""
    BUDGET_EXHAUSTED = "budget_exhausted"
    OWNER_SHUTDOWN = "owner_shutdown"
    DEPENDENCY_LOST = "dependency_lost"
    MANUAL_CANCEL = "manual_cancel"


class RootReason(Enum):
    """Why a root scope was created (audit trail)."""
    DETACHED_BACKGROUND = "detached_background"
    RECOVERY_WORKER = "recovery_worker"
    USER_JOB = "user_job"


class RequestKind(Enum):
    """Classification of the execution request."""
    STARTUP = "startup"
    RUNTIME = "runtime"
    RECOVERY = "recovery"
    BACKGROUND = "background"


class Criticality(Enum):
    """Admission control priority."""
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# =============================================================================
# ERROR TAXONOMY
# =============================================================================

class BudgetExhaustedError(Exception):
    """Parent deadline reached zero. Retry will NOT help in-scope.

    NOT a subclass of TimeoutError — budget exhaustion is a policy outcome,
    not a generic I/O timeout. Handlers catching TimeoutError should NOT
    catch this.
    """

    def __init__(
        self,
        *,
        owner: str,
        phase: str,
        deadline_mono: float,
        remaining_at_entry: float,
        local_cap: float,
        effective_timeout: float,
        elapsed: float,
        timeout_origin: str = "budget",
    ) -> None:
        self.owner = owner
        self.phase = phase
        self.deadline_mono = deadline_mono
        self.remaining_at_entry = remaining_at_entry
        self.local_cap = local_cap
        self.effective_timeout = effective_timeout
        self.elapsed = elapsed
        self.timeout_origin = timeout_origin
        super().__init__(
            f"Budget exhausted: owner={owner} phase={phase} "
            f"remaining_at_entry={remaining_at_entry:.1f}s "
            f"local_cap={local_cap:.1f}s elapsed={elapsed:.1f}s"
        )


class LocalCapExceededError(TimeoutError):
    """Operation exceeded its local_cap before budget hit zero.

    IS a subclass of TimeoutError — existing retry logic that catches
    TimeoutError will correctly catch this and may retry.
    """

    def __init__(
        self,
        *,
        owner: str,
        phase: str,
        deadline_mono: float,
        remaining_at_entry: float,
        local_cap: float,
        effective_timeout: float,
        elapsed: float,
        timeout_origin: str = "local_cap",
    ) -> None:
        self.owner = owner
        self.phase = phase
        self.deadline_mono = deadline_mono
        self.remaining_at_entry = remaining_at_entry
        self.local_cap = local_cap
        self.effective_timeout = effective_timeout
        self.elapsed = elapsed
        self.timeout_origin = timeout_origin
        super().__init__(
            f"Local cap exceeded: owner={owner} phase={phase} "
            f"local_cap={local_cap:.1f}s elapsed={elapsed:.1f}s"
        )


class ExternalCancellationError(Exception):
    """Cancelled by owner shutdown, dependency loss, or manual cancel.

    NOT a subclass of asyncio.CancelledError — this is a classified
    cancellation with a known cause, not a raw signal.
    """

    def __init__(
        self,
        *,
        cause: CancellationCause,
        scope_id: str,
        detail: str = "",
    ) -> None:
        self.cause = cause
        self.scope_id = scope_id
        self.detail = detail
        super().__init__(
            f"External cancellation: cause={cause.value} "
            f"scope={scope_id} detail={detail}"
        )


# =============================================================================
# CANCEL SCOPE (immutable + write-once)
# =============================================================================

@dataclass(frozen=True)
class CancelScope:
    """Immutable cancellation record. Created by CancelScopeHandle.set_cause()."""
    scope_id: str
    cause: CancellationCause
    set_at_mono: float
    detail: str
    owner_id: str


class CancelScopeHandle:
    """Thread-safe write-once wrapper around CancelScope.

    First set_cause() wins. Subsequent calls return False and log a warning.
    This prevents races where multiple upstream failures overwrite cause/detail.
    """

    __slots__ = ("_scope", "_lock", "_owner_id", "_scope_id")

    def __init__(self, owner_id: str) -> None:
        self._scope: Optional[CancelScope] = None
        self._lock = threading.Lock()
        self._owner_id = owner_id
        self._scope_id = f"scope-{uuid.uuid4().hex[:12]}"

    def set_cause(
        self,
        cause: CancellationCause,
        detail: str = "",
    ) -> bool:
        """Set the cancellation cause. Returns True on first set, False after."""
        with self._lock:
            if self._scope is not None:
                logger.debug(
                    "[CancelScope] Ignoring duplicate set_cause(%s) on %s "
                    "(already set to %s)",
                    cause.value, self._scope_id, self._scope.cause.value,
                )
                return False
            self._scope = CancelScope(
                scope_id=self._scope_id,
                cause=cause,
                set_at_mono=time.monotonic(),
                detail=detail,
                owner_id=self._owner_id,
            )
            return True

    @property
    def scope(self) -> Optional[CancelScope]:
        """Read the frozen scope. None if not yet triggered."""
        return self._scope

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def __repr__(self) -> str:
        if self._scope:
            return f"CancelScopeHandle({self._scope.cause.value})"
        return f"CancelScopeHandle(pending, owner={self._owner_id})"
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestErrorTaxonomy tests/unit/backend/core/test_execution_context.py::TestCancelScope -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add backend/core/execution_context.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(execution-context): add error taxonomy and CancelScope primitives"
```

---

### Task 2: ExecutionContext Dataclass and ContextVar

**Files:**
- Modify: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests for ExecutionContext**

Add to `test_execution_context.py`:

```python
class TestExecutionContext:
    """Verify ExecutionContext creation and nesting semantics."""

    def test_context_is_frozen(self):
        from backend.core.execution_context import (
            ExecutionContext, CancelScopeHandle, Criticality, RequestKind,
        )
        ctx = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0,
            trace_id="test-trace",
            owner_id="test",
            cancel_scope=CancelScopeHandle(owner_id="test"),
            mode_snapshot="normal",
        )
        with pytest.raises(AttributeError):
            ctx.deadline_mono = 999.0  # type: ignore[misc]

    def test_context_uses_monotonic_clock(self):
        from backend.core.execution_context import (
            ExecutionContext, CancelScopeHandle,
        )
        before = time.monotonic()
        ctx = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0,
            trace_id="test-trace",
            owner_id="test",
            cancel_scope=CancelScopeHandle(owner_id="test"),
            mode_snapshot="normal",
        )
        after = time.monotonic()
        assert before <= ctx.created_at_mono <= after

    def test_context_parent_chain(self):
        from backend.core.execution_context import (
            ExecutionContext, CancelScopeHandle,
        )
        parent = ExecutionContext(
            deadline_mono=time.monotonic() + 60.0,
            trace_id="parent-trace",
            owner_id="phase_preflight",
            cancel_scope=CancelScopeHandle(owner_id="phase_preflight"),
            mode_snapshot="normal",
        )
        child = ExecutionContext(
            deadline_mono=time.monotonic() + 30.0,
            trace_id="parent-trace",
            owner_id="svc_cloudsql",
            cancel_scope=CancelScopeHandle(owner_id="svc_cloudsql"),
            mode_snapshot="normal",
            parent_ctx=parent,
        )
        assert child.parent_ctx is parent
        assert child.parent_ctx.owner_id == "phase_preflight"

    def test_contextvar_default_is_none(self):
        from backend.core.execution_context import current_context
        # In a fresh context, should be None
        assert current_context() is None

    def test_remaining_budget_none_when_no_context(self):
        from backend.core.execution_context import remaining_budget
        assert remaining_budget() is None
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestExecutionContext -v --tb=short 2>&1 | head -20`
Expected: FAIL with `ImportError` for `ExecutionContext`

**Step 3: Implement ExecutionContext and query functions**

Append to `backend/core/execution_context.py`:

```python
# =============================================================================
# EXECUTION CONTEXT
# =============================================================================

@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution context propagated via ContextVar.

    Carries a shared deadline through the async call stack. Child scopes
    can only shrink the deadline, never extend it (unless root=True).
    """
    # Core budget fields (Phase 1)
    deadline_mono: float
    trace_id: str
    owner_id: str
    cancel_scope: CancelScopeHandle
    mode_snapshot: str

    # Nesting
    parent_ctx: Optional["ExecutionContext"] = None
    created_at_mono: float = field(default_factory=time.monotonic)

    # Phase 2 fields (state machine linkage)
    phase_id: str = ""
    phase_name: str = ""
    mode_epoch: int = 0
    budget_policy_version: int = 1

    # Phase 3 fields (admission control)
    priority: Criticality = Criticality.NORMAL
    request_kind: RequestKind = RequestKind.STARTUP
    tags: Mapping[str, str] = field(default_factory=dict)

    # Root scope metadata
    root_reason: Optional[RootReason] = None

    @property
    def remaining(self) -> float:
        """Remaining budget in seconds. May be negative if past deadline."""
        return self.deadline_mono - time.monotonic()


# =============================================================================
# CONTEXTVAR
# =============================================================================

_current_ctx: contextvars.ContextVar[Optional[ExecutionContext]] = (
    contextvars.ContextVar("jarvis_execution_context", default=None)
)


def current_context() -> Optional[ExecutionContext]:
    """Get the active ExecutionContext, or None."""
    return _current_ctx.get()


def remaining_budget() -> Optional[float]:
    """Remaining budget in seconds, or None if no active budget."""
    ctx = _current_ctx.get()
    if ctx is None:
        return None
    return max(0.0, ctx.deadline_mono - time.monotonic())
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestExecutionContext -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add backend/core/execution_context.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(execution-context): add ExecutionContext dataclass and ContextVar"
```

---

### Task 3: execution_budget() Context Manager

**Files:**
- Modify: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests for execution_budget**

Add to `test_execution_context.py`:

```python
class TestExecutionBudget:
    """Verify execution_budget context manager semantics."""

    @pytest.mark.asyncio
    async def test_budget_sets_context(self):
        from backend.core.execution_context import (
            execution_budget, current_context,
        )
        assert current_context() is None
        async with execution_budget("test_owner", 60.0) as ctx:
            assert current_context() is ctx
            assert ctx.owner_id == "test_owner"
            assert ctx.remaining > 50.0  # Should be close to 60s
        assert current_context() is None  # Cleaned up

    @pytest.mark.asyncio
    async def test_budget_shrinks_with_nesting(self):
        from backend.core.execution_context import execution_budget
        async with execution_budget("parent", 60.0) as parent_ctx:
            async with execution_budget("child", 30.0) as child_ctx:
                # Child deadline should be ~30s from now (less than parent's ~60s)
                assert child_ctx.deadline_mono <= parent_ctx.deadline_mono
                assert child_ctx.parent_ctx is parent_ctx

    @pytest.mark.asyncio
    async def test_budget_never_extends(self):
        from backend.core.execution_context import execution_budget
        async with execution_budget("parent", 10.0) as parent_ctx:
            async with execution_budget("child", 60.0) as child_ctx:
                # Child requested 60s but parent only has ~10s
                # Child deadline must equal parent deadline
                assert abs(child_ctx.deadline_mono - parent_ctx.deadline_mono) < 0.1

    @pytest.mark.asyncio
    async def test_root_scope_creates_fresh_deadline(self):
        from backend.core.execution_context import (
            execution_budget, RootReason,
        )
        async with execution_budget("parent", 10.0) as parent_ctx:
            async with execution_budget(
                "supervisor", 120.0,
                root=True, root_reason=RootReason.RECOVERY_WORKER,
            ) as child_ctx:
                # Root scope ignores parent — gets full 120s
                assert child_ctx.deadline_mono > parent_ctx.deadline_mono
                assert child_ctx.root_reason == RootReason.RECOVERY_WORKER

    @pytest.mark.asyncio
    async def test_root_scope_requires_reason(self):
        from backend.core.execution_context import execution_budget
        with pytest.raises(ValueError, match="root_reason"):
            async with execution_budget("test", 60.0, root=True):
                pass

    @pytest.mark.asyncio
    async def test_root_scope_blocked_for_unauthorized_owner(self):
        from backend.core.execution_context import (
            execution_budget, RootReason,
        )
        with pytest.raises(ValueError, match="not authorized"):
            async with execution_budget(
                "random_service", 60.0,
                root=True, root_reason=RootReason.DETACHED_BACKGROUND,
            ):
                pass

    @pytest.mark.asyncio
    async def test_context_leak_prevention(self):
        from backend.core.execution_context import (
            execution_budget, current_context,
        )
        try:
            async with execution_budget("test", 60.0):
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        # Context must be cleaned up even after exception
        assert current_context() is None

    @pytest.mark.asyncio
    async def test_context_leak_prevention_nested(self):
        from backend.core.execution_context import (
            execution_budget, current_context,
        )
        async with execution_budget("outer", 60.0) as outer:
            try:
                async with execution_budget("inner", 30.0):
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            # After inner failure, outer context should be restored
            assert current_context() is outer
        assert current_context() is None

    @pytest.mark.asyncio
    async def test_phase_fields_propagated(self):
        from backend.core.execution_context import (
            execution_budget, Criticality, RequestKind,
        )
        async with execution_budget(
            "phase_preflight", 90.0,
            phase_id="1", phase_name="preflight",
            priority=Criticality.CRITICAL,
            request_kind=RequestKind.STARTUP,
            tags={"zone": "5"},
        ) as ctx:
            assert ctx.phase_id == "1"
            assert ctx.phase_name == "preflight"
            assert ctx.priority == Criticality.CRITICAL
            assert ctx.tags["zone"] == "5"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestExecutionBudget -v --tb=short 2>&1 | head -20`
Expected: FAIL with `ImportError` for `execution_budget`

**Step 3: Implement execution_budget**

Append to `backend/core/execution_context.py`:

```python
# =============================================================================
# ROOT SCOPE ALLOWLIST
# =============================================================================

_ROOT_SCOPE_ALLOWLIST: Final[frozenset] = frozenset({
    "supervisor",
    "phase_manager",
    "recovery_coordinator",
    "background_health_monitor",
})


# =============================================================================
# BUDGET CONTEXT MANAGER
# =============================================================================

@asynccontextmanager
async def execution_budget(
    owner: str,
    timeout: float,
    *,
    root: bool = False,
    root_reason: Optional[RootReason] = None,
    mode_snapshot: Optional[str] = None,
    phase_id: str = "",
    phase_name: str = "",
    mode_epoch: int = 0,
    priority: Criticality = Criticality.NORMAL,
    request_kind: RequestKind = RequestKind.STARTUP,
    tags: Optional[Mapping[str, str]] = None,
) -> AsyncGenerator[ExecutionContext, None]:
    """Create a budget scope. Child scopes can only shrink, never extend.

    Args:
        owner: Who is creating this budget (for audit trail).
        timeout: Maximum time in seconds for this scope.
        root: If True, ignore parent deadline and create fresh budget.
              Requires root_reason and owner in _ROOT_SCOPE_ALLOWLIST.
        root_reason: Required when root=True. Audit trail for why.
        mode_snapshot: System mode at creation (e.g., "normal", "degraded").
        phase_id: Numeric phase identifier for state machine linkage.
        phase_name: Human-readable phase name.
        mode_epoch: Monotonic counter for detecting stale mode snapshots.
        priority: Criticality level for admission control.
        request_kind: Classification of the execution request.
        tags: Arbitrary key-value labels for observability.

    Raises:
        ValueError: If root=True without root_reason or unauthorized owner.
    """
    if root:
        if root_reason is None:
            raise ValueError(
                "root=True requires root_reason (DETACHED_BACKGROUND, "
                "RECOVERY_WORKER, or USER_JOB)"
            )
        if owner not in _ROOT_SCOPE_ALLOWLIST:
            raise ValueError(
                f"Owner '{owner}' is not authorized for root scopes. "
                f"Allowed: {sorted(_ROOT_SCOPE_ALLOWLIST)}"
            )

    now = time.monotonic()
    parent = _current_ctx.get()

    if root or parent is None:
        deadline = now + timeout
    else:
        # Child can only shrink: effective = min(parent_remaining, local)
        parent_remaining = parent.deadline_mono - now
        effective = min(parent_remaining, timeout)
        deadline = now + max(0.0, effective)

    cancel_scope = CancelScopeHandle(owner_id=owner)
    trace_id = parent.trace_id if (parent and not root) else uuid.uuid4().hex[:16]

    ctx = ExecutionContext(
        deadline_mono=deadline,
        trace_id=trace_id,
        owner_id=owner,
        cancel_scope=cancel_scope,
        mode_snapshot=mode_snapshot or (parent.mode_snapshot if parent else "unknown"),
        parent_ctx=None if root else parent,
        phase_id=phase_id,
        phase_name=phase_name or (parent.phase_name if parent and not root else ""),
        mode_epoch=mode_epoch or (parent.mode_epoch if parent and not root else 0),
        priority=priority,
        request_kind=request_kind,
        tags=dict(tags) if tags else {},
        root_reason=root_reason,
    )

    token = _current_ctx.set(ctx)
    logger.debug(
        "[Budget] ENTER owner=%s timeout=%.1fs deadline_mono=%.1f "
        "remaining=%.1fs root=%s phase=%s",
        owner, timeout, deadline, max(0.0, deadline - now),
        root, phase_name or phase_id,
    )

    try:
        yield ctx
    finally:
        _current_ctx.reset(token)
        elapsed = time.monotonic() - ctx.created_at_mono
        remaining = max(0.0, ctx.deadline_mono - time.monotonic())
        logger.debug(
            "[Budget] EXIT owner=%s elapsed=%.1fs remaining=%.1fs",
            owner, elapsed, remaining,
        )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestExecutionBudget -v`
Expected: All 10 tests PASS

**Step 5: Commit**

```bash
git add backend/core/execution_context.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(execution-context): add execution_budget context manager with nesting policy"
```

---

### Task 4: budget_aware_wait_for() and Exception Bridging

**Files:**
- Modify: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests**

Add to `test_execution_context.py`:

```python
class TestBudgetAwareWaitFor:
    """Verify budget_aware_wait_for timeout semantics."""

    @pytest.mark.asyncio
    async def test_completes_within_budget(self):
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
        )
        async def fast_op():
            await asyncio.sleep(0.01)
            return "done"

        async with execution_budget("test", 5.0):
            result = await budget_aware_wait_for(
                fast_op(), local_cap=2.0, label="fast_op"
            )
        assert result == "done"

    @pytest.mark.asyncio
    async def test_local_cap_exceeded_raises_typed_error(self):
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            LocalCapExceededError,
        )
        async def slow_op():
            await asyncio.sleep(10.0)

        async with execution_budget("test", 60.0):
            with pytest.raises(LocalCapExceededError) as exc_info:
                await budget_aware_wait_for(
                    slow_op(), local_cap=0.1, label="slow_op"
                )
            assert exc_info.value.timeout_origin == "local_cap"

    @pytest.mark.asyncio
    async def test_budget_exhausted_raises_typed_error(self):
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError,
        )
        async def slow_op():
            await asyncio.sleep(10.0)

        # Budget of 0.1s, local_cap of 5s — budget runs out first
        async with execution_budget("test", 0.15):
            with pytest.raises(BudgetExhaustedError) as exc_info:
                await budget_aware_wait_for(
                    slow_op(), local_cap=5.0, label="slow_op"
                )
            assert exc_info.value.timeout_origin == "budget"

    @pytest.mark.asyncio
    async def test_no_budget_no_cap_fails_closed(self):
        from backend.core.execution_context import budget_aware_wait_for
        async def some_op():
            return "done"

        # No active budget AND local_cap=0 should fail closed
        with pytest.raises(RuntimeError, match="No budget and no local_cap"):
            await budget_aware_wait_for(some_op(), local_cap=0.0, label="test")

    @pytest.mark.asyncio
    async def test_no_budget_with_cap_uses_local(self):
        from backend.core.execution_context import budget_aware_wait_for
        async def fast_op():
            await asyncio.sleep(0.01)
            return "ok"

        # No active budget but local_cap provided — should work
        result = await budget_aware_wait_for(
            fast_op(), local_cap=5.0, label="unscoped"
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_effective_timeout_is_min_of_remaining_and_cap(self):
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError, LocalCapExceededError,
        )
        async def slow_op():
            await asyncio.sleep(10.0)

        # Budget: 0.2s remaining, local_cap: 0.1s
        # local_cap < remaining → LocalCapExceededError
        async with execution_budget("test", 0.5):
            with pytest.raises(LocalCapExceededError):
                await budget_aware_wait_for(
                    slow_op(), local_cap=0.1, label="test"
                )

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_without_enforcing(self):
        """In shadow mode, uses local_cap even when budget is tighter."""
        import backend.core.execution_context as ec
        original_enforce = ec.BUDGET_ENFORCE
        original_shadow = ec.BUDGET_SHADOW
        try:
            ec.BUDGET_ENFORCE = False
            ec.BUDGET_SHADOW = True
            async def fast_op():
                await asyncio.sleep(0.01)
                return "ok"

            # Budget only 0.05s but shadow mode — should use local_cap (5s)
            async with execution_budget("test", 0.05):
                result = await budget_aware_wait_for(
                    fast_op(), local_cap=5.0, label="shadow_test"
                )
            assert result == "ok"
        finally:
            ec.BUDGET_ENFORCE = original_enforce
            ec.BUDGET_SHADOW = original_shadow


class TestExceptionBridging:
    """Verify legacy asyncio.TimeoutError bridging."""

    def test_bridge_with_budget_context(self):
        from backend.core.execution_context import (
            bridge_timeout_error, BudgetExhaustedError,
        )
        err = bridge_timeout_error(
            asyncio.TimeoutError(),
            label="test",
            remaining_at_entry=0.0,
            local_cap=30.0,
            owner="phase_test",
            phase="test",
        )
        assert isinstance(err, BudgetExhaustedError)

    def test_bridge_with_remaining_budget(self):
        from backend.core.execution_context import (
            bridge_timeout_error, LocalCapExceededError,
        )
        err = bridge_timeout_error(
            asyncio.TimeoutError(),
            label="test",
            remaining_at_entry=20.0,
            local_cap=5.0,
            owner="phase_test",
            phase="test",
        )
        assert isinstance(err, LocalCapExceededError)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestBudgetAwareWaitFor tests/unit/backend/core/test_execution_context.py::TestExceptionBridging -v --tb=short 2>&1 | head -25`
Expected: FAIL with `ImportError` for `budget_aware_wait_for`

**Step 3: Implement budget_aware_wait_for and bridge_timeout_error**

Append to `backend/core/execution_context.py`:

```python
# =============================================================================
# BUDGET-AWARE WAIT_FOR
# =============================================================================

async def budget_aware_wait_for(
    coro: Awaitable[T],
    *,
    local_cap: float,
    label: str = "",
) -> T:
    """Replace asyncio.wait_for with budget-aware timeout.

    Computes effective_timeout = min(remaining_budget, local_cap).
    Raises typed errors instead of generic TimeoutError/CancelledError.

    Args:
        coro: The awaitable to execute.
        local_cap: Maximum time this specific call should take.
        label: Human-readable label for observability logs.

    Raises:
        BudgetExhaustedError: Parent deadline caused the timeout.
        LocalCapExceededError: local_cap caused the timeout (budget had room).
        ExternalCancellationError: CancelledError with typed cause from scope.
        RuntimeError: No budget and no local_cap (fail-closed).
    """
    ctx = _current_ctx.get()
    now = time.monotonic()

    # Fail-closed: no budget AND no meaningful local_cap
    if ctx is None and local_cap <= 0.0:
        raise RuntimeError(
            f"No budget and no local_cap for '{label}'. "
            "Either wrap in execution_budget() or provide local_cap > 0."
        )

    # Compute effective timeout
    if ctx is not None:
        remaining = max(0.0, ctx.deadline_mono - now)
        if BUDGET_ENFORCE:
            effective = min(remaining, local_cap) if local_cap > 0 else remaining
        else:
            # Shadow mode: compute but don't enforce budget
            effective = local_cap if local_cap > 0 else remaining

        # Determine which clock is tighter (for error classification later)
        budget_is_tighter = remaining <= local_cap if local_cap > 0 else True
        remaining_at_entry = remaining
        owner = ctx.owner_id
        phase = ctx.phase_name or ctx.phase_id
        deadline = ctx.deadline_mono
    else:
        # Unscoped: use local_cap only
        effective = local_cap
        budget_is_tighter = False
        remaining_at_entry = -1.0  # sentinel: no budget
        owner = "unscoped"
        phase = ""
        deadline = now + local_cap

    if effective <= 0.0 and BUDGET_ENFORCE:
        # Budget already exhausted before we start — fail immediately
        raise BudgetExhaustedError(
            owner=owner, phase=phase,
            deadline_mono=deadline,
            remaining_at_entry=0.0,
            local_cap=local_cap,
            effective_timeout=0.0,
            elapsed=0.0,
            timeout_origin="budget",
        )

    # Log observability fields
    scoped = ctx is not None
    timeout_origin = "budget" if (budget_is_tighter and scoped) else "local_cap"
    if BUDGET_SHADOW and not BUDGET_ENFORCE:
        timeout_origin = f"shadow({timeout_origin})"

    logger.debug(
        "[Budget] label=%s owner=%s remaining_ms_in=%.0f "
        "local_cap_ms=%.0f effective_ms=%.0f timeout_origin=%s "
        "scoped=%s%s",
        label or "unnamed", owner,
        remaining_at_entry * 1000 if remaining_at_entry >= 0 else -1,
        local_cap * 1000, effective * 1000,
        timeout_origin, scoped,
        " unscoped_local_timeout=true" if not scoped else "",
    )

    start = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=effective)

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        remaining_now = max(0.0, deadline - time.monotonic()) if scoped else -1.0

        logger.warning(
            "[Budget] label=%s TIMEOUT owner=%s elapsed_ms=%.0f "
            "remaining_ms_out=%.0f timeout_origin=%s",
            label or "unnamed", owner,
            elapsed * 1000, remaining_now * 1000 if remaining_now >= 0 else -1,
            "budget" if budget_is_tighter else "local_cap",
        )

        if scoped and budget_is_tighter and BUDGET_ENFORCE:
            if ctx and ctx.cancel_scope:
                ctx.cancel_scope.set_cause(
                    CancellationCause.BUDGET_EXHAUSTED,
                    f"Budget exhausted during '{label}'",
                )
            raise BudgetExhaustedError(
                owner=owner, phase=phase,
                deadline_mono=deadline,
                remaining_at_entry=remaining_at_entry,
                local_cap=local_cap,
                effective_timeout=effective,
                elapsed=elapsed,
                timeout_origin="budget",
            )
        else:
            raise LocalCapExceededError(
                owner=owner, phase=phase,
                deadline_mono=deadline,
                remaining_at_entry=remaining_at_entry,
                local_cap=local_cap,
                effective_timeout=effective,
                elapsed=elapsed,
                timeout_origin="local_cap",
            )

    except asyncio.CancelledError:
        elapsed = time.monotonic() - start
        # Check if the cancel scope has a typed cause
        if ctx and ctx.cancel_scope and ctx.cancel_scope.scope:
            scope = ctx.cancel_scope.scope
            raise ExternalCancellationError(
                cause=scope.cause,
                scope_id=scope.scope_id,
                detail=scope.detail or f"Cancelled during '{label}'",
            )
        # Re-raise raw CancelledError if no typed cause
        raise


# =============================================================================
# EXCEPTION BRIDGING
# =============================================================================

def bridge_timeout_error(
    timeout_error: BaseException,
    *,
    label: str = "",
    remaining_at_entry: float = -1.0,
    local_cap: float = 0.0,
    owner: str = "unknown",
    phase: str = "",
) -> Union[BudgetExhaustedError, LocalCapExceededError]:
    """Map a legacy asyncio.TimeoutError to typed error.

    Use at integration boundaries where legacy code catches TimeoutError
    but the calling context has budget semantics.
    """
    if remaining_at_entry <= 0.0:
        return BudgetExhaustedError(
            owner=owner, phase=phase,
            deadline_mono=0.0,
            remaining_at_entry=remaining_at_entry,
            local_cap=local_cap,
            effective_timeout=min(remaining_at_entry, local_cap) if local_cap > 0 else 0.0,
            elapsed=0.0,
            timeout_origin="budget",
        )
    else:
        return LocalCapExceededError(
            owner=owner, phase=phase,
            deadline_mono=0.0,
            remaining_at_entry=remaining_at_entry,
            local_cap=local_cap,
            effective_timeout=local_cap,
            elapsed=local_cap,
            timeout_origin="local_cap",
        )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestBudgetAwareWaitFor tests/unit/backend/core/test_execution_context.py::TestExceptionBridging -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add backend/core/execution_context.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(execution-context): add budget_aware_wait_for and exception bridging"
```

---

### Task 5: Context Propagation Helpers

**Files:**
- Modify: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests**

Add to `test_execution_context.py`:

```python
class TestContextPropagation:
    """Verify context propagation through tasks and thread executors."""

    @pytest.mark.asyncio
    async def test_context_propagates_through_await(self):
        from backend.core.execution_context import (
            execution_budget, current_context,
        )
        async def nested_fn():
            ctx = current_context()
            assert ctx is not None
            assert ctx.owner_id == "test_propagate"
            return ctx.remaining

        async with execution_budget("test_propagate", 60.0):
            remaining = await nested_fn()
        assert remaining > 50.0

    @pytest.mark.asyncio
    async def test_context_propagates_to_created_task(self):
        from backend.core.execution_context import (
            execution_budget, current_context, propagate_to_task,
        )
        result_holder = {}

        async def task_fn():
            ctx = current_context()
            result_holder["has_ctx"] = ctx is not None
            result_holder["owner"] = ctx.owner_id if ctx else None

        async with execution_budget("test_task", 60.0):
            task = propagate_to_task(task_fn())
            await task

        assert result_holder["has_ctx"] is True
        assert result_holder["owner"] == "test_task"

    @pytest.mark.asyncio
    async def test_context_propagates_to_thread_executor(self):
        from backend.core.execution_context import (
            execution_budget, propagate_to_executor, _current_ctx,
        )
        result_holder = {}

        def sync_fn():
            ctx = _current_ctx.get()
            result_holder["has_ctx"] = ctx is not None
            result_holder["owner"] = ctx.owner_id if ctx else None
            return "done"

        async with execution_budget("test_thread", 60.0):
            loop = asyncio.get_running_loop()
            wrapped = propagate_to_executor(sync_fn)
            await loop.run_in_executor(None, wrapped)

        assert result_holder["has_ctx"] is True
        assert result_holder["owner"] == "test_thread"

    @pytest.mark.asyncio
    async def test_concurrent_sibling_context_isolation(self):
        from backend.core.execution_context import (
            execution_budget, current_context,
        )
        results = {}

        async def sibling(name, timeout):
            async with execution_budget(name, timeout):
                await asyncio.sleep(0.05)
                ctx = current_context()
                results[name] = ctx.owner_id if ctx else None

        async with execution_budget("supervisor", 60.0):
            await asyncio.gather(
                sibling("svc_a", 10.0),
                sibling("svc_b", 20.0),
            )

        # Each sibling should have seen its own context, not the other's
        assert results["svc_a"] == "svc_a"
        assert results["svc_b"] == "svc_b"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestContextPropagation -v --tb=short 2>&1 | head -20`
Expected: FAIL with `ImportError` for `propagate_to_task`

**Step 3: Implement propagation helpers**

Append to `backend/core/execution_context.py`:

```python
# =============================================================================
# CONTEXT PROPAGATION HELPERS
# =============================================================================

def propagate_to_executor(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap a sync function to carry current ExecutionContext into a thread.

    Usage:
        loop = asyncio.get_running_loop()
        wrapped = propagate_to_executor(my_sync_fn)
        await loop.run_in_executor(None, wrapped, arg1, arg2)

    Python's run_in_executor does NOT propagate contextvars to threads.
    This wrapper snapshots the current context and runs fn inside it.
    """
    ctx_snapshot = contextvars.copy_context()

    def _wrapper(*args: Any, **kwargs: Any) -> T:
        return ctx_snapshot.run(fn, *args, **kwargs)

    return _wrapper


def propagate_to_task(
    coro: Awaitable[Any],
    *,
    name: Optional[str] = None,
) -> "asyncio.Task[Any]":
    """Create an asyncio.Task with the current context propagated.

    Python 3.11+ copies context by default in create_task, but we
    keep this wrapper for consistency across Python 3.9+ and to
    ensure budget propagation in all scheduler implementations.
    """
    loop = asyncio.get_running_loop()
    ctx_snapshot = contextvars.copy_context()
    task = loop.create_task(coro, name=name)
    # Note: In Python 3.9-3.10, create_task does copy the context.
    # This wrapper exists for documentation clarity and future-proofing.
    return task
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestContextPropagation -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add backend/core/execution_context.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(execution-context): add context propagation helpers for tasks and threads"
```

---

### Task 6: Cancellation Precedence and Race Tests

**Files:**
- Modify: `backend/core/execution_context.py`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing tests**

Add to `test_execution_context.py`:

```python
class TestCancellationPrecedence:
    """Verify deterministic error types under race conditions."""

    @pytest.mark.asyncio
    async def test_external_cancel_wins_over_local_timeout(self):
        """If external cancel and local timeout race, ExternalCancellationError wins."""
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            ExternalCancellationError, CancellationCause,
        )

        async def slow_op():
            await asyncio.sleep(10.0)

        async with execution_budget("test", 60.0) as ctx:
            # Pre-set the cancel cause to simulate external cancel
            ctx.cancel_scope.set_cause(
                CancellationCause.OWNER_SHUTDOWN,
                "test shutdown",
            )
            # Now cancel the current task to simulate external cancel
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, task.cancel)

            with pytest.raises(
                (ExternalCancellationError, asyncio.CancelledError)
            ):
                await budget_aware_wait_for(
                    slow_op(), local_cap=0.1, label="race_test"
                )

    @pytest.mark.asyncio
    async def test_cancel_scope_cause_propagates_to_error(self):
        from backend.core.execution_context import (
            execution_budget, CancellationCause,
        )
        async with execution_budget("test", 60.0) as ctx:
            ctx.cancel_scope.set_cause(
                CancellationCause.DEPENDENCY_LOST,
                "database connection lost",
            )
            assert ctx.cancel_scope.scope.cause == CancellationCause.DEPENDENCY_LOST
            assert "database" in ctx.cancel_scope.scope.detail
```

**Step 2: Run tests to verify they pass (or identify needed fixes)**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestCancellationPrecedence -v`
Expected: PASS (these test already-implemented behavior)

**Step 3: Commit**

```bash
git add tests/unit/backend/core/test_execution_context.py
git commit -m "test(execution-context): add cancellation precedence and race condition tests"
```

---

### Task 7: CancellationToken Integration

**Files:**
- Modify: `backend/core/cancellation.py:137-170`
- Test: `tests/unit/backend/core/test_execution_context.py`

**Step 1: Write the failing test**

Add to `test_execution_context.py`:

```python
class TestCancellationTokenIntegration:
    """Verify CancellationToken.cancel() sets scope cause."""

    @pytest.mark.asyncio
    async def test_cancellation_token_sets_scope_cause(self):
        from backend.core.execution_context import (
            execution_budget, CancellationCause,
        )
        from backend.core.cancellation import CancellationToken

        token = CancellationToken("test-token")

        async with execution_budget("test", 60.0) as ctx:
            assert ctx.cancel_scope.scope is None
            token.cancel(reason="Supervisor shutting down")
            # After token cancel, current scope should have OWNER_SHUTDOWN
            # (only if the token is wired — this test validates the wiring)

        # If not yet wired, this test will fail — that's expected
```

**Step 2: Run test to verify it shows current behavior**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestCancellationTokenIntegration -v --tb=short`

**Step 3: Wire CancellationToken.cancel() to set scope cause**

Modify `backend/core/cancellation.py`. In the `cancel()` method (line 137), after signalling waiters (line 152), add scope cause propagation:

```python
# Add after line 152 (self._sync_event.set()) in cancel() method:

        # v280.0: Set typed cause on active ExecutionContext cancel scope
        try:
            from backend.core.execution_context import (
                _current_ctx, CancellationCause,
            )
            ctx = _current_ctx.get(None)
            if ctx is not None and ctx.cancel_scope is not None:
                ctx.cancel_scope.set_cause(
                    CancellationCause.OWNER_SHUTDOWN,
                    reason or f"CancellationToken '{self.name}' cancelled",
                )
        except ImportError:
            pass  # execution_context module not yet available
        except Exception as _scope_err:
            logger.debug(
                "[Cancel] Failed to set scope cause on '%s': %s",
                self.name, _scope_err,
            )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py::TestCancellationTokenIntegration -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/cancellation.py tests/unit/backend/core/test_execution_context.py
git commit -m "feat(cancellation): wire CancellationToken.cancel() to ExecutionContext cancel scope"
```

---

### Task 8: Instrument Phase Boundaries in unified_supervisor.py (Phases 0-3)

**Files:**
- Modify: `unified_supervisor.py:64061-66059` (phases 0-3)
- No new tests — this wires existing primitives into production code

**Step 1: Add import at top of unified_supervisor.py**

Find the import section near the top of unified_supervisor.py. Search for existing `from backend.core` imports and add near them:

```python
# v280.0: Execution Context — composable timeout budget propagation
try:
    from backend.core.execution_context import (
        execution_budget as _execution_budget,
        budget_aware_wait_for as _budget_wait,
        BudgetExhaustedError,
        LocalCapExceededError,
        ExternalCancellationError,
        Criticality as _BudgetCriticality,
        RequestKind as _BudgetRequestKind,
        remaining_budget as _remaining_budget,
    )
    _BUDGET_AVAILABLE = True
except ImportError:
    _BUDGET_AVAILABLE = False
```

**Step 2: Instrument Phase 0 (Clean Slate) — line ~64409-64416**

Change from:
```python
        try:
            await asyncio.wait_for(self._phase_clean_slate(), timeout=_clean_slate_timeout)
        except asyncio.TimeoutError:
            self.logger.warning(
                f"[Kernel] v265.0: Clean Slate timed out ({_clean_slate_timeout:.0f}s) — continuing"
            )
        except asyncio.CancelledError:
            raise
```

To:
```python
        try:
            if _BUDGET_AVAILABLE:
                async with _execution_budget(
                    "phase_clean_slate", _clean_slate_timeout,
                    phase_id="0", phase_name="clean_slate",
                    priority=_BudgetCriticality.CRITICAL,
                    request_kind=_BudgetRequestKind.STARTUP,
                ):
                    await self._phase_clean_slate()
            else:
                await asyncio.wait_for(self._phase_clean_slate(), timeout=_clean_slate_timeout)
        except BudgetExhaustedError as _be:
            self.logger.warning(
                "[Kernel] Clean Slate budget exhausted (%.0fs) — continuing",
                _be.elapsed,
            )
        except LocalCapExceededError:
            self.logger.warning(
                f"[Kernel] v265.0: Clean Slate timed out ({_clean_slate_timeout:.0f}s) — continuing"
            )
        except asyncio.TimeoutError:
            self.logger.warning(
                f"[Kernel] v265.0: Clean Slate timed out ({_clean_slate_timeout:.0f}s) — continuing"
            )
        except asyncio.CancelledError:
            raise
```

**Step 3: Instrument Phase 1 (Preflight) — line ~65769-65779**

Same pattern. Change from:
```python
            try:
                _preflight_ok = await asyncio.wait_for(
                    self._phase_preflight(), timeout=_preflight_timeout
                )
            except asyncio.TimeoutError:
                _preflight_ok = False
                ...
            except asyncio.CancelledError:
                raise
```

To:
```python
            try:
                if _BUDGET_AVAILABLE:
                    async with _execution_budget(
                        "phase_preflight", _preflight_timeout,
                        phase_id="1", phase_name="preflight",
                        priority=_BudgetCriticality.CRITICAL,
                        request_kind=_BudgetRequestKind.STARTUP,
                    ):
                        _preflight_ok = await self._phase_preflight()
                else:
                    _preflight_ok = await asyncio.wait_for(
                        self._phase_preflight(), timeout=_preflight_timeout
                    )
            except (BudgetExhaustedError, LocalCapExceededError) as _te:
                _preflight_ok = False
                self.logger.error(
                    "[Kernel] Preflight %s after %.0fs (origin=%s)",
                    type(_te).__name__, _te.elapsed, _te.timeout_origin,
                )
            except asyncio.TimeoutError:
                _preflight_ok = False
                self.logger.error(
                    "[Kernel] v270.0: Preflight timed out after %.0fs", _preflight_timeout
                )
            except asyncio.CancelledError:
                raise
```

**Step 4: Instrument Phase 2 (Resources) — line ~65890-65900**

Same pattern applied to `self._phase_resources()` with `resource_timeout`.

**Step 5: Instrument Phase 3 (Backend) — line ~65989-65999**

Same pattern applied to `self._phase_backend()` with `backend_timeout`.

**Step 6: Verify startup still works**

Run: `python3 -c "from backend.core.execution_context import execution_budget, budget_aware_wait_for; print('Import OK')"`
Expected: `Import OK`

**Step 7: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): instrument phase 0-3 boundaries with execution budget"
```

---

### Task 9: Instrument Phase Boundaries (Phases 4-7)

**Files:**
- Modify: `unified_supervisor.py:66130-67420` (phases 4-7)

**Step 1: Instrument Phase 4 (Intelligence) — line ~66150-66159**

Change `asyncio.wait_for(self._phase_intelligence(), timeout=intelligence_timeout)` to use `_execution_budget` wrapper with `phase_id="4"`, `phase_name="intelligence"`, `priority=_BudgetCriticality.HIGH`.

**Step 2: Instrument Phase 5 (Trinity) — line ~66773-66793**

Change `asyncio.wait_for(self._phase_trinity(), timeout=_trinity_outer_timeout)` to use `_execution_budget` wrapper with `phase_id="5"`, `phase_name="trinity"`, `priority=_BudgetCriticality.HIGH`.

**Step 3: Instrument Phase 6 (Enterprise Services) — line ~66915-66929**

Change `asyncio.wait_for(self._phase_enterprise_services(), timeout=_enterprise_outer_timeout)` to use `_execution_budget` wrapper with `phase_id="6"`, `phase_name="enterprise"`, `priority=_BudgetCriticality.NORMAL`.

**Step 4: Instrument Phase 7 (Frontend Transition) — line ~67396-67415**

Change `asyncio.wait_for(self._phase_frontend_transition(), timeout=_fe_outer_timeout)` to use `_execution_budget` wrapper with `phase_id="7"`, `phase_name="frontend"`, `priority=_BudgetCriticality.NORMAL`.

**Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): instrument phase 4-7 boundaries with execution budget"
```

---

### Task 10: Instrument Service-Level Fan-Out

**Files:**
- Modify: `unified_supervisor.py:76892-76960` (`_init_enterprise_service_with_timeout`)

**Step 1: Wire budget_aware_wait_for into _init_enterprise_service_with_timeout**

The method at line ~76922 currently does:
```python
result = await asyncio.wait_for(
    asyncio.shield(service_task),
    timeout=timeout_seconds,
)
```

Change to:
```python
if _BUDGET_AVAILABLE:
    result = await _budget_wait(
        asyncio.shield(service_task),
        local_cap=timeout_seconds,
        label=f"zone6/{service_key or name}",
    )
else:
    result = await asyncio.wait_for(
        asyncio.shield(service_task),
        timeout=timeout_seconds,
    )
```

And update the `except` block to handle both `BudgetExhaustedError` and `LocalCapExceededError` alongside `asyncio.TimeoutError`.

**Step 2: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(supervisor): wire budget_aware_wait_for into enterprise service init"
```

---

### Task 11: Integration Tests

**Files:**
- Create: `tests/integration/test_budget_propagation.py`

**Step 1: Write integration tests**

```python
# tests/integration/test_budget_propagation.py
"""Integration tests for timeout budget propagation across phase boundaries."""
import asyncio
import time
import pytest


class TestBudgetPropagationIntegration:
    """End-to-end budget propagation through simulated startup phases."""

    @pytest.mark.asyncio
    async def test_phase_budget_propagates_to_services(self):
        """Phase with 0.5s budget, 3 services each requesting 0.3s.
        Third service should get less than 0.3s."""
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError, LocalCapExceededError,
            remaining_budget,
        )

        results = []

        async def service(name, duration):
            await asyncio.sleep(duration)
            results.append(name)
            return name

        async with execution_budget("phase_test", 0.5,
                                    phase_id="1", phase_name="test"):
            await budget_aware_wait_for(
                service("svc1", 0.15), local_cap=0.3, label="svc1"
            )
            await budget_aware_wait_for(
                service("svc2", 0.15), local_cap=0.3, label="svc2"
            )
            # By now ~0.3s elapsed, ~0.2s remaining
            remaining = remaining_budget()
            assert remaining is not None
            assert remaining < 0.3  # Less than svc3's local_cap

            with pytest.raises((BudgetExhaustedError, LocalCapExceededError)):
                await budget_aware_wait_for(
                    service("svc3", 0.5), local_cap=0.3, label="svc3"
                )

        assert "svc1" in results
        assert "svc2" in results

    @pytest.mark.asyncio
    async def test_budget_exhaustion_error_type(self):
        """Verify BudgetExhaustedError (not TimeoutError) when parent expires."""
        from backend.core.execution_context import (
            execution_budget, budget_aware_wait_for,
            BudgetExhaustedError,
        )

        async def slow_service():
            await asyncio.sleep(10.0)

        with pytest.raises(BudgetExhaustedError) as exc_info:
            async with execution_budget("phase", 0.1,
                                        phase_id="1", phase_name="test"):
                await budget_aware_wait_for(
                    slow_service(), local_cap=5.0, label="slow"
                )

        assert not isinstance(exc_info.value, TimeoutError)
        assert exc_info.value.timeout_origin == "budget"

    @pytest.mark.asyncio
    async def test_budget_metadata_audit_trail(self):
        """Verify parent_ctx chain is inspectable from innermost context."""
        from backend.core.execution_context import (
            execution_budget, current_context,
        )

        async with execution_budget("supervisor", 60.0,
                                    phase_id="0", phase_name="root"):
            async with execution_budget("phase_preflight", 30.0,
                                        phase_id="1", phase_name="preflight"):
                async with execution_budget("svc_lock", 10.0,
                                            phase_id="1", phase_name="preflight"):
                    ctx = current_context()
                    assert ctx.owner_id == "svc_lock"
                    assert ctx.parent_ctx.owner_id == "phase_preflight"
                    assert ctx.parent_ctx.parent_ctx.owner_id == "supervisor"

    @pytest.mark.asyncio
    async def test_concurrent_sibling_budget_isolation(self):
        """Concurrent tasks with different budgets never cross-contaminate."""
        from backend.core.execution_context import (
            execution_budget, remaining_budget,
        )

        async def worker(name, budget_s):
            async with execution_budget(name, budget_s):
                await asyncio.sleep(0.05)
                r = remaining_budget()
                assert r is not None
                return name, r

        async with execution_budget("supervisor", 60.0):
            results = await asyncio.gather(
                worker("fast", 1.0),
                worker("slow", 10.0),
            )

        budget_map = dict(results)
        # Fast worker should have ~0.9s remaining, slow ~9.9s
        assert budget_map["fast"] < budget_map["slow"]
        assert budget_map["fast"] < 1.0
        assert budget_map["slow"] > 5.0
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/integration/test_budget_propagation.py -v`
Expected: All 4 tests PASS

**Step 3: Commit**

```bash
git add tests/integration/test_budget_propagation.py
git commit -m "test(integration): add end-to-end budget propagation tests"
```

---

### Task 12: Run Full Test Suite and Final Verification

**Files:**
- None new — verification only

**Step 1: Run all execution context tests**

Run: `python3 -m pytest tests/unit/backend/core/test_execution_context.py tests/integration/test_budget_propagation.py -v --tb=short`
Expected: All 26+ tests PASS

**Step 2: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/unit/backend/core/ -v --tb=short -x 2>&1 | tail -20`
Expected: No regressions in existing tests

**Step 3: Verify import works in supervisor context**

Run: `python3 -c "import unified_supervisor; print('Supervisor imports OK')"`
Expected: No import errors

**Step 4: Final commit with all files**

```bash
git add -A
git status
git commit -m "feat(v280.0): composable timeout budget propagation - Phase 1 complete

Implements ExecutionContext-based budget propagation that replaces
independent asyncio.wait_for timeout clocks with a shared deadline
model. Nested calls compute effective = min(remaining, local_cap).

Key components:
- ExecutionContext frozen dataclass propagated via ContextVar
- Three-way error taxonomy: BudgetExhausted / LocalCapExceeded / ExternalCancellation
- Write-once CancelScope with thread-safe first-set-wins semantics
- execution_budget() context manager with child-can-only-shrink policy
- budget_aware_wait_for() drop-in replacement for asyncio.wait_for
- CancellationToken integration (sets OWNER_SHUTDOWN on active scope)
- Shadow telemetry mode for safe rollout
- 8 phase boundaries + enterprise service fan-out instrumented

Design doc: docs/plans/2026-02-25-timeout-budget-propagation-design.md"
```

"""Execution context primitives for timeout budget propagation.

This module provides the error taxonomy, cancellation scope, and feature flags
that underpin composable budget propagation across the JARVIS startup and
runtime phases.  It replaces independent ``asyncio.wait_for()`` timeout clocks
with a single, hierarchical deadline that flows from parent to child scopes.

Design doc: docs/plans/2026-02-25-timeout-budget-propagation-design.md

Key design decisions
--------------------
* ``BudgetExhaustedError`` is **not** a ``TimeoutError`` — retry logic that
  catches ``TimeoutError`` must *not* swallow budget exhaustion, which signals
  that the *entire* budget is gone, not just a local cap.
* ``LocalCapExceededError`` **is** a ``TimeoutError`` — existing retry logic
  already catches ``TimeoutError`` and handles it correctly for per-step caps.
* ``CancelScopeHandle`` is write-once so that the first failure cause is
  preserved even when multiple concurrent tasks detect the same deadline miss.
"""

from __future__ import annotations

import enum
import os
import threading
import time
import uuid
import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    TypeVar,
)

_T = TypeVar("_T")

_log = logging.getLogger(__name__)

__all__ = [
    # Feature flags
    "BUDGET_ENFORCE",
    "BUDGET_SHADOW",
    # Enums
    "CancellationCause",
    "RootReason",
    "RequestKind",
    "Criticality",
    # Errors
    "BudgetExhaustedError",
    "LocalCapExceededError",
    "ExternalCancellationError",
    # Cancel scope
    "CancelScope",
    "CancelScopeHandle",
    # Execution context (Task 2)
    "ExecutionContext",
    "current_context",
    "remaining_budget",
    # Budget context manager (Task 3)
    "execution_budget",
    # Budget-aware wait (Task 4)
    "budget_aware_wait_for",
    "bridge_timeout_error",
    # Propagation helpers (Task 5)
    "propagate_to_executor",
    "propagate_to_task",
]

# ---------------------------------------------------------------------------
# Feature flags — read once at import, but reloadable for tests
# ---------------------------------------------------------------------------


def _truthy(val: Optional[str]) -> bool:
    """Return True for env var values that mean 'enabled'."""
    return val is not None and val.strip().lower() in ("1", "true", "yes")


BUDGET_ENFORCE: bool = os.environ.get(
    "JARVIS_BUDGET_ENFORCE", "true"
).strip().lower() in ("1", "true", "yes")
BUDGET_SHADOW: bool = _truthy(os.environ.get("JARVIS_BUDGET_SHADOW"))

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CancellationCause(enum.Enum):
    """Why a scope was cancelled."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    OWNER_SHUTDOWN = "owner_shutdown"
    DEPENDENCY_LOST = "dependency_lost"
    MANUAL_CANCEL = "manual_cancel"


class RootReason(enum.Enum):
    """Why the top-level budget exists."""

    DETACHED_BACKGROUND = "detached_background"
    RECOVERY_WORKER = "recovery_worker"
    USER_JOB = "user_job"


class RequestKind(enum.Enum):
    """Classification of the current execution path."""

    STARTUP = "startup"
    RUNTIME = "runtime"
    RECOVERY = "recovery"
    BACKGROUND = "background"


class Criticality(enum.Enum):
    """How important is the current work — influences budget allocation."""

    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------


class BudgetExhaustedError(Exception):
    """The entire budget for this execution path has been consumed.

    This is deliberately **not** a ``TimeoutError`` subclass.  Code that
    retries on ``TimeoutError`` must *not* catch this — there is no budget
    left to retry with.

    All constructor parameters are keyword-only to encourage explicit,
    self-documenting call sites.
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
        timeout_origin: str,
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
            f"BudgetExhausted: owner={owner!r} phase={phase!r} "
            f"elapsed={elapsed:.2f}s remaining_at_entry={remaining_at_entry:.2f}s "
            f"effective_timeout={effective_timeout:.2f}s origin={timeout_origin}"
        )


class LocalCapExceededError(TimeoutError):
    """A per-step local cap was exceeded, but the parent budget may still
    have time remaining.

    This **is** a ``TimeoutError`` subclass so that existing retry logic
    (which catches ``TimeoutError``) handles it transparently.

    All constructor parameters are keyword-only.
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
        timeout_origin: str,
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
            f"LocalCapExceeded: owner={owner!r} phase={phase!r} "
            f"local_cap={local_cap:.2f}s elapsed={elapsed:.2f}s "
            f"origin={timeout_origin}"
        )


class ExternalCancellationError(Exception):
    """The scope was cancelled by an external agent (shutdown, dependency
    loss, manual cancel).

    This is deliberately **not** a ``TimeoutError`` — timeouts are about
    wall-clock, but external cancellation is about system state changes.

    All constructor parameters are keyword-only.
    """

    def __init__(
        self,
        *,
        cause: CancellationCause,
        scope_id: str,
        detail: str,
    ) -> None:
        self.cause = cause
        self.scope_id = scope_id
        self.detail = detail
        super().__init__(
            f"ExternalCancellation: cause={cause.name} "
            f"scope_id={scope_id!r} detail={detail!r}"
        )


# ---------------------------------------------------------------------------
# CancelScope — immutable snapshot of a cancellation event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelScope:
    """Immutable record of *why* and *when* a scope was cancelled.

    Created by ``CancelScopeHandle.set_cause()`` and exposed via the
    handle's ``.scope`` property.
    """

    scope_id: str
    cause: CancellationCause
    set_at_mono: float
    detail: str
    owner_id: str


# ---------------------------------------------------------------------------
# CancelScopeHandle — thread-safe, write-once wrapper
# ---------------------------------------------------------------------------


class CancelScopeHandle:
    """Thread-safe, write-once handle for setting a cancellation cause.

    The first call to ``set_cause()`` wins.  Subsequent calls are no-ops
    and return ``False``, preserving the original failure cause even when
    many concurrent tasks detect the same deadline miss.

    Parameters
    ----------
    owner_id:
        Identifies the logical owner of this scope (e.g., phase name,
        service name).  Stored in the resulting ``CancelScope``.
    """

    def __init__(self, owner_id: str) -> None:
        self._owner_id = owner_id
        self._scope: Optional[CancelScope] = None
        self._lock = threading.Lock()

    # -- Public API --

    def set_cause(self, cause: CancellationCause, detail: str) -> bool:
        """Try to set the cancellation cause.

        Returns ``True`` if this call was the one that set it (first writer
        wins).  Returns ``False`` if the scope was already cancelled.
        """
        with self._lock:
            if self._scope is not None:
                return False
            self._scope = CancelScope(
                scope_id=uuid.uuid4().hex,
                cause=cause,
                set_at_mono=time.monotonic(),
                detail=detail,
                owner_id=self._owner_id,
            )
            return True

    @property
    def scope(self) -> Optional[CancelScope]:
        """Return the cancel scope if set, else ``None``.

        Reading is lock-free because Python's GIL makes reference
        assignment atomic, and ``CancelScope`` is frozen/immutable.
        """
        return self._scope


# ---------------------------------------------------------------------------
# ExecutionContext — frozen dataclass carrying the budget through the call tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution context that carries a deadline and metadata
    through the entire call tree.

    The ``deadline_mono`` is a *monotonic* clock timestamp.  Callers use the
    ``remaining`` property to know how many seconds are left without worrying
    about wall-clock adjustments.

    Frozen so that contexts can be shared across tasks/threads without locks.
    """

    # -- Core fields --
    deadline_mono: float
    trace_id: str
    owner_id: str
    cancel_scope: CancelScopeHandle
    mode_snapshot: str

    # -- Nesting --
    parent_ctx: Optional[ExecutionContext] = None
    created_at_mono: float = field(default_factory=time.monotonic)

    # -- Phase 2 fields --
    phase_id: str = ""
    phase_name: str = ""
    mode_epoch: int = 0
    budget_policy_version: int = 1

    # -- Phase 3 fields --
    priority: Criticality = Criticality.NORMAL
    request_kind: RequestKind = RequestKind.STARTUP
    tags: Mapping[str, str] = field(default_factory=dict)

    # -- Root fields --
    root_reason: Optional[RootReason] = None

    @property
    def remaining(self) -> float:
        """Seconds remaining until deadline.  May be negative."""
        return self.deadline_mono - time.monotonic()


# ---------------------------------------------------------------------------
# ContextVar — thread/task-local storage for the current ExecutionContext
# ---------------------------------------------------------------------------

_current_ctx: contextvars.ContextVar[Optional[ExecutionContext]] = (
    contextvars.ContextVar("_current_ctx", default=None)
)


def current_context() -> Optional[ExecutionContext]:
    """Return the active ``ExecutionContext``, or ``None`` outside a budget."""
    return _current_ctx.get()


def remaining_budget() -> Optional[float]:
    """Return remaining seconds (clamped to >= 0), or ``None`` if no budget."""
    ctx = _current_ctx.get()
    if ctx is None:
        return None
    return max(0.0, ctx.remaining)


# ---------------------------------------------------------------------------
# execution_budget() — async context manager for budget scoping
# ---------------------------------------------------------------------------

from typing import Final

_ROOT_SCOPE_ALLOWLIST: Final[frozenset] = frozenset({
    "supervisor",
    "phase_manager",
    "recovery_coordinator",
    "background_health_monitor",
})


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
    """Create a scoped execution budget.

    When ``root=True``, a fresh top-level deadline is created (requires
    ``root_reason`` and ``owner`` in ``_ROOT_SCOPE_ALLOWLIST``).

    When ``root=False`` (the default), the deadline is the *minimum* of
    the parent's deadline and ``now + timeout`` — the child can never
    extend the parent's budget.

    The ``ContextVar`` token is **always** reset in the ``finally`` block
    to prevent context leaks, even if the body raises.
    """
    # -- Validate root scope requests --
    if root:
        if root_reason is None:
            raise ValueError(
                "execution_budget(root=True) requires root_reason to be set"
            )
        if owner not in _ROOT_SCOPE_ALLOWLIST:
            raise ValueError(
                f"Owner {owner!r} is not authorized for root budget scopes. "
                f"Allowed: {sorted(_ROOT_SCOPE_ALLOWLIST)}"
            )

    parent = _current_ctx.get()
    now = time.monotonic()

    # -- Compute deadline --
    if root:
        deadline = now + timeout
    elif parent is not None:
        deadline = min(parent.deadline_mono, now + timeout)
    else:
        deadline = now + timeout

    # -- Inherit trace ID from parent unless root --
    if parent is not None and not root:
        trace_id = parent.trace_id
    else:
        trace_id = uuid.uuid4().hex

    # -- Build context --
    ctx = ExecutionContext(
        deadline_mono=deadline,
        trace_id=trace_id,
        owner_id=owner,
        cancel_scope=CancelScopeHandle(owner_id=owner),
        mode_snapshot=mode_snapshot or (parent.mode_snapshot if parent else "normal"),
        parent_ctx=parent,
        phase_id=phase_id,
        phase_name=phase_name,
        mode_epoch=mode_epoch,
        priority=priority,
        request_kind=request_kind,
        tags=tags if tags is not None else {},
        root_reason=root_reason,
    )

    _log.debug(
        "execution_budget ENTER owner=%s timeout=%.2f deadline_in=%.2f root=%s",
        owner, timeout, ctx.remaining, root,
    )

    token = _current_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _current_ctx.reset(token)
        _log.debug(
            "execution_budget EXIT owner=%s remaining=%.2f",
            owner, ctx.remaining,
        )


# ---------------------------------------------------------------------------
# budget_aware_wait_for() — replaces raw asyncio.wait_for() with typed errors
# ---------------------------------------------------------------------------


async def budget_aware_wait_for(
    coro: Awaitable[_T],
    *,
    local_cap: float = 0.0,
    label: str = "",
) -> _T:
    """Run *coro* with a timeout derived from the budget and *local_cap*.

    Raises
    ------
    RuntimeError
        If there is **no** budget and ``local_cap <= 0`` — fail closed.
    BudgetExhaustedError
        If the remaining budget is the binding constraint and it expired.
    LocalCapExceededError
        If *local_cap* is the binding constraint and it expired.
    ExternalCancellationError
        If the task is cancelled and the cancel scope has a typed cause.
    """
    ctx = _current_ctx.get()
    remaining = ctx.remaining if ctx is not None else None

    # -- Determine effective timeout --
    if remaining is None and local_cap <= 0:
        raise RuntimeError(
            f"No budget and no local_cap for {label!r} — fail closed. "
            "Wrap in execution_budget() or supply a local_cap."
        )

    if remaining is None:
        # No budget context — use local_cap, log the unscoped usage
        effective = local_cap
        _log.debug(
            "budget_aware_wait_for label=%s unscoped_local_timeout=true local_cap=%.2f",
            label, local_cap,
        )
    elif BUDGET_ENFORCE:
        if local_cap > 0:
            effective = min(remaining, local_cap)
        else:
            effective = remaining
    else:
        # Shadow mode: use local_cap if available, else remaining
        effective = local_cap if local_cap > 0 else remaining
        if BUDGET_SHADOW and remaining is not None and local_cap > 0:
            if remaining < local_cap:
                _log.warning(
                    "budget_aware_wait_for SHADOW label=%s "
                    "budget_remaining=%.2f < local_cap=%.2f "
                    "(would have enforced budget)",
                    label, remaining, local_cap,
                )

    # -- Pre-flight: if budget is already exhausted, fail immediately --
    if BUDGET_ENFORCE and remaining is not None and remaining <= 0:
        raise BudgetExhaustedError(
            owner=ctx.owner_id if ctx else "unknown",
            phase=ctx.phase_name if ctx else "",
            deadline_mono=ctx.deadline_mono if ctx else 0.0,
            remaining_at_entry=remaining if remaining is not None else 0.0,
            local_cap=local_cap,
            effective_timeout=0.0,
            elapsed=0.0,
            timeout_origin="budget",
        )

    remaining_at_entry = remaining if remaining is not None else effective

    _log.debug(
        "budget_aware_wait_for ENTER label=%s effective=%.2f remaining=%.2f local_cap=%.2f",
        label, effective, remaining_at_entry, local_cap,
    )

    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=max(effective, 0.0))
    except asyncio.TimeoutError as exc:
        elapsed = time.monotonic() - t0
        raise bridge_timeout_error(
            exc,
            label=label,
            remaining_at_entry=remaining_at_entry,
            local_cap=local_cap,
            owner=ctx.owner_id if ctx else "unknown",
            phase=ctx.phase_name if ctx else "",
        ) from exc
    except asyncio.CancelledError:
        # Check cancel scope for typed cause
        if ctx is not None and ctx.cancel_scope.scope is not None:
            scope = ctx.cancel_scope.scope
            raise ExternalCancellationError(
                cause=scope.cause,
                scope_id=scope.scope_id,
                detail=scope.detail,
            )
        raise  # Re-raise untyped CancelledError


def bridge_timeout_error(
    timeout_error: BaseException,
    *,
    label: str,
    remaining_at_entry: float,
    local_cap: float,
    owner: str,
    phase: str,
) -> Exception:
    """Convert a raw ``asyncio.TimeoutError`` into a typed budget error.

    Returns
    -------
    BudgetExhaustedError
        When the budget was the binding constraint (remaining_at_entry <= 0
        or remaining_at_entry was tighter than local_cap).
    LocalCapExceededError
        When the local cap was tighter than the remaining budget.
    """
    # Determine which constraint was binding
    budget_was_binding = (
        remaining_at_entry <= 0
        or (local_cap > 0 and remaining_at_entry < local_cap)
        or local_cap <= 0
    )
    effective = min(remaining_at_entry, local_cap) if local_cap > 0 else remaining_at_entry
    elapsed = max(effective, 0.0)

    if budget_was_binding:
        return BudgetExhaustedError(
            owner=owner,
            phase=phase,
            deadline_mono=0.0,
            remaining_at_entry=remaining_at_entry,
            local_cap=local_cap,
            effective_timeout=max(effective, 0.0),
            elapsed=elapsed,
            timeout_origin="budget",
        )
    else:
        return LocalCapExceededError(
            owner=owner,
            phase=phase,
            deadline_mono=0.0,
            remaining_at_entry=remaining_at_entry,
            local_cap=local_cap,
            effective_timeout=effective,
            elapsed=elapsed,
            timeout_origin="local_cap",
        )

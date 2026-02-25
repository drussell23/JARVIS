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
from dataclasses import dataclass, field
from typing import Optional

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
]

# ---------------------------------------------------------------------------
# Feature flags — read once at import, but reloadable for tests
# ---------------------------------------------------------------------------


def _truthy(val: Optional[str]) -> bool:
    """Return True for env var values that mean 'enabled'."""
    return val is not None and val.strip().lower() in ("1", "true", "yes")


BUDGET_ENFORCE: bool = _truthy(os.environ.get("JARVIS_BUDGET_ENFORCE"))
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

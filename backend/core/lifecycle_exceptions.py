"""
Lifecycle Exception Taxonomy (Disease 5+6 MVP)
===============================================
Typed exception hierarchy for lifecycle state machine and
exception policy enforcement.

Hierarchy:
  BaseException
  └── LifecycleSignal          (control-flow, never swallowed)
      ├── ShutdownRequested
      └── LifecycleCancelled
  Exception
  └── LifecycleError           (policy errors with state context)
      ├── LifecycleFatalError
      ├── LifecycleRecoverableError
      │   └── DependencyUnavailableError
      └── TransitionRejected
"""
from enum import Enum
from typing import Optional


class LifecyclePhase(str, Enum):
    """Coarse lifecycle phase. Used in error context, not state machine."""
    PRECHECK = "precheck"
    BRINGUP = "bringup"
    CONTRACT_GATE = "contract_gate"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"


class LifecycleErrorCode(str, Enum):
    """Machine-readable error codes for deterministic policy routing."""
    DEP_UNREACHABLE = "dep_unreachable"
    CONTRACT_INCOMPATIBLE = "contract_incompatible"
    TRANSITION_INVALID = "transition_invalid"
    SHUTDOWN_REENTRANT = "shutdown_reentrant"
    TASK_ORPHAN_DETECTED = "task_orphan_detected"
    EPOCH_STALE = "epoch_stale"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"


# =========================================================================
# CONTROL-FLOW SIGNALS (BaseException — never swallowed)
# =========================================================================

# NOTE: We cannot use @dataclass on BaseException subclasses in Python 3.9
# because BaseException.__init__ has incompatible signature. Instead we
# use __init_subclass__ + explicit __init__ with frozen-like semantics.

class LifecycleSignal(BaseException):
    """Control-flow signal, not an error. Must never be swallowed.

    Catch only to annotate and re-raise.
    """
    __slots__ = ("reason", "epoch", "requested_by", "at_monotonic")

    def __init__(self, *, reason: str, epoch: int,
                 requested_by: str, at_monotonic: float):
        self.reason = reason
        self.epoch = epoch
        self.requested_by = requested_by
        self.at_monotonic = at_monotonic
        super().__init__(reason)

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise AttributeError(f"Cannot modify {name} on frozen LifecycleSignal")
        super().__setattr__(name, value)


class ShutdownRequested(LifecycleSignal):
    """Operator/system/watchdog requested graceful shutdown."""
    pass


class LifecycleCancelled(LifecycleSignal):
    """Cooperative cancellation wrapping CancelledError metadata."""
    __slots__ = ("cancelled_task",)

    def __init__(self, *, cancelled_task: str = "", **kwargs):
        super().__init__(**kwargs)
        # Use BaseException.__setattr__ to bypass frozen guard
        BaseException.__setattr__(self, "cancelled_task", cancelled_task)


# =========================================================================
# LIFECYCLE ERRORS (Exception — catchable with policy)
# =========================================================================

class LifecycleError(Exception):
    """Base for all lifecycle errors. Carries state context + staleness guard.

    Catch policy:
      - LifecycleFatalError: catch to trigger deterministic FAILED transition
      - LifecycleRecoverableError: catch only where retry policy is explicit
      - TransitionRejected: log at INFO, do not escalate unless threshold exceeded
      - except Exception: allowed ONLY at top supervisory boundary
    """

    def __init__(self, message: str, *, error_code: str,
                 state_at_raise: str, phase: LifecyclePhase,
                 epoch: int, cause: Optional[Exception] = None):
        self.error_code = error_code
        self.state_at_raise = state_at_raise
        self.phase = phase
        self.epoch = epoch
        self.cause = cause
        super().__init__(message)


class LifecycleFatalError(LifecycleError):
    """Unrecoverable. Triggers deterministic FAILED transition."""
    pass


class LifecycleRecoverableError(LifecycleError):
    """Retry-eligible. Caller must apply explicit retry policy."""

    def __init__(self, message: str, *, retry_hint: str = "backoff", **kwargs):
        self.retry_hint = retry_hint  # "backoff", "immediate", "deferred"
        super().__init__(message, **kwargs)


class DependencyUnavailableError(LifecycleRecoverableError):
    """External dependency missing or unreachable."""

    def __init__(self, message: str, *, dependency: str,
                 fallback_available: bool = False, **kwargs):
        self.dependency = dependency
        self.fallback_available = fallback_available
        super().__init__(message, **kwargs)


class TransitionRejected(LifecycleError):
    """Non-fatal rejection for expected races (duplicate shutdown, etc.).

    Observability-visible but not escalated as fatal/alert
    unless threshold exceeded.
    """
    pass

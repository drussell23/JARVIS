# Exception & Lifecycle Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Cure Disease 5 (exception swallowing) and Disease 6 (no lifecycle state machine) via a scoped MVP: typed exception hierarchy, guarded state machine, single signal authority, and top-20 dangerous handler fixes.

**Architecture:** Three new modules (`lifecycle_exceptions.py`, `lifecycle_engine.py`, `signal_authority.py`) provide the exception taxonomy, transition-guarded state machine, and centralized signal handling. The supervisor's 14 direct `self._state =` writes are replaced with `engine.transition()` calls. The top-20 most dangerous silent `except Exception: pass` handlers in startup/shutdown paths are replaced with typed, logged handling. CI grep rules prevent regression.

**Tech Stack:** Python 3.9+, dataclasses, enums, asyncio, threading.Lock, signal, collections.deque

---

### Task 1: LifecyclePhase + LifecycleErrorCode enums

**Files:**
- Create: `backend/core/lifecycle_exceptions.py`
- Test: `tests/unit/backend/test_lifecycle_exceptions.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/test_lifecycle_exceptions.py`:

```python
#!/usr/bin/env python3
"""Tests for lifecycle exception taxonomy (Disease 5+6 MVP)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.lifecycle_exceptions import LifecyclePhase, LifecycleErrorCode


class TestLifecyclePhaseEnum:
    def test_all_phases_exist(self):
        assert LifecyclePhase.PRECHECK == "precheck"
        assert LifecyclePhase.BRINGUP == "bringup"
        assert LifecyclePhase.CONTRACT_GATE == "contract_gate"
        assert LifecyclePhase.RUNNING == "running"
        assert LifecyclePhase.DRAINING == "draining"
        assert LifecyclePhase.STOPPING == "stopping"
        assert LifecyclePhase.STOPPED == "stopped"

    def test_exactly_seven_phases(self):
        assert len(LifecyclePhase) == 7


class TestLifecycleErrorCodeEnum:
    @pytest.mark.parametrize("code", [
        "dep_unreachable", "contract_incompatible", "transition_invalid",
        "shutdown_reentrant", "task_orphan_detected", "epoch_stale",
        "timeout_exceeded", "resource_exhausted",
    ])
    def test_error_code_exists(self, code):
        assert LifecycleErrorCode(code) == code

    def test_exactly_eight_codes(self):
        assert len(LifecycleErrorCode) == 8
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_exceptions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.lifecycle_exceptions'`

**Step 3: Write minimal implementation**

Create `backend/core/lifecycle_exceptions.py`:

```python
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
from dataclasses import dataclass
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
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_exceptions.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/lifecycle_exceptions.py tests/unit/backend/test_lifecycle_exceptions.py
git commit -m "feat(lifecycle): add LifecyclePhase and LifecycleErrorCode enums (Disease 5+6, Task 1)"
```

---

### Task 2: LifecycleSignal + ShutdownRequested + LifecycleCancelled

**Files:**
- Modify: `backend/core/lifecycle_exceptions.py`
- Test: `tests/unit/backend/test_lifecycle_exceptions.py`

**Step 1: Write the failing test**

Add to test file:

```python
from backend.core.lifecycle_exceptions import (
    LifecycleSignal, ShutdownRequested, LifecycleCancelled,
)


class TestLifecycleSignals:
    """Control-flow signals (BaseException) must never be caught by except Exception."""

    def test_lifecycle_signal_is_base_exception(self):
        assert issubclass(LifecycleSignal, BaseException)
        assert not issubclass(LifecycleSignal, Exception)

    def test_shutdown_requested_fields(self):
        sig = ShutdownRequested(
            reason="operator", epoch=1,
            requested_by="signal:SIGTERM", at_monotonic=1000.0,
        )
        assert sig.reason == "operator"
        assert sig.epoch == 1
        assert sig.requested_by == "signal:SIGTERM"
        assert sig.at_monotonic == 1000.0

    def test_shutdown_requested_is_frozen(self):
        sig = ShutdownRequested(
            reason="test", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        with pytest.raises(AttributeError):
            sig.reason = "changed"

    def test_lifecycle_cancelled_fields(self):
        sig = LifecycleCancelled(
            reason="task timeout", epoch=2,
            requested_by="watchdog", at_monotonic=2000.0,
            cancelled_task="health_monitor",
        )
        assert sig.cancelled_task == "health_monitor"

    def test_lifecycle_cancelled_default_task(self):
        sig = LifecycleCancelled(
            reason="cancel", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        assert sig.cancelled_task == ""

    def test_except_exception_does_not_catch_signal(self):
        sig = ShutdownRequested(
            reason="test", epoch=0, requested_by="test", at_monotonic=0.0,
        )
        caught_by_exception = False
        try:
            raise sig
        except Exception:
            caught_by_exception = True
        except BaseException:
            pass
        assert not caught_by_exception, "except Exception must NOT catch LifecycleSignal"
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add to `backend/core/lifecycle_exceptions.py`:

```python
import time as _time


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
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_exceptions.py -v`

**Step 5: Commit**

```bash
git add backend/core/lifecycle_exceptions.py tests/unit/backend/test_lifecycle_exceptions.py
git commit -m "feat(lifecycle): add LifecycleSignal, ShutdownRequested, LifecycleCancelled (Disease 5+6, Task 2)"
```

---

### Task 3: LifecycleError hierarchy + TransitionRejected

**Files:**
- Modify: `backend/core/lifecycle_exceptions.py`
- Test: `tests/unit/backend/test_lifecycle_exceptions.py`

**Step 1: Write the failing test**

Add to test file:

```python
from backend.core.lifecycle_exceptions import (
    LifecycleError, LifecycleFatalError, LifecycleRecoverableError,
    DependencyUnavailableError, TransitionRejected,
)


class TestLifecycleErrors:
    """Lifecycle errors carry state context and epoch staleness guard."""

    def test_lifecycle_error_fields(self):
        err = LifecycleError(
            "test error",
            error_code=LifecycleErrorCode.TRANSITION_INVALID,
            state_at_raise="running",
            phase=LifecyclePhase.RUNNING,
            epoch=3,
        )
        assert err.error_code == LifecycleErrorCode.TRANSITION_INVALID
        assert err.state_at_raise == "running"
        assert err.phase == LifecyclePhase.RUNNING
        assert err.epoch == 3
        assert err.cause is None
        assert "test error" in str(err)

    def test_lifecycle_error_with_cause(self):
        cause = ValueError("port 99999")
        err = LifecycleError(
            "wrapped", error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="starting_backend", phase=LifecyclePhase.BRINGUP,
            epoch=1, cause=cause,
        )
        assert err.cause is cause

    def test_fatal_is_lifecycle_error(self):
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(LifecycleFatalError, Exception)

    def test_recoverable_has_retry_hint(self):
        err = LifecycleRecoverableError(
            "timeout", retry_hint="backoff",
            error_code=LifecycleErrorCode.TIMEOUT_EXCEEDED,
            state_at_raise="starting_resources",
            phase=LifecyclePhase.BRINGUP, epoch=1,
        )
        assert err.retry_hint == "backoff"

    def test_recoverable_default_retry_hint(self):
        err = LifecycleRecoverableError(
            "test", error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="running", phase=LifecyclePhase.RUNNING, epoch=0,
        )
        assert err.retry_hint == "backoff"

    def test_dependency_unavailable_fields(self):
        err = DependencyUnavailableError(
            "Prime unreachable", dependency="jarvis_prime",
            fallback_available=True,
            error_code=LifecycleErrorCode.DEP_UNREACHABLE,
            state_at_raise="running", phase=LifecyclePhase.RUNNING, epoch=2,
        )
        assert err.dependency == "jarvis_prime"
        assert err.fallback_available is True
        assert isinstance(err, LifecycleRecoverableError)

    def test_transition_rejected_is_lifecycle_error(self):
        err = TransitionRejected(
            "already stopped",
            error_code=LifecycleErrorCode.TRANSITION_INVALID,
            state_at_raise="stopped", phase=LifecyclePhase.STOPPED, epoch=1,
        )
        assert isinstance(err, LifecycleError)
        assert not isinstance(err, LifecycleFatalError)

    def test_inheritance_hierarchy(self):
        """Verify the full hierarchy is correct."""
        assert issubclass(DependencyUnavailableError, LifecycleRecoverableError)
        assert issubclass(LifecycleRecoverableError, LifecycleError)
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(TransitionRejected, LifecycleError)
        assert issubclass(LifecycleError, Exception)
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add to `backend/core/lifecycle_exceptions.py`:

```python
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
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_exceptions.py -v`

**Step 5: Commit**

```bash
git add backend/core/lifecycle_exceptions.py tests/unit/backend/test_lifecycle_exceptions.py
git commit -m "feat(lifecycle): add LifecycleError hierarchy with TransitionRejected (Disease 5+6, Task 3)"
```

---

### Task 4: LifecycleEvent enum + TransitionRecord + VALID_TRANSITIONS table

**Files:**
- Create: `backend/core/lifecycle_engine.py`
- Test: `tests/unit/backend/test_lifecycle_engine.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/test_lifecycle_engine.py`:

```python
#!/usr/bin/env python3
"""Tests for lifecycle state machine engine (Disease 5+6 MVP)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
# KernelState lives in unified_supervisor.py which is hard to import.
# We re-export it from lifecycle_engine for testability.
from backend.core.lifecycle_engine import (
    LifecycleEvent, TransitionRecord, VALID_TRANSITIONS, KernelState,
)


class TestLifecycleEventEnum:
    def test_all_events_exist(self):
        assert LifecycleEvent.PREFLIGHT_START == "preflight_start"
        assert LifecycleEvent.BRINGUP_START == "bringup_start"
        assert LifecycleEvent.BACKEND_START == "backend_start"
        assert LifecycleEvent.INTEL_START == "intel_start"
        assert LifecycleEvent.TRINITY_START == "trinity_start"
        assert LifecycleEvent.READY == "ready"
        assert LifecycleEvent.SHUTDOWN == "shutdown"
        assert LifecycleEvent.STOPPED == "stopped"
        assert LifecycleEvent.FATAL == "fatal"

    def test_exactly_nine_events(self):
        assert len(LifecycleEvent) == 9


class TestTransitionRecord:
    def test_record_fields(self):
        rec = TransitionRecord(
            old_state="initializing", event="preflight_start",
            new_state="preflight", epoch=1, actor="supervisor",
            at_monotonic=1000.0, reason="boot",
        )
        assert rec.old_state == "initializing"
        assert rec.epoch == 1
        assert rec.actor == "supervisor"

    def test_record_is_frozen(self):
        rec = TransitionRecord(
            old_state="a", event="b", new_state="c",
            epoch=0, actor="", at_monotonic=0.0, reason="",
        )
        with pytest.raises(AttributeError):
            rec.old_state = "changed"


class TestTransitionTable:
    def test_forward_startup_sequence(self):
        """Full startup path exists in table."""
        sequence = [
            (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START, KernelState.PREFLIGHT),
            (KernelState.PREFLIGHT, LifecycleEvent.BRINGUP_START, KernelState.STARTING_RESOURCES),
            (KernelState.STARTING_RESOURCES, LifecycleEvent.BACKEND_START, KernelState.STARTING_BACKEND),
            (KernelState.STARTING_BACKEND, LifecycleEvent.INTEL_START, KernelState.STARTING_INTELLIGENCE),
            (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.TRINITY_START, KernelState.STARTING_TRINITY),
            (KernelState.STARTING_TRINITY, LifecycleEvent.READY, KernelState.RUNNING),
        ]
        for from_state, event, expected_to in sequence:
            assert VALID_TRANSITIONS[(from_state, event)] == expected_to

    def test_shutdown_from_every_active_state(self):
        active_states = [
            KernelState.RUNNING, KernelState.PREFLIGHT,
            KernelState.STARTING_RESOURCES, KernelState.STARTING_BACKEND,
            KernelState.STARTING_INTELLIGENCE, KernelState.STARTING_TRINITY,
        ]
        for state in active_states:
            assert VALID_TRANSITIONS[(state, LifecycleEvent.SHUTDOWN)] == KernelState.SHUTTING_DOWN

    def test_duplicate_shutdown_is_idempotent(self):
        assert VALID_TRANSITIONS[
            (KernelState.SHUTTING_DOWN, LifecycleEvent.SHUTDOWN)
        ] == KernelState.SHUTTING_DOWN

    def test_fatal_from_every_non_terminal_state(self):
        non_terminal = [
            KernelState.INITIALIZING, KernelState.PREFLIGHT,
            KernelState.STARTING_RESOURCES, KernelState.STARTING_BACKEND,
            KernelState.STARTING_INTELLIGENCE, KernelState.STARTING_TRINITY,
            KernelState.RUNNING, KernelState.SHUTTING_DOWN,
        ]
        for state in non_terminal:
            assert VALID_TRANSITIONS[(state, LifecycleEvent.FATAL)] == KernelState.FAILED

    def test_stopped_and_failed_are_terminal(self):
        for event in LifecycleEvent:
            assert (KernelState.STOPPED, event) not in VALID_TRANSITIONS
            assert (KernelState.FAILED, event) not in VALID_TRANSITIONS

    def test_all_kernel_states_covered(self):
        """Every KernelState appears in the table as a from-state."""
        states_in_table = {k[0] for k in VALID_TRANSITIONS.keys()}
        non_terminal = set(KernelState) - {KernelState.STOPPED, KernelState.FAILED}
        assert non_terminal.issubset(states_in_table)
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Create `backend/core/lifecycle_engine.py`:

```python
"""
Lifecycle Engine (Disease 5+6 MVP)
===================================
Guarded state machine with transition table, epoch tracking,
and centralized state mutation.

KernelState is re-exported here for testability (the original
lives in unified_supervisor.py but is hard to import in tests).
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from backend.core.lifecycle_exceptions import (
    LifecycleFatalError,
    LifecyclePhase,
    LifecycleErrorCode,
    TransitionRejected,
)


# Re-export KernelState so tests and other modules can import it
# without pulling in the 98K-line unified_supervisor.py.
class KernelState(Enum):
    """States of the system kernel."""
    INITIALIZING = "initializing"
    PREFLIGHT = "preflight"
    STARTING_RESOURCES = "starting_resources"
    STARTING_BACKEND = "starting_backend"
    STARTING_INTELLIGENCE = "starting_intelligence"
    STARTING_TRINITY = "starting_trinity"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


class LifecycleEvent(str, Enum):
    """Typed events that drive state transitions."""
    PREFLIGHT_START = "preflight_start"
    BRINGUP_START = "bringup_start"
    BACKEND_START = "backend_start"
    INTEL_START = "intel_start"
    TRINITY_START = "trinity_start"
    READY = "ready"
    SHUTDOWN = "shutdown"
    STOPPED = "stopped"
    FATAL = "fatal"


@dataclass(frozen=True)
class TransitionRecord:
    """Stable schema for transition audit trail."""
    old_state: str
    event: str
    new_state: str
    epoch: int
    actor: str
    at_monotonic: float
    reason: str


# Transition table: (from_state, event) -> to_state
VALID_TRANSITIONS: Dict[Tuple[KernelState, LifecycleEvent], KernelState] = {
    # Forward startup sequence
    (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START):       KernelState.PREFLIGHT,
    (KernelState.PREFLIGHT, LifecycleEvent.BRINGUP_START):            KernelState.STARTING_RESOURCES,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.BACKEND_START):   KernelState.STARTING_BACKEND,
    (KernelState.STARTING_BACKEND, LifecycleEvent.INTEL_START):       KernelState.STARTING_INTELLIGENCE,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.TRINITY_START): KernelState.STARTING_TRINITY,
    (KernelState.STARTING_TRINITY, LifecycleEvent.READY):             KernelState.RUNNING,

    # Shutdown from any active state
    (KernelState.RUNNING, LifecycleEvent.SHUTDOWN):                   KernelState.SHUTTING_DOWN,
    (KernelState.PREFLIGHT, LifecycleEvent.SHUTDOWN):                 KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.SHUTDOWN):        KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_BACKEND, LifecycleEvent.SHUTDOWN):          KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.SHUTDOWN):     KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_TRINITY, LifecycleEvent.SHUTDOWN):          KernelState.SHUTTING_DOWN,

    # Idempotent duplicate shutdown
    (KernelState.SHUTTING_DOWN, LifecycleEvent.SHUTDOWN):             KernelState.SHUTTING_DOWN,

    # Completion
    (KernelState.SHUTTING_DOWN, LifecycleEvent.STOPPED):              KernelState.STOPPED,

    # Fatal from any non-terminal state
    (KernelState.INITIALIZING, LifecycleEvent.FATAL):                 KernelState.FAILED,
    (KernelState.PREFLIGHT, LifecycleEvent.FATAL):                    KernelState.FAILED,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.FATAL):           KernelState.FAILED,
    (KernelState.STARTING_BACKEND, LifecycleEvent.FATAL):             KernelState.FAILED,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.FATAL):        KernelState.FAILED,
    (KernelState.STARTING_TRINITY, LifecycleEvent.FATAL):             KernelState.FAILED,
    (KernelState.RUNNING, LifecycleEvent.FATAL):                      KernelState.FAILED,
    (KernelState.SHUTTING_DOWN, LifecycleEvent.FATAL):                KernelState.FAILED,
}
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_engine.py -v`

**Step 5: Commit**

```bash
git add backend/core/lifecycle_engine.py tests/unit/backend/test_lifecycle_engine.py
git commit -m "feat(lifecycle): add LifecycleEvent, TransitionRecord, VALID_TRANSITIONS table (Disease 5+6, Task 4)"
```

---

### Task 5: LifecycleEngine class

**Files:**
- Modify: `backend/core/lifecycle_engine.py`
- Test: `tests/unit/backend/test_lifecycle_engine.py`

**Step 1: Write the failing test**

Add to test file:

```python
from backend.core.lifecycle_engine import LifecycleEngine
from backend.core.lifecycle_exceptions import (
    LifecycleFatalError, TransitionRejected,
)


class TestLifecycleEngine:
    """Guarded state machine with epoch tracking."""

    def test_initial_state(self):
        engine = LifecycleEngine()
        assert engine.state == KernelState.INITIALIZING
        assert engine.epoch == 0

    def test_valid_forward_transition(self):
        engine = LifecycleEngine()
        result = engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert result == KernelState.PREFLIGHT
        assert engine.state == KernelState.PREFLIGHT

    def test_epoch_increments_on_preflight(self):
        engine = LifecycleEngine()
        assert engine.epoch == 0
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert engine.epoch == 1

    def test_invalid_transition_non_terminal_raises_fatal(self):
        engine = LifecycleEngine()
        with pytest.raises(LifecycleFatalError) as exc_info:
            engine.transition(LifecycleEvent.READY, actor="test")
        assert exc_info.value.error_code == "transition_invalid"

    def test_invalid_transition_terminal_raises_rejected(self):
        engine = LifecycleEngine()
        # Drive to FAILED
        engine.transition(LifecycleEvent.FATAL, actor="test")
        with pytest.raises(TransitionRejected):
            engine.transition(LifecycleEvent.SHUTDOWN, actor="test")

    def test_duplicate_shutdown_is_idempotent(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        # Second shutdown should NOT raise
        result = engine.transition(LifecycleEvent.SHUTDOWN, actor="test2")
        assert result == KernelState.SHUTTING_DOWN

    def test_history_records_transitions(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="boot", reason="startup")
        history = engine.history
        assert len(history) == 1
        assert history[0].old_state == "initializing"
        assert history[0].event == "preflight_start"
        assert history[0].new_state == "preflight"
        assert history[0].actor == "boot"
        assert history[0].reason == "startup"
        assert history[0].epoch == 1

    def test_history_is_bounded(self):
        engine = LifecycleEngine()
        # Transitions: preflight -> shutdown -> stopped won't reach 100
        # Just verify deque has maxlen
        assert engine._history.maxlen == 100

    def test_listener_notified(self):
        engine = LifecycleEngine()
        events = []
        engine.subscribe(lambda old, ev, new: events.append((old, ev, new)))
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert len(events) == 1
        assert events[0] == (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START, KernelState.PREFLIGHT)

    def test_listener_not_notified_on_noop(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        events = []
        engine.subscribe(lambda old, ev, new: events.append(1))
        # Duplicate shutdown = no-op = no notification
        engine.transition(LifecycleEvent.SHUTDOWN, actor="test")
        assert len(events) == 0

    def test_broken_listener_does_not_break_transition(self):
        engine = LifecycleEngine()
        engine.subscribe(lambda old, ev, new: 1 / 0)  # raises ZeroDivisionError
        # Should NOT raise
        result = engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        assert result == KernelState.PREFLIGHT

    def test_full_startup_shutdown_cycle(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="boot")
        engine.transition(LifecycleEvent.BRINGUP_START, actor="boot")
        engine.transition(LifecycleEvent.BACKEND_START, actor="boot")
        engine.transition(LifecycleEvent.INTEL_START, actor="boot")
        engine.transition(LifecycleEvent.TRINITY_START, actor="boot")
        engine.transition(LifecycleEvent.READY, actor="boot")
        assert engine.state == KernelState.RUNNING
        engine.transition(LifecycleEvent.SHUTDOWN, actor="operator")
        engine.transition(LifecycleEvent.STOPPED, actor="cleanup")
        assert engine.state == KernelState.STOPPED
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Add `LifecycleEngine` class to `backend/core/lifecycle_engine.py`:

```python
_logger = logging.getLogger(__name__)


class LifecycleEngine:
    """Single authority for all state transitions.

    Thread-safe for mutation via threading.Lock().
    Async callers use request_transition() which delegates.
    Signal-thread callers use loop.call_soon_threadsafe().
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = KernelState.INITIALIZING
        self._epoch = 0
        self._history: deque = deque(maxlen=100)
        self._listeners: List[Callable] = []

    def transition(self, event: LifecycleEvent, *,
                   actor: str = "", reason: str = "") -> KernelState:
        """Attempt a guarded state transition. Thread-safe.

        Returns new state on success.
        Raises TransitionRejected for expected races (terminal state).
        Raises LifecycleFatalError for true invariant violations.
        """
        with self._lock:
            key = (self._state, event)
            if key not in VALID_TRANSITIONS:
                if self._state in (KernelState.STOPPED, KernelState.FAILED):
                    raise TransitionRejected(
                        f"Transition rejected: {self._state.value} is terminal",
                        error_code=LifecycleErrorCode.TRANSITION_INVALID,
                        state_at_raise=self._state.value,
                        phase=self._state_to_phase(),
                        epoch=self._epoch,
                    )
                raise LifecycleFatalError(
                    f"Invalid transition: {self._state.value} + {event.value}",
                    error_code=LifecycleErrorCode.TRANSITION_INVALID,
                    state_at_raise=self._state.value,
                    phase=self._state_to_phase(),
                    epoch=self._epoch,
                )
            old = self._state
            new = VALID_TRANSITIONS[key]
            is_noop = (old == new)
            self._state = new

            # Epoch increments on lifecycle session start
            if event == LifecycleEvent.PREFLIGHT_START:
                self._epoch += 1

            self._history.append(TransitionRecord(
                old_state=old.value, event=event.value,
                new_state=new.value, epoch=self._epoch,
                actor=actor, at_monotonic=time.monotonic(),
                reason=reason,
            ))

        if not is_noop:
            self._notify_listeners(old, event, new)
        return new

    async def request_transition(self, event: LifecycleEvent,
                                  actor: str = "", reason: str = "") -> KernelState:
        """Async-friendly transition wrapper."""
        return self.transition(event, actor=actor, reason=reason)

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def state(self) -> KernelState:
        with self._lock:
            return self._state

    @property
    def history(self) -> List[TransitionRecord]:
        with self._lock:
            return list(self._history)

    def subscribe(self, listener: Callable) -> None:
        """Subscribe to transition events."""
        self._listeners.append(listener)

    def _notify_listeners(self, old: KernelState, event: LifecycleEvent,
                          new: KernelState) -> None:
        """Notify listeners. Failures are isolated."""
        for listener in self._listeners:
            try:
                listener(old, event, new)
            except Exception as e:
                _logger.warning(
                    "[LifecycleEngine] Listener %s failed: %s",
                    getattr(listener, '__name__', repr(listener)), e,
                )

    def _state_to_phase(self) -> LifecyclePhase:
        _MAP = {
            KernelState.INITIALIZING: LifecyclePhase.PRECHECK,
            KernelState.PREFLIGHT: LifecyclePhase.PRECHECK,
            KernelState.STARTING_RESOURCES: LifecyclePhase.BRINGUP,
            KernelState.STARTING_BACKEND: LifecyclePhase.BRINGUP,
            KernelState.STARTING_INTELLIGENCE: LifecyclePhase.BRINGUP,
            KernelState.STARTING_TRINITY: LifecyclePhase.BRINGUP,
            KernelState.RUNNING: LifecyclePhase.RUNNING,
            KernelState.SHUTTING_DOWN: LifecyclePhase.STOPPING,
            KernelState.STOPPED: LifecyclePhase.STOPPED,
            KernelState.FAILED: LifecyclePhase.STOPPED,
        }
        return _MAP.get(self._state, LifecyclePhase.RUNNING)
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_engine.py -v`

**Step 5: Commit**

```bash
git add backend/core/lifecycle_engine.py tests/unit/backend/test_lifecycle_engine.py
git commit -m "feat(lifecycle): add LifecycleEngine with guarded transitions and epoch tracking (Disease 5+6, Task 5)"
```

---

### Task 6: SignalAuthority

**Files:**
- Create: `backend/core/signal_authority.py`
- Test: `tests/unit/backend/test_signal_authority.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/test_signal_authority.py`:

```python
#!/usr/bin/env python3
"""Tests for SignalAuthority (Disease 5+6 MVP)."""
import asyncio
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from backend.core.signal_authority import SignalAuthority
from backend.core.lifecycle_engine import LifecycleEngine, LifecycleEvent


class TestSignalAuthority:
    def test_install_is_idempotent(self):
        engine = LifecycleEngine()
        loop = MagicMock()
        loop.add_signal_handler = MagicMock(side_effect=NotImplementedError)
        auth = SignalAuthority(engine, loop)
        with patch("signal.signal"):
            auth.install()
            auth.install()  # second call should be no-op
        assert auth._installed

    def test_handle_signal_triggers_shutdown(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        assert engine.state.value == "shutting_down"

    def test_duplicate_signal_is_idempotent(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        # Second signal should NOT raise (duplicate shutdown is idempotent)
        auth._handle_signal(signal.SIGTERM.value)
        assert engine.state.value == "shutting_down"

    def test_repeated_signals_trigger_emergency_exit(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        with patch.object(auth, '_emergency_exit') as mock_exit:
            for _ in range(4):
                auth._handle_signal(signal.SIGTERM.value)
            mock_exit.assert_called_once()

    def test_signal_count_tracked(self):
        engine = LifecycleEngine()
        engine.transition(LifecycleEvent.PREFLIGHT_START, actor="test")
        loop = MagicMock()
        auth = SignalAuthority(engine, loop)
        auth._handle_signal(signal.SIGTERM.value)
        auth._handle_signal(signal.SIGTERM.value)
        assert auth._signal_count[signal.SIGTERM.value] == 2
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Create `backend/core/signal_authority.py`:

```python
"""
Signal Authority (Disease 5+6 MVP)
====================================
Single owner of all OS signal registrations.

Uses loop.add_signal_handler() on POSIX when available.
Falls back to signal.signal() + call_soon_threadsafe() otherwise.
Modules subscribe to lifecycle events, never to OS signals directly.
"""
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Dict

from backend.core.lifecycle_engine import LifecycleEngine, LifecycleEvent
from backend.core.lifecycle_exceptions import TransitionRejected

_logger = logging.getLogger(__name__)


class SignalAuthority:
    """Single owner of all OS signal registrations.

    One instance per process. Bridges OS signals into the
    lifecycle engine's transition table.
    """

    def __init__(self, engine: LifecycleEngine, loop):
        self._engine = engine
        self._loop = loop
        self._signal_count: Dict[int, int] = {}
        self._installed = False

    def install(self) -> None:
        """Register handlers for SIGTERM, SIGINT. Call once at boot."""
        if self._installed:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._handle_signal, sig.value)
            except (NotImplementedError, AttributeError):
                # Fallback for non-POSIX or mock loops
                signal.signal(sig, self._handle_signal_compat)
        self._installed = True
        _logger.info("[SignalAuthority] Installed handlers for SIGTERM, SIGINT")

    def _handle_signal(self, signum: int) -> None:
        """POSIX path: runs in event loop context (add_signal_handler)."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        count = self._signal_count[signum]

        if count > 3:
            self._emergency_exit(signum)
            return

        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)

        _logger.warning(
            "[SignalAuthority] Received %s (count=%d)", sig_name, count,
        )

        try:
            self._engine.transition(
                LifecycleEvent.SHUTDOWN,
                actor=f"signal:{sig_name}",
                reason=f"OS signal received (count={count})",
            )
        except TransitionRejected:
            _logger.info("[SignalAuthority] Shutdown already in progress, signal deduplicated")
        except Exception as e:
            _logger.error("[SignalAuthority] Transition failed: %s", e)

    def _handle_signal_compat(self, signum: int, frame) -> None:
        """Fallback: runs in signal thread. Bridges to event loop."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        if self._signal_count[signum] > 3:
            self._emergency_exit(signum)
            return
        try:
            self._loop.call_soon_threadsafe(self._handle_signal, signum)
        except RuntimeError:
            # Loop already closed — handle synchronously
            self._handle_signal(signum)

    def _emergency_exit(self, signum: int) -> None:
        """Hard exit after repeated signals. Best-effort snapshot first."""
        _logger.critical(
            "[SignalAuthority] Emergency exit: signal %d received >3 times", signum,
        )
        try:
            snapshot = {
                "exit_reason": f"repeated_signal:{signum}",
                "signal_counts": dict(self._signal_count),
                "engine_state": self._engine.state.value,
                "engine_epoch": self._engine.epoch,
                "at_monotonic": time.monotonic(),
            }
            Path("/tmp/jarvis_emergency_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8",
            )
        except Exception:
            pass  # best effort, bounded
        os._exit(128 + signum)
```

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/unit/backend/test_signal_authority.py -v`

**Step 5: Commit**

```bash
git add backend/core/signal_authority.py tests/unit/backend/test_signal_authority.py
git commit -m "feat(lifecycle): add SignalAuthority with POSIX-first signal bridging (Disease 5+6, Task 6)"
```

---

### Task 7: Exception debt contract tests (CI grep rules)

**Files:**
- Create: `tests/contracts/test_exception_debt.py`

**Step 1: Write the tests**

```python
#!/usr/bin/env python3
"""
Exception debt contract tests (Disease 5+6 MVP).

These tests enforce that new code does not introduce silent exception
swallowing or scattered signal registration in lifecycle-critical modules.
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Lifecycle-critical files where silent except Exception: pass is banned
LIFECYCLE_CRITICAL_FILES = [
    "backend/core/lifecycle_engine.py",
    "backend/core/lifecycle_exceptions.py",
    "backend/core/signal_authority.py",
]


class TestNoSilentExceptionPass:
    """No 'except Exception: pass' in lifecycle-critical modules."""

    @pytest.mark.parametrize("filepath", LIFECYCLE_CRITICAL_FILES)
    def test_no_silent_pass_in_lifecycle_modules(self, filepath):
        path = Path(filepath)
        if not path.exists():
            pytest.skip(f"{filepath} not found")
        source = path.read_text()
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is not None and isinstance(node.type, ast.Name):
                    if node.type.id == "Exception":
                        # Check if body is just 'pass'
                        if (len(node.body) == 1
                                and isinstance(node.body[0], ast.Pass)):
                            violations.append(node.lineno)
        assert not violations, (
            f"{filepath} has silent 'except Exception: pass' at lines: {violations}"
        )


class TestNoScatteredSignalRegistration:
    """signal.signal() must only appear in signal_authority.py."""

    def test_no_signal_signal_in_lifecycle_engine(self):
        path = Path("backend/core/lifecycle_engine.py")
        if not path.exists():
            pytest.skip("lifecycle_engine.py not found")
        source = path.read_text()
        assert "signal.signal(" not in source, (
            "lifecycle_engine.py must not register signal handlers directly"
        )

    def test_no_signal_signal_in_lifecycle_exceptions(self):
        path = Path("backend/core/lifecycle_exceptions.py")
        if not path.exists():
            pytest.skip("lifecycle_exceptions.py not found")
        source = path.read_text()
        assert "signal.signal(" not in source


class TestNoDirectStateWriteInEngine:
    """self._state = ... must only appear inside LifecycleEngine.transition()."""

    def test_state_writes_only_in_transition(self):
        path = Path("backend/core/lifecycle_engine.py")
        if not path.exists():
            pytest.skip("lifecycle_engine.py not found")
        source = path.read_text()
        tree = ast.parse(source)

        state_writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and target.attr == "_state"
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        state_writes.append(node.lineno)

        # _state is written in __init__ and transition() only
        assert len(state_writes) <= 2, (
            f"self._state written at {len(state_writes)} locations "
            f"(expected <=2: __init__ + transition): lines {state_writes}"
        )


class TestExceptionTaxonomyComplete:
    """All required exception classes exist with correct hierarchy."""

    def test_full_hierarchy(self):
        from backend.core.lifecycle_exceptions import (
            LifecycleSignal, ShutdownRequested, LifecycleCancelled,
            LifecycleError, LifecycleFatalError, LifecycleRecoverableError,
            DependencyUnavailableError, TransitionRejected,
        )
        # Signals
        assert issubclass(LifecycleSignal, BaseException)
        assert not issubclass(LifecycleSignal, Exception)
        assert issubclass(ShutdownRequested, LifecycleSignal)
        assert issubclass(LifecycleCancelled, LifecycleSignal)
        # Errors
        assert issubclass(LifecycleError, Exception)
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(LifecycleRecoverableError, LifecycleError)
        assert issubclass(DependencyUnavailableError, LifecycleRecoverableError)
        assert issubclass(TransitionRejected, LifecycleError)
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/contracts/test_exception_debt.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/contracts/test_exception_debt.py
git commit -m "test(contracts): add exception debt CI enforcement rules (Disease 5+6, Task 7)"
```

---

### Task 8: Wire LifecycleEngine into unified_supervisor.py (replace 14 direct state writes)

**Files:**
- Modify: `unified_supervisor.py:66239,68551,69244,69813,74655,74964,74982,75599,76723,77743,83190,86458,90904,91309`
- Test: `tests/contracts/test_exception_debt.py` (already has AST check)

**Step 1: Write the failing test**

Add to `tests/contracts/test_exception_debt.py`:

```python
class TestSupervisorUsesLifecycleEngine:
    """unified_supervisor.py must use LifecycleEngine, not direct state writes."""

    def test_supervisor_imports_lifecycle_engine(self):
        source = Path("unified_supervisor.py").read_text()
        assert "LifecycleEngine" in source, (
            "unified_supervisor.py must import and use LifecycleEngine"
        )

    def test_supervisor_imports_lifecycle_event(self):
        source = Path("unified_supervisor.py").read_text()
        assert "LifecycleEvent" in source, (
            "unified_supervisor.py must use typed LifecycleEvent enum"
        )
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

This is the largest task. Replace each `self._state = KernelState.X` with `self._lifecycle_engine.transition(LifecycleEvent.Y, actor="supervisor", reason="...")`.

**Locations and replacements** (14 sites):

| Line | Old | New |
|------|-----|-----|
| 66239 | `self._state = KernelState.INITIALIZING` | `self._lifecycle_engine = LifecycleEngine()` (init in `__init__`) |
| 74982 | `self._state = KernelState.PREFLIGHT` | `self._lifecycle_engine.transition(LifecycleEvent.PREFLIGHT_START, actor="supervisor", reason="boot")` |
| 75599 | `self._state = KernelState.STARTING_RESOURCES` | `self._lifecycle_engine.transition(LifecycleEvent.BRINGUP_START, actor="supervisor", reason="resources")` |
| 76723 | `self._state = KernelState.STARTING_BACKEND` | `self._lifecycle_engine.transition(LifecycleEvent.BACKEND_START, actor="supervisor", reason="backend")` |
| 77743 | `self._state = KernelState.STARTING_INTELLIGENCE` | `self._lifecycle_engine.transition(LifecycleEvent.INTEL_START, actor="supervisor", reason="intelligence")` |
| 83190 | `self._state = KernelState.STARTING_TRINITY` | `self._lifecycle_engine.transition(LifecycleEvent.TRINITY_START, actor="supervisor", reason="trinity")` |
| 74655 | `self._state = KernelState.RUNNING` | `self._lifecycle_engine.transition(LifecycleEvent.READY, actor="supervisor", reason="all phases complete")` |
| 68551 | `self._state = KernelState.SHUTTING_DOWN` | `self._lifecycle_engine.transition(LifecycleEvent.SHUTDOWN, actor="supervisor", reason=reason)` |
| 90904 | `self._state = KernelState.SHUTTING_DOWN` | `self._lifecycle_engine.transition(LifecycleEvent.SHUTDOWN, actor="supervisor", reason="graceful")` |
| 69244 | `self._state = KernelState.STOPPED` | `self._lifecycle_engine.transition(LifecycleEvent.STOPPED, actor="supervisor", reason="cleanup complete")` |
| 91309 | `self._state = KernelState.STOPPED` | `self._lifecycle_engine.transition(LifecycleEvent.STOPPED, actor="supervisor", reason="shutdown complete")` |
| 69813 | `self._state = KernelState.FAILED` | `self._lifecycle_engine.transition(LifecycleEvent.FATAL, actor="supervisor", reason="startup timeout")` |
| 74964 | `self._state = KernelState.FAILED` | `self._lifecycle_engine.transition(LifecycleEvent.FATAL, actor="supervisor", reason="startup exception")` |
| 86458 | `self._state = KernelState.FAILED` | `self._lifecycle_engine.transition(LifecycleEvent.FATAL, actor="supervisor", reason="post-boot failure")` |

Also add backward-compat read-only `_state` property and add the import near the top of the class `__init__`:

```python
from backend.core.lifecycle_engine import LifecycleEngine, LifecycleEvent
```

And replace `self._state` reads throughout. Use a property adapter:

```python
@property
def _state(self):
    """Backward-compat adapter: reads from lifecycle engine."""
    return self._lifecycle_engine.state
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/contracts/test_exception_debt.py tests/unit/backend/test_lifecycle_engine.py -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/contracts/test_exception_debt.py
git commit -m "refactor(lifecycle): replace 14 direct state writes with LifecycleEngine.transition() (Disease 5+6, Task 8)"
```

---

### Task 9: Fix top-20 dangerous exception handlers in shutdown path

**Files:**
- Modify: `unified_supervisor.py:68551-68860` (emergency shutdown block)

**Step 1: Write the failing test**

Add to `tests/contracts/test_exception_debt.py`:

```python
class TestNoSilentPassInShutdown:
    """Emergency shutdown must not silently swallow exceptions."""

    def test_no_silent_pass_in_shutdown_block(self):
        """Lines 68500-69000 (emergency shutdown) must not have bare except: pass."""
        source = Path("unified_supervisor.py").read_text()
        lines = source.split("\n")
        # Scan shutdown zone (approximately lines 68500-69000)
        violations = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("except") and "Exception" in line:
                # Check if next meaningful line is just 'pass'
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip() == "pass":
                    # Check if we're in the shutdown zone
                    if 68400 <= i + 1 <= 69100:
                        violations.append(i + 1)
            i += 1
        assert len(violations) <= 5, (
            f"Shutdown zone has {len(violations)} silent 'except Exception: pass' "
            f"handlers at lines: {violations}. Target: <=5 after MVP."
        )
```

**Step 2: Run test — expect FAIL (currently ~15 silent passes in that zone)**

**Step 3: Implement**

For each silent `except Exception: pass` in lines 68500-69000, replace with:

```python
except Exception as _exc:
    self.logger.debug(
        "[Shutdown] %s: %s: %s", "<context>",
        type(_exc).__name__, _exc,
    )
```

The specific handlers (from top-20 analysis):
- Line 68561: trace hook `_trace_on_shutdown` → log at debug
- Line 68566: trace hook `_trace_shutdown` → log at debug
- Line 68604: component status capture → log at debug
- Line 68616: Trinity state capture → log at debug
- Line 68626: memory info capture → log at debug
- Line 68638: outer crash marker write → log at warning (critical forensics path)
- Line 68701: VM terminate inner → log at warning (resource leak risk)
- Line 68703: VM terminate outer → log at warning (resource leak risk)
- Line 68774: Neural Mesh stop → log at debug
- Line 68781: Neural Mesh fallback → log at debug
- Line 68853: Reactor Core shutdown → log at debug
- Line 68858: Voice sidecar stop → log at debug

**Step 4: Run test — expect PASS (<=5 remaining)**

Run: `python3 -m pytest tests/contracts/test_exception_debt.py::TestNoSilentPassInShutdown -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix(lifecycle): replace 12 silent exception handlers in shutdown path with logged handling (Disease 5+6, Task 9)"
```

---

### Task 10: Fix dangerous handlers in startup path + singleton cleanup

**Files:**
- Modify: `unified_supervisor.py` (startup zone ~74900-75700, cleanup zone ~69200-69300)

**Step 1: Write the failing test**

Add to `tests/contracts/test_exception_debt.py`:

```python
class TestNoSilentPassInStartup:
    """Startup phase transitions must not silently swallow exceptions."""

    def test_no_silent_pass_near_state_transitions(self):
        """Within 30 lines of any KernelState write, no silent except: pass."""
        source = Path("unified_supervisor.py").read_text()
        lines = source.split("\n")
        # Find all lines containing state transitions or lifecycle engine calls
        transition_lines = []
        for i, line in enumerate(lines):
            if "LifecycleEvent." in line and "transition(" in line:
                transition_lines.append(i)
        # Scan 30-line window around each for silent pass
        violations = []
        for tl in transition_lines:
            window_start = max(0, tl - 30)
            window_end = min(len(lines), tl + 30)
            for i in range(window_start, window_end):
                stripped = lines[i].strip()
                if stripped.startswith("except") and "Exception" in stripped:
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and lines[j].strip() == "pass":
                        violations.append(i + 1)
        assert len(violations) <= 3, (
            f"Found {len(violations)} silent handlers near state transitions: "
            f"lines {violations}. Target: <=3."
        )
```

**Step 2: Run test — expect FAIL**

**Step 3: Implement**

Replace silent handlers near startup transitions:
- Lines near 69268, 69280: singleton cleanup import failures → log at debug
- Lines near 69812-69813: trace hooks during timeout → log at debug
- Lines near 74697: dashboard update → log at debug
- Lines near 75662: startup checkpoint → log at debug
- Lines near 77754: event infrastructure → log at warning (critical path)

Same pattern: `except Exception: pass` → `except Exception as _exc: self.logger.debug(...)`.

**Step 4: Run test — expect PASS**

Run: `python3 -m pytest tests/contracts/test_exception_debt.py -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix(lifecycle): replace silent exception handlers in startup and cleanup paths (Disease 5+6, Task 10)"
```

---

### Task 11: Disease 5+6 gate test

**Files:**
- Test: `tests/unit/backend/test_lifecycle_engine.py`

**Step 1: Write the gate test**

Add to `tests/unit/backend/test_lifecycle_engine.py`:

```python
class TestDisease56Gate:
    """Gate: All Disease 5+6 MVP fixes verified."""

    @pytest.mark.parametrize("check", [
        "lifecycle_phase_enum",
        "lifecycle_error_code_enum",
        "lifecycle_signal_hierarchy",
        "lifecycle_error_hierarchy",
        "lifecycle_engine_exists",
        "transition_table_complete",
        "signal_authority_exists",
        "supervisor_uses_engine",
        "exception_debt_rules_exist",
    ])
    def test_disease56_gate(self, check):
        if check == "lifecycle_phase_enum":
            from backend.core.lifecycle_exceptions import LifecyclePhase
            assert len(LifecyclePhase) == 7

        elif check == "lifecycle_error_code_enum":
            from backend.core.lifecycle_exceptions import LifecycleErrorCode
            assert len(LifecycleErrorCode) == 8

        elif check == "lifecycle_signal_hierarchy":
            from backend.core.lifecycle_exceptions import (
                LifecycleSignal, ShutdownRequested, LifecycleCancelled,
            )
            assert issubclass(LifecycleSignal, BaseException)
            assert not issubclass(LifecycleSignal, Exception)

        elif check == "lifecycle_error_hierarchy":
            from backend.core.lifecycle_exceptions import (
                LifecycleError, LifecycleFatalError,
                LifecycleRecoverableError, DependencyUnavailableError,
                TransitionRejected,
            )
            assert issubclass(LifecycleFatalError, LifecycleError)
            assert issubclass(DependencyUnavailableError, LifecycleRecoverableError)

        elif check == "lifecycle_engine_exists":
            from backend.core.lifecycle_engine import LifecycleEngine
            engine = LifecycleEngine()
            assert callable(getattr(engine, "transition", None))
            assert callable(getattr(engine, "subscribe", None))

        elif check == "transition_table_complete":
            from backend.core.lifecycle_engine import VALID_TRANSITIONS, KernelState
            non_terminal = set(KernelState) - {KernelState.STOPPED, KernelState.FAILED}
            covered = {k[0] for k in VALID_TRANSITIONS.keys()}
            assert non_terminal.issubset(covered)

        elif check == "signal_authority_exists":
            from backend.core.signal_authority import SignalAuthority
            assert callable(getattr(SignalAuthority, "install", None))

        elif check == "supervisor_uses_engine":
            source = Path("unified_supervisor.py").read_text()
            assert "LifecycleEngine" in source
            assert "LifecycleEvent" in source

        elif check == "exception_debt_rules_exist":
            assert Path("tests/contracts/test_exception_debt.py").exists()
```

**Step 2: Run gate test**

Run: `python3 -m pytest tests/unit/backend/test_lifecycle_engine.py::TestDisease56Gate -v`
Expected: All 9 checks PASS

**Step 3: Commit**

```bash
git add tests/unit/backend/test_lifecycle_engine.py
git commit -m "test(disease56): add Disease 5+6 gate tests for lifecycle and exception hardening"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | LifecyclePhase + LifecycleErrorCode enums | `lifecycle_exceptions.py` + test |
| 2 | LifecycleSignal + ShutdownRequested + LifecycleCancelled | `lifecycle_exceptions.py` + test |
| 3 | LifecycleError hierarchy + TransitionRejected | `lifecycle_exceptions.py` + test |
| 4 | LifecycleEvent + TransitionRecord + VALID_TRANSITIONS | `lifecycle_engine.py` + test |
| 5 | LifecycleEngine class | `lifecycle_engine.py` + test |
| 6 | SignalAuthority | `signal_authority.py` + test |
| 7 | Exception debt contract tests | `test_exception_debt.py` |
| 8 | Wire engine into supervisor (14 state writes) | `unified_supervisor.py` |
| 9 | Fix top-12 shutdown path handlers | `unified_supervisor.py` |
| 10 | Fix startup + cleanup path handlers | `unified_supervisor.py` |
| 11 | Disease 5+6 gate test | test |

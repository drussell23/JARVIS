# Exception & Lifecycle Hardening — Design Document

**Date:** 2026-03-05
**Diseases:** #5 (Exception Swallowing at Scale) + #6 (No Formal Lifecycle State Machine)
**Strategy:** A-for-architecture (combined design), C-for-delivery (scoped MVP)

---

## Problem Statement

**Disease 5:** 1,425 `except Exception` blocks in `unified_supervisor.py`. ~35% are silent (`pass`). No exception hierarchy. `CancelledError` (BaseException in Python 3.9+) leaks through untyped handlers. Bugs hide behind swallowed exceptions.

**Disease 6:** `KernelState` enum exists (10 values) but has no transition guards. 12 unguarded `self._state =` mutation points. 16 unorchestrated cleanup methods. Signal handlers registered in 4+ files. No re-entrancy protection.

**Interdependency:** Exception policy needs lifecycle context to determine severity. Lifecycle state machine needs typed exceptions to make recovery decisions. They must be designed together.

---

## Exception Taxonomy

### LifecyclePhase Enum

```python
class LifecyclePhase(str, Enum):
    PRECHECK = "precheck"
    BRINGUP = "bringup"
    CONTRACT_GATE = "contract_gate"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
```

### Control-Flow Signals (BaseException — never swallowed)

```python
@dataclass(frozen=True)
class LifecycleSignal(BaseException):
    """Control-flow signals, not errors. Must never be swallowed.

    Catch only to annotate and re-raise.
    """
    reason: str
    epoch: int
    requested_by: str          # "signal:SIGTERM", "watchdog", "operator", etc.
    at_monotonic: float

@dataclass(frozen=True)
class ShutdownRequested(LifecycleSignal):
    """Operator/system/watchdog requested graceful shutdown."""
    pass

@dataclass(frozen=True)
class LifecycleCancelled(LifecycleSignal):
    """Cooperative cancellation wrapping CancelledError metadata.

    Created at boundary adapters when CancelledError is caught.
    Never created deep in business logic.
    """
    cancelled_task: str = ""
```

### Lifecycle Errors (Exception — catchable with policy)

```python
class LifecycleError(Exception):
    """Base for all lifecycle errors. Carries state context + staleness guard."""

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

### Error Codes

```python
class LifecycleErrorCode(str, Enum):
    DEP_UNREACHABLE = "dep_unreachable"
    CONTRACT_INCOMPATIBLE = "contract_incompatible"
    TRANSITION_INVALID = "transition_invalid"
    SHUTDOWN_REENTRANT = "shutdown_reentrant"
    TASK_ORPHAN_DETECTED = "task_orphan_detected"
    EPOCH_STALE = "epoch_stale"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
```

### Catch Policy Invariant

In lifecycle-critical code paths (startup, shutdown, state transitions, process supervision):

1. **`LifecycleSignal`** — catch only to annotate and re-raise
2. **`LifecycleRecoverableError`** — catch only where retry policy is explicitly applied
3. **`LifecycleFatalError`** — catch to trigger deterministic FAILED transition
4. **`TransitionRejected`** — log at INFO, do not escalate unless threshold exceeded
5. **`except Exception`** — allowed ONLY at top supervisory boundary, where it wraps into typed `LifecycleError` and emits structured incident

### Staleness Guard

`epoch` field on every error and signal. Handlers compare `error.epoch` against current lifecycle epoch. Stale errors (epoch < current) are logged at DEBUG and discarded — they cannot mutate current lifecycle state.

### Cancellation Normalization

`CancelledError` conversion to `LifecycleCancelled` happens ONLY at boundary adapters (task wrappers, signal handlers). Never deep in business logic. The adapter captures task name, epoch, and monotonic timestamp.

---

## Lifecycle State Machine

### LifecycleEvent Enum

```python
class LifecycleEvent(str, Enum):
    PREFLIGHT_START = "preflight_start"
    BRINGUP_START = "bringup_start"
    BACKEND_START = "backend_start"
    INTEL_START = "intel_start"
    TRINITY_START = "trinity_start"
    READY = "ready"
    SHUTDOWN = "shutdown"
    STOPPED = "stopped"
    FATAL = "fatal"
```

### Transition Table

```python
VALID_TRANSITIONS: Dict[Tuple[KernelState, LifecycleEvent], KernelState] = {
    # Forward startup sequence
    (KernelState.INITIALIZING, LifecycleEvent.PREFLIGHT_START):     KernelState.PREFLIGHT,
    (KernelState.PREFLIGHT, LifecycleEvent.BRINGUP_START):          KernelState.STARTING_RESOURCES,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.BACKEND_START): KernelState.STARTING_BACKEND,
    (KernelState.STARTING_BACKEND, LifecycleEvent.INTEL_START):     KernelState.STARTING_INTELLIGENCE,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.TRINITY_START): KernelState.STARTING_TRINITY,
    (KernelState.STARTING_TRINITY, LifecycleEvent.READY):           KernelState.RUNNING,

    # Shutdown from any active state
    (KernelState.RUNNING, LifecycleEvent.SHUTDOWN):                 KernelState.SHUTTING_DOWN,
    (KernelState.PREFLIGHT, LifecycleEvent.SHUTDOWN):               KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.SHUTDOWN):      KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_BACKEND, LifecycleEvent.SHUTDOWN):        KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.SHUTDOWN):   KernelState.SHUTTING_DOWN,
    (KernelState.STARTING_TRINITY, LifecycleEvent.SHUTDOWN):        KernelState.SHUTTING_DOWN,

    # Idempotent duplicate shutdown (no-op, not fatal)
    (KernelState.SHUTTING_DOWN, LifecycleEvent.SHUTDOWN):           KernelState.SHUTTING_DOWN,

    # Completion
    (KernelState.SHUTTING_DOWN, LifecycleEvent.STOPPED):            KernelState.STOPPED,

    # Fatal from any non-terminal state
    (KernelState.INITIALIZING, LifecycleEvent.FATAL):               KernelState.FAILED,
    (KernelState.PREFLIGHT, LifecycleEvent.FATAL):                  KernelState.FAILED,
    (KernelState.STARTING_RESOURCES, LifecycleEvent.FATAL):         KernelState.FAILED,
    (KernelState.STARTING_BACKEND, LifecycleEvent.FATAL):           KernelState.FAILED,
    (KernelState.STARTING_INTELLIGENCE, LifecycleEvent.FATAL):      KernelState.FAILED,
    (KernelState.STARTING_TRINITY, LifecycleEvent.FATAL):           KernelState.FAILED,
    (KernelState.RUNNING, LifecycleEvent.FATAL):                    KernelState.FAILED,
    (KernelState.SHUTTING_DOWN, LifecycleEvent.FATAL):              KernelState.FAILED,
}
```

### Terminal State Policy

`FAILED` and `STOPPED` are terminal. No transitions out. Recovery requires a new process (new epoch).

### LifecycleEngine

```python
class LifecycleEngine:
    """Single authority for all state transitions.

    Thread-safe for mutation via threading.Lock().
    Async callers use request_transition() which schedules via event loop.
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
        Raises TransitionRejected for expected races.
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

    async def request_transition(self, event: LifecycleEvent, actor: str = "",
                                  reason: str = "") -> KernelState:
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

    def subscribe(self, listener: Callable) -> None:
        """Subscribe to transition events."""
        self._listeners.append(listener)

    def _notify_listeners(self, old: KernelState, event: LifecycleEvent,
                          new: KernelState) -> None:
        """Notify listeners. Failures are isolated — never break transition flow."""
        for listener in self._listeners:
            try:
                listener(old, event, new)
            except Exception as e:
                logging.warning(
                    "[LifecycleEngine] Listener %s failed: %s",
                    getattr(listener, '__name__', repr(listener)), e
                )

    def _state_to_phase(self) -> LifecyclePhase:
        """Map current KernelState to LifecyclePhase."""
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

### Transition Record Schema

```python
@dataclass(frozen=True)
class TransitionRecord:
    """Stable schema for transition audit trail."""
    old_state: str
    event: str
    new_state: str
    epoch: int
    actor: str          # "signal:SIGTERM", "supervisor", "watchdog", etc.
    at_monotonic: float
    reason: str         # human-readable context
```

### Backward Compatibility Shim

During migration, legacy code that reads `self._state` directly gets a read-only property that delegates to the engine:

```python
@property
def _state(self) -> KernelState:
    return self._lifecycle_engine.state
```

Direct writes (`self._state = ...`) are replaced with `self._lifecycle_engine.transition(...)`. A grep test enforces no direct `self._state =` assignments outside `LifecycleEngine`.

---

## Signal Authority

Separate module: `backend/core/signal_authority.py`

```python
class SignalAuthority:
    """Single owner of all OS signal registrations.

    Uses loop.add_signal_handler() on POSIX (preferred).
    Falls back to signal.signal() + call_soon_threadsafe() otherwise.
    Modules subscribe to lifecycle events, never to OS signals directly.
    """

    def __init__(self, engine: LifecycleEngine, loop: asyncio.AbstractEventLoop):
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
                self._loop.add_signal_handler(sig, self._handle_signal, sig)
            except NotImplementedError:
                signal.signal(sig, self._handle_signal_compat)
        self._installed = True

    def _handle_signal(self, signum: int) -> None:
        """POSIX path: runs in event loop context (add_signal_handler)."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        if self._signal_count[signum] > 3:
            self._emergency_exit(signum)
        try:
            self._engine.transition(
                LifecycleEvent.SHUTDOWN,
                actor=f"signal:{signal.Signals(signum).name}",
                reason=f"OS signal received (count={self._signal_count[signum]})",
            )
        except TransitionRejected:
            pass  # Idempotent — already shutting down

    def _handle_signal_compat(self, signum: int, frame) -> None:
        """Fallback path: runs in signal thread. Bridges to event loop."""
        self._signal_count[signum] = self._signal_count.get(signum, 0) + 1
        if self._signal_count[signum] > 3:
            self._emergency_exit(signum)
        self._loop.call_soon_threadsafe(self._handle_signal, signum)

    def _emergency_exit(self, signum: int) -> None:
        """Hard exit after repeated signals. Best-effort snapshot first."""
        try:
            # Bounded emergency snapshot (100ms max)
            import json, time as _time
            snapshot = {
                "exit_reason": f"repeated_signal:{signum}",
                "signal_counts": dict(self._signal_count),
                "engine_state": self._engine.state.value,
                "engine_epoch": self._engine.epoch,
                "at_monotonic": _time.monotonic(),
            }
            Path("/tmp/jarvis_emergency_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8"
            )
        except Exception:
            pass  # best effort
        os._exit(128 + signum)
```

### Signal Deduplication

Signal count tracks per-signum, but the `TransitionRejected` catch on duplicate shutdown provides epoch-global deduplication. If `SHUTTING_DOWN + SHUTDOWN → SHUTTING_DOWN` is already in effect, the transition is a no-op.

---

## Exception Debt Meter

Track remaining untyped exception handlers for progressive cleanup:

```python
class ExceptionDebtMeter:
    """Tracks untyped exception handlers across the codebase.

    Granularity: module/function/error_code class.
    Used for CI reporting and progressive reduction.
    """

    @dataclass
    class DebtEntry:
        file: str
        line: int
        function: str
        pattern: str    # "pass", "debug_log", "warning_log", "generic_catch"
        zone: str       # "lifecycle_critical", "monitoring", "import", "other"

    @staticmethod
    def scan(root: str = ".") -> List["ExceptionDebtMeter.DebtEntry"]:
        """AST-scan Python files for untyped exception handlers."""
        ...
```

### CI Grep Rules

- **Block:** new `except Exception: pass` in lifecycle-critical files (`lifecycle_engine.py`, `signal_authority.py`, `lifecycle_exceptions.py`)
- **Block:** `signal.signal(` outside `signal_authority.py`
- **Block:** direct `self._state =` outside `LifecycleEngine.transition()`
- **Exclude:** tests/, docs/, examples/ from grep rules

---

## Top-20 Dangerous Handler Selection

Objective ranking criteria (all must be scored):

1. **Silent pass** — handler discards exception with no logging (+3)
2. **Lifecycle criticality** — handler is in startup/shutdown/state-transition path (+3)
3. **Async context** — handler is inside `async def` where `CancelledError` matters (+2)
4. **State mutation** — handler modifies `self._state` or similar state after catch (+2)
5. **Frequency** — handler is in a loop or monitoring path that runs repeatedly (+1)
6. **Side-effect risk** — handler controls resource cleanup, file writes, process management (+1)

Score ≥ 6 → in MVP scope. The exploration found these zones score highest:
- Startup phase transitions (state mutation + lifecycle critical)
- Shutdown/cleanup methods (state mutation + side effects)
- Process supervision loops (frequency + async + silent)
- Signal handler contexts (lifecycle critical + async)
- Contract gate validation (lifecycle critical + state mutation)

---

## Testing Strategy

### Unit Tests (`tests/unit/backend/test_lifecycle_engine.py`)

- Transition table: every valid transition succeeds
- Invalid transition from non-terminal state: raises `LifecycleFatalError`
- Invalid transition from terminal state: raises `TransitionRejected`
- Duplicate shutdown: idempotent (no raise, returns SHUTTING_DOWN)
- Epoch increments on `PREFLIGHT_START`
- Stale epoch rejected: error with old epoch is discarded
- History records actor, epoch, reason, monotonic timestamp
- Listener failure isolated: broken listener doesn't break transition

### Unit Tests (`tests/unit/backend/test_lifecycle_exceptions.py`)

- Taxonomy: all 8 classes exist with correct inheritance
- `LifecycleSignal` is `BaseException`, not `Exception`
- `LifecycleError` carries error_code, state_at_raise, phase, epoch
- `LifecycleCancelled` is frozen dataclass
- Catch policy: `except Exception` does NOT catch `LifecycleSignal`

### Unit Tests (`tests/unit/backend/test_signal_authority.py`)

- Install registers handlers
- Duplicate install is idempotent
- Signal triggers transition to SHUTTING_DOWN
- Repeated signals (>3) trigger emergency exit
- `TransitionRejected` on duplicate signal is swallowed silently

### Contract Tests (`tests/contracts/test_exception_debt.py`)

- No `except Exception: pass` in lifecycle-critical files
- No `signal.signal(` outside `signal_authority.py`
- No direct `self._state =` outside `LifecycleEngine`
- Exception debt meter produces valid report

### Gate Test

- All taxonomy classes importable
- `LifecycleEngine` transition table covers all `KernelState` values
- `SignalAuthority` importable and installable
- Supervisor references `LifecycleEngine` (AST check)

---

## Files Changed

| File | Change |
|------|--------|
| `backend/core/lifecycle_exceptions.py` | **New:** `LifecyclePhase`, `LifecycleSignal`, `ShutdownRequested`, `LifecycleCancelled`, `LifecycleError`, `LifecycleFatalError`, `LifecycleRecoverableError`, `DependencyUnavailableError`, `TransitionRejected`, `LifecycleErrorCode` |
| `backend/core/lifecycle_engine.py` | **New:** `LifecycleEvent`, `VALID_TRANSITIONS`, `TransitionRecord`, `LifecycleEngine` |
| `backend/core/signal_authority.py` | **New:** `SignalAuthority` |
| `unified_supervisor.py` | Replace ~12 direct `self._state =` writes with `engine.transition()`. Add `_lifecycle_engine` property. Add backward-compat `_state` read-only property. Replace top-20 dangerous exception handlers. Remove scattered `signal.signal()` calls. |
| `tests/unit/backend/test_lifecycle_engine.py` | **New** |
| `tests/unit/backend/test_lifecycle_exceptions.py` | **New** |
| `tests/unit/backend/test_signal_authority.py` | **New** |
| `tests/contracts/test_exception_debt.py` | **New** |

---

## Scope Boundary

### Deferred (separate projects)

- Full 1,425-handler sweep (requires per-module triage)
- Restart backoff + quarantine (covered in lifecycle-resilience-hardening-design.md)
- Inference drain protocol (requires Prime/Reactor changes)
- Journal-backed GCP lifecycle (separate design exists)
- Distributed lifecycle state (multi-instance only)

### Go/No-Go Criteria

1. Lifecycle transitions are guard-enforced and audited (transition table test)
2. One signal authority exists (grep test: no `signal.signal` outside `SignalAuthority`)
3. No silent `except Exception: pass` in lifecycle-critical modules (grep test)
4. `CancelledError` is never swallowed — converted to `LifecycleCancelled` at boundaries
5. Duplicate shutdown is idempotent, not fatal
6. Stale-epoch errors are discarded with structured log
7. Exception debt meter tracks remaining untyped handlers

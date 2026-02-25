# Timeout Budget Propagation — Phase 1 Design

**Date:** 2026-02-25
**Status:** Approved
**Scope:** Phase 1 of 4-phase SystemKernel hardening (A → B → C → D)
**Target:** `backend/core/execution_context.py` + surgical instrumentation of `unified_supervisor.py`

---

## 1. Problem Statement

The JARVIS startup pipeline has 400+ `asyncio.wait_for()` call sites with independent timeout clocks. Nested calls do not share a budget — a phase with 90s remaining can spawn a sub-operation with a fresh 30s clock, exceeding the parent's deadline. When the parent fires `CancelledError`, the sub-operation misclassifies it as a service fault rather than budget exhaustion. Every diagnostic message downstream is wrong.

### Root Cause

There is no shared deadline propagated through the async call stack. Each `asyncio.wait_for(coro, timeout=X)` creates an independent timer. Nested timers never compute `remaining = parent_deadline - monotonic()`.

### Why This Is P0

- **State machine decisions (Phase 2) depend on clean failure signals.** Without typed timeout errors, transition logic cannot distinguish "operation was slow" from "parent ran out of time."
- **Mode policy (Phase 3) depends on valid state transitions.** Degradation/recovery keyed off misclassified failures will oscillate or deadlock.
- **Retry logic fires on budget exhaustion**, wasting time on operations that cannot succeed within the remaining budget.

---

## 2. Approach

**Approach C: ContextVar-propagated ExecutionContext with explicit boundary markers.**

- An `ExecutionContext` frozen dataclass lives in a `ContextVar`.
- Phase/service boundaries explicitly create contexts via `execution_budget()` context manager.
- Interior calls use `budget_aware_wait_for()` which reads the contextvar automatically.
- Thread executors use `contextvars.copy_context().run()` for propagation.
- Boundary-first rollout: instrument ~30 lifecycle boundaries first, then expand inward.

### Why Not Alternatives

- **Pure ContextVar (Approach A):** No explicit boundary markers makes audit impossible.
- **Explicit parameter threading (Approach B):** Hundreds of signature changes in a 73K-line file. Unrealistic blast radius.

---

## 3. Data Model

### 3.1 Error Taxonomy (three-way split)

```python
class BudgetExhaustedError(Exception):
    """Parent deadline reached zero. Retry will NOT help.

    Fields:
        owner: str              - who set the budget
        phase: str              - current phase name
        deadline_mono: float    - when budget expired
        remaining_at_entry: float - budget remaining when call started
        local_cap: float        - what the caller requested
        effective_timeout: float - min(remaining, local_cap)
        elapsed: float          - operation duration before expiry
        timeout_origin: str     - always "budget"
        cause: str              - always "budget_exhausted"
    """

class LocalCapExceededError(TimeoutError):
    """Operation exceeded its local_cap before budget hit zero. Retry MAY help.

    Inherits TimeoutError intentionally — existing `except TimeoutError`
    handlers catch this, preserving backward compatibility during migration.

    Fields: same as BudgetExhaustedError, timeout_origin="local_cap"
    """

class ExternalCancellationError(Exception):
    """Cancelled by owner shutdown, dependency loss, or manual cancel.

    Fields:
        cause: CancellationCause
        scope_id: str
        detail: str
    """
```

**Retry contract (non-negotiable):**

| Error Type | Retry? | Action |
|------------|--------|--------|
| `LocalCapExceededError` | Allowed (policy-gated) | Caller decides |
| `BudgetExhaustedError` | Never in-scope | Escalate/degrade |
| `ExternalCancellationError` | Never automatically | Propagate or clean up |

**Exception bridging policy:** At integration boundaries where legacy code catches `asyncio.TimeoutError`, the handler must explicitly check error type and re-raise `BudgetExhaustedError` if the cause was budget exhaustion. A utility `bridge_timeout_error()` function handles this mapping.

**Cancellation precedence rule:** If both local timeout and external cancel happen near-simultaneously, `ExternalCancellationError` wins. External cancellation represents a policy decision from a higher authority (shutdown, dependency loss) and must not be masked by a coincidental local timeout.

### 3.2 CancelScope (immutable + tokenized, write-once)

```python
class CancellationCause(Enum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    OWNER_SHUTDOWN = "owner_shutdown"
    DEPENDENCY_LOST = "dependency_lost"
    MANUAL_CANCEL = "manual_cancel"

@dataclass(frozen=True)
class CancelScope:
    scope_id: str
    cause: CancellationCause
    set_at_mono: float
    detail: str
    owner_id: str

class CancelScopeHandle:
    """Thread-safe write-once wrapper. First set wins."""
    _scope: Optional[CancelScope]  # None until triggered
    _lock: threading.Lock

    def set_cause(self, cause, detail) -> bool:
        """Returns True if first set, False if already set (logs warning)."""

    @property
    def scope(self) -> Optional[CancelScope]:
        """Read the frozen scope."""
```

### 3.3 ExecutionContext

```python
class RootReason(Enum):
    DETACHED_BACKGROUND = "detached_background"
    RECOVERY_WORKER = "recovery_worker"
    USER_JOB = "user_job"

class RequestKind(Enum):
    STARTUP = "startup"
    RUNTIME = "runtime"
    RECOVERY = "recovery"
    BACKGROUND = "background"

class Criticality(Enum):
    CRITICAL = "critical"   # must complete or system fails
    HIGH = "high"           # important but degradable
    NORMAL = "normal"       # standard operation
    LOW = "low"             # best-effort, sheddable

@dataclass(frozen=True)
class ExecutionContext:
    # Core budget fields (Phase 1)
    deadline_mono: float            # time.monotonic() absolute deadline
    trace_id: str                   # correlation ID
    owner_id: str                   # who created this budget
    cancel_scope: CancelScopeHandle # write-once cancel state
    mode_snapshot: str              # system mode at creation

    # Nesting
    parent_ctx: Optional['ExecutionContext'] = None
    created_at_mono: float = field(default_factory=time.monotonic)

    # Phase 2 fields (state machine linkage)
    phase_id: str = ""
    phase_name: str = ""
    mode_epoch: int = 0             # detect stale mode snapshots
    budget_policy_version: int = 1  # rollout/compat diagnostics

    # Phase 3 fields (admission control)
    priority: Criticality = Criticality.NORMAL
    request_kind: RequestKind = RequestKind.STARTUP
    tags: Mapping[str, str] = field(default_factory=dict)

    # Root scope metadata
    root_reason: Optional[RootReason] = None
```

### 3.4 ContextVar

```python
_current_ctx: ContextVar[Optional[ExecutionContext]] = ContextVar(
    "jarvis_execution_context", default=None
)
```

### 3.5 Nested Budget Policy

```
effective_deadline = min(parent.deadline_mono, now + local_cap)
```

- Child scopes can only shrink parent budgets, never extend.
- `root=True` creates a fresh deadline (ignores parent).
- `root=True` requires `root_reason` enum and owner in allowlist.
- `root=True` without these raises `ValueError`.

```python
_ROOT_SCOPE_ALLOWLIST: Final[frozenset] = frozenset({
    "supervisor",
    "phase_manager",
    "recovery_coordinator",
    "background_health_monitor",
})
```

---

## 4. API Surface

### 4.1 `execution_budget()` — boundary marker

```python
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
    priority: Criticality = Criticality.NORMAL,
    request_kind: RequestKind = RequestKind.STARTUP,
    tags: Optional[Mapping[str, str]] = None,
) -> AsyncGenerator[ExecutionContext, None]:
```

**Context leak prevention:** The `finally` block always resets the ContextVar token, even under nested failures, preventing cross-request context bleed.

### 4.2 `budget_aware_wait_for()` — interior replacement

```python
async def budget_aware_wait_for(
    coro: Awaitable[T],
    *,
    local_cap: float,
    label: str = "",
) -> T:
```

**Fail-closed contract:**

| Situation | Behavior | Log field |
|-----------|----------|-----------|
| Active budget + local_cap | `effective = min(remaining, local_cap)` | `scoped=true` |
| No budget + local_cap | Local-only timeout | `unscoped_local_timeout=true` |
| No budget + no local_cap | `RuntimeError` | N/A (crash) |

### 4.3 Query functions

```python
def remaining_budget() -> Optional[float]:
    """Remaining ms, or None if no active budget."""

def current_context() -> Optional[ExecutionContext]:
    """Active ExecutionContext, or None."""
```

### 4.4 Propagation helpers

```python
def propagate_to_executor(fn: Callable[..., T]) -> Callable[..., T]:
    """Wraps fn with contextvars.copy_context().run() for thread executors."""

def propagate_to_task(coro: Coroutine) -> asyncio.Task:
    """Creates task with copied context. Consistent across Python 3.9-3.12+."""
```

**Task propagation note:** Python 3.11+ copies context in `create_task` by default. The wrapper is kept for consistency across Python versions and nonstandard schedulers.

### 4.5 Exception bridging

```python
def bridge_timeout_error(
    timeout_error: asyncio.TimeoutError,
    *,
    label: str = "",
) -> Union[BudgetExhaustedError, LocalCapExceededError]:
    """Maps legacy asyncio.TimeoutError to typed error based on active context."""
```

### 4.6 Rollout safety

```python
# Feature flag: shadow mode (compute + log without enforcing)
BUDGET_ENFORCE: bool = os.getenv("JARVIS_BUDGET_ENFORCE", "true").lower() == "true"
BUDGET_SHADOW: bool = os.getenv("JARVIS_BUDGET_SHADOW", "false").lower() == "true"
```

In shadow mode, `budget_aware_wait_for` computes the effective timeout and logs telemetry but uses the raw `local_cap` for actual enforcement. This enables low-risk rollout with full observability.

---

## 5. Observability

Every `budget_aware_wait_for` call emits structured log fields:

**On entry:**
```
[Budget] label=lock_handover owner=phase_preflight
         deadline_mono=184729.3 remaining_ms_in=47200
         local_cap_ms=30000 effective_ms=30000
         timeout_origin=local_cap scoped=true
```

**On success:**
```
[Budget] label=lock_handover COMPLETED
         remaining_ms_out=41800 elapsed_ms=5400
```

**On failure:**
```
[Budget] label=ipc_bind BUDGET_EXHAUSTED
         remaining_ms_out=0 elapsed_ms=9800
         timeout_origin=budget owner=phase_preflight
         parent_chain=phase_preflight->supervisor
```

Fields: `deadline_at`, `remaining_ms_in`, `remaining_ms_out`, `timeout_origin` (budget|local_cap|external_cancel), `scoped`, `unscoped_local_timeout`.

---

## 6. Surgical Change Points

### 6.1 New file

`backend/core/execution_context.py` — ~400 lines.

### 6.2 `unified_supervisor.py` — 8 phase boundary sites

Each phase entry in `_startup_impl()` changes from:

```python
await asyncio.wait_for(self._phase_xxx(), timeout=xxx_timeout)
```

To:

```python
async with execution_budget(
    "phase_xxx", xxx_timeout,
    phase_id="N", phase_name="xxx",
    priority=Criticality.CRITICAL,
    request_kind=RequestKind.STARTUP,
):
    await self._phase_xxx()
```

Lines: ~64410, ~65770, ~65877, ~65974, ~66133, ~66755, ~66897, ~67378.

Exception handlers change from `except asyncio.TimeoutError` to:
```python
except BudgetExhaustedError as e:
    # Log with full metadata, escalate/degrade
except LocalCapExceededError as e:
    # Log, may retry or continue
```

### 6.3 `unified_supervisor.py` — ~20 service-level fan-out sites

Inside phases, top-level sub-calls change from `asyncio.wait_for` to `budget_aware_wait_for`:

- `_init_enterprise_service_with_timeout()` wrapper
- `ParallelInitializer` per-component timeouts
- Model load paths in `_phase_intelligence()` and `_phase_trinity()`

### 6.4 `backend/core/async_safety.py` — propagation verification

Line ~1806 already uses `contextvars.copy_context().run()`. Add `propagate_to_task` and `propagate_to_executor` helpers.

### 6.5 `backend/core/cancellation.py` — integration

`CancellationToken.cancel()` gains ~5 lines: reads `_current_ctx`, if active, calls `cancel_scope.set_cause(OWNER_SHUTDOWN, reason)`.

---

## 7. Acceptance Tests

### 7.1 Unit tests (`tests/unit/backend/core/test_execution_context.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_budget_shrinks_with_nesting` | `effective = min(parent_deadline, now + local_cap)` |
| 2 | `test_budget_never_extends` | Child with large `local_cap` gets parent's remaining |
| 3 | `test_root_scope_creates_fresh_deadline` | `root=True` ignores parent |
| 4 | `test_root_scope_requires_reason` | `root=True` without `root_reason` → `ValueError` |
| 5 | `test_root_scope_blocked_for_unauthorized_owner` | Owner not in allowlist → `ValueError` |
| 6 | `test_budget_exhausted_raises_typed_error` | `BudgetExhaustedError` with metadata |
| 7 | `test_local_cap_exceeded_raises_typed_error` | `LocalCapExceededError(TimeoutError)` |
| 8 | `test_external_cancel_raises_typed_error` | `ExternalCancellationError` with cause |
| 9 | `test_no_budget_no_cap_fails_closed` | `RuntimeError` |
| 10 | `test_no_budget_with_cap_uses_local` | Works, logs `unscoped_local_timeout=true` |
| 11 | `test_cancel_scope_write_once` | Second `set_cause()` returns False |
| 12 | `test_context_propagates_through_await` | Budget visible in nested async |
| 13 | `test_context_propagates_to_thread_executor` | Budget preserved in threads |
| 14 | `test_context_propagates_to_created_task` | Budget preserved in tasks |
| 15 | `test_monotonic_clock_only` | Uses `time.monotonic()`, never `time.time()` |
| 16 | `test_observability_fields_logged` | All telemetry fields present |
| 17 | `test_cancellation_token_sets_scope_cause` | Token cancel → OWNER_SHUTDOWN |
| 18 | `test_context_leak_prevention` | ContextVar reset in finally block |
| 19 | `test_cancel_precedence_over_local_timeout` | External cancel wins over local timeout |
| 20 | `test_concurrent_sibling_context_isolation` | Parallel tasks don't see each other's context |
| 21 | `test_shadow_mode_logs_without_enforcing` | Shadow mode uses local_cap, logs effective |
| 22 | `test_exception_bridging` | `bridge_timeout_error` maps correctly |

### 7.2 Integration tests (`tests/integration/test_budget_propagation.py`)

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_phase_budget_propagates_to_services` | 5s budget → 3x3s services → third gets ~2s |
| 2 | `test_budget_exhaustion_error_type` | `BudgetExhaustedError` (not `TimeoutError`) |
| 3 | `test_budget_metadata_audit_trail` | `parent_ctx` chain inspectable |
| 4 | `test_race_external_cancel_vs_local_timeout` | Deterministic: external cancel wins |
| 5 | `test_concurrent_sibling_budget_isolation` | Sibling tasks with different budgets isolated |

---

## 8. Rollout Strategy

1. **Shadow mode first:** Deploy with `JARVIS_BUDGET_ENFORCE=false JARVIS_BUDGET_SHADOW=true`. All budget math runs, telemetry emits, but enforcement uses raw `local_cap`. Validates correctness without risk.
2. **Phase boundaries enforced:** Enable enforcement at 8 phase boundary sites only. Interior calls remain unscoped.
3. **Service fan-out enforced:** Enable enforcement at ~20 service-level sites.
4. **Burn-down unscoped calls:** Use `unscoped_local_timeout=true` log field to identify and migrate remaining interior calls incrementally.

---

## 9. Non-Goals (Phase 1)

- No state machine formalization (Phase 2)
- No mode unification or recovery policy (Phase 3)
- No admission control or backpressure (Phase 4)
- No global replacement of all 400+ `asyncio.wait_for` calls (incremental migration)

---

## 10. Dependencies

- **Phase 2 consumes:** `phase_id`, `phase_name`, `mode_epoch` fields from `ExecutionContext`
- **Phase 3 consumes:** `priority`, `request_kind`, `mode_snapshot`, `tags` fields
- **Phase 4 consumes:** `remaining_budget()` query for admission control decisions

---

## 11. File Manifest

| File | Action | Lines |
|------|--------|-------|
| `backend/core/execution_context.py` | CREATE | ~400 |
| `unified_supervisor.py` | EDIT (8 phase boundaries + ~20 service sites) | ~150 net |
| `backend/core/cancellation.py` | EDIT (cancel → scope integration) | ~10 net |
| `backend/core/async_safety.py` | EDIT (propagation helpers) | ~30 net |
| `tests/unit/backend/core/test_execution_context.py` | CREATE | ~500 |
| `tests/integration/test_budget_propagation.py` | CREATE | ~200 |

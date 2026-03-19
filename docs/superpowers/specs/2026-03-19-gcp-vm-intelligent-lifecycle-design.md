# GCP VM Intelligent Lifecycle Management

**Date:** 2026-03-19
**Version:** v298.0
**Status:** Approved for implementation
**Spec authors:** Engineering
**Depends on:** v297.0 (`2026-03-19-gcp-handshake-correlated-session-design.md`) — must be implemented first
**Related plan:** `2026-03-19-gcp-vm-intelligent-lifecycle.md` (to be written)

---

## 1. Problem Statement

### 1.1 Observed Symptom

Every JARVIS startup emits:

```
Zone failover: trying us-central1-b (2/4)
```

at ~40% boot progress. The GCP golden image VM (NVIDIA L4, Qwen2.5-7B + all deps, ~60s startup) is not running because it is started lazily — only when a Prime inference request arrives. By then the supervisor is already routing through zone fallback paths.

### 1.2 Root Causes

1. **Lazy start only.** `ensure_static_vm_ready()` is called on demand. No proactive boot trigger exists.
2. **Duplicate lifecycle state.** `VMLifecycleState` in `supervisor_gcp_controller.py` is mutated by the controller itself AND implicitly by `gcp_vm_manager.py`. Two independent trackers diverge.
3. **No drain safety.** `prime_client.py` has no in-flight request count. VM can be stopped during an active stream.
4. **No activity classification.** Health pings, telemetry, and inference requests all land on the same `record_jprime_activity()` hook. Health pings reset the idle timer and silently prevent shutdown.
5. **Hardcoded idle thresholds.** `_idle_monitor_loop()` polls every 60 s with embedded constants that cannot be tuned per deployment.
6. **No process fencing.** A duplicate supervisor process or dirty restart can race the lifecycle state machine, causing dual-authority split-brain.

### 1.3 Goals

- GCP VM is proactively started at supervisor boot — not on first request.
- Idle shutdown is cost-aware, configurable, and driven by **meaningful** activity only.
- Drain safety is an enforced invariant: no VM stop during active streams, tool calls, or in-flight Prime requests.
- One authoritative FSM owns all lifecycle state; legacy state is a read-only projection.
- Process fencing prevents dual-authority across restarts.
- All timing is deadline-driven (monotonic); no fixed-interval polling.
- Boot-order contract validation (JARVIS / J-Prime / Reactor) is gated before first Prime routing.
- J-Prime offline is a first-class `DEGRADED_BOOT_MODE`, not a silent fallback.

### 1.4 Non-Goals

- This spec does not modify the GCP provisioning path (zone fallback, instance creation).
- This spec does not change the v297.0 handshake session, failure taxonomy, or recovery matrix — it integrates with them.
- Remote / multi-device unlock workflows are out of scope.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  VMLifecycleManager  (new — backend/core/vm_lifecycle_manager.py)       │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  VMFsmState  (single authoritative FSM)                           │  │
│  │  COLD → WARMING → READY ⇄ IN_USE → IDLE_GRACE → STOPPING → COLD  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  LifecycleLease        ActivityRegistry      VMLifecycleConfig          │
│  (process fencing)     (caller registry)     (all env-driven)           │
│                                                                         │
│  work_slot()           ensure_warmed()       LifecycleTransitionEvent   │
│  (drain + idle)        (single warm entry)   (structured telemetry)     │
└────────────────────────────┬────────────────────────────────────────────┘
                             │ VMController protocol
                             ▼
┌────────────────────────────────────────────────────────────────────────┐
│  supervisor_gcp_controller.py  (modified)                              │
│  VMLifecycleState → read-only projection of VMFsmState                 │
│  _GCPControllerAdapter implements VMController protocol                │
│  _idle_monitor_loop() REMOVED                                          │
│  startup: asyncio.create_task(lifecycle.ensure_warmed("boot"))         │
└────────────────────────────────────────────────────────────────────────┘
                             │
               ┌─────────────┴──────────────┐
               ▼                            ▼
┌──────────────────────────┐  ┌─────────────────────────────────────────┐
│  prime_client.py         │  │  startup_orchestrator.py                │
│  set_lifecycle_manager() │  │  set_lifecycle_manager()                │
│  work_slot(MEANINGFUL)   │  │  acquire_gcp_lease() calls              │
│  wraps all requests      │  │  lifecycle.ensure_warmed("lease_req")   │
└──────────────────────────┘  └─────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────┐
│  prime_router.py (minor) │
│  record_activity_from()  │
│  instead of flat signal  │
└──────────────────────────┘
```

**Integration with v297.0:** `ensure_warmed()` delegates VM start + handshake to `VMController.start_vm()`, which calls `supervisor_gcp_controller._start_gcp_vm()`. The correlated `HandshakeSession` flows through `gcp_readiness_lease.acquire()` exactly as v297.0 specifies. The lifecycle manager has no knowledge of handshake internals — it receives only `(success: bool, failed_step: HandshakeStep, failure_class: ReadinessFailureClass)` from the controller.

---

## 3. Module: `backend/core/vm_lifecycle_manager.py`

### 3.1 Configuration

```python
@dataclass(frozen=True)
class VMLifecycleConfig:
    inactivity_threshold_s: float       # JARVIS_VM_INACTIVITY_THRESHOLD_S   default 1800
    idle_grace_s: float                 # JARVIS_VM_IDLE_GRACE_S             default 300
    warming_await_timeout_s: float      # JARVIS_VM_WARMING_AWAIT_S          default 90
    max_uptime_s: Optional[float]       # JARVIS_VM_MAX_UPTIME_S             optional
    quiet_hours: Optional[Tuple[int, int]]  # JARVIS_VM_QUIET_HOURS="22:6"
    quiet_hours_threshold_factor: float # JARVIS_VM_QUIET_HOURS_FACTOR       default 0.25
    drain_hard_cap_s: float             # JARVIS_VM_DRAIN_HARD_CAP_S         default 600
    warm_max_strikes: int               # JARVIS_VM_WARM_MAX_STRIKES         default 3
    lease_dir: Path                     # JARVIS_VM_LEASE_DIR      default ~/.jarvis/lifecycle/
    strict_drain: bool                  # JARVIS_VM_STRICT_DRAIN             default False (warn), True in test

    @classmethod
    def from_env(cls) -> "VMLifecycleConfig": ...
```

`quiet_hours` semantics: during the configured hours, `_effective_threshold_s()` returns `inactivity_threshold_s * quiet_hours_threshold_factor`. The VM shuts down **faster** during off-hours. `max_uptime_s` is never multiplied by quiet hours factor.

### 3.2 FSM States and Legal Transitions

```
COLD ─────── ensure_warmed() ──────────────────────────────────────────────┐
  │                                                                        │
  │ ensure_warmed()                                              stop confirmed
  ▼                                                                        │
WARMING                                                              STOPPING
  │                                           grace expires                │
  │ handshake success                  + _drain_clear_event set            │
  ▼                                                 │                      │
READY ─── work_slot(MEANINGFUL) ──► IN_USE ─────────┘                      │
  │                                   │                                    │
  │ inactivity timer fires            │ last MEANINGFUL slot released      │
  │                                   │ AND timer already elapsed          │
  └───────────────────────────────────┴────────────► IDLE_GRACE ───────────┘
                                                         │
                                              work_slot(MEANINGFUL)
                                                         │
                                                     IN_USE (grace cancelled)
```

**Illegal transitions** (all raise `LifecycleFSMError`):
- Any transition that skips IDLE_GRACE before STOPPING (drain bypass)
- `STOPPING → any` (STOPPING is a terminal step toward COLD; COLD is re-entrant via `ensure_warmed`)
- `COLD → IN_USE` without passing through WARMING → READY

**Transition table:**

| From | To | Trigger | Guard |
|---|---|---|---|
| COLD | WARMING | `ensure_warmed()` called | Process lease held |
| WARMING | READY | Handshake success | — |
| WARMING | COLD | Handshake failure / timeout | Records `_last_warming_failure` |
| READY | IN_USE | `work_slot(MEANINGFUL)` enter | — |
| READY | IDLE_GRACE | Inactivity timer fires | — |
| IN_USE | READY | Last MEANINGFUL slot released, timer not elapsed | `_meaningful_count == 0` |
| IN_USE | IDLE_GRACE | Last MEANINGFUL slot released, timer already elapsed | `_meaningful_count == 0` |
| IDLE_GRACE | IN_USE | `work_slot(MEANINGFUL)` enter | Cancels `_grace_period_task` |
| IDLE_GRACE | STOPPING | Grace period expires | `_meaningful_count == 0` (event-driven) |
| STOPPING | COLD | VM stop confirmed or stop timed out | — |

### 3.3 Process-Level Lifecycle Fencing — `LifecycleLease`

`LifecycleLease` is acquired in `VMLifecycleManager.start()` before any FSM work. It prevents dual-authority across restarts or accidental duplicate supervisor processes.

**Lease file:** `{lease_dir}/vm_lifecycle.lease` — JSON: `{"pid": int, "session_id": str, "acquired_at": float}`

**`acquire()` decision tree:**

```
1. open(O_CREAT|O_RDWR) + fcntl.flock(LOCK_EX|LOCK_NB)
   └─ LOCK_NB fails → DualAuthorityError(incumbent=None, reason="flock_held")

2. Read and parse existing JSON
   └─ parse failure → treat as stale; overwrite; log WARNING

3. Check incumbent PID:
   os.kill(pid, 0) → ProcessLookupError  → stale  → overwrite; log WARNING
   os.kill(pid, 0) → PermissionError     → live   → DualAuthorityError(reason="pid_live")
   os.kill(pid, 0) → success             → live   → DualAuthorityError(reason="pid_live")
   pid == os.getpid()                    → self   → overwrite (re-acquire after fork edge case)

4. Write own record; flush; fdatasync

5. Register atexit(self.release)
   Returns session_id (uuid4().hex)
```

`DualAuthorityError` carries: `incumbent_pid: int`, `incumbent_session_id: str`, `incumbent_acquired_at: float`, `reason: str`. Logged at CRITICAL. Supervisor startup aborts — no retry.

`release()`: zero PID field, flush, `flock(LOCK_UN)`. Idempotent.

`session_id` generated at `acquire()` time. Propagated to all `LifecycleTransitionEvent.session_id` and to `StartupEventBus` trace_id for cross-system log correlation.

### 3.4 Activity Classification Registry

The registry is the contract. External callers use `record_activity_from(caller_id)`. The flat `record_activity(ActivityClass)` method is package-private (`_record_activity`). Callers that are not in the registry raise `UnregisteredActivitySourceError` when `strict_drain=True`.

```python
class ActivityClass(str, Enum):
    MEANINGFUL     = "meaningful"      # resets idle timer; counted in drain invariant
    NON_MEANINGFUL = "non_meaningful"  # does NOT reset timer; not counted in drain invariant

@dataclass(frozen=True)
class ActivitySource:
    caller_id: str
    activity_class: ActivityClass
    description: str

_ACTIVITY_REGISTRY: Dict[str, ActivitySource] = {
    # MEANINGFUL — drain-relevant
    "prime_client.execute_request":    ActivitySource(..., MEANINGFUL, "HTTP/streaming inference request"),
    "prime_client.stream_chunks":      ActivitySource(..., MEANINGFUL, "Active SSE stream consumption"),
    "prime_client.websocket_session":  ActivitySource(..., MEANINGFUL, "Open WS session to J-Prime"),
    "prime_client.tool_call_execute":  ActivitySource(..., MEANINGFUL, "Tool call round-trip"),
    # NON_MEANINGFUL — infrastructure, never resets idle timer
    "health_probe.probe_health":       ActivitySource(..., NON_MEANINGFUL, "/health ping"),
    "health_probe.probe_capabilities": ActivitySource(..., NON_MEANINGFUL, "/capabilities check"),
}
```

New callers must be added to `_ACTIVITY_REGISTRY` with explicit classification. This is the enforcement gate.

**Unregistered caller behavior by mode:**
- `strict_drain=True` (tests): raises `UnregisteredActivitySourceError` immediately.
- `strict_drain=False` (production default): logs a WARNING and classifies the caller as `NON_MEANINGFUL` (safe default — unknown callers cannot reset the idle timer or block STOPPING). This is intentionally conservative: unknown callers are never treated as meaningful work.

### 3.5 Drain Safety — Dual Counters

Two separate counters. The drain invariant checks only the meaningful counter:

```python
self._meaningful_count: int = 0       # MEANINGFUL work_slots in flight
self._non_meaningful_count: int = 0   # NON_MEANINGFUL work_slots in flight (informational only)
self._drain_clear_event: asyncio.Event  # set when _meaningful_count drops to 0

def _drain_clear(self) -> bool:
    return self._meaningful_count == 0  # NON_MEANINGFUL never blocks STOPPING
```

**`work_slot()` admission rules by state:**

| State | MEANINGFUL | NON_MEANINGFUL |
|---|---|---|
| COLD | `VMNotReadyError` | `VMNotReadyError` |
| WARMING | bounded-await up to `warming_await_timeout_s`, then `VMNotReadyError` | same |
| READY | admitted | admitted |
| IN_USE | admitted | admitted |
| IDLE_GRACE | admitted; cancels grace → IN_USE | admitted; no timer reset, no grace cancel |
| STOPPING | `VMNotReadyError` | `VMNotReadyError` (rejected immediately) |

Health probes in IDLE_GRACE are admitted and tracked but do not block STOPPING. Health probes in STOPPING are rejected — the probe handles `VMNotReadyError` as "VM going down, skip probe." This prevents health probes from indefinitely blocking shutdown.

### 3.6 `work_slot()` During WARMING — Taxonomy-Mapped Recovery

When `work_slot()` times out waiting for WARMING to complete, the `VMNotReadyError` carries a `RecoveryStrategy` derived from the v297.0 `_RECOVERY_MATRIX` — never hardcoded:

```python
@asynccontextmanager
async def work_slot(self, activity_class: ActivityClass, *, description: str = ""):
    async with self._lock:
        current_state = self._state
        warming_future = self._warming_future if current_state == VMFsmState.WARMING else None

    if warming_future is not None:
        try:
            await asyncio.wait_for(asyncio.shield(warming_future), timeout=self._config.warming_await_timeout_s)
        except asyncio.TimeoutError:
            step, fc = self._last_warming_failure or (HandshakeStep.HEALTH, ReadinessFailureClass.TRANSIENT_INFRA)
            strategy = select_recovery_strategy(step, fc)   # v297.0 _RECOVERY_MATRIX
            raise VMNotReadyError(state=VMFsmState.WARMING, recovery=strategy, failure_class=fc,
                                  detail="warming_await_timeout")

    # NOTE: double-lock pattern is intentional. The first lock acquisition snapshots
    # the warming_future without blocking. The asyncio.shield() await then runs without
    # holding the lock (preventing deadlock and allowing other state mutations during
    # the wait). The second lock acquisition re-reads self._state because the FSM
    # may have advanced (WARMING→READY, WARMING→COLD, or even READY→STOPPING) while
    # we were waiting. Always re-check under lock after any await.
    async with self._lock:
        if self._state in (VMFsmState.COLD, VMFsmState.STOPPING):
            step, fc = self._last_warming_failure or (HandshakeStep.HEALTH, ReadinessFailureClass.TRANSIENT_INFRA)
            strategy = select_recovery_strategy(step, fc)
            raise VMNotReadyError(state=self._state, recovery=strategy, failure_class=fc)
        # ... increment count, manage timers
```

`VMNotReadyError(recovery: RecoveryStrategy, failure_class: ReadinessFailureClass)` — PrimeRouter uses `recovery` to route the fallback decision through the existing routing matrix. No routing logic lives in `vm_lifecycle_manager.py`.

### 3.7 Deadline-Driven Timers

No fixed-interval polling. All timers sleep exactly until the next threshold crossing:

```python
async def _idle_timer_task(self) -> None:
    """Single-shot: fires exactly at inactivity threshold. Re-spawned on MEANINGFUL reset."""
    deadline = self._last_meaningful_mono + self._effective_threshold_s()
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return   # cancelled by record_activity(MEANINGFUL)
    await self._on_inactivity_threshold_elapsed()

async def _grace_period_task(self) -> None:
    """Single-shot: fires at grace expiry, then awaits drain event (event-driven)."""
    deadline = self._idle_grace_entered_mono + self._config.idle_grace_s
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return   # cancelled by IDLE_GRACE → IN_USE
    # Grace elapsed. Wait for drain — event-driven, not polling.
    if not self._drain_clear():
        await self._drain_clear_event.wait()
    await self._on_grace_and_drain_complete()

async def _max_uptime_task(self) -> None:
    """Single-shot: spawned at WARMING→READY. Fires exactly at max_uptime_s."""
    await asyncio.sleep(self._config.max_uptime_s)
    await self._on_max_uptime_elapsed()
```

`_drain_clear_event` management:
- Set when `_meaningful_count` drops to 0
- Cleared when a MEANINGFUL slot is entered
- Initial state: set (no work in flight at start)

When `record_activity_from(MEANINGFUL)` fires: cancel `_idle_timer_task`; reset `_last_meaningful_mono`; spawn new `_idle_timer_task` with updated deadline.

### 3.8 max_uptime Precedence Rules

| Condition | Behavior |
|---|---|
| `max_uptime` elapses, state is COLD/WARMING | No-op |
| `max_uptime` elapses, state is READY | `READY → IDLE_GRACE` immediately |
| `max_uptime` elapses, state is IN_USE | `IN_USE → IDLE_GRACE`. Work continues. Drain honored. |
| Grace expires, `_meaningful_count > 0` | Await `_drain_clear_event` (event-driven) |
| Time since `max_uptime` exceeded `drain_hard_cap_s` | Emit `max_uptime_drain_hard_cap_exceeded` telemetry event. **Do not force stop.** Active inference is never killed mid-stream. |
| Quiet hours active when `max_uptime` fires | `max_uptime` is not affected by quiet hours factor. IDLE_GRACE proceeds normally. |
| VM re-warmed after max_uptime stop | `_warm_started_mono` reset; new uptime window starts fresh. |

### 3.9 Structured Transition Telemetry

Every FSM transition emits a `LifecycleTransitionEvent`. State mutation is synchronous under the lock. Telemetry is a fire-and-forget task scheduled after lock release — never on the critical path:

```python
@dataclass
class LifecycleTransitionEvent:
    session_id: str                          # from LifecycleLease.session_id
    timestamp_mono: float
    timestamp_wall: float
    from_state: VMFsmState
    to_state: VMFsmState
    trigger: str                             # "inactivity_threshold_elapsed", "work_slot_acquired", ...
    reason_code: str                         # "IDLE_THRESHOLD_ELAPSED", "DRAIN_COMPLETE", "HANDSHAKE_FAILED", ...
    strategy: Optional[str]                  # RecoveryStrategy.value if applicable
    latency_s: float                         # time spent in from_state
    retry_count: int                         # handshake retries (0 for non-WARMING transitions)
    active_work_count_at_transition: int     # total (meaningful + non_meaningful)
    meaningful_count_at_transition: int
    detail: Optional[str]

async def _transition(self, to_state, *, trigger, reason_code, strategy=None, detail=None):
    async with self._lock:
        from_state = self._state
        event = LifecycleTransitionEvent(
            session_id=self._session_id,
            latency_s=time.monotonic() - self._state_entered_mono,
            retry_count=self._retry_count,
            active_work_count_at_transition=self._meaningful_count + self._non_meaningful_count,
            meaningful_count_at_transition=self._meaningful_count,
            # ... rest of fields
        )
        self._state = to_state
        self._state_entered_mono = time.monotonic()
    # Lock released — telemetry is fire-and-forget
    task = asyncio.create_task(self._telemetry_sink.emit(event))
    task.add_done_callback(
        lambda t: t.exception() and _log.warning("lifecycle telemetry failed: %s", t.exception())
    )
```

`telemetry_sink` is injected at construction: a `StartupEventBusAdapter` in production, `_RecordingSink` in tests. No telemetry hard-dependency in the lifecycle module.

**`StartupEventBusAdapter`** (defined in `vm_lifecycle_manager.py`) implements `LifecycleTelemetrySink` and bridges the type mismatch between `LifecycleTransitionEvent` and `StartupEvent`:

```python
class StartupEventBusAdapter:
    """Implements LifecycleTelemetrySink. Wraps StartupEventBus.

    Converts LifecycleTransitionEvent → StartupEvent so the lifecycle module
    has no direct dependency on StartupEvent's schema.
    """
    def __init__(self, bus: StartupEventBus) -> None:
        self._bus = bus

    async def emit(self, event: LifecycleTransitionEvent) -> None:
        startup_event = self._bus.create_event(
            event_type="vm_lifecycle_transition",
            detail={
                "from_state": event.from_state.value,
                "to_state": event.to_state.value,
                "trigger": event.trigger,
                "reason_code": event.reason_code,
                "strategy": event.strategy,
                "latency_s": event.latency_s,
                "retry_count": event.retry_count,
                "active_work_count": event.active_work_count_at_transition,
                "meaningful_count": event.meaningful_count_at_transition,
                "detail": event.detail,
            },
            phase=None,
            authority_state=None,
        )
        await self._bus.emit(startup_event)
```

`StartupEventBusAdapter` is the only class in `vm_lifecycle_manager.py` that imports from `startup_telemetry.py`. `VMLifecycleManager` itself only holds a `LifecycleTelemetrySink` reference — no direct `StartupEventBus` import.

### 3.10 Public API Summary

```python
class VMLifecycleManager:
    # Construction
    def __init__(self, config: VMLifecycleConfig, controller: VMController,
                 telemetry_sink: LifecycleTelemetrySink) -> None: ...
    async def start(self) -> None        # binds event loop, acquires LifecycleLease, starts background tasks
    async def stop(self) -> None         # cancels tasks, releases lease; idempotent

    # Single canonical warm entrypoint
    async def ensure_warmed(self, reason: str) -> bool
    # Concurrent calls collapse: if WARMING, awaits the in-progress future.
    # If READY/IN_USE, returns True immediately.
    # Returns False on handshake failure.

    # Drain-safe work tracking
    @asynccontextmanager
    async def work_slot(self, activity_class: ActivityClass, *, description: str = "")

    # Activity signal (fire-and-forget)
    def record_activity_from(self, caller_id: str) -> None

    # Explicit shutdown
    async def request_shutdown(self, reason: str = "") -> None

    # State inspection
    @property def state(self) -> VMFsmState
    @property def active_work_count(self) -> int        # total
    @property def meaningful_work_count(self) -> int
    @property def uptime_s(self) -> Optional[float]     # None if COLD/WARMING
    @property def boot_mode(self) -> BootMode
    @property def session_id(self) -> str
```

### 3.11 VMController Protocol

```python
class VMController(Protocol):
    async def start_vm(self) -> Tuple[bool, Optional[HandshakeStep], Optional[ReadinessFailureClass]]:
        """Start VM and run handshake. Returns (success, failed_step, failure_class)."""
    async def stop_vm(self) -> None
    def get_vm_host_port(self) -> Optional[Tuple[str, int]]
    def notify_vm_unreachable(self) -> None    # prober failure → triggers COLD transition
```

No import of `VMLifecycleManager` in the controller. Dependency flows one way: `VMLifecycleManager` → `VMController` protocol only.

---

## 4. DEGRADED_BOOT_MODE

J-Prime offline is a first-class state, not a silent fallback.

```python
class BootMode(str, Enum):
    NORMAL   = "normal"    # All gates enforced; J-Prime required for Prime routing
    DEGRADED = "degraded"  # J-Prime unreachable; gate disabled; local-only routing

@dataclass(frozen=True)
class BootModeRecord:
    mode: BootMode
    reason: str                              # "j_prime_unreachable", "brain_handshake_timeout"
    degraded_capabilities: FrozenSet[str]    # {"prime_inference", "gpu_acceleration"}
    entered_at_wall: float
```

When `run_boot_handshake()` determines J-Prime is offline:

1. Required brain set → empty (gate disabled / observatory mode, existing behavior)
2. `CapabilityDomain.MODEL_ROUTER` → `mark_degraded(detail="j_prime_unreachable")`
3. `BootModeRecord` stored on `StartupOrchestrator` with `mode=DEGRADED`
4. PrimeRouter routing table: PRIME_API tier removed; PRIME_LOCAL → CLAUDE path remains
5. `VMLifecycleManager`: proactive `ensure_warmed()` records a failure strike. After `warm_max_strikes` consecutive failures, `ensure_warmed()` returns `False` immediately and backs off (exponential, starting at 60 s, cap 600 s) before accepting the next call.
6. `BootModeRecord` included in `/health/ready` response under `"boot_mode"` key.

**Recovery path:** DEGRADED_BOOT_MODE is recoverable. If J-Prime later responds to a PROBE_HEALTH check (triggered by the exponential backoff retry), `run_boot_handshake()` is re-run, required brains are re-validated, and `BootMode` transitions back to `NORMAL` with a `DEGRADED→NORMAL` `LifecycleTransitionEvent`.

---

## 5. Boot Sequence and Cross-Repo Contract Validation

```
Phase 0  LifecycleLease.acquire()
         ← DualAuthorityError → CRITICAL log, supervisor startup aborts

Phase 1  FastAPI bind
         CapabilityDomain.BACKEND_HTTP + WEBSOCKET → mark_satisfied

Phase 2  asyncio.create_task(lifecycle.ensure_warmed(reason="boot"))
         Non-blocking — runs concurrently with phases 3-4 (~60 s GCP warm)

Phase 3  CapabilityDomain.MODEL_ROUTER readiness probe
         (local model OR wait for GCP via Phase 2)

Phase 4  run_boot_handshake()   ← BLOCKING gate before first Prime routing
         Validates brain contracts:
           • /v1/brains on JARVIS local                  (required)
           • /v1/brains on J-Prime                       (required if reachable; → DEGRADED if not)
           • /v1/brains on Reactor                       (required if JARVIS_REACTOR_REPO_PATH set)
         ← missing required brain      → hard abort
         ← j-prime offline             → BootMode.DEGRADED; observatory mode; continue
         ← reactor offline (optional)  → log WARNING; continue

Phase 5  StartupOrchestrator.resolve_phase("CORE_READY")
         ← registered as gate dependency on Phase 4 completion in PhaseGateCoordinator

Phase 6  StartupOrchestrator.attempt_handoff()
         RoutingAuthorityFSM: BOOT_POLICY_ACTIVE → HYBRID_ACTIVE

Phase 7  First Prime routing eligible
         ← VMLifecycleManager.state must be READY or IN_USE
         ← if still WARMING: work_slot() bounded-await (warming_await_timeout_s)
         ← if handshake failed: VMNotReadyError.recovery → v297.0 routing matrix
```

**Phase 2 / Phase 7 concurrency:** GCP warming runs in parallel with phases 3-4 because it dominates latency. The `work_slot()` WARMING await handles the case where Phase 7 is reached before Phase 2 completes. This bounded wait (`warming_await_timeout_s`, default 90 s) is the only synchronization point.

**Phase 4 dependency:** `_PHASE_DEPENDENCIES` in `startup_phase_gate.py` is a **module-level constant dict** (not a runtime-mutable registry). Adding the Phase 4 → CORE_READY dependency means modifying the constant at module level by adding `StartupPhase.CORE_READY` as a dependency of the `BOOT_HANDSHAKE` phase (or equivalent). The plan task must verify the exact `StartupPhase` enum value to use — if no `BOOT_HANDSHAKE` phase exists yet, a new enum member must be added. This is a code change to `startup_phase_gate.py`, not a runtime call.

---

## 6. File-by-File Changes

### 6.1 `backend/core/vm_lifecycle_manager.py` (new, ~460 lines)

Contains: `VMFsmState`, `ActivityClass`, `ActivitySource`, `_ACTIVITY_REGISTRY`, `VMLifecycleConfig`, `VMController` Protocol, `LifecycleLease`, `LifecycleTransitionEvent`, `LifecycleTelemetrySink` Protocol, `VMNotReadyError`, `UnregisteredActivitySourceError`, `DualAuthorityError`, `LifecycleFSMError`, `BootMode`, `BootModeRecord`, `VMLifecycleManager`.

No imports from `supervisor_gcp_controller`, `prime_client`, or `prime_router`. Depends only on: `gcp_readiness_lease.py` (for `HandshakeStep`, `ReadinessFailureClass`), `startup_routing_policy.py` (for `select_recovery_strategy`, `RecoveryStrategy`), `startup_telemetry.py` (for `StartupEventBus` interface).

### 6.2 `backend/core/supervisor_gcp_controller.py` (modified, net ~−80 lines)

- **Remove:** `_idle_monitor_loop()`, its task handle, and all calls to `record_vm_activity()` for idle tracking. In `supervisor_gcp_controller.stop()`, remove the corresponding task cancellation line for `_idle_monitor_loop` — it no longer exists. `VMLifecycleManager.stop()` owns all background task teardown. Shutdown order: `VMLifecycleManager.stop()` is called first (cancels `_idle_timer_task`, `_grace_period_task`, `_max_uptime_task`, releases `LifecycleLease`), then `supervisor_gcp_controller.stop()` proceeds with GCP API cleanup.
- **Remove:** direct mutations to `VMLifecycleState` from the controller (it is now projection-only)
- **Add:** `_GCPControllerAdapter` inner class implementing `VMController` protocol
- **Add:** `vm_lifecycle_state` read-only property projecting `VMFsmState → VMLifecycleState`
- **Add:** `set_lifecycle_manager(manager)` wiring method
- **Add:** in supervisor startup sequence: `asyncio.create_task(self._lifecycle.ensure_warmed("boot"))`
- **Keep unchanged:** zone fallback logic, `ensure_static_vm_ready()`, `_terminate_static_vm()`, `ActiveVM` metadata dataclass, `VMLifecycleState` enum definition (needed for projection)

### 6.3 `backend/core/prime_client.py` (modified, +45 lines)

- **Add:** `set_lifecycle_manager(manager: Optional[VMLifecycleManager])` — called once at wiring time
- **Modify:** `_execute_request()` (or equivalent HTTP send path):

  ```python
  if self._lifecycle is not None:
      async with self._lifecycle.work_slot(ActivityClass.MEANINGFUL,
                                           description="prime_client.execute_request"):
          return await self._do_request(...)
  else:
      return await self._do_request(...)
  ```

- **Modify:** streaming response consumption path: wrap with `work_slot(MEANINGFUL, description="prime_client.stream_chunks")`
- **Modify:** any WebSocket session: wrap with `work_slot(MEANINGFUL, description="prime_client.websocket_session")`
- **Keep unchanged:** all routing, retry, endpoint hot-swap logic

### 6.4 `backend/core/prime_router.py` (modified, +10 lines)

- **Replace:** `self._gcp_vm_manager.record_jprime_activity()` with `self._lifecycle.record_activity_from("prime_client.execute_request")` if lifecycle manager is wired, else no-op (backwards compatible)
- **Keep unchanged:** `_transition_in_flight`, routing decision logic, `RoutingDecision` enum

### 6.5 `backend/core/startup_orchestrator.py` (modified, +35 lines)

- **Add:** `set_lifecycle_manager(manager: VMLifecycleManager)`
- **Modify:** `acquire_gcp_lease()`: if lifecycle manager is wired and state is COLD, call `await lifecycle.ensure_warmed(reason="lease_request")` before proceeding with `self._lease.acquire()`
- **Keep unchanged:** all phase gate, FSM, budget, invariant logic

---

## 7. Test Matrix

All tests are hermetic. `VMController` is mocked. No real GCP calls. Time is injectable via `_clock: Callable[[], float]` parameter (default `time.monotonic`). `strict_drain=True` in all tests.

| # | Name | Correction verified |
|---|---|---|
| T1 | `test_ensure_warmed_cold_to_ready` | C2, C1 — single entrypoint, COLD→WARMING→READY |
| T2 | `test_concurrent_ensure_warmed_collapses` | C2 — two concurrent calls → one VM start |
| T3 | `test_meaningful_activity_resets_idle_timer` | C8 — MEANINGFUL resets `_last_meaningful_mono` |
| T4 | `test_non_meaningful_does_not_reset_idle_timer` | C8 — NON_MEANINGFUL leaves timer unchanged |
| T5 | `test_health_probe_1000_calls_no_idle_reset` | C8 — 1000× probe_health → `_last_meaningful_mono` unchanged |
| T6 | `test_health_probe_does_not_block_stopping` | Fix 4 — probe in IDLE_GRACE → STOPPING proceeds |
| T7 | `test_meaningful_drain_blocks_stopping` | Fix 5 — work_slot held → IDLE_GRACE does not transition to STOPPING |
| T8 | `test_drain_event_driven_releases_stopping` | Fix 5 — slot released → `_drain_clear_event` set → STOPPING fires |
| T9 | `test_idle_grace_cancelled_by_new_work` | C3 — IDLE_GRACE + work_slot(MEANINGFUL) → IN_USE, grace cancelled |
| T10 | `test_work_slot_warming_bounded_await_success` | Fix 3, C3 — WARMING → await → READY → slot proceeds |
| T11 | `test_work_slot_warming_timeout_taxonomy_recovery` | Fix 3 — warming timeout → VMNotReadyError.recovery from _RECOVERY_MATRIX |
| T12 | `test_work_slot_cold_taxonomy_recovery` | Fix 3 — COLD + prior failure → VMNotReadyError.recovery from matrix |
| T13 | `test_max_uptime_enters_idle_grace_not_stopping` | C7 — max_uptime elapses → IDLE_GRACE (drain honored) |
| T14 | `test_max_uptime_drain_hard_cap_emits_telemetry` | C7 — hard cap exceeded → event emitted, no force stop |
| T15 | `test_restart_consistency` | C1 — COLD→WARM→READY→STOPPING→COLD→WARM→READY |
| T16 | `test_lifecycle_lease_stale_pid_overwrite` | Fix 2 — stale PID → overwrite; success |
| T17 | `test_lifecycle_lease_live_pid_dual_authority` | Fix 2 — live PID → DualAuthorityError |
| T18 | `test_unregistered_caller_raises` | C8 — unknown caller_id → UnregisteredActivitySourceError |
| T19 | `test_transition_telemetry_off_critical_path` | Fix 1 — telemetry task scheduled after lock release |
| T20 | `test_degraded_boot_mode_on_jprimer_offline` | DEGRADED_BOOT_MODE — MODEL_ROUTER marked DEGRADED, exponential backoff |

---

## 8. Residual Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **GCP-side VM death not reflected in FSM** — preemption/OOM leaves FSM in READY/IN_USE | Medium | Prober health failure → `VMController.notify_vm_unreachable()` → `_transition(COLD)`. Next `work_slot()` raise routes through fallback. |
| **`_drain_clear_event` spurious set** — event set at READY (count → 0) before grace starts | Low | Grace task only awaits event after grace elapsed AND drain check fails. Spurious sets are harmless — event is re-cleared when next MEANINGFUL slot enters. |
| **v297.0 plan not yet executed** — `HandshakeSession`, `select_recovery_strategy()` don't exist yet | Medium | Implementation sequencing: v297.0 tasks must complete before v298.0 tasks. Plan will enforce this as an explicit prerequisite. |
| **Import of `select_recovery_strategy` from `startup_routing_policy`** — if v297.0 adds it at a different symbol name | Low | Plan task will verify exact symbol name after v297.0 completion before coding. |
| **Quiet hours threshold factor = 0.25 on very short `inactivity_threshold_s`** — could cause near-instant shutdown during off-hours for test configs | Low | `_effective_threshold_s()` clamps minimum at 60 s regardless of factor multiplication. |
| **max_uptime drain hard cap** — currently emits telemetry but does not force stop; very long-running streams could hold VM indefinitely | Medium | Documented explicitly. Operator can reduce `max_uptime_s` and `drain_hard_cap_s`. A future spec can add forced stop with an admin kill switch — out of scope here. |
| **LifecycleLease atexit vs asyncio shutdown ordering** — atexit runs after the event loop is closed; lease release is synchronous (flock only), so this is safe | Low | `release()` is synchronous, no event loop dependency. |

---

## 9. Open Questions (Resolved)

| Question | Resolution |
|---|---|
| Does `max_uptime` override drain safety? | No. `max_uptime` → IDLE_GRACE only. Drain is always honored. Hard cap emits telemetry but never force-stops. |
| Do health probes in STOPPING block shutdown? | No. STOPPING rejects all `work_slot()` calls (Fix 4). |
| Is quiet hours "shut down faster" or "stay up longer"? | Shut down faster. Factor < 1.0 reduces threshold during off-hours. |
| Where does `BootModeRecord` live? | On `StartupOrchestrator`. Accessible via `orchestrator.boot_mode_record`. |
| Does the warm strike counter reset on success? | Yes. `_warm_strike_count` resets to 0 on any successful `ensure_warmed()`. |

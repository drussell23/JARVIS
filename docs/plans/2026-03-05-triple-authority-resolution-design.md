# Triple Authority Resolution Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the Triple Authority Problem (3 supervisors, 0 clarity) by establishing `unified_supervisor.py` as the single Root Authority with explicit contracts, active crash detection, and managed-mode demotion of sub-supervisors in Prime and Reactor.

**Architecture:** Thin `root_authority.py` module defines policy + state machine + verdicts. `ProcessOrchestrator` (existing) becomes pure executor. Prime/Reactor get minimal managed-mode conformance. Staged rollout: shadow -> per-subsystem -> full -> deprecate old paths.

**Scope:** All 3 repos (JARVIS-AI-Agent, jarvis-prime, reactor-core), executed in C-shaped sequencing: JARVIS first, then Prime/Reactor conformance.

---

## Context: The Disease

Each repo has its own supervisor that thinks it's in charge:

| Repo | Supervisor | Claims |
|------|-----------|--------|
| JARVIS-AI-Agent | `unified_supervisor.py` | "I'm the kernel of the AI OS" |
| jarvis-prime | `run_supervisor.py` -> `UnifiedSupervisor` | "I manage Prime lifecycle" |
| reactor-core | `run_supervisor.py` -> `AGISupervisor` | "I manage Reactor lifecycle" |

Additionally in JARVIS-AI-Agent: `cross_repo_startup_orchestrator.py` (25K lines) spawns Prime/Reactor as subprocesses, and `start_system.py` (22K lines) is another startup path.

The conflict: when JARVIS's orchestrator restarts Prime, and Prime's own supervisor also tries to restart itself, dual-authority restart storms occur. Active diseases include restart conflicts, readiness split-brain, cross-repo contract drift, and async timeout cancellation side effects.

## Active Diseases (Driving Priority)

1. **Triple authority restart storms** -- competing lifecycle logic in code paths
2. **Heartbeat/readiness split-brain** -- APARS/readiness mismatch (97% stuck)
3. **Cross-repo contract fragility** -- schema/health/capability drift
4. **Async safety/timeout cancellation** -- timeouts and partial-phase failures
5. **Process-group orphan VRAM leaks** -- crash cleanup gaps under load
6. **Health false negatives under load** -- control/data path contention
7. **PID identity ambiguity** -- fast restart/reuse scenarios

---

## Section 1: Managed-Mode Contract

### Environment Variables

| Variable | Type | Set By | Read By | Purpose |
|----------|------|--------|---------|---------|
| `JARVIS_ROOT_MANAGED` | bool | Root (USP) | Prime, Reactor | "You are not your own supervisor" |
| `JARVIS_ROOT_SESSION_ID` | uuid4 | Root (USP) | Prime, Reactor | Unique boot session -- prevents stale identity |
| `JARVIS_CONTROL_PLANE_URL` | url | Root (USP) | Prime, Reactor | Callback/report channel for lifecycle events |
| `JARVIS_CONTROL_PLANE_SECRET` | hex(32) | Root (USP) | Prime, Reactor | Per-boot random HMAC secret (NOT derivable from session ID) |
| `JARVIS_SUBSYSTEM_ROLE` | str | Root (USP) | Prime, Reactor | `"jarvis-prime"` or `"reactor-core"` -- canonical identity |

### Process Identity Fingerprint (4-tuple)

```
ProcessIdentity = (
    pid: int,              # OS process ID
    start_time_ns: int,    # monotonic clock at process boot (never resets on hot reload)
    session_id: str,       # JARVIS_ROOT_SESSION_ID (scopes to boot cycle)
    exec_fingerprint: str  # sha256 of binary path + cmdline (detects binary swap)
)
```

Watcher validates all four on every health response. Mismatch on any = impostor.

### Health Response Schema (Two-Field Status Model)

```json
{
  "liveness": "up|down",
  "readiness": "ready|not_ready|degraded|draining",
  "session_id": "<echo of JARVIS_ROOT_SESSION_ID>",
  "pid": 12345,
  "start_time_ns": 1709654400000000000,
  "exec_fingerprint": "sha256:abc123...",
  "subsystem_role": "jarvis-prime",
  "schema_version": "1.0.0",
  "capability_hash": "sha256:def456...",
  "uptime_s": 47.2,
  "phase": "model_loading",
  "progress_pct": 72,
  "observed_at_ns": 1709654447200000000,
  "wall_time_utc": "2026-03-05T14:30:47.200Z",
  "drain_id": null
}
```

Status semantics:
- **`liveness`**: binary -- process running (`up`) or not (`down`). Crash detection signal.
- **`readiness`**: traffic-routing signal.
  - `ready` = accepting requests
  - `not_ready` = alive but can't serve (loading)
  - `degraded` = serving with impairment
  - `draining` = finishing in-flight, rejecting new

Timestamp semantics:
- **`observed_at_ns`**: monotonic clock -- used for ALL duration/timeout math in supervision logic
- **`wall_time_utc`**: wall clock -- used for audit logs ONLY, never for timeout/SLA logic

Schema compatibility:
- **`schema_version`**: N/N-1 enforced. Root rejects N-2 or older.
- **`capability_hash`**: digest of declared capabilities. Root rejects mismatch at handshake.

### Exit Code Contract

| Range | Meaning | Root Recovery Policy |
|-------|---------|---------------------|
| `0` | Clean shutdown | No restart (expected) |
| `100-109` | Config/contract error | Do NOT restart -- operator must fix |
| `200-209` | Dependency failure | Retry with backoff (dependency may recover) |
| `300-309` | Runtime fatal | Restart with escalating backoff |
| Other non-zero | Unknown crash | Restart once, then escalate to operator alert |

Signal-aware classification:
- `SIGSEGV`, `SIGBUS`, `SIGABRT` -> `crash_signal`, always restart
- `SIGTERM`, `SIGKILL` from root -> `managed_kill`, no auto-restart (root did it)
- Distinguished via: did watcher issue a kill verdict before exit? If yes -> `managed_kill`.

### Endpoint Contract (when `JARVIS_ROOT_MANAGED=true`)

| Endpoint | Method | Required | Session-Gated | Purpose |
|----------|--------|----------|---------------|---------|
| `/health` | GET | YES | Response includes `session_id` | Liveness + readiness |
| `/health/ready` | GET | SHOULD | Response includes `session_id` | Readiness-only (load balancers) |
| `/lifecycle/drain` | POST | YES | Request + response must match `session_id` + HMAC | Graceful stop |

Drain semantics:
- Mismatched `session_id` -> `409 Conflict` (no-op)
- HMAC auth required: `X-Root-Auth: HMAC-SHA256(session_id + timestamp + nonce, JARVIS_CONTROL_PLANE_SECRET)`
- Returns `202 Accepted {"drain_id": "<uuid>", "session_id": "..."}`
- Repeated calls are idempotent (return existing `drain_id`)
- Health includes `drain_id` while `readiness=draining`
- Drain result envelope before TERM escalation: `completed | timed_out | flush_failed`

### Socket Contract

- `SO_REUSEADDR`: REQUIRED on all listening sockets
- `SO_REUSEPORT`: OPTIONAL, platform-guarded (`hasattr(socket, 'SO_REUSEPORT')`)

### Managed-Mode Behavioral Contract

When `JARVIS_ROOT_MANAGED=true`:
- **MUST NOT** self-restart the process on internal failure
- **MAY** self-heal within process boundaries (restart internal workers, reconnect pools, reload config)
- **MUST** emit structured log `{"event": "fatal", "exit_code": <N>, "reason": "..."}` before exit
- **MUST** exit with contracted exit code via controlled shutdown path (not `sys.exit()` in async context)
- **MUST** echo `session_id` in all health/lifecycle responses

When `JARVIS_ROOT_MANAGED` is unset or `false`: subsystems behave as today (self-supervising). Zero behavior change.

---

## Section 2: Root Authority Watcher

Lives in `backend/core/root_authority.py`. Owns the lifecycle state machine for each managed subsystem. Emits verdicts -- does NOT execute them.

### Per-Subsystem State Machine

```
                    +------------+
          spawn     |  STARTING  |
       +----------->|            |
       |            +-----+------+
       |                  | liveness=up
       |                  v
       |            +------------+
       |            | HANDSHAKE  |----> pass ------>+------------+
       |            +-----+------+                  |   ALIVE    |
       |                  |                         | (not ready)|
       |                  | fail                    +-----+------+
       |                  v                               |
       |            +------------+                        | readiness=ready
       |            |  REJECTED  | (terminal)             v
       |            +------------+                  +------------+
       |                                            |   READY    |
       |                                            +-----+------+
       |                                                  |
       |              readiness=degraded (from any alive)  |
       |                                                  v
       |                                            +------------+
       |                                            |  DEGRADED  |
       |                                            +-----+------+
       |                                                  |
       |                          degraded SLO expired or drain issued
       |                                                  v
       |                                            +------------+
       |                                            |  DRAINING  |
       |                                            +-----+------+
       |                                                  |
       |                              process exits or drain timeout
       |                                                  v
       |                                            +------------+
       |                                            |  STOPPED   |
       |                                            +-----+------+
       |                                                  |
       |                                         restart verdict
       +--------------------------------------------------+

  Any state --- process.wait() fires ---> CRASHED
  CRASHED --- restart verdict ---> STARTING
  CRASHED --- no-restart verdict ---> escalate_operator (terminal)
```

Terminal states: `STOPPED` (graceful), `CRASHED` (abnormal), `REJECTED` (contract mismatch).

Key rules:
- Transitions driven by observed events (health responses, `process.wait()`), not timers alone
- `DEGRADED` does NOT auto-trigger restart -- monitored for `degraded_tolerance_s` (default 60s)
- If subsystem self-heals within window -> return to ALIVE/READY
- If still degraded after window -> emit drain verdict
- `DRAINING` has hard deadline -- drain timeout -> SIGTERM -> process-group SIGKILL

### Dual Detection Model

**Active detection** (instant): `await proc.wait()` fires within milliseconds of crash.

**Passive detection** (periodic, jittered): health polling catches deadlocks, infinite loops, event-loop starvation that keep the process alive but non-functional.

Graduated response for health failures:
- 1 consecutive miss: log warning
- 2 misses: mark DEGRADED
- 3 misses: issue drain verdict
- 5 misses (or drain timeout): issue kill verdict

Health poll jitter: `actual_interval = base_interval * (1 + random.uniform(-0.2, 0.2))`

Both detectors run concurrently. Both can fire for the same incident.

### Verdict System

Strongly typed verdicts emitted to a bounded channel:

```python
class LifecycleAction(Enum):
    DRAIN = "drain"
    TERM = "term"
    GROUP_KILL = "group_kill"
    RESTART = "restart"
    ESCALATE_OPERATOR = "escalate_operator"

@dataclass(frozen=True)
class LifecycleVerdict:
    subsystem: str
    identity: ProcessIdentity
    action: LifecycleAction
    reason: str                       # human-readable
    reason_code: str                  # machine: "health_timeout", "crash_exit_300", etc.
    correlation_id: str               # groups all events for one incident
    incident_id: str                  # sha256(subsystem + identity + reason_code + time_bucket_60s)
    exit_code: Optional[int]
    observed_at_ns: int               # monotonic
    wall_time_utc: str                # audit only
```

**Verdict deduplication**: `incident_id` deduplicates within 60s time buckets. Second verdict for same incident is coalesced.

**Verdict channel**: `asyncio.Queue(maxsize=64)`. Overflow: coalesce same-incident, drop superseded lower-severity. Counters: `verdicts_dropped_total`, `verdicts_coalesced_total` (never silent overflow).

**Race-safe gating**: before executing any verdict, executor re-reads current `ProcessIdentity` from watcher. Stale verdicts (identity changed) are discarded.

### Kill Escalation Policy

```
1. POST /lifecycle/drain (bounded by drain_timeout_s, default 30s)
   -> drain result: completed | timed_out | flush_failed
   |
   v timeout or failure
2. SIGTERM to PID (bounded by term_timeout_s, default 10s)
   |
   v timeout
3. SIGKILL to -PGID (entire process group, immediate)
   |
   v always
4. Verify all child PIDs in group are dead
   |
   v confirmed
5. Restart (or escalate_operator if max_restarts exceeded)
```

Each step emits a verdict. Executor reports back with acknowledgment envelope.

### Restart Policy

```python
@dataclass
class RestartPolicy:
    max_restarts: int = 3             # per sliding window
    window_s: float = 300.0           # 5-minute window
    base_delay_s: float = 2.0         # initial backoff
    max_delay_s: float = 60.0         # cap
    jitter_factor: float = 0.3        # +/-30% randomization

    # Exit-code-aware
    no_restart_exit_codes: tuple = (0, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109)
    retry_exit_codes: tuple = (200, 201, 202, 203, 204, 205, 206, 207, 208, 209)
```

Exceeding `max_restarts` within `window_s` -> `escalate_operator` verdict, stop retrying.

### Timeout Classes

```python
@dataclass
class TimeoutPolicy:
    startup_grace_s: float = 120.0    # before first health failure counts
    health_timeout_s: float = 5.0     # per-request GET /health timeout
    health_poll_interval_s: float = 5.0  # base interval (jittered +/-20%)
    drain_timeout_s: float = 30.0     # max time for graceful drain
    term_timeout_s: float = 10.0      # max time for SIGTERM before SIGKILL
    degraded_tolerance_s: float = 60.0  # stay degraded before restart verdict
    degraded_recovery_check_s: float = 10.0  # re-check interval during degraded window
```

All timeout/SLA logic uses monotonic clock only. Wall time is audit-only.

---

## Section 3: ProcessOrchestrator Integration

### Role Split (enforced by design)

| Concern | Owner | NOT Owner |
|---------|-------|-----------|
| "Should we restart?" | RootAuthorityWatcher | ProcessOrchestrator |
| "How do we restart?" | ProcessOrchestrator | RootAuthorityWatcher |
| "Is this process healthy?" | RootAuthorityWatcher | ProcessOrchestrator |
| "Spawn the process" | ProcessOrchestrator | RootAuthorityWatcher |
| "Kill the process group" | ProcessOrchestrator | RootAuthorityWatcher |
| "Wire USP startup sequence" | `unified_supervisor.py` | Either module |

Watcher has zero imports from orchestrator or USP. Receives process handles, emits verdicts.

### VerdictExecutor Protocol

```python
class VerdictExecutor(Protocol):
    async def execute_drain(self, subsystem: str, identity: ProcessIdentity,
                            drain_timeout_s: float) -> ExecutionResult: ...
    async def execute_term(self, subsystem: str, identity: ProcessIdentity,
                           term_timeout_s: float) -> ExecutionResult: ...
    async def execute_group_kill(self, subsystem: str, identity: ProcessIdentity) -> ExecutionResult: ...
    async def execute_restart(self, subsystem: str, delay_s: float) -> ExecutionResult: ...
    def get_current_identity(self, subsystem: str) -> Optional[ProcessIdentity]: ...
```

### Execution Acknowledgment Envelope

```python
@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    executed: bool
    result: str            # "success", "timeout", "stale_identity", "error"
    new_identity: Optional[ProcessIdentity]  # if restart
    error_code: Optional[str]
    correlation_id: str
```

Prevents watcher/executor state divergence.

### Migration from ProcessOrchestrator

**Remove** (moves to watcher):
- `_health_monitor_loop()` -- health polling
- `_should_restart()` / restart decision logic
- `calculate_backoff()` -- backoff computation
- `CircuitBreaker` trip/reset decisions (watcher owns trip policy)
- `max_restarts` tracking

**Add** (executor role):
- Implement `VerdictExecutor` protocol
- `_build_auth_header()` for HMAC-signed drain
- `_identity_matches()` for race-safe gating
- `_verify_group_dead()` for process-group kill confirmation
- Return `ProcessIdentity` after spawn

**Keep unchanged**:
- `_spawn_service()` / `_spawn_service_core()` -- spawn mechanics
- Port management, env var assembly, output streaming
- `ServiceDefinitionRegistry`
- `start_new_session=True` isolation

### Backward Compatibility

```python
# In unified_supervisor.py
if self.config.use_root_authority:
    self.watcher = RootAuthorityWatcher(...)
    self.orchestrator.set_verdict_executor_mode(True)  # disables internal policy
else:
    self.watcher = None
    # orchestrator uses existing health/restart logic (fallback)
```

Fallback mode logs with `policy_source=orchestrator`. Root-managed mode logs with `policy_source=root_authority`. Never ambiguous.

**Deprecation deadline**: orchestrator-internal policy mode removed 4 weeks after full activation. No permanent dual-behavior.

### Circuit Breaker Split

- Watcher owns: trip policy (when to open/close based on failure patterns)
- Orchestrator exposes: execution gate primitive (`is_open() -> bool`, `force_open()`, `force_close()`)
- Watcher calls `force_open()`/`force_close()` via the VerdictExecutor interface

---

## Section 4: Prime & Reactor Managed-Mode Conformance

### Principle: Minimal Conformance, Not Rewrite

Changes per repo:

| File | Change | Approx Size |
|------|--------|-------------|
| `managed_mode.py` (new) | Shared contract utilities | ~120 lines |
| `run_supervisor.py` | Disable self-restart when managed | ~20 lines changed |
| `run_server.py` (Prime) / `api/server.py` (Reactor) | Enrich `/health`, add `/lifecycle/drain` | ~80 lines added |

### Self-Restart Demotion

**Prime** (`run_supervisor.py`):
```python
_ROOT_MANAGED = os.environ.get("JARVIS_ROOT_MANAGED", "").lower() == "true"

# In ComponentConfig:
auto_restart: bool = not _ROOT_MANAGED

# In health monitor loop -- when unhealthy and managed:
# Do NOT self-restart. Set shutdown event, exit with contracted code.
_shutdown_exit_code = 300  # runtime fatal
_shutdown_event.set()
```

**Reactor** (`run_supervisor.py`):
```python
def should_restart(self, component_name: str) -> Tuple[bool, float]:
    if _ROOT_MANAGED:
        return (False, 0.0)  # defer to root authority
    # ... existing logic ...
```

Internal self-healing (worker restart, pool reconnection) is untouched -- only process-level restart is disabled.

### Health Enrichment

Existing health response fields preserved. New managed-mode fields are additive:
```json
{
  "liveness": "up",
  "readiness": "ready",
  "session_id": "...",
  "pid": 12345,
  "start_time_ns": 1709654400000000000,
  "exec_fingerprint": "sha256:abc123...",
  "subsystem_role": "jarvis-prime",
  "schema_version": "1.0.0",
  "capability_hash": "sha256:def456...",
  "observed_at_ns": 1709654447200000000,
  "wall_time_utc": "2026-03-05T14:30:47.200Z"
}
```

Only enriched when `JARVIS_ROOT_SESSION_ID` is set. Standalone mode returns current response unchanged.

### Drain Endpoint

- `POST /lifecycle/drain` -> `202 Accepted {"drain_id": "<uuid>", "session_id": "..."}`
- Session gating: mismatched `session_id` -> `409 Conflict`
- HMAC auth: `X-Root-Auth` header required
- Idempotent: repeated calls return existing `drain_id`
- Controlled shutdown: sets event flag, main loop exits cleanly (no `sys.exit()` in async handler)
- Returns drain result (`completed | timed_out | flush_failed`) before TERM escalation

### `managed_mode.py` (Duplicated in Both Repos)

Shared contract utilities:
- `SCHEMA_VERSION = "1.0.0"`
- Root-managed flag, session ID, role, secret from env
- Exit code constants (`EXIT_CLEAN=0`, `EXIT_CONFIG_ERROR=100`, `EXIT_DEPENDENCY_FAILURE=200`, `EXIT_RUNTIME_FATAL=300`)
- `compute_exec_fingerprint()`, `compute_capability_hash()`, `verify_hmac_auth()`, `build_health_envelope()`

**Anti-drift safeguards**:
- Contract version lock: `SCHEMA_VERSION` must match root authority version
- Golden contract tests in all 3 repos (same fixtures, same expected fields)
- Boot-time compatibility gate: root refuses managed mode on version/hash mismatch
- CI drift check: compare normalized constants across repos (fail on divergence)

**Migration path**: duplicated now (avoids cross-repo runtime dependency during active-fire phase). Migrate to shared contract package once protocol stabilizes.

### What Does NOT Change

- Model loading, inference, routing -- untouched
- Internal worker management, pool reconnection -- untouched
- Existing `/health` response fields -- preserved
- Standalone mode (`JARVIS_ROOT_MANAGED` unset) -- 100% current behavior
- Test suites -- no regression, only additive tests

---

## Section 5: Contract Hash & Readiness Gating

### Boot-Time Handshake

After spawn, when liveness=up, watcher performs one-time handshake before ALIVE:

```
STARTING -> liveness=up -> HANDSHAKE -> pass -> ALIVE
                                     -> fail -> REJECTED (terminal)
```

Checks:
1. `schema_version` -- N/N-1 compatible
2. `capability_hash` -- matches last-known-good or is in allowed-migrations list
3. `session_id` -- echoed correctly
4. Required fields present in health response

### Schema Version Compatibility

N/N-1 accepted. N-2 or older rejected. Major version mismatch always rejected.

### Capability Hash Management

- Root stores last-known-good hash per subsystem
- Hash mismatch: check allowed-migrations list
- Allowed-migrations entries have expiration timestamps (prevent permanent drift)
- First boot (no baseline): accept and store
- Approval: configuration-driven (root config file), not code-driven

### Emergency Bypass

`JARVIS_CONTRACT_BYPASS=<subsystem>` -- time-limited (max 1h, enforced by watcher), loudly logged every 30s:
```
WARNING: Contract bypass active for jarvis-prime (expires in 42m). Handshake validation SKIPPED.
```

Prevents ops lockout during urgent incidents. Cannot be set silently.

### Contract Conformance Tests

Identical test fixtures across all 3 repos validating:
- Required health fields present
- Valid liveness/readiness values
- Schema version matches
- Exit code ranges correct
- Drain endpoint returns correct schema

CI compares test file hashes across repos to detect drift.

---

## Section 6: Observability Contract

### Lifecycle Event Schema

```python
@dataclass(frozen=True)
class LifecycleEvent:
    event_type: str          # "spawn", "health_check", "verdict_emitted",
                             # "verdict_executed", "state_transition", "handshake"
    subsystem: str
    correlation_id: str      # groups all events for one lifecycle incident
    session_id: str
    identity: Optional[ProcessIdentity]
    from_state: Optional[str]
    to_state: Optional[str]
    verdict_action: Optional[str]
    reason_code: Optional[str]
    exit_code: Optional[int]
    observed_at_ns: int      # monotonic
    wall_time_utc: str       # audit
    policy_source: str       # "root_authority" or "orchestrator"
```

### Policy Source Namespacing

- Root-managed mode: `policy_source = "root_authority"`
- Fallback mode: `policy_source = "orchestrator"`
- Never ambiguous during incident analysis or migration period

### Correlation ID Flow

One `correlation_id` traces entire incident from detection through resolution:
```
health_check(timeout) -> state_transition(READY->DEGRADED) -> verdict(DRAIN)
-> verdict_executed(DRAIN, timed_out) -> verdict(TERM) -> verdict_executed(TERM, timeout)
-> verdict(GROUP_KILL) -> verdict_executed(GROUP_KILL, success) -> verdict(RESTART) -> spawn
-> handshake(pass) -> state_transition(STARTING->ALIVE)
```

### Queue Overflow Telemetry

Counters (never silent):
- `verdicts_dropped_total` -- verdicts dropped due to queue overflow
- `verdicts_coalesced_total` -- verdicts merged with existing same-incident

### Event Sink

Pluggable `LifecycleEventSink` protocol. Default: structured JSON logger. Future: event bus, metrics pipeline, external alerting.

---

## Section 7: Migration & Rollout Strategy

### Phase 1: Shadow Mode (Week 1)

- Deploy `root_authority.py` with watcher
- Watcher monitors and emits verdicts to log ONLY
- Verdicts NOT executed -- orchestrator keeps its own policy
- Compare watcher verdicts vs orchestrator decisions
- Kill switch: `JARVIS_ROOT_AUTHORITY_MODE=shadow`

### Phase 2: Per-Subsystem Activation (Week 2-3)

- Activate for Reactor first (lower blast radius)
- `JARVIS_ROOT_AUTHORITY_MODE=active`
- `JARVIS_ROOT_AUTHORITY_SUBSYSTEMS=reactor-core`
- Orchestrator defers to watcher for reactor-core only
- Prime still uses orchestrator's internal policy

### Phase 3: Full Activation (Week 3-4)

- Activate for Prime
- `JARVIS_ROOT_AUTHORITY_SUBSYSTEMS=reactor-core,jarvis-prime`
- Both subsystems fully root-managed

### Phase 4: Deprecation (Week 6+)

- Remove orchestrator-internal health monitoring, restart logic, backoff
- Remove `JARVIS_ROOT_AUTHORITY_MODE` toggle (always active)
- Orchestrator becomes pure executor
- Deadline: 4 weeks after full activation

### Migration Sequencing in Code

1. Introduce protocol + adapter shims
2. Run watcher in shadow mode (observe-only)
3. Flip execute mode per-subsystem with kill switch
4. Remove old policy paths once stable

### Rollout Gates

| Gate | Requirement | Phase |
|------|------------|-------|
| G1 | Watcher detects hard crash via `process.wait()` in <1s | Shadow |
| G2 | Watcher shadow verdicts >= 99% parity with orchestrator decisions for 48h (mismatches classified) | Shadow -> Active |
| G3 | Drain completes with no state corruption in 10 forced-failure tests | Active (Reactor) |
| G4 | Process-group kill leaves zero orphaned children in 10 tests | Active (Reactor) |
| G5 | Prime managed mode runs 48h with zero restart storms | Active (Prime) |
| G6 | Contract mismatch blocks READY deterministically in 5 tests | Active (Prime) |
| G7 | Orchestrator-internal policy removal causes zero behavior change | Deprecation |

---

## File Map

### JARVIS-AI-Agent (Root Authority)

| File | Action | Purpose |
|------|--------|---------|
| `backend/core/root_authority.py` | CREATE | Watcher, state machine, verdicts, contracts |
| `backend/core/root_authority_types.py` | CREATE | Shared types (ProcessIdentity, LifecycleAction, etc.) |
| `backend/supervisor/cross_repo_startup_orchestrator.py` | MODIFY | Implement VerdictExecutor, remove internal policy |
| `unified_supervisor.py` | MODIFY | Wire watcher + orchestrator, boot sequence |
| `tests/unit/core/test_root_authority.py` | CREATE | Watcher unit tests |
| `tests/unit/core/test_verdict_executor.py` | CREATE | Executor protocol tests |
| `tests/unit/core/test_managed_mode_contract.py` | CREATE | Contract conformance tests |

### jarvis-prime

| File | Action | Purpose |
|------|--------|---------|
| `managed_mode.py` | CREATE | Contract utilities (duplicated) |
| `run_supervisor.py` | MODIFY | Disable self-restart when managed |
| `run_server.py` | MODIFY | Enrich `/health`, add `/lifecycle/drain` |
| `tests/test_managed_mode_contract.py` | CREATE | Contract conformance tests |

### reactor-core

| File | Action | Purpose |
|------|--------|---------|
| `managed_mode.py` | CREATE | Contract utilities (duplicated) |
| `run_supervisor.py` | MODIFY | Disable self-restart when managed |
| `reactor_core/api/server.py` | MODIFY | Enrich `/health`, add `/lifecycle/drain` |
| `tests/test_managed_mode_contract.py` | CREATE | Contract conformance tests |

---

## Implementation Order (Dependency-Ordered)

1. Root contract types + managed-mode utilities (`root_authority_types.py`, `managed_mode.py`)
2. Root watcher state machine + verdict system (`root_authority.py`)
3. Active `process.wait()` crash detection in watcher
4. Passive health polling with jitter in watcher
5. Kill escalation policy (drain -> term -> group_kill)
6. VerdictExecutor protocol + ProcessOrchestrator adapter
7. Surgical policy removal from ProcessOrchestrator
8. USP wiring (shadow mode first)
9. Prime managed-mode conformance (self-restart demotion, health enrichment, drain)
10. Reactor managed-mode conformance (same pattern)
11. Contract hash gating + boot-time handshake
12. Observability event stream
13. Contract conformance tests (all 3 repos)
14. Shadow-mode validation
15. Per-subsystem activation + gate testing

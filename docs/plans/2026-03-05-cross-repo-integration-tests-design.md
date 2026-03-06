# Disease 9: Cross-Repo Integration Test Harness — Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an authoritative cross-repo failure/recovery harness that proves system-level behavior under real failure conditions — not just unit-level correctness.

**Architecture:** Mock/real dual-mode harness with StateOracle abstraction, scoped fault injection, continuous invariant checking, and causality-chain assertions. Wraps (not replaces) existing FaultInjector infrastructure.

**Tech Stack:** Python 3.11+, pytest, asyncio, existing `FaultInjector` from `tests/adversarial/`, `OrchestrationJournal`, `LifecycleEngine`, `PrimeRouter`.

---

## Diagnosis

The project has 72+ design documents, a `FaultInjector`, adversarial tests, and integration fixtures — but no authoritative harness that proves:

- When Prime goes down, does JARVIS correctly fall back? When it comes back, does state restore?
- When a contract becomes incompatible mid-session, does the system degrade gracefully?
- When shutdown races with recovery, which wins deterministically?

Individual unit tests exist. What's missing is **system-level failure/recovery verification with causal ordering guarantees**.

---

## Section 1: Harness Architecture

### 1.1 Core Components

```
HarnessOrchestrator
  |
  +-- StateOracle (normalized read interface)
  |     +-- MockStateOracle (in-memory, CI)
  |     +-- LiveStateOracle (polls real processes, staging)
  |
  +-- ScopedFaultInjector (wraps existing FaultInjector)
  |     +-- FaultHandle (scope + affected/unaffected sets)
  |     +-- FaultComposition policy (REJECT | STACK | REPLACE)
  |
  +-- InvariantRegistry (continuous + periodic checks)
  |
  +-- ScenarioRunner
  |     +-- ScenarioDefinition (setup -> inject -> verify -> recover)
  |     +-- PhaseResult + ScenarioResult
  |
  +-- ComponentProcess (ABC)
        +-- MockComponentProcess (in-memory state transitions)
        +-- RealComponentProcess (actual subprocess management)
```

### 1.2 Dual-Mode Execution

| Aspect | Mock Mode | Real Mode |
|---|---|---|
| Components | In-memory state machines | Actual processes via `asyncio.create_subprocess_exec` |
| Oracle | `MockStateOracle` (synchronous, exact) | `LiveStateOracle` (async polling, eventually consistent) |
| Fault injection | Direct state mutation | Process signals, port blocking, config injection |
| CI target | PR gate (<60s) | Nightly/staging (~10 min) |
| Determinism | Full (single-threaded updates) | Bounded (staleness contracts) |

### 1.3 Mandatory Guardrails

1. **Deterministic scheduler contract**: Mock-mode uses a deterministic event scheduler. No raw `asyncio.sleep()` in scenarios — only `oracle.wait_until(predicate, deadline)`.
2. **StateOracle abstraction**: Single normalized read path for all assertions. Scenarios never read component state directly.
3. **Event provenance model**: Every state change produces an `ObservedEvent` with source, epoch, trace IDs, and scenario phase.
4. **Fault scope boundaries**: Every fault declares explicit blast radius (affected + unaffected component sets). Harness enforces isolation automatically.
5. **Isolation semantics**: Mock-mode components share no mutable state. Real-mode processes run in isolated network namespace or fallback isolation profile.
6. **Real-mode process governance**: All processes killed in `finally` block. Stdout/stderr captured for forensics. Resource limits enforced.
7. **Mode parity checks**: Nightly CI compares mock vs real event-type orderings. Warning at >15% divergence, hard fail at >30%.
8. **Invariant registry**: Pluggable invariant checks evaluated on state-change events AND periodic watchdog cadence.

---

## Section 2: Scenario Catalog (10 MVP Scenarios)

All scenarios follow the four-phase structure: **setup -> inject -> verify -> recover**.

All assertions use `oracle.wait_until(predicate, deadline)` — no raw sleeps. Each scenario declares a per-phase SLO deadline.

### S1: Prime Crash -> Fallback to Claude

**Inject:** Kill Prime process (SIGKILL).
**Verify:** Router converges to `CLAUDE` within 10s. No request returns 5xx during transition. Causality: fault event precedes routing change in `oracle_event_seq`.
**Recover:** Restart Prime. Router converges back to `PRIME_API` within 30s. Epoch increments.
**SLO:** Fallback <10s, recovery <30s.

### S2: Prime Latency Spike -> Circuit Breaker Opens

**Inject:** Inject 5s latency on Prime health endpoint.
**Verify:** Circuit breaker opens after configured threshold. Router falls back. Verify hysteresis: breaker does NOT close on first healthy response — requires sustained health window (configurable, default 3 consecutive checks).
**Recover:** Remove latency. Breaker closes after sustained health window. Router returns to Prime.
**SLO:** Breaker open <15s, recovery <45s.

### S3: Contract Version Mismatch -> Graceful Degradation

**Inject:** Inject incompatible contract version on Prime handshake response.
**Verify:** System degrades to fallback. Contract status shows `INCOMPATIBLE` with `reason_code=VERSION_WINDOW`. No crash, no silent failure.
**Recover:** Restore compatible contract. System re-promotes Prime after successful re-handshake.
**SLO:** Degradation <10s, re-promotion <30s.

### S4: Network Partition (Asymmetric) -> Split-Brain Prevention

**Inject:** Asymmetric partition — Prime can reach JARVIS but JARVIS cannot reach Prime. Health endpoint times out but Prime continues processing.
**Verify:** JARVIS treats Prime as LOST (not FAILED — partition is ambiguous). No split-brain: exactly one routing target active at all times (`single_routing_target` invariant). Verify partition detector distinguishes "unreachable" from "confirmed dead."
**Recover:** Heal partition. State reconciliation completes. Journal epoch consistent.
**SLO:** Detection <15s, reconciliation <30s.

### S5: Cascading Failure -> Hard Dep Propagation

**Inject:** Fail a component with hard dependents.
**Verify:** Hard dependents transition to FAILED. Soft dependents transition to DEGRADED. Unrelated components unaffected (fault isolation invariant).
**Recover:** Restart failed root. Dependents recover in wave order.
**SLO:** Cascade complete <10s, recovery <60s.

### S6: Shutdown During Active Recovery

**Inject:** Start Prime recovery (restart in progress), then inject SHUTDOWN before recovery completes.
**Verify:** Shutdown wins deterministically. Recovery attempt cancelled (not orphaned). All components reach STOPPED/FAILED. Assert: no component stuck in STARTING after shutdown completes. Verify drain phase executes even under race.
**Recover:** N/A (terminal state). Verify clean process tree.
**SLO:** Shutdown complete <30s.

### S7: Epoch Stale During Transition

**Inject:** Trigger lifecycle transition, then inject a stale-epoch journal write (epoch N-1 during epoch N).
**Verify:** `StaleEpochError` raised. Stale write rejected. Current-epoch state unaffected. Journal monotonicity invariant holds. Assert: stale write doesn't partially mutate any observable state.
**Recover:** System continues normally after rejected write.
**SLO:** Rejection <1s.

### S8: Rapid Failure/Recovery Oscillation (Flapping)

**Inject:** Kill/restart Prime 5 times in 30 seconds.
**Verify:** System does NOT oscillate routing on every cycle. Flap damping engages after configured threshold (default: 3 transitions in 60s). After damping, system holds fallback until stability window expires. Assert: request error rate during oscillation <5%. Verify exponential backoff on restart attempts.
**Recover:** Stop oscillation. System converges to stable state within damping window.
**SLO:** Damping engages <15s, convergence <60s after last flap.

### S13: Stale Healthy After Failover

**Inject:** Prime goes down, JARVIS fails over to Claude. Prime comes back but with stale state (old epoch, old journal revision).
**Verify:** System detects stale-healthy Prime via epoch/revision fence. Does NOT re-promote stale Prime. Emits `stale_healthy_detected` event. Requires Prime to complete fresh handshake + epoch sync before re-promotion.
**Recover:** Prime completes fresh handshake with current epoch. Re-promotion proceeds.
**SLO:** Stale detection <5s, re-promotion after sync <15s.

### S14: Dual Recovery Race

**Inject:** Two components fail simultaneously. Both begin recovery at the same time. Recovery of component A depends on component B being READY (hard dep).
**Verify:** No deadlock. Wave ordering respected — B recovers first, then A. Journal shows correct causal ordering. No phantom "READY" state observed for B before it actually completes handshake.
**Recover:** Both components reach READY.
**SLO:** Both recovered <60s.

### Assertion Nuances (All Scenarios)

- **Causality chains**: Assertions verify `oracle_event_seq` ordering, not just endpoint states.
- **Negative assertions**: "X did NOT happen" checked via `oracle.event_log()` scan, not absence of observation.
- **Timing assertions**: Use monotonic clock deltas for SLO checks, `oracle_event_seq` for ordering.
- **Fault isolation**: Every scenario with fault injection asserts `unaffected_components` remained healthy.

---

## Section 3: StateOracle + Fault Scope Boundaries

### 3.1 StateOracle Abstraction

The StateOracle is the **single normalized read interface** for all scenario assertions. Scenarios NEVER read component state directly.

```python
class StateOracle(Protocol):
    def component_status(self, name: str) -> OracleObservation: ...
    def routing_decision(self) -> OracleObservation: ...
    def kernel_state(self) -> OracleObservation: ...
    def epoch(self) -> int: ...
    def contract_status(self, contract_name: str) -> ContractStatus: ...
    def store_revision(self, store_name: str) -> int: ...
    def event_log(self, since_phase: Optional[str] = None) -> List[ObservedEvent]: ...
    def component_declarations(self) -> Dict[str, ComponentDeclaration]: ...
    def time_in_current_state(self, name: str) -> float: ...
    def wait_until(self, predicate: Callable, deadline: float,
                   description: str = "") -> Awaitable[None]: ...
```

**Two implementations:**

| | `MockStateOracle` | `LiveStateOracle` |
|---|---|---|
| Source | In-memory registry, synchronous | Polls real health endpoints, reads journal |
| Latency | Instant | Configurable poll interval (default 500ms) |
| Consistency | Exact (single-threaded) | Eventually consistent (bounded by `max_observation_lag_ms`) |
| Used by | Mock-mode scenarios (CI) | Real-mode scenarios (staging) |

**Staleness contract**: `LiveStateOracle` has a configurable `max_observation_lag_ms` (default 2000ms). `wait_until()` fails with `oracle_stale` if the oracle cannot refresh within the staleness budget, rather than silently passing on cached data.

**Oracle observation wrapper**:

```python
@dataclass(frozen=True)
class OracleObservation:
    value: Any
    observed_at_mono: float
    observation_quality: Literal["fresh", "stale", "timeout", "divergent"]
    source: str
```

**Divergence detection**: `LiveStateOracle` compares health endpoint vs journal vs router for the same component. Disagreement emits `oracle_divergence` event. In strict mode, raises `OracleDivergenceError`. In lenient mode, logs warning and marks observation quality as `"divergent"`.

### 3.2 ObservedEvent (Event Provenance Model)

```python
@dataclass(frozen=True)
class ObservedEvent:
    oracle_event_seq: int          # total-order key, assigned by oracle only
    timestamp_mono: float          # time.monotonic() at observation
    source: str                    # "prime_router" | "lifecycle_engine" | "fault_injector"
    event_type: str                # "state_change" | "fault_injected" | "recovery_started"
    component: Optional[str]
    old_value: Optional[str]
    new_value: str
    epoch: int                     # lifecycle epoch at event time
    scenario_phase: str            # "setup" | "inject" | "verify" | "recover"
    trace_root_id: str             # scenario-level trace root
    trace_id: str                  # per-fault or per-action trace ID
    metadata: Dict[str, Any]
```

**`oracle_event_seq` ownership**: The oracle is the sole emitter of sequence numbers. Neither the orchestrator nor the injector assigns seq values — they submit events to the oracle, which stamps them. This prevents ordering forks.

**Causality chain assertions** use `oracle_event_seq` for ordering, `timestamp_mono` only for SLO duration checks:

```python
events = oracle.event_log(since_phase="inject")
fault_event = first(e for e in events if e.event_type == "fault_injected")
fallback_event = first(e for e in events if e.new_value == "CLAUDE")
assert fault_event.oracle_event_seq < fallback_event.oracle_event_seq  # causal order
assert fallback_event.timestamp_mono - fault_event.timestamp_mono < 10.0  # SLO
```

### 3.3 Contract Status Taxonomy

```python
@dataclass(frozen=True)
class ContractStatus:
    compatible: bool
    reason_code: ContractReasonCode
    detail: Optional[str] = None

class ContractReasonCode(Enum):
    OK = "ok"
    VERSION_WINDOW = "version_window"
    SCHEMA_HASH = "schema_hash"
    MISSING_CAPABILITY = "missing_capability"
    HANDSHAKE_MISSING = "handshake_missing"
    HANDSHAKE_EXPIRED = "handshake_expired"
```

### 3.4 Fault Scope Boundaries

Every fault injection declares an explicit blast radius type:

```python
class FaultScope(Enum):
    COMPONENT = "component"    # single component (kill process)
    TRANSPORT = "transport"    # network/IPC layer (block port)
    CONTRACT = "contract"      # contract compatibility (version mismatch)
    CLOCK = "clock"            # time manipulation (freeze heartbeat TTL)
    PROCESS = "process"        # OS-level (SIGKILL, OOM simulation)
```

Each injection returns a `FaultHandle`:

```python
@dataclass(frozen=True)
class FaultHandle:
    fault_id: str
    scope: FaultScope
    target: str
    affected_components: FrozenSet[str]
    unaffected_components: FrozenSet[str]
    pre_fault_baseline: Dict[str, str]  # component -> status before fault
    convergence_deadline_s: float        # post-revert recovery deadline
    revert: Callable[[], Awaitable[None]]
```

**Isolation invariant** (checked after every injection):

```python
async def _check_isolation(oracle: StateOracle, handle: FaultHandle):
    for name in handle.unaffected_components:
        status = oracle.component_status(name)
        # DEGRADED is allowed (soft-dep effect); FAILED/LOST is not
        assert status.value not in (ComponentStatus.FAILED, ComponentStatus.LOST), (
            f"Fault {handle.fault_id} (scope={handle.scope.value}, target={handle.target}) "
            f"leaked to unaffected component {name} (status={status.value})"
        )
```

**Re-entrant fault guard**:

```python
class FaultComposition(Enum):
    REJECT = "reject"      # default: raise if fault already active on target
    STACK = "stack"         # allow stacking (e.g., latency + partition)
    REPLACE = "replace"     # revert existing, inject new
```

Overlapping faults on the same target are rejected unless the scenario declares a composition policy.

**Revert verification**: Post-revert asserts convergence to **pre-fault baseline class** (or declared target state), not just generic READY|DEGRADED:

```python
async def revert(self, handle: FaultHandle):
    await handle.revert()
    await self._check_isolation(handle)

    # Convergence: affected components must return to pre-fault baseline class
    baseline = handle.pre_fault_baseline
    await self._oracle.wait_until(
        lambda: all(
            self._oracle.component_status(c).value.name == baseline[c]
            or self._oracle.component_status(c).value in (
                ComponentStatus.READY, ComponentStatus.DEGRADED)
            for c in handle.affected_components
        ),
        deadline=handle.convergence_deadline_s,
        description=f"post-revert convergence for {handle.fault_id}",
    )
```

### 3.5 ScopedFaultInjector (Wraps Existing Infrastructure)

```python
class ScopedFaultInjector:
    """Wraps existing FaultInjector with scope boundaries and event provenance."""

    def __init__(self, inner: FaultInjector, oracle: StateOracle):
        self._inner = inner
        self._oracle = oracle
        self._active_by_target: Dict[str, FaultHandle] = {}

    async def inject(self, *, scope: FaultScope, target: str,
                     fault_type: str, affected: FrozenSet[str],
                     unaffected: FrozenSet[str],
                     composition: FaultComposition = FaultComposition.REJECT,
                     convergence_deadline_s: float = 30.0,
                     **kwargs) -> FaultHandle:
        # Re-entrant guard
        if target in self._active_by_target:
            if composition == FaultComposition.REJECT:
                raise ReentrantFaultError(f"Fault already active on {target}")
            elif composition == FaultComposition.REPLACE:
                await self.revert(self._active_by_target[target])

        # Capture pre-fault baseline
        baseline = {name: self._oracle.component_status(name).value.name
                    for name in affected}

        # Delegate to inner injector
        inner_result = await self._inner.inject_failure(target, fault_type, **kwargs)

        handle = FaultHandle(
            fault_id=uuid4().hex[:12], scope=scope, target=target,
            affected_components=affected, unaffected_components=unaffected,
            pre_fault_baseline=baseline,
            convergence_deadline_s=convergence_deadline_s,
            revert=inner_result.revert,
        )

        # Emit provenance event (oracle assigns oracle_event_seq)
        self._oracle.emit_event(ObservedEvent(
            oracle_event_seq=0,  # placeholder, oracle assigns real seq
            timestamp_mono=time.monotonic(),
            source="fault_injector", event_type="fault_injected",
            component=target, old_value=None, new_value=fault_type,
            epoch=self._oracle.epoch(),
            scenario_phase=self._oracle.current_phase(),
            trace_root_id=self._current_trace_root,
            trace_id=handle.fault_id,
            metadata={"scope": scope.value, "affected": list(affected)},
        ))

        self._active_by_target[target] = handle
        return handle
```

### 3.6 Invariant Registry

Invariants run on **two triggers**: (1) after every state-change event emitted to the oracle, and (2) on a periodic watchdog (default 2s) to catch silent stalls.

```python
class InvariantRegistry:
    def __init__(self, debounce_window_s: float = 5.0):
        self._invariants: List[Tuple[str, Callable, bool]] = []  # (name, check, suppress_flapping)
        self._debounce_window_s = debounce_window_s

    def register(self, name: str,
                 check: Callable[[StateOracle], Optional[str]],
                 suppress_flapping: bool = True):
        self._invariants.append((name, check, suppress_flapping))
```

**Flapping suppression defaults**:
- **OFF** for critical invariants: `epoch_monotonic`, `single_routing_target`, `fault_isolation`
- **ON** for noisy invariants: `no_zombie_components` (during expected transitional windows)

**MVP invariants** (always registered):

| Invariant | Description | Flap Suppression |
|---|---|---|
| `epoch_monotonic` | Epoch never decreases | OFF |
| `terminal_is_final` | STOPPED/FAILED never transition (except restart) | OFF |
| `single_routing_target` | Exactly one active routing target at all times | OFF |
| `fault_isolation` | Faults don't leak beyond declared scope | OFF |
| `no_zombie_components` | No component stuck in STARTING beyond `start_timeout_s + grace_s` | ON |

**Zombie invariant precision**: Reads `start_timeout_s` from each component's `ComponentDeclaration` plus an explicit `grace_s` from component metadata. Known long-start components are exempt under their declared grace contract.

---

## Section 4: Execution Model, CI Integration, and Go/No-Go

### 4.1 HarnessOrchestrator Execution Model

```python
class HarnessOrchestrator:
    def __init__(self, mode: Literal["mock", "real"],
                 oracle: StateOracle, injector: ScopedFaultInjector,
                 invariants: InvariantRegistry, config: HarnessConfig):
        self._mode = mode
        self._oracle = oracle
        self._injector = injector
        self._invariants = invariants
        self._config = config
        self._current_phase: Optional[str] = None
        self._phase_boundary_seq: Dict[str, int] = {}  # seq, not epoch

    async def run_scenario(self, scenario: ScenarioDefinition) -> ScenarioResult:
        trace_root_id = uuid4().hex[:16]
        violations: List[str] = []
        phase_results: Dict[str, PhaseResult] = {}

        for phase_name in ("setup", "inject", "verify", "recover"):
            self._current_phase = phase_name
            self._phase_boundary_seq[phase_name] = self._oracle.current_seq()

            phase_fn = getattr(scenario, phase_name)
            phase_start = time.monotonic()

            try:
                await asyncio.wait_for(
                    phase_fn(self._oracle, self._injector, trace_root_id),
                    timeout=scenario.phase_deadlines.get(phase_name, 60.0),
                )
            except asyncio.TimeoutError:
                violations.append(PhaseFailure(
                    phase=phase_name,
                    failure_type="phase_timeout",
                    detail=f"Exceeded {scenario.phase_deadlines.get(phase_name, 60.0)}s deadline",
                ))
                break

            # Invariant check on phase boundary
            inv_violations = self._invariants.check_all(self._oracle)
            for v in inv_violations:
                violations.append(PhaseFailure(
                    phase=phase_name,
                    failure_type="invariant_violation",
                    detail=v,
                ))

            # Phase boundary fence
            self._oracle.fence_phase(phase_name, self._phase_boundary_seq[phase_name])

            phase_results[phase_name] = PhaseResult(
                duration_s=time.monotonic() - phase_start,
                violations=[v for v in violations if v.phase == phase_name],
            )

        return ScenarioResult(
            scenario_name=scenario.name,
            trace_root_id=trace_root_id,
            passed=len(violations) == 0,
            violations=violations,
            phases=phase_results,
            event_log=self._oracle.event_log(),
        )
```

**Typed failure classification**: Phase failures are typed as `phase_timeout`, `oracle_stale`, `invariant_violation`, or `divergence_error` for deterministic triage.

**Phase boundary fencing**: Events with `oracle_event_seq < phase_boundary_seq[current_phase]` are tagged `stale=True` and excluded from current-phase assertions unless the scenario explicitly opts in.

### 4.2 CI Integration

```
pytest mark hierarchy:
  @pytest.mark.integration          -- all cross-repo tests
  @pytest.mark.integration_mock     -- mock-mode (fast, CI-safe)
  @pytest.mark.integration_real     -- real-mode (staging only)

CI pipeline:
  PR gate:     pytest -m integration_mock     (< 60s, no external deps)
  Nightly:     pytest -m integration_real      (staging env, ~10 min)
  Pre-release: pytest -m integration           (both modes, full matrix)
```

**Mode parity check** (nightly CI): Compare mock-mode and real-mode `ScenarioResult.event_log` event types and orderings for each scenario:
- **Warning** at >15% divergence
- **Hard fail** at >30% divergence

This catches mock-mode tests that pass but would fail against real components.

### 4.3 Real-Mode Process Governance

```python
class RealComponentProcess(ComponentProcess):
    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._spawn_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._oracle.wait_until(
            lambda: self._oracle.component_status(self.name).value == ComponentStatus.READY,
            deadline=self._decl.start_timeout_s,
        )

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
```

**Governance rules**:
- All processes killed in `finally` block — zero orphans
- Stdout/stderr captured and attached to `ScenarioResult` for forensics
- Resource limits enforced via cgroup or ulimit where available

**Isolation profiles** (tiered by environment capability):

| Profile | Mechanism | When |
|---|---|---|
| **Full** | Network namespace + cgroup | Container/VM CI runners |
| **Fallback** | Unique ports + process group + `$TMPDIR` roots + strict cleanup | Bare-metal runners without namespace support |

The harness auto-detects available isolation and selects the strongest profile. Scenarios declare minimum isolation level; real-mode scenarios that require full isolation skip on fallback-only runners.

### 4.4 Go/No-Go Criteria

The harness is MVP-complete when ALL gates pass:

| Gate | Criteria |
|---|---|
| **Oracle parity** | `MockStateOracle` and `LiveStateOracle` pass identical interface conformance suite (>=20 assertions) |
| **Scenario coverage** | All 10 MVP scenarios (S1-S8, S13, S14) pass in mock-mode |
| **Invariant coverage** | All 5 MVP invariants registered and exercised by >=1 scenario |
| **Fault isolation proven** | >=3 scenarios verify `unaffected_components` remain healthy |
| **Causality verified** | >=5 scenarios assert `oracle_event_seq` ordering (not just state endpoints) |
| **CI green** | `pytest -m integration_mock` passes in <60s on CI |
| **Real-mode smoke** | >=2 scenarios (S1, S3) pass in real-mode on staging |
| **Mode parity** | Mock vs real event-type ordering divergence <15% for smoke scenarios |
| **No orphans** | Real-mode cleanup verified: zero lingering processes after test suite |

---

## Appendix: Key Design Decisions

1. **Wrap, don't replace**: ScopedFaultInjector wraps existing FaultInjector rather than reimplementing. This preserves battle-tested injection logic.
2. **Oracle owns sequencing**: `oracle_event_seq` is assigned solely by the oracle. No other component assigns sequence numbers, preventing ordering forks.
3. **Phase fencing**: Late events from previous phases cannot satisfy next-phase assertions. This prevents false-positive passes from stale observations.
4. **Invariant dual-trigger**: Event-driven + periodic watchdog catches both active violations and silent stalls.
5. **Typed failures**: Phase failures classified as `phase_timeout | oracle_stale | invariant_violation | divergence_error` for deterministic automated triage.
6. **Pre-fault baseline capture**: Revert verification asserts convergence to pre-fault state class, not a generic target.
7. **Tiered isolation**: Full namespace when available, fallback isolation profile otherwise. Scenarios declare minimum requirements.

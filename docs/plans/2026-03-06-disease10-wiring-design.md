# Disease 10 Wiring — Integration Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the 5 standalone Disease 10 startup sequencing modules into the JARVIS codebase with production-grade orchestration, authority management, auto-recovery, and observability.

**Architecture:** A new `startup_orchestrator.py` sits between `unified_supervisor.py` and the Disease 10 modules, owning the full startup lifecycle: phase gate progression, tiered concurrency budget enforcement, GCP readiness lease management, routing authority handoff via an explicit FSM, and boot invariant checking. Event-sourced telemetry feeds a single stream consumed by structured logs, metrics, and the TUI dashboard.

**Tech Stack:** Python 3.9+, asyncio, dataclasses, Textual (TUI), aiohttp (probe HTTP), JSON schema validation.

---

## 1. Architecture Overview

```
unified_supervisor.py (kernel)
  |
  +---> StartupOrchestrator (new)
          |-- PhaseGateCoordinator
          |-- StartupBudgetPolicy (tiered: hard/soft semaphores)
          |-- GCPReadinessLease + GCPVMReadinessProber (hybrid adapter)
          |-- StartupRoutingPolicy (boot authority)
          |-- BootInvariantChecker
          |-- RoutingAuthorityFSM (BOOT_POLICY -> HANDOFF -> HYBRID)
          +-- StartupTelemetry (event bus -> logs, metrics, TUI)
                |
                |---> supervisor_tui.py (inline boot summary)
                +---> supervisor_tui.py (detail drill-down panel)
```

**Post-handoff:** `GCPHybridPrimeRouter` is sole routing authority. `PrimeRouter` becomes a read-only mirror/facade. `StartupRoutingPolicy` is finalized and frozen.

---

## 2. Routing Authority FSM

### State Machine

```
States:
  BOOT_POLICY_ACTIVE  --> HANDOFF_PENDING  --> HYBRID_ACTIVE
         ^                      |
         +----------------------+
                          HANDOFF_FAILED
                               |
                               +--> BOOT_POLICY_ACTIVE (rollback)

  HYBRID_ACTIVE --> BOOT_POLICY_ACTIVE (catastrophic recovery)
```

### Transitions

| From | To | Guards (ALL must pass) |
|------|----|----------------------|
| BOOT_POLICY_ACTIVE | HANDOFF_PENDING | CORE_READY gate passed, cross-repo contracts valid, invariants clean |
| HANDOFF_PENDING | HYBRID_ACTIVE | hybrid_router_ready, lease_valid OR local_model_loaded, readiness_contract_passed, invariants_clean, no_in_flight_requests (bounded drain) |
| HANDOFF_PENDING | HANDOFF_FAILED | Any guard fails, or HANDOFF_TIMEOUT_S exceeded |
| HANDOFF_FAILED | BOOT_POLICY_ACTIVE | Automatic rollback |
| HYBRID_ACTIVE | BOOT_POLICY_ACTIVE | Catastrophic causes: lease_loss, readiness_regression, router_internal_error, contract_drift |

### Properties

- **Fail-closed:** Unknown state or transition error defaults to BOOT_POLICY_ACTIVE.
- **One-writer:** Each state has an `authority_holder` field; only the holder may issue routing decisions.
- **Guard evaluation order:** Deterministic — cheap/static first (contracts, invariants), then dynamic (lease, readiness), then in-flight drain.
- **Transition deadlines:** Each transition has an independent timeout with reason code.
- **Hysteresis:** Require N=3 (configurable) consecutive readiness passes before HANDOFF_PENDING -> HYBRID_ACTIVE.
- **Token uniqueness invariant:** Exactly one valid authority token exists; stale tokens auto-revoked.
- **Persistent journal:** FSM state + transitions persisted to `$JARVIS_STATE_DIR/startup_fsm_journal.jsonl` for restart recovery.

### PrimeRouter Mirror Contract

- `_mirror_mode: bool` flag with shared guard decorator on ALL mutating methods.
- When mirror_mode=True: `_decide_route()`, `promote_gcp_endpoint()`, `demote_gcp_endpoint()` all raise `RuntimeError`.
- Invariant: `mirror_decisions_issued == 0`.
- Fail-closed at API boundary — decorator, not per-method checks.

---

## 3. Tiered Concurrency Budget

### Budget Policy

```python
BUDGET_POLICY = {
    "max_hard_concurrent": 1,      # env: JARVIS_BUDGET_MAX_HARD
    "max_total_concurrent": 3,     # env: JARVIS_BUDGET_MAX_TOTAL
    "hard_gate_categories": ["MODEL_LOAD", "REACTOR_LAUNCH", "SUBPROCESS_SPAWN"],
    "soft_gate_categories": ["ML_INIT", "GCP_PROVISION"],
    "soft_gate_preconditions": {
        "ML_INIT": {
            "require_phase": "CORE_READY",
            "require_memory_stable_s": 10,
            "memory_slope_threshold_mb_s": 0.5,
            "memory_sample_interval_s": 1.0
        }
    },
    "gcp_parallel_allowed": true
}
```

### Two Semaphores

- `_hard_sem = Semaphore(1)` — mutual exclusion for MODEL_LOAD, REACTOR_LAUNCH, SUBPROCESS_SPAWN.
- `_total_sem = Semaphore(3)` — global cap across all 5 categories.

### Acquisition Flow

1. Acquire `_total_sem` (with timeout + reason code)
2. If hard-gate category: also acquire `_hard_sem`
3. If soft-gate with preconditions: validate preconditions before acquiring
4. Yield `TaskSlot`
5. On release: release in reverse order, append `CompletedTask` with wait duration

### Safety Properties

- **Starvation protection:** Per-category max wait budget; fair queueing prevents GCP_PROVISION from indefinitely delaying hard tasks.
- **Timeout taxonomy:** `acquire_total_timeout`, `acquire_hard_timeout`, `precondition_timeout`, `phase_timeout` — distinct for observability.
- **Leak hardening:** Releases happen under cancellation/exception paths with invariant `held_slots == 0` at phase end.
- **Cross-phase carryover:** Default no carryover; tokens cannot be held across gates unless explicitly declared.
- **Contention telemetry:** Structured events for queue depth, wait duration, timed-out acquisitions, starvation counts.

### Declarative Phase Gate Config

```python
PHASE_CONFIG = {
    "PREWARM_GCP": {
        "dependencies": [],
        "timeout_s": 45,           # env: JARVIS_GATE_PREWARM_TIMEOUT
        "on_timeout": "skip"
    },
    "CORE_SERVICES": {
        "dependencies": ["PREWARM_GCP"],
        "timeout_s": 120,          # env: JARVIS_GATE_CORE_SERVICES_TIMEOUT
        "on_timeout": "fail"
    },
    "CORE_READY": {
        "dependencies": ["CORE_SERVICES"],
        "timeout_s": 60,           # env: JARVIS_GATE_CORE_READY_TIMEOUT
        "on_timeout": "fail"
    },
    "DEFERRED_COMPONENTS": {
        "dependencies": ["CORE_READY"],
        "timeout_s": 90,           # env: JARVIS_GATE_DEFERRED_TIMEOUT
        "on_timeout": "fail"
    }
}
```

- Gate order driven by config, not code paths.
- All timeouts overridable via env vars with range validation (min/max bounds).
- DAG soundness validated at boot: cycle detection, unknown targets, duplicates, unreachable phases.
- Skip semantics: skipped gate triggers explicit fallback state update so downstream guards don't treat it as unknown.

---

## 4. Hybrid ReadinessProber & Lease Lifecycle

### GCPVMReadinessProber (Adapter)

```python
class GCPVMReadinessProber(ReadinessProber):
    def __init__(self, vm_manager: GCPVMManager, probe_cache_ttl: float = 3.0):
        ...

    async def probe_health(host, port, timeout) -> HandshakeResult:
        # Delegates to vm_manager.ping_health() (renamed from _ping_health_endpoint)
        # Maps HealthVerdict -> HandshakeResult with failure classification
        # Cached for probe_cache_ttl seconds

    async def probe_capabilities(host, port, timeout) -> HandshakeResult:
        # Delegates to vm_manager.check_lineage() (renamed from _check_vm_golden_image_lineage)
        # Plus GET /capabilities for ProviderManifest
        # SCHEMA_MISMATCH if manifest doesn't match expected contract

    async def probe_warm_model(host, port, timeout) -> HandshakeResult:
        # Standalone HTTP: POST /v1/warm_check with small test prompt
        # NOT cached (must reflect real-time model readiness)
        # Failure classification: TIMEOUT, NETWORK, RESOURCE
```

### Readiness Contract (strict)

```
READY = health_ok AND capability_ok AND warm_model_ok
```

No partial ready. Any step failure -> lease stays INACTIVE/FAILED.

### Lease Lifecycle

1. **Acquire:** During PREWARM_GCP phase, `lease.acquire(host, port, timeout_per_step)`.
   - Hysteresis: 3 consecutive health probes pass before acquire attempted.
   - Success: gate.resolve(PREWARM_GCP), policy.signal_gcp_ready().
   - Failure: classify, gate.skip/fail based on on_timeout policy.

2. **Refresh loop:** Background task every TTL/3 (~40s), `lease.refresh(timeout)`.
   - Success: extend TTL.
   - Failure: lease auto-expires, trigger revocation path.

3. **Revocation (first-class, deterministic):**
   - `lease.revoke(reason)` -> immediate invalidation.
   - Orchestrator emits LEASE_REVOKED telemetry event.
   - Triggers authority rollback if HYBRID_ACTIVE.
   - Triggers policy.signal_gcp_revoked() if BOOT_POLICY_ACTIVE.
   - Invariant check: gcp_offload_active == False after revocation.

4. **Cross-repo contract validation:** Runs AFTER lease acquired, BEFORE authority handoff. Contract drift blocks handoff with cause `contract_drift`.

---

## 5. Event-Sourced Telemetry & TUI

### Event Schema

```python
@dataclass(frozen=True)
class StartupEvent:
    trace_id: str              # boot-level UUID, set once
    event_type: str            # phase_gate, budget_acquire, lease_probe, etc.
    timestamp: float           # time.monotonic()
    wall_clock: str            # ISO-8601
    authority_state: str       # current FSM state
    phase: Optional[str]       # current gate phase
    detail: Dict[str, Any]     # event-specific payload
```

### Event Types

| Event Type | Key Payload Fields |
|------------|-------------------|
| phase_gate | phase, status, failure_reason, duration_s |
| budget_acquire | category, name, wait_s, queue_depth, hard_slot |
| budget_release | category, name, held_s |
| budget_timeout | category, name, timeout_s, queue_depth |
| budget_starvation | category, starved_s, blocked_by |
| lease_probe | step, passed, failure_class, latency_ms, cached |
| lease_acquired | host, port, lease_epoch, ttl_s |
| lease_refreshed | lease_epoch, new_expiry |
| lease_revoked | lease_epoch, reason, trigger |
| lease_expired | lease_epoch, elapsed_s |
| authority_transition | from_state, to_state, guards_checked, failed_guard, duration_ms |
| authority_rollback | from_state, cause, rollback_reason |
| invariant_check | results[] with id, passed, severity, trace |
| routing_decision | decision, fallback_reason, authority |
| contract_validation | result, drift_detected, detail |

### Consumers (single event stream)

- **StructuredLogger:** JSON lines to startup trace log file.
- **MetricsCollector:** Counters/histograms (phase durations, wait times, probe latencies).
- **TUIBridge:** Feeds both inline summary and detail panel.
- **FSMJournal:** Persists authority transitions for restart recovery.

### TUI Inline Summary (during boot)

```
+-- Startup Sequencing ------------------------------------+
| Phase: CORE_SERVICES > CORE_READY (waiting)             |
| Authority: BOOT_POLICY_ACTIVE                           |
| Budget: [##.] 1/3 total, 1/1 hard (MODEL_LOAD)         |
| GCP Lease: ACTIVE (ttl 87s) health+ caps+ warm+        |
| Invariants: 4/4 pass                                    |
+---------------------------------------------------------+
```

### TUI Detail Panel (post-boot drill-down)

Shows: phase timeline with durations, budget contention with wait/held times, lease history with per-step latencies, authority handoff trace with guard results and drain timing, invariant results.

---

## 6. Auto-Recovery & Deterministic Downgrade

### Scenario A: Lease loss during boot (BOOT_POLICY_ACTIVE)

1. lease.revoke(reason) or lease expires
2. policy.signal_gcp_revoked(reason)
3. policy.decide() -> fallback chain (LOCAL_MINIMAL -> CLOUD_CLAUDE -> DEGRADED)
4. Invariant check: gcp_offload_active == False
5. Gate: if PREWARM_GCP still PENDING -> skip("lease_lost")
6. Continue boot on fallback, no retry until next boot

### Scenario B: Lease loss post-handoff (HYBRID_ACTIVE)

1. lease.revoke(reason) or lease expires
2. FSM: HYBRID_ACTIVE -> BOOT_POLICY_ACTIVE (cause: lease_loss)
3. Controlled unfreeze: policy re-activated with current signal state
4. GCPHybridPrimeRouter: set read-only
5. Drain window: wait up to JARVIS_DRAIN_WINDOW_S (5s) for in-flight GCP requests
6. Invariant check: one authority, no stale offload
7. Optional: schedule background lease re-acquisition (with hysteresis)

### Scenario C: Handoff failure (HANDOFF_PENDING -> HANDOFF_FAILED)

1. Guard check fails or HANDOFF_TIMEOUT_S exceeded
2. FSM: HANDOFF_PENDING -> HANDOFF_FAILED -> BOOT_POLICY_ACTIVE
3. Log failed guard with reason code
4. Policy remains active (was never finalized)
5. No automatic retry unless JARVIS_HANDOFF_RETRY_ENABLED=true

---

## 7. Environment Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| JARVIS_BUDGET_MAX_HARD | 1 | Hard semaphore limit |
| JARVIS_BUDGET_MAX_TOTAL | 3 | Total semaphore limit |
| JARVIS_BUDGET_MAX_WAIT_S | 60.0 | Starvation protection ceiling |
| JARVIS_GATE_PREWARM_TIMEOUT | 45.0 | PREWARM_GCP gate timeout |
| JARVIS_GATE_CORE_SERVICES_TIMEOUT | 120.0 | CORE_SERVICES gate timeout |
| JARVIS_GATE_CORE_READY_TIMEOUT | 60.0 | CORE_READY gate timeout |
| JARVIS_GATE_DEFERRED_TIMEOUT | 90.0 | DEFERRED_COMPONENTS gate timeout |
| JARVIS_GCP_DEADLINE_S | 60.0 | Routing policy GCP deadline |
| JARVIS_LEASE_TTL_S | 120.0 | GCP readiness lease TTL |
| JARVIS_PROBE_TIMEOUT_S | 15.0 | Per-step probe timeout |
| JARVIS_PROBE_CACHE_TTL | 3.0 | Probe result cache duration |
| JARVIS_MEMORY_STABLE_S | 10.0 | ML_INIT memory stability window |
| JARVIS_MEMORY_SLOPE_THRESHOLD | 0.5 | MB/s slope for "stable" |
| JARVIS_DRAIN_WINDOW_S | 5.0 | In-flight drain during rollback |
| JARVIS_LEASE_REACQUIRE_DELAY_S | 30.0 | Backoff before lease re-acquisition |
| JARVIS_LEASE_HYSTERESIS_COUNT | 3 | Consecutive passes before handoff |
| JARVIS_HANDOFF_TIMEOUT_S | 10.0 | Max time in HANDOFF_PENDING |
| JARVIS_HANDOFF_RETRY_ENABLED | false | Auto-retry failed handoff |
| JARVIS_FSM_JOURNAL_PATH | $JARVIS_STATE_DIR/startup_fsm_journal.jsonl | FSM persistence |

All env overrides are range-validated with min/max sane limits.

---

## 8. Integration Touchpoints

### New Files

| File | Purpose | Est. Lines |
|------|---------|-----------|
| backend/core/startup_orchestrator.py | Orchestrator lifecycle, signal wiring, handoff | ~600 |
| backend/core/gcp_vm_readiness_prober.py | Hybrid ReadinessProber adapter | ~200 |
| backend/core/routing_authority_fsm.py | Explicit FSM, guards, journal, token uniqueness | ~350 |
| backend/core/startup_telemetry.py | Event bus, structured logger, metrics, TUI bridge | ~400 |
| backend/core/startup_budget_policy.py | Tiered budget (hard/soft, preconditions, starvation) | ~300 |
| backend/core/startup_config.py | Declarative config loader, schema/DAG validation | ~200 |

### Modified Files

| File | Changes | Est. Lines |
|------|---------|-----------|
| unified_supervisor.py | Import orchestrator, create at init, call at phase boundaries | ~50 |
| backend/core/prime_router.py | Mirror mode flag + guard decorator | ~30 |
| backend/core/gcp_hybrid_prime_router.py | set_active(bool) method | ~20 |
| backend/core/gcp_vm_manager.py | Expose ping_health + check_lineage as public | ~15 |
| backend/core/supervisor_tui.py | Inline summary widget + detail panel | ~80 |

### Test Files

| File | Coverage |
|------|----------|
| tests/unit/core/test_startup_orchestrator.py | Lifecycle, phase progression, signal wiring |
| tests/unit/core/test_routing_authority_fsm.py | Transitions, guard failures, rollback, journal |
| tests/unit/core/test_gcp_vm_readiness_prober.py | Adapter delegation, cache, warm probe |
| tests/unit/core/test_startup_budget_policy.py | Tiered enforcement, starvation, preconditions |
| tests/unit/core/test_startup_telemetry.py | Event emission, consumer routing, TUI bridge |
| tests/unit/core/test_startup_config.py | Schema validation, DAG soundness, env overrides |
| tests/integration/test_disease10_wiring.py | End-to-end boot -> handoff -> steady-state |
| tests/integration/test_disease10_recovery.py | Lease loss, handoff failure, rollback |

### Estimated Total

~2,200 new lines + ~195 lines modified across 5 existing files.

### Unchanged Disease 10 Modules (consumed via existing APIs)

- startup_phase_gate.py
- gcp_readiness_lease.py
- startup_concurrency_budget.py (wrapped by startup_budget_policy.py)
- boot_invariants.py
- startup_routing_policy.py

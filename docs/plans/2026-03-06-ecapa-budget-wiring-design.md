# ECAPA Budget Wiring Design

## Problem Statement

ECAPA voice unlock is completely blocked during startup because it operates independently of the Disease 10 startup sequencing system. Two failure modes:

1. **Cloud endpoint contract probe times out** (30s) -- the network stack is starved during memory thrash (pagein rate 3120/sec).
2. **Local ECAPA load blocked** -- `MemoryQuantizer._thrash_state == "emergency"` prevents the ~700MB PyTorch + SpeechBrain allocation.

Root cause: ECAPA verification launches as an uncoordinated background task after Phase 4 (`unified_supervisor.py:73065-73219`). It has ad-hoc memory checks but does NOT participate in the Disease 10 budget/phase gate system. No `HeavyTaskCategory.MODEL_LOAD` budget slot acquired, no `DEFERRED_COMPONENTS` registration, and deferred recovery polls blindly every 30s.

## Goal

Wire ECAPA initialization and recovery into the Disease 10 startup sequencing system so voice unlock participates in the budget/phase gate system instead of hitting memory walls.

## Architecture

**Approach: Budget Token Bridge** -- A new thin module (`backend/core/ecapa_budget_bridge.py`, ~200 lines) serves as the single shared coordinator between the supervisor (startup path) and MLEngineRegistry (recovery path). Both layers are budget-governed through one shared bridge instance with deterministic token lifecycle.

**Integration principle: Both layers, one shared coordinator, no duplicate acquisition.**

- **Supervisor path** owns startup sequencing and phase visibility (DEFERRED_COMPONENTS, boot invariants, authority state).
- **Registry path** owns post-startup recovery attempts, so deferred retries are also budget-governed.
- Budget token passthrough from supervisor to registry prevents double-locking.

---

## Component Design

### New Module: `backend/core/ecapa_budget_bridge.py`

Process-wide singleton that mediates all ECAPA budget interactions.

#### Core Types

```python
class BudgetTokenState(str, enum.Enum):
    ACQUIRED     = "acquired"      # Slot held, work not started
    TRANSFERRED  = "transferred"   # Passed from supervisor to registry
    REUSED       = "reused"        # Registry using supervisor's token
    RELEASED     = "released"      # Slot returned to pool
    EXPIRED      = "expired"       # Cleanup reclaimed after crash/timeout

class EcapaBudgetRejection(str, enum.Enum):
    PHASE_BLOCKED      = "phase_blocked"       # DEFERRED_COMPONENTS/CORE_READY not reached
    MEMORY_UNSTABLE    = "memory_unstable"      # Memory slope > threshold
    BUDGET_TIMEOUT     = "budget_timeout"       # Semaphore wait exceeded
    SLOT_UNAVAILABLE   = "slot_unavailable"     # Hard gate full (MODEL_LOAD=1 in use)
    THRASH_EMERGENCY   = "thrash_emergency"     # MemoryQuantizer reports emergency
    CONTRACT_MISMATCH  = "contract_mismatch"    # Probe contract version incompatible

@dataclass
class BudgetToken:
    token_id: str                              # uuid4
    owner_session_id: str                      # Crash cleanup disambiguation
    state: BudgetTokenState
    category: HeavyTaskCategory                # ML_INIT or MODEL_LOAD
    acquired_at: float
    transferred_at: Optional[float] = None
    released_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    token_ttl_s: float = 120.0                 # Max hold time before EXPIRED
    rejection_reason: Optional[EcapaBudgetRejection] = None
    probe_failure_reason: Optional[str] = None # Persisted for recovery path selection
```

#### Category Mapping (single source of truth)

```python
ECAPA_CATEGORY_MAP = {
    "probe": HeavyTaskCategory.ML_INIT,
    "model_load": HeavyTaskCategory.MODEL_LOAD,
}
```

#### Bridge Singleton Methods

- `acquire_probe_slot(timeout_s) -> Result[BudgetToken, EcapaBudgetRejection]` -- acquires ML_INIT for cloud probe
- `acquire_model_slot(timeout_s) -> Result[BudgetToken, EcapaBudgetRejection]` -- acquires MODEL_LOAD for local load
- `transfer_token(token) -> BudgetToken` -- CAS: ACQUIRED -> TRANSFERRED (single-use, fails on second call)
- `reuse_token(token) -> BudgetToken` -- TRANSFERRED -> REUSED (validates owner_session_id or trusted transfer chain)
- `heartbeat(token)` -- Updates last_heartbeat_at, extends effective TTL
- `release(token)` -- Idempotent, returns slot to pool, marks RELEASED
- `cleanup_expired(max_age_s)` -- Crash-safe reclaim of orphaned tokens; respects active REUSED heartbeats

#### Telemetry

Canonical event schema for all bridge actions:
- `ecapa_budget.acquire_attempt`
- `ecapa_budget.acquire_granted`
- `ecapa_budget.acquire_denied`
- `ecapa_budget.transfer`
- `ecapa_budget.reuse`
- `ecapa_budget.release`
- `ecapa_budget.expire_cleanup`
- `ecapa_budget.invariant_violation`

All events emitted to `StartupEventBus` with reason code, timing, and token metadata.

---

## Data Flow

### Path 1: Startup (Supervisor -> Bridge -> Registry)

```
Phase 4 (Intelligence) completes
    |
    v
DEFERRED_COMPONENTS gate reached -> bridge.signal_phase("DEFERRED_COMPONENTS")
    |
    v
Supervisor: bridge.acquire_probe_slot(timeout_s=10)
    +-- Bridge checks: phase >= CORE_READY? memory stable? ML_INIT slot free?
    +-- DENIED -> reason code -> log -> skip cloud, fall through to local attempt
    +-- GRANTED -> probe_token (state=ACQUIRED)
         |
         v
    Supervisor: run cloud contract probe (bounded 5s timeout + 2 retries w/jitter)
         +-- SUCCESS -> bridge.release(probe_token) -> cloud path active, done
         +-- FAIL/TIMEOUT ->
              bridge.release(probe_token)
              persist probe_failure_reason for recovery path selection
              bridge.acquire_model_slot(timeout_s=30)
              +-- DENIED -> reason code -> schedule deferred recovery -> done
              +-- GRANTED -> model_token (state=ACQUIRED)
                   |
                   v
              bridge.transfer_token(model_token)  [CAS: ACQUIRED->TRANSFERRED]
              registry.load_ecapa_with_token(model_token)
                   |
                   v
              Registry: bridge.reuse_token(model_token) [TRANSFERRED->REUSED]
              Registry: heartbeat every 15s during load
              Registry: load PyTorch + SpeechBrain (~700MB)
              Registry: bridge.release(model_token) [REUSED->RELEASED]
```

### Path 2: Deferred Recovery (Registry -> Bridge, no supervisor)

```
Recovery loop triggers (budget-aware backoff / memory callback / event-driven)
    |
    v
Registry: bridge.acquire_probe_slot(timeout_s=5)
    +-- DENIED (budget_timeout/slot_unavailable) ->
    |    backoff = min(base * 2^attempt, max) + jitter
    |    wait backoff, retry (budget-aware, not blind 30s)
    +-- GRANTED -> probe_token
         |
         v
    Cloud probe (5s timeout)
         +-- SUCCESS -> bridge.release(probe_token) -> cloud path, done
         +-- FAIL ->
              bridge.release(probe_token)
              Check: MemoryQuantizer.thrash_state != EMERGENCY
              bridge.acquire_model_slot(timeout_s=30)
              +-- DENIED -> backoff, retry later
              +-- GRANTED -> model_token (fresh, no transfer needed)
                   |
                   v
              Load local ECAPA under budget
              bridge.release(model_token)
```

### Key Invariants

1. **At most 1 MODEL_LOAD slot active globally** -- hard semaphore enforced by StartupBudgetPolicy. Bridge maintains `_active_model_load_count` counter + assertion.
2. **No double-lock** -- startup path transfers token; recovery path acquires fresh.
3. **Budget-aware backoff** -- recovery respects slot availability, not blind timer.
4. **Deterministic fallback** -- always cloud-first (light slot) then local (heavy slot).
5. **Acquire fairness** -- startup path uses priority=HIGH, recovery uses priority=NORMAL. NORMAL is not starved indefinitely under repeated HIGH bursts (bounded wait guarantee).

---

## Error Handling

### Crash-Safe Token Cleanup

- Primary: orchestrator shutdown hook (deterministic)
- Secondary: periodic reconciler (30s timer calling `cleanup_expired()`)
- Tertiary: `atexit` handler (best-effort only)
- Tokens in REUSED with stale heartbeat (>45s silence) -> EXPIRED, slot reclaimed, event emitted
- Tokens in ACQUIRED/TRANSFERRED with no heartbeat + age > TTL -> EXPIRED
- **Never** force-release in-flight tokens on invariant breach. Instead: freeze new acquisitions, emit CRITICAL, enter degraded recovery path, require orchestrated reconciliation.

### Differentiated Backoff

| Rejection | Retryable? | Strategy |
|---|---|---|
| `PHASE_BLOCKED` | Wait-for-gate | Subscribe to phase event, wake on signal |
| `MEMORY_UNSTABLE` | Exponential | 5s base, 2x, cap 60s, jitter +/-20% |
| `BUDGET_TIMEOUT` | Exponential | 5s base, 2x, cap 60s, jitter +/-20% |
| `SLOT_UNAVAILABLE` | Wait-for-release | Subscribe to release event, wake on signal |
| `THRASH_EMERGENCY` | Exponential (slower) | 15s base, 2x, cap 120s, jitter +/-30% |
| `CONTRACT_MISMATCH` | Non-retryable | Wait for contract/version change event |

### Timeout Matrix

| Operation | Max Budget |
|---|---|
| Probe slot acquire | 10s (startup), 5s (recovery) |
| Cloud probe execution | 5s per attempt, 2 retries with jitter |
| Model slot acquire | 30s |
| Load heartbeat silence | 45s before EXPIRED |
| Total load wall-clock | 120s (token_ttl_s) |
| Recovery loop cycle deadline | 180s per full probe+load cycle |

---

## Files

### Created

| File | Purpose | Size |
|---|---|---|
| `backend/core/ecapa_budget_bridge.py` | Shared coordinator singleton | ~200 lines |
| `tests/unit/core/test_ecapa_budget_bridge.py` | Unit tests | ~15-20 tests |
| `tests/integration/test_ecapa_budget_wiring.py` | Integration tests | ~7 tests |

### Modified

| File | Change |
|---|---|
| `unified_supervisor.py` (~73065-73220) | Replace ad-hoc ECAPA verification with bridge-coordinated flow |
| `backend/voice_unlock/ml_engine_registry.py` (~5686-5784) | Wire deferred recovery to use bridge instead of blind polling |
| `backend/core/startup_config.py` | Add ECAPA_PROBE -> ML_INIT mapping in soft_preconditions |

### Unchanged

| File | Reason |
|---|---|
| `backend/core/startup_concurrency_budget.py` | Existing HeavyTaskCategory enum already has MODEL_LOAD + ML_INIT |
| `backend/core/startup_budget_policy.py` | Existing tiered semaphore already enforces hard gate for MODEL_LOAD |

---

## Testing Strategy

### Unit Tests (`test_ecapa_budget_bridge.py`, ~15-20 tests)

1. Token lifecycle: ACQUIRED -> TRANSFERRED -> REUSED -> RELEASED happy path
2. CAS enforcement: transfer_token twice -> second fails
3. Ownership verification: reuse with wrong session_id -> rejected
4. Probe -> load fallback: probe denied -> acquires model slot correctly
5. Double-lock prevention: startup transfers token, recovery doesn't double-acquire
6. Denial reason codes: each rejection type produces correct enum
7. Heartbeat timeout: token without heartbeat expires after TTL
8. Cleanup safety: REUSED with active heartbeat NOT reclaimed
9. Invariant assertion: concurrent MODEL_LOAD > 1 triggers violation (freeze, not force-release)
10. Phase-gated wake: PHASE_BLOCKED denial resolves when phase reached
11. Memory-unstable backoff: retry respects exponential backoff with correct base/cap/jitter
12. Budget-aware recovery: recovery loop uses bridge, not blind polling
13. Telemetry emission: all bridge actions emit canonical events
14. Idempotent release: double-release is safe (no error, no double-free)
15. Startup fairness: HIGH priority preempts NORMAL; NORMAL not starved (bounded wait)
16. Probe failure reason persistence: stored on token, available to recovery path

### Integration Tests (`test_ecapa_budget_wiring.py`, ~7 tests)

1. Full startup: supervisor creates bridge -> acquires probe -> transfers to registry -> loads -> releases
2. Recovery path: acquires independently when no startup token exists
3. Memory thrash blocks acquisition -> clears -> acquisition succeeds
4. Full startup sequence with ECAPA in DEFERRED_COMPONENTS gate
5. Concurrent MODEL_LOAD: second requester blocks, resumes in order, no token leak
6. Crash-resume: token in REUSED with stale heartbeat reclaimed, next recovery succeeds
7. Contract mismatch: non-retryable rejection, waits for version change event

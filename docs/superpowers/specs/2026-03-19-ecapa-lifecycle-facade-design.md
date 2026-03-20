# ECAPA Lifecycle Facade Design Spec

**Date:** 2026-03-19
**Status:** Proposed
**Approach:** C (Facade + Registry)

## Problem Statement

The ECAPA-TDNN speaker verification subsystem has 13 independent model load paths,
5 separate state tracking systems, and 17+ consumer modules — with no single
authoritative owner. This causes:

- Duplicate model loads competing for ~700MB RAM on a 16GB Mac
- Scattered state (policy dict in supervisor, engine metrics in registry, circuit
  breakers in cloud client, 4 environment variables, 2 background tasks)
- Warning noise: one timeout cascades into 10+ unrelated warnings across phases
- Cloud SQL gating ECAPA readiness, causing false DEGRADED states
- Phase 2 and Phase 4 running equivalent probe logic independently
- Race conditions between concurrent load attempts with different lock types

## Solution

Introduce `EcapaFacade` (`backend/core/ecapa_facade.py`) as the **single
authoritative ECAPA lifecycle owner**. The facade owns all state transitions,
backend selection, health monitoring, and tier policy. `MLEngineRegistry` retains
its role as the model loader — the facade is its only ECAPA caller.

All 13 direct load paths are eliminated. All 17+ consumers call facade APIs.
~600 lines of ECAPA plumbing are removed from `unified_supervisor.py`.

## Architecture

```
unified_supervisor.py
  |
  |-- creates EcapaFacade (once, at boot)
  |     |
  |     |-- owns: state machine, tier policy, backend selection,
  |     |         health hysteresis, telemetry, capability contract
  |     |
  |     |-- delegates model loading to:
  |     |     MLEngineRegistry.get_wrapper("ecapa_tdnn")
  |     |
  |     |-- delegates cloud extraction to:
  |     |     CloudECAPAClient (existing, no policy logic)
  |     |
  |     |-- exposes to consumers:
  |           extract_embedding(), ensure_ready(), check_capability()
  |
  |-- passes facade reference to all voice subsystems
```

## State Machine

### States

| State | Meaning | Entry Condition |
|-------|---------|-----------------|
| `UNINITIALIZED` | Facade created, no work started | Construction |
| `PROBING` | Discovering available backends | `start()` called |
| `LOADING` | Loading model on selected backend | Probe found viable backend |
| `READY` | At least one backend healthy and serving | Load succeeded or cloud responding |
| `DEGRADED` | Backend intermittently failing | M consecutive failures from READY |
| `UNAVAILABLE` | No backend responding | All backends failed |
| `RECOVERING` | Re-probing after failure period | Background reprobe finds candidate |

### Legal Transitions

```
UNINITIALIZED ---start()-----------> PROBING
PROBING --------backend_found------> LOADING
PROBING --------no_backends--------> UNAVAILABLE
LOADING --------load_ok------------> READY
LOADING --------load_fail----------> UNAVAILABLE  (no cloud fallback)
LOADING --------cloud_ok-----------> READY        (cloud available while local loads)
READY ----------M_failures---------> DEGRADED
READY ----------stop()-------------> UNINITIALIZED
DEGRADED -------N_successes--------> READY
DEGRADED -------all_fail-----------> UNAVAILABLE
DEGRADED -------stop()-------------> UNINITIALIZED
UNAVAILABLE ----reprobe_ok---------> RECOVERING
UNAVAILABLE ----stop()-------------> UNINITIALIZED
RECOVERING -----N_successes--------> READY
RECOVERING -----probe_fail---------> UNAVAILABLE
RECOVERING -----stop()-------------> UNINITIALIZED
```

### Illegal Transitions (enforced)

- `UNINITIALIZED` to anything except `PROBING`
- `READY` directly to `UNAVAILABLE` (must pass through `DEGRADED`)
- `RECOVERING` to `DEGRADED` (succeeds to READY or fails to UNAVAILABLE)
- Any backward transition without `stop()`

### Transition Parameters

| Parameter | Env Var | Default | Purpose |
|-----------|---------|---------|---------|
| M (failure threshold) | `ECAPA_FAILURE_THRESHOLD` | 3 | Consecutive failures before READY->DEGRADED |
| N (recovery threshold) | `ECAPA_RECOVERY_THRESHOLD` | 3 | Consecutive successes before ->READY |
| Cooldown | `ECAPA_TRANSITION_COOLDOWN_S` | 10.0 | Min seconds between state transitions |
| Reprobe interval | `ECAPA_REPROBE_INTERVAL_S` | 15.0 | Base interval for background reprobe |
| Reprobe max backoff | `ECAPA_REPROBE_MAX_BACKOFF_S` | 120.0 | Max reprobe interval (exponential) |
| Reprobe budget | `ECAPA_REPROBE_BUDGET` | 20 | Max reprobe attempts before giving up |
| Probe timeout | `ECAPA_PROBE_TIMEOUT_S` | 8.0 | Per-backend probe timeout |
| Local load timeout | `ECAPA_LOCAL_LOAD_TIMEOUT_S` | 45.0 | Local model load timeout |

## Capability Matrix (Tier Contract)

| Capability | `READY` | `DEGRADED` | `UNAVAILABLE` | Reason Code |
|-----------|---------|------------|----------------|-------------|
| Full voice unlock | Yes | Cached only (1) | No | `CAP_VOICE_UNLOCK` |
| Authenticated voice commands | Yes | + secondary confirm (2) | No | `CAP_AUTH_COMMAND` |
| Non-auth voice commands | Yes | Yes | Yes | `CAP_BASIC_COMMAND` |
| New enrollment | Yes | No | No | `CAP_ENROLLMENT` |
| Continuous learning (writes) | Yes | No (read-only) | No | `CAP_LEARNING_WRITE` |
| Profile reads | Yes | Yes | Yes (cached) | `CAP_PROFILE_READ` |
| Embedding extraction | Yes | Best-effort (3) | No | `CAP_EXTRACT_EMBEDDING` |
| Password/passkey fallback | Available | Available | Required for auth | `CAP_PASSWORD_FALLBACK` |
| Queue non-urgent auth intents | N/A | N/A | Yes (replay on recovery) | `CAP_QUEUE_INTENT` |

**Footnotes:**
1. Cached unlock: embedding TTL <= 10 min, same-session binding, liveness check
   required, max 1 attempt before password fallback.
2. Secondary confirmation: voice PIN, manual confirm button, or password.
3. Best-effort: may timeout or return stale; caller must handle None.

### CapabilityCheck Response

```python
@dataclass(frozen=True)
class CapabilityCheck:
    allowed: bool
    tier: EcapaTier              # READY | DEGRADED | UNAVAILABLE
    reason_code: str             # e.g. "CAP_VOICE_UNLOCK"
    constraints: Dict[str, Any]  # e.g. {"max_attempts": 1, "ttl_s": 600}
    fallback: Optional[str]      # e.g. "password", "secondary_confirm"
    root_cause_id: Optional[str] # links to originating state transition
```

## Warning-to-State Mapping

Every warning maps to exactly one state transition. Derived warnings link to a
root_cause_id.

| Code | Message | Root/Derived | Trigger | Transition |
|------|---------|--------------|---------|------------|
| `ECAPA_W001` | Probing backends | Root | start() called | UNINITIALIZED->PROBING |
| `ECAPA_W002` | No backends discovered | Root | All probes failed | PROBING->UNAVAILABLE |
| `ECAPA_W003` | Cloud responding, local loading | Root | Cloud OK, local pending | PROBING->LOADING |
| `ECAPA_W004` | Local model loaded | Root | Local load complete | LOADING->READY |
| `ECAPA_W005` | Backend failure {backend}: {error} | Root | Extraction/probe fails | Counter increment |
| `ECAPA_W006` | Entering DEGRADED: M failures | Root | Threshold hit | READY->DEGRADED |
| `ECAPA_W007` | Recovered: N successes | Root | Recovery threshold | ->READY |
| `ECAPA_W008` | All backends failed | Root | All fail from DEGRADED | DEGRADED->UNAVAILABLE |
| `ECAPA_W009` | Reprobe found candidate | Root | Reprobe success | UNAVAILABLE->RECOVERING |
| `ECAPA_W010` | Memory pressure, deferring local | Root | RAM constrained | Stays current state |
| `ECAPA_W011` | CPU backpressure, delaying probe | Derived(W001) | CPU > threshold | Delays PROBING |
| `ECAPA_W012` | Cached unlock attempted (DEGRADED) | Derived(W006) | Unlock in DEGRADED | N/A |
| `ECAPA_W013` | Capability denied: {cap} at {tier} | Derived | Consumer denied | N/A |
| `ECAPA_W014` | Backend promoted: {old}->{new} | Root | N successes on new | Backend switch |
| `ECAPA_W015` | Reprobe budget exhausted | Root | Max reprobes reached | Stays UNAVAILABLE |
| `ECAPA_W016` | Transition cooldown suppressing | Derived | Within cooldown | No transition |

### Telemetry Event Structure

```python
@dataclass(frozen=True)
class EcapaStateEvent:
    event_id: str               # UUID
    root_cause_id: str          # UUID of originating root event
    timestamp: float            # monotonic
    warning_code: str           # ECAPA_W001..W016
    previous_state: EcapaState
    new_state: EcapaState
    tier: EcapaTier
    active_backend: Optional[str]
    reason: str                 # Human-readable
    error_class: Optional[str]  # e.g. "TimeoutError"
    latency_ms: Optional[float]
    metadata: Dict[str, Any]
```

## Facade Public API

```python
class EcapaFacade:
    # --- Lifecycle ---
    async def start(self) -> None
    async def stop(self) -> None

    # --- State ---
    @property
    def state(self) -> EcapaState
    @property
    def tier(self) -> EcapaTier
    @property
    def active_backend(self) -> Optional[str]

    # --- Operations ---
    async def extract_embedding(self, audio: bytes) -> EmbeddingResult
    async def ensure_ready(self, timeout: float = 10.0) -> bool

    # --- Consumer Contract ---
    def check_capability(self, cap: VoiceCapability) -> CapabilityCheck

    # --- Observability ---
    def get_status(self) -> Dict[str, Any]
    def subscribe(self, callback: Callable[[EcapaStateEvent], None]) -> None
```

## Concurrency Model

### Single-writer state machine
- One `asyncio.Lock` (`_state_lock`) guards all state transitions.
- State is an enum, not a mutable dict. Transitions are atomic assignments.
- No consumer can mutate state.

### In-flight deduplication
- `ensure_ready()` shares one `asyncio.Event`. Concurrent callers await same event.
- `extract_embedding()` does NOT dedupe (each call has distinct audio input).
- `start()` is idempotent.

### Cancellation safety
- `stop()` cancels all background tasks via `task.cancel()` +
  `asyncio.gather(*tasks, return_exceptions=True)`.
- Background tasks catch `CancelledError` to release resources.
- No orphan model loads.

### Process-level fencing
- Singleton via `_facade_instance` + `asyncio.Lock` at module level.
- `get_ecapa_facade()` factory function.
- Second instance detected via `_ECAPA_FACADE_PID` env var and raises.

### Backpressure
- Embedding requests bounded by `asyncio.Semaphore(ECAPA_MAX_CONCURRENT_EXTRACTIONS)`
  (default: 4).
- If full, caller gets `EcapaOverloadError` with retry-after hint.

## Consumer Migration Plan

### Migration categories

**Direct ECAPA loaders (DELETE load code, route through facade):**
- `parallel_initializer.py` (load_ecapa_heavy)
- `parallel_model_loader.py` (load_ecapa_encoder, load_all_voice_models)
- `budgeted_loaders.py` (EcapaBudgetedLoader)
- `unified_voice_cache_manager.py` (2 paths)
- `process_isolated_ml_loader.py` (_worker_speechbrain_loader ECAPA path)
- `ecapa_cloud_service.py` (2 paths: subprocess + main process)

**Primary consumers (REWIRE to facade):**
- `speaker_verification_service.py`
- `intelligent_voice_unlock_service.py`
- `voice_biometric_intelligence.py`

**Secondary consumers (REWIRE to facade):**
- `parallel_vbi_orchestrator.py`
- `vbi_debug_tracer.py`
- `speaker_aware_command_handler.py`
- `mode_dispatcher.py`
- `enroll_voice.py`
- `neural_mesh/adapters/voice_adapter.py`

**Supervisor (GUT ~600 lines):**
- Remove `_ecapa_policy` dict and all policy methods
- Remove `_apply_ecapa_policy`, `_verify_ecapa_pipeline`
- Remove `_ecapa_reprobe_task`, `_ecapa_cloud_warmup_task`
- Replace with: `self._ecapa_facade = EcapaFacade(...); await self._ecapa_facade.start()`

**CLI scripts (KEEP as-is):**
- `prebake_model.py`, `compile_model.py` (offline tools, not runtime)

**MLEngineRegistry (SCOPE reduction):**
- Keeps `ECAPATDNNWrapper` for model loading.
- Facade calls `registry.get_wrapper("ecapa_tdnn").load()` / `.extract()`.
- Registry loses all policy/probe/routing logic.

### Cloud SQL Decoupling

Cloud SQL no longer gates ECAPA readiness. The facade probes ECAPA backends
independently. Cloud SQL status is a post-readiness enrichment: the facade
subscribes to Cloud SQL events and triggers profile loading when DB becomes
available, but never blocks ECAPA READY state on it.

## SLOs

| Metric | Target | Measurement |
|--------|--------|-------------|
| First-usable ECAPA | <= 10s (p95) | Time from `start()` to first successful extraction |
| Local promotion | <= 45s when resources allow | Time from cloud-serving to local READY |
| Startup blocking | 0s | Facade never blocks supervisor startup |
| Warning noise | <= 3 warnings per state transition | Count warnings per root_cause_id |

## Test Plan

| Test | Validates | Mock Setup |
|------|-----------|------------|
| `test_concurrent_ensure_ready` | In-flight dedupe | Slow mock backend (1s) |
| `test_flapping_hysteresis` | N/M thresholds | Alternating success/fail |
| `test_cloud_unavailable_local_fallback` | Cloud fail -> local | Cloud mock 503 |
| `test_local_unavailable_cloud_fallback` | Local fail -> cloud | Local mock ImportError |
| `test_both_unavailable` | PROBING -> UNAVAILABLE -> reprobe | All mocks fail |
| `test_backend_crash_after_ready` | READY -> DEGRADED -> RECOVERING -> READY | Mock crash after 5 calls |
| `test_startup_cancellation` | stop() during LOADING | Cancel after 100ms |
| `test_restart_consistency` | stop() -> start() -> READY | Full lifecycle |
| `test_memory_pressure_defer` | RAM high -> defer local | Mock quantizer CONSTRAINED |
| `test_capability_check_per_tier` | Tier capability matrix | Force each tier |
| `test_backpressure_semaphore` | 5th extraction -> overload error | 4 slow extractions |
| `test_state_event_telemetry` | Events emitted with root_cause_id | Subscribe + trigger |
| `test_singleton_fencing` | Second instance raises | Call factory twice |
| `test_illegal_transition_rejected` | READY->UNAVAILABLE blocked | Direct manipulation |

# ECAPA Lifecycle Facade Design Spec

**Date:** 2026-03-19
**Status:** Proposed
**Approach:** C (Facade + Registry)
**Review:** v2 — addresses all 6 critical gaps from spec review

## Problem Statement

The ECAPA-TDNN speaker verification subsystem has 13 independent model load paths,
5 separate state tracking systems, and 27+ consumer modules — with no single
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

All 13 direct load paths are eliminated. All consumers call facade APIs.
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

## Core Type Definitions

```python
import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
import numpy as np

class EcapaState(enum.Enum):
    """Internal lifecycle states (7 states)."""
    UNINITIALIZED = "uninitialized"
    PROBING = "probing"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"

class EcapaTier(enum.Enum):
    """Consumer-facing capability tiers (3 tiers).
    Derived from EcapaState — consumers never see internal states."""
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

# State-to-Tier mapping (consumers see tiers, not states):
STATE_TO_TIER = {
    EcapaState.UNINITIALIZED: EcapaTier.UNAVAILABLE,
    EcapaState.PROBING: EcapaTier.UNAVAILABLE,
    EcapaState.LOADING: EcapaTier.UNAVAILABLE,
    EcapaState.READY: EcapaTier.READY,
    EcapaState.DEGRADED: EcapaTier.DEGRADED,
    EcapaState.UNAVAILABLE: EcapaTier.UNAVAILABLE,
    EcapaState.RECOVERING: EcapaTier.UNAVAILABLE,
}

class VoiceCapability(enum.Enum):
    """Capabilities that consumers can check against the current tier."""
    VOICE_UNLOCK = "CAP_VOICE_UNLOCK"
    AUTH_COMMAND = "CAP_AUTH_COMMAND"
    BASIC_COMMAND = "CAP_BASIC_COMMAND"
    ENROLLMENT = "CAP_ENROLLMENT"
    LEARNING_WRITE = "CAP_LEARNING_WRITE"
    PROFILE_READ = "CAP_PROFILE_READ"
    EXTRACT_EMBEDDING = "CAP_EXTRACT_EMBEDDING"
    PASSWORD_FALLBACK = "CAP_PASSWORD_FALLBACK"

@dataclass
class EcapaFacadeConfig:
    """All facade parameters. Reads from env vars with sane defaults."""
    failure_threshold: int = 3
    recovery_threshold: int = 3
    transition_cooldown_s: float = 10.0
    reprobe_interval_s: float = 15.0
    reprobe_max_backoff_s: float = 120.0
    reprobe_budget: int = 20
    probe_timeout_s: float = 8.0
    local_load_timeout_s: float = 45.0
    max_concurrent_extractions: int = 4
    recovering_fail_threshold: int = 2  # Failures in RECOVERING -> UNAVAILABLE

    @classmethod
    def from_env(cls) -> "EcapaFacadeConfig":
        import os
        def _int(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)))
        def _float(key: str, default: float) -> float:
            return float(os.getenv(key, str(default)))
        return cls(
            failure_threshold=_int("ECAPA_FAILURE_THRESHOLD", 3),
            recovery_threshold=_int("ECAPA_RECOVERY_THRESHOLD", 3),
            transition_cooldown_s=_float("ECAPA_TRANSITION_COOLDOWN_S", 10.0),
            reprobe_interval_s=_float("ECAPA_REPROBE_INTERVAL_S", 15.0),
            reprobe_max_backoff_s=_float("ECAPA_REPROBE_MAX_BACKOFF_S", 120.0),
            reprobe_budget=_int("ECAPA_REPROBE_BUDGET", 20),
            probe_timeout_s=_float("ECAPA_PROBE_TIMEOUT_S", 8.0),
            local_load_timeout_s=_float("ECAPA_LOCAL_LOAD_TIMEOUT_S", 45.0),
            max_concurrent_extractions=_int("ECAPA_MAX_CONCURRENT_EXTRACTIONS", 4),
            recovering_fail_threshold=_int("ECAPA_RECOVERING_FAIL_THRESHOLD", 2),
        )

@dataclass(frozen=True)
class EmbeddingResult:
    """Result of an embedding extraction request."""
    embedding: Optional[np.ndarray]  # 192-dim ECAPA-TDNN, None on failure
    backend: str                     # "local" | "cloud_run" | "docker"
    latency_ms: float
    from_cache: bool
    dimension: int                   # Expected 192
    error: Optional[str]             # None on success

    @property
    def success(self) -> bool:
        return self.embedding is not None and self.error is None

@dataclass(frozen=True)
class CapabilityCheck:
    """Result of a capability check against the current tier."""
    allowed: bool
    tier: EcapaTier
    reason_code: str
    constraints: Dict[str, Any]
    fallback: Optional[str]          # "password", "secondary_confirm", None
    root_cause_id: Optional[str]

@dataclass(frozen=True)
class EcapaStateEvent:
    """Emitted on every state transition and notable operational event."""
    event_id: str
    root_cause_id: str
    timestamp: float
    warning_code: str
    previous_state: EcapaState
    new_state: EcapaState
    tier: EcapaTier
    active_backend: Optional[str]
    reason: str
    error_class: Optional[str]
    latency_ms: Optional[float]
    metadata: Dict[str, Any]
```

## Error Model

```python
class EcapaError(Exception):
    """Base exception for all facade errors."""
    pass

class EcapaUnavailableError(EcapaError):
    """Raised by extract_embedding() when tier is UNAVAILABLE."""
    pass

class EcapaOverloadError(EcapaError):
    """Raised when backpressure semaphore is full."""
    def __init__(self, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__(f"ECAPA overloaded, retry after {retry_after_s:.1f}s")

class EcapaTimeoutError(EcapaError):
    """Raised when an extraction exceeds its per-request timeout."""
    pass
```

**API error contract:**
- `extract_embedding()` raises `EcapaUnavailableError` if tier is UNAVAILABLE,
  `EcapaOverloadError` if semaphore full, `EcapaTimeoutError` if backend times out.
  Returns `EmbeddingResult(error=...)` for backend-level failures that don't
  warrant an exception (partial degradation).
- `ensure_ready()` never raises. Returns `True` for READY/DEGRADED, `False` on
  timeout (still UNAVAILABLE/PROBING/LOADING/RECOVERING after deadline).
- `check_capability()` never raises. Always returns a `CapabilityCheck`.

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
PROBING --------cloud_immediate----> READY         (cloud responds during probe)
PROBING --------no_backends--------> UNAVAILABLE
LOADING --------load_ok------------> READY
LOADING --------load_fail----------> UNAVAILABLE   (no cloud fallback)
LOADING --------cloud_ok-----------> READY         (cloud available while local loads)
READY ----------M_failures---------> DEGRADED
READY ----------backend_switch-----> READY         (intra-state, W014)
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

**Probe sequence clarification:** `start()` probes all backends concurrently
(local, cloud_run, docker). If cloud responds first within `ECAPA_PROBE_TIMEOUT_S`,
the facade transitions `PROBING -> READY` immediately with cloud as active backend.
Local loading continues in background. When local load completes, a backend
promotion occurs (intra-state `READY -> READY` with backend switch, emitting
`ECAPA_W014`). If no backend responds, `PROBING -> UNAVAILABLE`.

### Illegal Transitions (enforced)

- `UNINITIALIZED` to anything except `PROBING`
- `READY` directly to `UNAVAILABLE` (must pass through `DEGRADED`)
- `RECOVERING` to `DEGRADED` (succeeds to READY or fails to UNAVAILABLE)
- Any backward transition without `stop()`

**RECOVERING exit criteria:** In RECOVERING state, the facade tests the candidate
backend. If `recovering_fail_threshold` (default: 2) consecutive failures occur,
the facade transitions back to UNAVAILABLE and decrements the reprobe budget.
There is no maximum time in RECOVERING — the exit is always via success count
(N successes -> READY) or failure count (recovering_fail_threshold failures ->
UNAVAILABLE).

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

## Backend Selection Policy

### Probe Order
All backends are probed concurrently at startup. First healthy backend wins.

### Adaptive Priority (no hardcoded preference)
At startup, the facade evaluates:
1. Available RAM (via memory quantizer): if CONSTRAINED+, skip local, prefer cloud
2. Cloud endpoint configured (env var present): probe cloud_run and docker
3. Local SpeechBrain available (importable): probe local

### Promotion Criteria
A non-active backend is promoted to active when:
- N consecutive successful extractions on the candidate (default: 3)
- Candidate latency < active backend latency * 1.5 (latency-aware, not just success)
- No transition cooldown active

### Demotion Criteria
Active backend is demoted when:
- M consecutive failures or timeouts (default: 3)
- Triggers READY -> DEGRADED transition (not just backend switch)

### Priority Tiebreaker
When multiple backends are healthy: prefer local (lower latency, no cost) unless
memory pressure is CONSTRAINED+ (then prefer cloud).

## Capability Matrix (Tier Contract)

| Capability | `READY` | `DEGRADED` | `UNAVAILABLE` | Reason Code |
|-----------|---------|------------|----------------|-------------|
| Full voice unlock | Yes | Cached only (1) | No | `CAP_VOICE_UNLOCK` |
| Authenticated voice commands | Yes | + secondary confirm (2) | No | `CAP_AUTH_COMMAND` |
| Non-auth voice commands (3) | Yes | Yes | Yes | `CAP_BASIC_COMMAND` |
| New enrollment | Yes | No | No | `CAP_ENROLLMENT` |
| Continuous learning (writes) | Yes | No (read-only) | No | `CAP_LEARNING_WRITE` |
| Profile reads (4) | Yes | Yes | Yes (cached) | `CAP_PROFILE_READ` |
| Embedding extraction | Yes | Best-effort (5) | No | `CAP_EXTRACT_EMBEDDING` |
| Password/passkey fallback | Available | Available | Required for auth | `CAP_PASSWORD_FALLBACK` |

**Footnotes:**
1. Cached unlock: embedding TTL <= 10 min, same-session binding, liveness check
   required, max 1 attempt before password fallback.
2. Secondary confirmation: voice PIN, manual confirm button, or password.
3. Basic commands do not require ECAPA (wake-word + STT only). They work regardless
   of ECAPA state.
4. Profile reads use a local in-memory/disk cache populated during previous READY
   states. On first boot (never reached READY), profile reads return empty — consumer
   must handle this gracefully.
5. Best-effort: may timeout or return EmbeddingResult with error; caller must handle.

**Note:** `CAP_QUEUE_INTENT` is deferred to v2. Non-urgent auth intent queuing
requires a durable queue specification (max depth, TTL, replay ordering, persistence
across restarts) that is out of scope for the initial facade.

### CapabilityCheck Response

```python
@dataclass(frozen=True)
class CapabilityCheck:
    allowed: bool
    tier: EcapaTier
    reason_code: str
    constraints: Dict[str, Any]      # e.g. {"max_attempts": 1, "ttl_s": 600}
    fallback: Optional[str]          # "password", "secondary_confirm", None
    root_cause_id: Optional[str]
```

## Warning-to-State Mapping

### Transition Warnings (map to state changes)

| Code | Message | Root/Derived | Trigger | Transition |
|------|---------|--------------|---------|------------|
| `ECAPA_W001` | Probing backends | Root | start() called | UNINITIALIZED->PROBING |
| `ECAPA_W002` | No backends discovered | Root | All probes failed | PROBING->UNAVAILABLE |
| `ECAPA_W003` | Cloud responding, local loading in bg | Root | Cloud OK during probe | PROBING->READY (cloud) |
| `ECAPA_W004` | Local model loaded | Root | Local load complete | LOADING->READY or promotion |
| `ECAPA_W005` | Backend failure {backend}: {error} | Root | Extraction/probe fails | Counter increment |
| `ECAPA_W006` | Entering DEGRADED: M failures | Root | Threshold hit | READY->DEGRADED |
| `ECAPA_W007` | Recovered: N successes | Root | Recovery threshold | ->READY |
| `ECAPA_W008` | All backends failed | Root | All fail from DEGRADED | DEGRADED->UNAVAILABLE |
| `ECAPA_W009` | Reprobe found candidate | Root | Reprobe success | UNAVAILABLE->RECOVERING |
| `ECAPA_W014` | Backend promoted: {old}->{new} | Root | N successes on new | READY->READY (intra-state) |
| `ECAPA_W015` | Reprobe budget exhausted | Root | Max reprobes reached | Stays UNAVAILABLE |

### Operational Warnings (no state change)

| Code | Message | Root/Derived | Trigger | Context |
|------|---------|--------------|---------|---------|
| `ECAPA_W010` | Memory pressure, deferring local | Root | RAM constrained | Local load deferred |
| `ECAPA_W011` | CPU backpressure, delaying probe | Derived(W001) | CPU > threshold | Probe delayed |
| `ECAPA_W012` | Cached unlock attempted (DEGRADED) | Derived(W006) | Unlock in DEGRADED | Operational event |
| `ECAPA_W013` | Capability denied: {cap} at {tier} | Derived | Consumer denied | Operational event |
| `ECAPA_W016` | Transition cooldown suppressing | Derived | Within cooldown | No transition |

### Telemetry Event Structure

```python
@dataclass(frozen=True)
class EcapaStateEvent:
    event_id: str
    root_cause_id: str               # Self for root events, parent for derived
    timestamp: float
    warning_code: str
    previous_state: EcapaState
    new_state: EcapaState            # Same as previous for operational warnings
    tier: EcapaTier
    active_backend: Optional[str]
    reason: str
    error_class: Optional[str]
    latency_ms: Optional[float]
    metadata: Dict[str, Any]
```

## Facade Public API

```python
class EcapaFacade:
    def __init__(
        self,
        registry: "MLEngineRegistry",
        cloud_client: Optional["CloudECAPAClient"] = None,
        config: Optional["EcapaFacadeConfig"] = None,
    ) -> None:
        """
        Args:
            registry: MLEngineRegistry for local model loading (required).
            cloud_client: CloudECAPAClient for cloud extraction (optional).
            config: Override default thresholds/timeouts (optional, reads env).
        """

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
    def subscribe(self, callback: Callable[[EcapaStateEvent], None]) -> Callable[[], None]
```

**`ensure_ready()` semantics:**
- Returns `True` if tier is READY or DEGRADED (extraction is possible).
- Returns `False` if timeout expires and tier is still UNAVAILABLE.
- Blocks only from UNINITIALIZED/PROBING/LOADING/RECOVERING states.
- Concurrent callers share one `asyncio.Event` (in-flight dedupe).

**`subscribe()` semantics:**
- Callbacks are dispatched via `asyncio.create_task()` outside the state lock.
- Fire-and-forget with exception logging (subscriber errors don't crash facade).
- Returns an unsubscribe callable.

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
- Fencing via PID file (`~/.jarvis/ecapa_facade.pid`) with stale-PID detection
  (check if PID is alive via `os.kill(pid, 0)`). Env vars are inherited by
  subprocesses and are unsuitable for fencing.
- Subprocess consumers (e.g. `process_isolated_ml_loader.py`) do NOT instantiate
  the facade. They either call the parent process via IPC or are eliminated during
  migration (facade replaces their role).

### Backpressure
- Embedding requests bounded by `asyncio.Semaphore(ECAPA_MAX_CONCURRENT_EXTRACTIONS)`
  (default: 4).
- If full, caller gets `EcapaOverloadError` with retry-after hint.

## Consumer Migration Plan

### Phased Migration Strategy

Migration is phased to minimize blast radius. Each phase is independently
deployable and reversible.

**Feature flag:** `ECAPA_USE_FACADE` (env var, default: `true` after Phase 3).
When `false`, old code paths remain active. Facade runs in shadow mode (probes
and logs but does not serve consumers). This allows bake-in before cutting over.

**Phase 1: Introduce facade (no consumer changes)**
- Create `backend/core/ecapa_facade.py` with full state machine
- Supervisor creates facade at boot, runs in shadow mode alongside old code
- Validate: facade reaches READY, telemetry emits correctly
- Risk: Zero (additive only)

**Phase 2: Migrate primary consumers (3 files)**
- `speaker_verification_service.py`
- `intelligent_voice_unlock_service.py`
- `voice_biometric_intelligence.py`
- Behind `ECAPA_USE_FACADE` flag — old path as fallback
- Validate: voice unlock works end-to-end via facade

**Phase 3: Migrate secondary consumers (13 files)**
- `parallel_vbi_orchestrator.py`
- `vbi_debug_tracer.py`
- `speaker_aware_command_handler.py`
- `mode_dispatcher.py`
- `enroll_voice.py`
- `neural_mesh/adapters/voice_adapter.py`
- `startup_integration.py`
- `ml_model_prewarmer.py`
- `speaker_recognition.py`
- `intelligent_voice_router.py`
- `voice_transparency_engine.py`
- `voice_experience_collector.py`
- `drift_detector.py`
- Flip `ECAPA_USE_FACADE` default to `true`
- Validate: all voice features work, no direct ECAPA imports remain

**Phase 4: Delete old load paths (8 files)**
- Delete direct `safe_from_hparams` ECAPA calls from:
  - `parallel_initializer.py` (load_ecapa_heavy)
  - `parallel_model_loader.py` (load_ecapa_encoder, load_all_voice_models)
  - `budgeted_loaders.py` (EcapaBudgetedLoader)
  - `unified_voice_cache_manager.py` (2 paths)
  - `ecapa_cloud_service.py` (subprocess + main process paths)
- Validate: only facade loads ECAPA models

**Phase 5: Gut supervisor (~600 lines)**
- Remove `_ecapa_policy` dict, `_apply_ecapa_policy`, `_verify_ecapa_pipeline`
- Remove `_ecapa_reprobe_task`, `_ecapa_cloud_warmup_task`
- Remove Phase 2 ECAPA probe duplication
- Replace with: `self._ecapa_facade = EcapaFacade(...); await self._ecapa_facade.start()`
- Validate: supervisor startup clean, no ECAPA warnings from old paths

**Phase 6: Remove feature flag, delete dead code**
- Remove `ECAPA_USE_FACADE` flag
- Remove shadow mode code
- Delete any remaining compatibility shims
- Reduce `MLEngineRegistry` ECAPA policy/routing logic

### Subprocess Consumer Strategy

`process_isolated_ml_loader.py` currently runs ECAPA in an isolated subprocess.
After migration, the facade replaces this role entirely:
- The facade's local loading path runs in `asyncio.to_thread()` (thread isolation)
- Process isolation is unnecessary when a single owner controls all loads
- If process isolation is still needed for safety, the subprocess calls the
  facade's parent process via a lightweight HTTP endpoint (existing loading_server
  infrastructure) rather than instantiating its own facade

### Files NOT migrated (keep as-is with justification)

- `prebake_model.py`, `compile_model.py` — offline CLI tools, not runtime
- `agi_os_coordinator.py` — references ECAPA only for budget allocation display,
  does not load or extract. Will read facade status via `get_status()`.
- `picovoice_integration.py` — references ECAPA in config comments only,
  no runtime dependency
- `feature_extraction.py` — mentions ECAPA embeddings in docstrings, no load path

### Cloud SQL Decoupling

Cloud SQL no longer gates ECAPA readiness. The facade probes ECAPA backends
independently. Cloud SQL status is a post-readiness enrichment: the facade
subscribes to Cloud SQL events and triggers profile loading when DB becomes
available, but never blocks ECAPA READY state on it.

## Rollback Strategy

If the migration causes issues at any phase:

1. Set `ECAPA_USE_FACADE=false` — immediately reverts to old code paths
2. Old load paths remain in codebase until Phase 4 (deletion phase)
3. Phase 4+ rollback: revert the git commits for that phase
4. Facade shadow mode continues logging even when not serving — provides
   diagnostic data without risk

No phase is irreversible until Phase 6 (flag removal). Phases 1-5 can all
be individually rolled back.

## SLOs

| Metric | Target | Measurement |
|--------|--------|-------------|
| First-usable ECAPA | <= 10s (p95) | Time from `start()` to first successful extraction |
| Local promotion | <= 45s when resources allow | Time from cloud-serving to local READY |
| Startup blocking | 0s | Facade never blocks supervisor startup |
| Warning noise | <= 3 warnings per state transition | Count warnings per root_cause_id |

## Test Plan

### State Machine Tests

| Test | Validates | Mock Setup |
|------|-----------|------------|
| `test_concurrent_ensure_ready` | In-flight dedupe — 10 concurrent calls share one future | Slow mock backend (1s) |
| `test_flapping_hysteresis` | N/M thresholds prevent flapping | Alternating success/fail |
| `test_cloud_unavailable_local_fallback` | Cloud fail -> local | Cloud mock 503 |
| `test_local_unavailable_cloud_fallback` | Local fail -> cloud | Local mock ImportError |
| `test_both_unavailable` | PROBING -> UNAVAILABLE -> reprobe | All mocks fail |
| `test_backend_crash_after_ready` | READY -> DEGRADED -> RECOVERING -> READY | Mock crash after 5 calls |
| `test_startup_cancellation` | stop() during LOADING cancels cleanly | Cancel after 100ms |
| `test_restart_consistency` | stop() -> start() -> READY | Full lifecycle |
| `test_memory_pressure_defer` | RAM high -> defer local | Mock quantizer CONSTRAINED |
| `test_capability_check_per_tier` | Tier capability matrix correct for all tiers | Force state to each tier |
| `test_backpressure_semaphore` | 5th concurrent extraction raises EcapaOverloadError | 4 slow extractions in flight |
| `test_state_event_telemetry` | Events emitted with root_cause_id | Subscribe + trigger transitions |
| `test_singleton_fencing` | Second instance raises | Call factory twice |
| `test_illegal_transition_rejected` | READY->UNAVAILABLE blocked | Direct state manipulation |
| `test_cloud_sql_down_ecapa_ready` | Cloud SQL unavailable, ECAPA still reaches READY | Mock Cloud SQL unreachable |
| `test_warning_noise_bounded` | <= 3 warnings per root_cause_id | Trigger transition, count warnings |

### Integration Tests (consumer migration validation)

| Test | Validates | Scope |
|------|-----------|-------|
| `test_speaker_verification_uses_facade` | SpeakerVerificationService calls facade | Phase 2 |
| `test_voice_unlock_uses_facade` | Voice unlock pipeline uses facade | Phase 2 |
| `test_supervisor_no_ecapa_policy` | No _ecapa_policy in supervisor | Phase 5 |
| `test_no_direct_ecapa_imports` | grep for direct safe_from_hparams ECAPA calls = 0 (except CLI) | Phase 4 |

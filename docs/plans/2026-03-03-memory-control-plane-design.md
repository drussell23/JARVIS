# Memory Control Plane — Design Document

**Date:** 2026-03-03
**Approach:** B+A Hybrid (New MemoryBudgetBroker consuming MemoryQuantizer as signal source)
**Scope:** Centralized memory admission control across all model loaders in JARVIS ecosystem
**Prerequisite for:** Trinity Autonomy Wiring (Phase 2)

---

## Context

### The Problem

On 2026-03-03, the JARVIS ecosystem caused an 81.56 GB virtual memory spike on a 16 GB
Apple Silicon Mac, triggering macOS Force Quit. Root cause: no centralized admission
control over concurrent model loading. Multiple subsystems (LLM, Whisper, ECAPA-TDNN,
SentenceTransformer x13+ instances) all read "available memory" at the same instant
(before any had allocated), then all allocated simultaneously.

### Current State

- `MemoryQuantizer` is the best signal source (macOS pressure, swap, thrash detection)
  but only the LLM loader uses its reservation system
- `ProactiveResourceGuard` and `IntelligentMLMemoryManager` are separate, uncoordinated
  budget systems using raw `psutil` (not macOS-aware)
- 13+ `SentenceTransformer` bypass sites fire with no memory gate during parallel startup
- Voice model parallel loader has zero memory pre-checks
- Startup `main.py` creates a fresh `MemoryQuantizer()` instance instead of the singleton

### Architecture Decision

- **Design for cross-process from day one** (canonical protocol + leases + broker abstraction)
- **Implement single-process broker first** (fastest path to stop crashes)
- **Flip to cross-process transport later** without changing callers

---

## 1. Broker API & Lease Lifecycle

### Core Interface

```python
class MemoryBudgetBroker:
    """Single admission authority for all memory-intensive operations.

    No model load path (LLM, Whisper, ECAPA, SentenceTransformer, warmups)
    may allocate without a broker grant.
    """

    async def request(
        self,
        component: str,            # e.g. "llm:mistral-7b-q4@v1"
        bytes_requested: int,      # from estimate_bytes(), calibrated by broker
        priority: BudgetPriority,  # BOOT_CRITICAL > BOOT_OPTIONAL > RUNTIME_INTERACTIVE > BACKGROUND
        phase: StartupPhase,       # current lifecycle phase
        *,
        ttl_seconds: float = 120.0,
        can_degrade: bool = False,
        degradation_options: Optional[List[DegradationOption]] = None,
        deadline: Optional[float] = None,
    ) -> BudgetGrant:
        """Block until grant is issued, degraded grant is offered, or deadline expires."""

    async def try_request(self, ...) -> Optional[BudgetGrant]:
        """Non-blocking: returns grant or None immediately."""

    def set_phase(self, phase: StartupPhase) -> None:
        """Called by supervisor to advance lifecycle phase."""

    def get_committed_bytes(self) -> int:
        """Sum of all ACTIVE lease actual_bytes."""
```

### Grant Object (Transactional)

```python
@dataclass
class BudgetGrant:
    lease_id: str
    component_id: str             # versioned: "llm:mistral-7b-q4@v1"
    granted_bytes: int
    degraded: bool
    degradation_applied: Optional[str]
    constraints: Dict[str, Any]   # machine-readable; loader must honor
    constraints_schema_version: str
    phase: StartupPhase
    priority: BudgetPriority
    expires_at: float             # monotonic
    created_at: float
    trace_id: str
    causal_parent_id: Optional[str]
    epoch: int                    # supervisor run ID

    async def heartbeat(self) -> None:
        """Extend TTL. Must call periodically during long loads."""

    async def commit(self, actual_bytes: int, config_proof: ConfigProof) -> None:
        """Load succeeded. Broker validates proof, transitions GRANTED -> ACTIVE."""

    async def rollback(self, reason: str) -> None:
        """Load failed. Release all reserved capacity. Idempotent."""

    async def release(self) -> None:
        """Long-lived usage complete. Transitions ACTIVE -> RELEASED. Idempotent."""

    async def __aenter__(self) -> 'BudgetGrant': ...
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Auto-rollback on exception. Warning if neither commit nor rollback called."""
```

All lease operations (`commit`, `rollback`, `heartbeat`, `release`) are idempotent
for retry-safe behavior.

### Lease State Machine

```
PENDING -> GRANTED -> ACTIVE (commit finalized, reservation tracked)
                   -> ROLLED_BACK (terminal, capacity released)
                   -> EXPIRED (terminal, auto-rollback + warning telemetry)
                   -> PREEMPTED (terminal, forced unload by broker)
PENDING -> DENIED (terminal, capacity never reserved)
PENDING -> DEGRADED_GRANT -> ACTIVE | ROLLED_BACK | EXPIRED | PREEMPTED
ACTIVE -> RELEASED (terminal, capacity freed after verified reclaim)
ACTIVE -> PREEMPTED (terminal, forced unload for higher-priority need)
```

Key: `COMMITTED` is not a terminal state. `commit()` transitions to `ACTIVE`.
`ACTIVE` persists until explicit `release()` or preemption.

### Overrun Policy

If `actual_bytes > granted_bytes` at `commit()` time:
- Broker accepts the commit (model is already loaded, rollback would waste the work)
- Emits `COMMIT_OVERRUN` telemetry with delta
- Triggers emergency mode assessment on next snapshot
- Forces degradation on next load cycle for that component
- Updates estimate calibrator with overrun data

### Phase Policy Table

| Phase | Max Concurrent Grants | Budget Cap | Allowed Priorities |
|---|---|---|---|
| `BOOT_CRITICAL` | 1 | 60% physical × pressure_factor | BOOT_CRITICAL only |
| `BOOT_OPTIONAL` | 2 | 70% physical × pressure_factor | BOOT_CRITICAL, BOOT_OPTIONAL |
| `RUNTIME_INTERACTIVE` | 3 | 80% physical × pressure_factor | All except BACKGROUND |
| `BACKGROUND` | 2 | 70% physical × pressure_factor | All |

Budget caps are percentages of **physical RAM**, multiplied by the dynamic
`pressure_factor` from the current `MemorySnapshot`, minus already-committed
reservations, minus `safety_floor_bytes`, minus kernel-wired and compressed
trend reserve.

Phase transitions are owned by the supervisor and communicated via
`broker.set_phase(phase)`. Policy only relaxes after sustained healthy state,
never during startup.

### Degradation Graph

```python
@dataclass
class DegradationOption:
    name: str             # e.g. "reduce_context_2048", "smaller_quant"
    bytes_required: int   # reduced footprint
    quality_impact: str   # human-readable for telemetry
    constraints: Dict[str, Any]  # machine-readable; loader must apply and prove
```

Broker tries options in order when `can_degrade=True` and full request exceeds headroom.
Grant carries `constraints` dict; loader returns `ConfigProof` in `commit()` proving
constraints were honored.

### Priority & Backpressure

- **Queue isolation per priority class** — separate queues for each `BudgetPriority`
  to avoid starvation under bursty workloads
- Sorted within class by `created_at` (oldest first)
- **Priority aging:** requests waiting >30s gain +1 priority level
- **Deadline enforcement:** queued past `deadline` -> `DENIED` with `reason="deadline_exceeded"`
- **Priority inversion guard:** `BOOT_CRITICAL` arrival can preempt `BACKGROUND` grants
  (via registered `can_preempt` callback with 10s ACK timeout)

### Three-Stage Preemption

1. **Cooperative cancel** (5s): send cancel token to loader; loader should begin teardown
2. **Timed unload** (10s): call `release_handle()` callback with timeout
3. **Forced teardown**: `del model_handle` + `gc.collect()` + transition to `PREEMPTED`

If component ignores cooperative cancel and timed unload, forced teardown proceeds
and loader is flagged for quarantine review.

### Swap Hysteresis

```python
class SwapHysteresisPolicy:
    SWAP_GROWTH_THRESHOLD_BPS = 50 * 1024 * 1024  # 50 MB/s
    RECOVERY_WINDOW_SECONDS = 60
    TIGHTENED_BUDGET_MULTIPLIER = 0.7
```

When tripped: all phase budget caps multiplied by 0.7, no new `BACKGROUND` grants,
existing `BACKGROUND` grants get preemption warning. Only relaxes after swap growth
rate stays below threshold for 60s continuously.

### Epoch Fence

Every grant carries the supervisor `epoch` (run ID). Stale grants from prior boots
cannot be used — all operations validate `grant.epoch == broker._current_epoch`.

---

## 2. Canonical MemorySnapshot & Signal Contract

### Design Principle

**One immutable object type flows through every decision path.** Zero direct `psutil`
calls outside `MemoryQuantizer` internals.

### MemorySnapshot Schema

```python
@dataclass(frozen=True)
class MemorySnapshot:
    """Immutable point-in-time memory state. Single source of truth."""

    # --- Physical truth (bytes) ---
    physical_total: int
    physical_wired: int
    physical_active: int
    physical_inactive: int
    physical_compressed: int
    physical_free: int

    # --- Swap state ---
    swap_total: int
    swap_used: int
    swap_growth_rate_bps: float   # bytes/sec, EMA-smoothed 10s window

    # --- Derived budget fields ---
    usable_bytes: int             # physical_total - wired - compressed_trend
    committed_bytes: int          # sum of all ACTIVE leases
    available_budget_bytes: int   # usable_bytes - committed_bytes

    # --- Pressure signals ---
    kernel_pressure: KernelPressure   # NORMAL | WARN | CRITICAL
    pressure_tier: PressureTier       # ABUNDANT..EMERGENCY (typed enum)
    thrash_state: ThrashState         # HEALTHY | THRASHING | EMERGENCY
    pageins_per_sec: float

    # --- Trend derivatives (30s window) ---
    host_rss_slope_bps: float         # total host RSS growth rate
    jarvis_tree_rss_slope_bps: float  # JARVIS process tree RSS growth
    swap_slope_bps: float
    pressure_trend: PressureTrend     # STABLE | RISING | FALLING

    # --- Safety ---
    safety_floor_bytes: int           # dynamic floor, scales with pressure tier
    compressed_trend_bytes: int       # EMA alpha=0.15, horizon=30s

    # --- Signal quality ---
    signal_quality: SignalQuality     # GOOD | DEGRADED | FALLBACK

    # --- Metadata ---
    timestamp: float                  # monotonic clock
    max_age_ms: int                   # staleness threshold for this snapshot's use case
    epoch: int                        # supervisor run ID
    snapshot_id: str                  # UUID for traceability
```

### Key Properties

```python
    @property
    def headroom_bytes(self) -> int:
        """How much can actually be granted right now."""
        return max(0, self.available_budget_bytes - self.safety_floor_bytes)

    @property
    def pressure_factor(self) -> float:
        """Dynamic multiplier for phase caps. 1.0 = healthy, 0.3 = emergency."""
        return {
            PressureTier.ABUNDANT: 1.0, PressureTier.OPTIMAL: 0.95,
            PressureTier.ELEVATED: 0.85, PressureTier.CONSTRAINED: 0.7,
            PressureTier.CRITICAL: 0.5, PressureTier.EMERGENCY: 0.3,
        }.get(self.pressure_tier, 0.5)

    @property
    def swap_hysteresis_active(self) -> bool:
        return self.swap_growth_rate_bps > 50 * 1024 * 1024
```

### Safety Floor Calculation

Dynamic floor that scales with system fragility:

| Pressure Tier | Floor (16 GB machine) |
|---|---|
| ABUNDANT | 1.6 GB (10%) |
| OPTIMAL | 1.6 GB |
| ELEVATED | 2.0 GB (12.5%) |
| CONSTRAINED | 2.4 GB (15%) |
| CRITICAL | 3.2 GB (20%) |
| EMERGENCY | 4.0 GB (25%) |

Note: `usable_bytes` does NOT subtract safety floor. Safety floor is applied only
in `headroom_bytes` and broker cap calculations to avoid double-subtraction.

### Signal Collection (MemoryQuantizer internals only)

```python
class MemoryQuantizer:
    """ONLY class allowed to call psutil, vm_stat, memory_pressure."""

    async def snapshot(self, max_age_ms: int = 0) -> MemorySnapshot:
        """Produce canonical snapshot. Called by broker, never by loaders."""
```

Signals collected: `memory_pressure` kernel command (primary), psutil wired+active+compressed
(secondary), `vm_stat` pageins/sec (thrash), swap growth rate (EMA).

### Signal Collection Failure

If `memory_pressure` or `vm_stat` fails:
- `signal_quality` set to `DEGRADED` or `FALLBACK`
- `DEGRADED`: caps `pressure_factor` at 0.7, blocks new BACKGROUND grants
- `FALLBACK`: caps `pressure_factor` at 0.5, blocks BACKGROUND and RUNTIME_INTERACTIVE

### Snapshot Staleness Contract

| Context | Max Age | Action on Stale |
|---|---|---|
| Grant evaluation | 0ms (fresh) | Always take fresh snapshot |
| Queued request re-eval | 2000ms | Use cached if within window |
| Telemetry/dashboard | 5000ms | Use cached |
| Heartbeat validation | 10000ms | Use cached |

Broker hard-rejects snapshots older than `max_age_ms` for grant decisions.
Runtime guard validates `snapshot.epoch == broker._current_epoch`.

### Tier Flap Dampener

Minimum 15s dwell time before tier transitions take effect in broker caps.
Prevents rapid oscillation between CONSTRAINED/CRITICAL under bursty loads.

### Enforcement

**Lint rule (CI):** Ban `psutil.virtual_memory()` and `psutil.swap_memory()` outside
`memory_quantizer.py`. Grep as fast gate + AST-based checker for bypass-proof enforcement.

### What Gets Retired

| Component | Replacement |
|---|---|
| `ProactiveResourceGuard.request_memory_budget()` | `MemoryBudgetBroker.request()` |
| `IntelligentMLMemoryManager._can_load_model()` | Broker grant required |
| `IntelligentMLMemoryManager` monitor loop | Broker preemption via tier callbacks |
| `MemoryQuantizer.reserve_memory()` (v266.0) | Subsumed by broker lease lifecycle |
| Direct `psutil.virtual_memory()` in 6+ files | `snapshot.headroom_bytes` via broker |
| `main.py` fresh `MemoryQuantizer()` instance | Broker singleton + phase gate |

---

## 3. Loader Wiring & Bypass Elimination

### BudgetedLoader Protocol

```python
class BudgetedLoader(Protocol):
    """Contract every model loader must implement."""

    @property
    def component_id(self) -> str:
        """Versioned ID. e.g. 'llm:mistral-7b-q4@v1'"""

    @property
    def phase(self) -> StartupPhase: ...

    @property
    def priority(self) -> BudgetPriority: ...

    def estimate_bytes(self, config: Dict[str, Any]) -> int:
        """Conservative pre-grant estimate. Calibrated by broker."""

    async def load_with_grant(self, grant: BudgetGrant) -> LoadResult:
        """Execute load within grant constraints. Heavyweight imports
        must be deferred to inside this method body (not at import time)."""

    def prove_config(self, constraints: Dict[str, Any]) -> ConfigProof:
        """Machine-readable proof of constraint compliance."""

    def measure_actual_bytes(self) -> int:
        """Advisory self-report. Broker cross-checks via process tree RSS delta."""

    async def release_handle(self, reason: str) -> None:
        """Preemption/recovery: unload and free memory. Idempotent. <30s."""
```

### LoadResult & ConfigProof

```python
@dataclass
class LoadResult:
    success: bool
    actual_bytes: int
    config_proof: ConfigProof
    model_handle: Any
    load_duration_ms: float
    error: Optional[str] = None

@dataclass
class ConfigProof:
    component_id: str
    requested_constraints: Dict[str, Any]
    applied_config: Dict[str, Any]
    compliant: bool
    evidence: Dict[str, Any]
```

### Actual Bytes Measurement (Broker-Owned)

`measure_actual_bytes()` on the loader is advisory only. The broker takes pre/post
snapshots of `jarvis_tree_rss` and computes the delta. Loader self-report is recorded
as a cross-check; divergence >20% triggers telemetry warning.

### Import-Time Allocation Guard

Some libraries allocate on import or constructor side-effects. Loaders must:
- Keep heavyweight imports (`import torch`, `from llama_cpp import Llama`, etc.) inside
  `load_with_grant()` body, not at module level
- Broker validates no RSS spike >10 MB between grant issuance and `load_with_grant()` entry

### Transactional Load Flow

```
1. Loader calls broker.request(component, calibrated_estimate, priority, phase, ...)
2. Broker evaluates snapshot.headroom_bytes vs request
   - Insufficient + can_degrade: try degradation_options in order
   - Insufficient + !can_degrade: queue (with deadline) or deny
   - Sufficient: issue BudgetGrant with constraints
3. Loader enters grant context:
   async with grant:
       result = await loader.load_with_grant(grant)
       if not result.success:
           raise LoadFailedError(result.error)  # triggers __aexit__ rollback
       proof = loader.prove_config(grant.constraints)
       if not proof.compliant:
           raise ConstraintViolationError(proof)  # triggers rollback
       actual = broker.measure_jarvis_tree_rss_delta()
       await grant.commit(actual, proof)
4. Grant transitions: GRANTED -> ACTIVE (tracked reservation with actual bytes)
5. On unload/shutdown: await grant.release() -> RELEASED
```

### Wired Loaders

#### LLM Loader (PrimeLocalClient)

- `component_id`: `"llm:{model_name}@v1"`
- `phase`: `BOOT_OPTIONAL` (default), `BACKGROUND` (headless mode), deferred (voice-only mode)
- Boot profile controlled by `JARVIS_BOOT_PROFILE` env: `interactive` | `headless` | `voice-only`
- Degradation options: reduce context (2048, 1024), smaller quant, CPU-only (`n_gpu_layers=0`)
- `prove_config` validates: `n_ctx <= max_context`, `n_gpu_layers <= constraint`, `size_mb <= max`

#### Whisper Loader

- `component_id`: `"whisper:{model_size}@v1"`
- `phase`: `BOOT_OPTIONAL`
- `estimate_bytes`: model size lookup + 200 MB PyTorch overhead
- Degradation: fall back to `tiny` model

#### ECAPA-TDNN Loader

- `component_id`: `"ecapa_tdnn@v1"`
- `phase`: `BOOT_OPTIONAL`
- `estimate_bytes`: ~350 MB (300 MB model + 50 MB overhead)
- No degradation options (small, binary: loaded or not)

#### Embedding Loader (SentenceTransformer)

- `component_id`: `"embedding:all-MiniLM-L6-v2@v1"`
- `phase`: `BOOT_OPTIONAL`
- `estimate_bytes`: ~400 MB (90 MB weights + tokenizer + torch pool overhead)
- No degradation (fixed model). If denied, `encode()` returns None; callers handle gracefully.
- **This is the ONLY approved path for SentenceTransformer instantiation.**

### Bypass Elimination

All 13+ direct `SentenceTransformer(...)` sites replaced with `EmbeddingService.get_instance()`:

| File | Line(s) |
|---|---|
| `ml_model_loader.py` | 31 |
| `learning_database.py` | 4565, 4653 |
| `trinity_knowledge_graph.py` | 432 |
| `long_term_memory.py` | 407 |
| `domain_knowledge.py` | 30 |
| `semantic_matcher.py` | 159 |
| `jarvis_embedding_client.py` | 283 |
| `lazy_vision_engine.py` | 124 |
| `ml_memory_manager.py` | 468 |
| `trinity_knowledge_indexer.py` | 163 |
| `rag_engine.py` | 165 |
| `semantic_memory.py` | 297 |
| `shared_knowledge_graph.py` | 219 |
| `semantic_cache_lsh.py` | 354 |
| `embedding_service.py` `encode_sync` | inline creation removed |

An `EmbeddingServiceAdapter` provides `encode()`, `encode_batch()`, `get_embedding_dim()`
for sites that need model-level methods beyond basic encoding.

### Startup Sequencing (Phase Graph)

```
Phase: BOOT_CRITICAL (1 concurrent, 60% cap)
  - MemoryQuantizer init (signal collection, ~0 bytes)
  - MemoryBudgetBroker init (governance, ~0 bytes)

Phase: BOOT_OPTIONAL (2 concurrent, 70% cap)
  - [Grant 1] EmbeddingBudgetedLoader -> SentenceTransformer (~400 MB)
      After commit: parallel_import_components may init embedding-dependent modules
  - [Grant 2] WhisperBudgetedLoader -> Whisper base (~350 MB)
      After commit: EcapaBudgetedLoader -> ECAPA-TDNN (~350 MB)
      (sequential within voice, parallel with embeddings)
  - After all BOOT_OPTIONAL committed:
      LLMBudgetedLoader -> best-fit from QUANT_CATALOG (remaining headroom)

Phase: RUNTIME_INTERACTIVE (3 concurrent, 80% cap)
  - Normal operation. Hot-swap, model upgrades.

Phase: BACKGROUND (2 concurrent, 70% cap)
  - Speculative preloads, canary model testing.
```

LLM loads last because it is largest and most degradable.

### CI Enforcement

- **Grep fast gate:** Ban `SentenceTransformer(` outside `embedding_service.py`;
  ban `psutil.virtual_memory` outside `memory_quantizer.py`
- **AST-based checker:** `ast.walk` for `Call` nodes matching banned constructors
  (bypasses string obfuscation, handles aliased imports)
- `# APPROVED_BYPASS:` escape hatch for test files only

---

## 4. Lease Persistence, Crash Reclaim & Telemetry

### 4.1 Lease Persistence

**File:** `~/.jarvis/memory/leases.json`

```python
@dataclass
class PersistedLease:
    lease_id: str
    component_id: str
    granted_bytes: int
    actual_bytes: Optional[int]
    state: str                     # LeaseState enum name
    priority: str
    phase: str
    pid: int
    epoch: int
    created_at_mono: float         # monotonic offset from boot
    created_at_wall: str           # ISO-8601 (human forensics)
    expires_at_mono: float
    last_heartbeat_mono: float
    constraints: Dict[str, Any]
    config_proof: Optional[Dict]
    trace_id: str
    causal_parent_id: Optional[str]
```

**Atomic persistence:** write-temp + `os.fsync()` + `os.rename()`. Crash-safe.

**Persistence triggers** (event-driven, not periodic):
- Grant issued, commit, rollback, release, preemption
- Heartbeat (batched: max 1 write per 5s)

**Monotonic + wall clock:** Lease file stores boot-time wall clock anchor.
Monotonic offsets are resolved via anchor for cross-boot reconciliation.

### 4.2 Crash Reclaim (Boot Reconciliation)

On broker init:
1. Read lease file
2. **Epoch fence:** any lease with `epoch != current_epoch` -> reclaim
3. **PID liveness:** same epoch but `pid` not alive -> reclaim
4. **TTL expiry:** `GRANTED` state past `expires_at_mono` -> reclaim
5. **Valid ACTIVE leases:** reimport into broker as committed capacity
6. **Corrupted file:** treat as total loss, log warning, delete file

After reconciliation: `committed_bytes` = sum of reimported ACTIVE leases only.

### 4.3 Post-Release Verification & Loader Quarantine

After `release_handle()` returns, broker monitors RSS for 15s:
- If RSS drops >= 50% of expected: verified, `RELEASED`
- If RSS does not drop: release failure logged

**Quarantine:** loader that fails to reclaim 3 times within a session:
- Quarantined for 300s (5 min)
- Cannot request new grants during quarantine
- Quarantine entry/exit emitted as telemetry events

### 4.4 Estimate Calibration

Persist `(component_id, estimated, actual, ratio)` history in
`~/.jarvis/memory/estimate_history.json`.

- Max 50 entries per component
- Broker applies p95 overrun factor to raw estimates before evaluating feasibility
- Default overrun factor: 1.2 (20% padding) when <3 samples
- Auto-adjusts as history grows

### 4.5 Telemetry Event Taxonomy

```python
class MemoryBudgetEventType(str, Enum):
    GRANT_REQUESTED = "grant_requested"
    GRANT_ISSUED = "grant_issued"
    GRANT_DENIED = "grant_denied"
    GRANT_DEGRADED = "grant_degraded"
    GRANT_QUEUED = "grant_queued"
    HEARTBEAT = "heartbeat"
    COMMIT = "commit"
    COMMIT_OVERRUN = "commit_overrun"
    ROLLBACK = "rollback"
    RELEASE_REQUESTED = "release_requested"
    RELEASE_VERIFIED = "release_verified"
    RELEASE_FAILED = "release_failed"
    PREEMPT_REQUESTED = "preempt_requested"
    PREEMPT_COOPERATIVE = "preempt_cooperative"
    PREEMPT_FORCED = "preempt_forced"
    LEASE_EXPIRED = "lease_expired"
    RECONCILIATION = "reconciliation"
    PHASE_TRANSITION = "phase_transition"
    SWAP_HYSTERESIS_TRIP = "swap_hysteresis_trip"
    SWAP_HYSTERESIS_RECOVER = "swap_hysteresis_recover"
    LOADER_QUARANTINED = "loader_quarantined"
    LOADER_UNQUARANTINED = "loader_unquarantined"
    ESTIMATE_CALIBRATION = "estimate_calibration"
    SNAPSHOT_STALE_REJECTED = "snapshot_stale_rejected"
```

Every event carries: `lease_id`, `component_id`, `trace_id`, `causal_parent_id`,
`epoch`, `snapshot_id`, `pressure_tier`, `signal_quality`, timing fields.

### 4.6 Dashboard Integration

Exposed via `/api/system/status` under `memory_control_plane` key:
- `broker_epoch`, `phase`, `active_leases[]`, `total_committed_bytes`, `headroom_bytes`
- `pressure_tier`, `swap_hysteresis_active`, `quarantined_loaders[]`
- `last_reconciliation`, `estimate_calibration{}`

---

## 5. Edge Cases & Advanced Nuances

| Gap | Mitigation |
|---|---|
| Re-entrant startup/race restarts | Serialize by epoch + lock; second boot waits for first to drain |
| Swap hysteresis (sustained degradation) | 60s recovery window, 0.7x cap multiplier, no BACKGROUND grants |
| Fragmentation drift (long uptime) | Include RSS slope derivatives, not just total free |
| Priority inversion | BOOT_CRITICAL can preempt BACKGROUND; aging prevents starvation |
| Cross-process orphan state | PID liveness check + epoch fence at reconciliation |
| Version skew (cross-repo) | `constraints_schema_version` on grants, compatibility check at boot |
| Cancellation holes (async) | Cancel token -> timed unload -> forced teardown; native alloc continues but lease expires |
| GPU+CPU coupled pressure (Apple Silicon) | Metal unified memory accounted in wired bytes; n_gpu_layers in constraints |
| False-safe inactive memory | Use wired+active+compressed, not psutil `available` |
| Memory-map accounting mismatch | mmap_factor in loader estimate; broker uses RSS delta not file size |
| Policy flapping (tier oscillation) | 15s dwell dampener on tier transitions |
| Clock anomalies in lease file | Monotonic offsets + wall clock anchor; reconciliation uses monotonic only |
| Observability cardinality blowup | Bounded component_id vocabulary; no per-request labels |
| Kernel pressure lag | Slopes may trip before kernel reports; use earliest signal |
| Import-time side-effect allocation | Loaders defer heavyweight imports to load_with_grant() body |

---

## 6. Files Modified

### New Files

| File | Purpose |
|---|---|
| `backend/core/memory_budget_broker.py` | MemoryBudgetBroker, BudgetGrant, lease lifecycle |
| `backend/core/memory_types.py` | MemorySnapshot, enums (PressureTier, BudgetPriority, etc.), ConfigProof, LoadResult |
| `backend/core/budgeted_loaders.py` | BudgetedLoader protocol + LLM/Whisper/ECAPA/Embedding loader implementations |
| `backend/core/estimate_calibrator.py` | Estimate vs actual tracking, p95 overrun factor |
| `backend/core/release_verifier.py` | Post-release RSS verification, loader quarantine |
| `tests/test_memory_budget_broker.py` | Broker integration tests |
| `tests/test_loader_contracts.py` | Per-loader contract test suite |
| `tests/test_memory_stress.py` | Stress & soak test harness |
| `.github/workflows/memory-governance.yml` | CI: grep + AST ban on direct constructors/psutil |

### Modified Files

| File | Change |
|---|---|
| `backend/core/memory_quantizer.py` | Add `snapshot()` method returning `MemorySnapshot`; accept broker ref for committed_bytes; add signal_quality tracking |
| `backend/core/embedding_service.py` | Wire to broker via EmbeddingBudgetedLoader; add EmbeddingServiceAdapter; remove inline SentenceTransformer in `encode_sync` |
| `backend/intelligence/unified_model_serving.py` | Replace direct Llama loading with LLMBudgetedLoader; remove internal reservation system; consume broker grants |
| `backend/voice/parallel_model_loader.py` | Replace fire-and-forget with broker-gated loading via Whisper/ECAPA BudgetedLoaders |
| `backend/main.py` | Replace fresh `MemoryQuantizer()` with singleton; add broker init to startup; wire phase transitions; add MCP status to `/api/system/status` |
| `unified_supervisor.py` | Add `set_phase()` calls at lifecycle boundaries; wire broker epoch |
| 14 bypass files (see Section 3) | Replace `SentenceTransformer(...)` with `EmbeddingService.get_instance()` / adapter |

### Retired (functionality subsumed)

| Component | Retired By |
|---|---|
| `ProactiveResourceGuard.request_memory_budget()` | `MemoryBudgetBroker.request()` |
| `IntelligentMLMemoryManager` budget/monitor logic | Broker preemption + tier callbacks |
| `MemoryQuantizer.reserve_memory()` v266.0 | Broker lease lifecycle |

### Environment Variables (new)

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_BOOT_PROFILE` | `interactive` | Boot mode: `interactive` / `headless` / `voice-only` |
| `JARVIS_MCP_PHASE_CAP_BOOT_CRITICAL` | `0.60` | Phase cap override |
| `JARVIS_MCP_PHASE_CAP_BOOT_OPTIONAL` | `0.70` | Phase cap override |
| `JARVIS_MCP_PHASE_CAP_RUNTIME` | `0.80` | Phase cap override |
| `JARVIS_MCP_PHASE_CAP_BACKGROUND` | `0.70` | Phase cap override |
| `JARVIS_MCP_LEASE_TTL_SECONDS` | `120` | Default grant TTL |
| `JARVIS_MCP_PREEMPT_TIMEOUT_SECONDS` | `10` | Cooperative preempt ACK timeout |
| `JARVIS_MCP_RELEASE_VERIFY_SECONDS` | `15` | Post-release RSS verification window |
| `JARVIS_MCP_QUARANTINE_SECONDS` | `300` | Loader quarantine duration |
| `JARVIS_MCP_SWAP_HYSTERESIS_BPS` | `52428800` | 50 MB/s swap growth threshold |
| `JARVIS_MCP_SWAP_RECOVERY_SECONDS` | `60` | Sustained recovery before relaxing |
| `JARVIS_MCP_TIER_DWELL_SECONDS` | `15` | Min dwell before tier transition |

---

## 7. Acceptance Gates

### Before Implementation Begins

- [ ] Design doc reviewed and committed

### Before Trinity Autonomy Wiring Proceeds

- [ ] Cold start (N=100 cycles): peak RSS never exceeds `physical_total * 0.85`; zero swap growth
- [ ] 30-minute soak: RSS stable within +/-5% band after warmup; no monotonic growth
- [ ] Forced LLM load failure: rollback within 5s, capacity reclaimed, next load succeeds
- [ ] Forced Whisper load failure: same criteria
- [ ] Network failure (Prime unreachable): boot completes read-only, no memory leak
- [ ] Kill -9 during model load: next boot reclaims orphaned leases, loads successfully
- [ ] Concurrent restart (race): serialized by epoch; second boot waits for first to drain
- [ ] Pressure spike (simulated): swap hysteresis trips, BACKGROUND denied, recovery within 60s
- [ ] All per-loader contract tests pass (grant-required, constraint compliance, overrun, preemption, rollback idempotency, stale-epoch rejection)
- [ ] CI governance checks pass (no SentenceTransformer bypasses, no direct psutil)
- [ ] Cross-repo contract compatibility check green at boot

---

## 8. Rollout Sequence

1. **Implement broker API + lease model + policy table** (new module, no callers yet)
2. **Wire all loaders through broker** (remove bypasses, enforce singleton)
3. **Add startup admission barriers** tied to supervisor phase transitions
4. **Add runtime backpressure/degradation graph**
5. **Pass stress/failure/restart harness** (acceptance gates above)
6. **Proceed with Trinity Autonomy Wiring** on stable lifecycle guarantees

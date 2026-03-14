# Adaptive Quantization Engine — Design Specification

**Date:** 2026-03-14
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Reviewed (v2 — post spec-review fixes)
**Target:** jarvis-prime (GCP VM) + JARVIS-AI-Agent (supervisor integration)

---

## 1. Problem Statement

The NVIDIA L4 GPU (23GB VRAM) hosts multiple quantization variants of the same base model (e.g., Qwen2.5-Coder-32B at IQ2_M/Q2_K/Q3_K_S/Q4_K_M). Currently, model selection is static — a symlink (`current.gguf`) points to one file, chosen manually. There is no system to:

1. Dynamically select the optimal quantization variant based on real-time VRAM budget
2. Score quantization quality using information-theoretic metrics
3. Auto-download missing variants when a better option is feasible
4. Monitor VRAM pressure and adaptively swap to avoid OOM
5. Optimize KV cache quantization to maximize context window within VRAM constraints
6. Perform quality regression testing across quantization tiers

### Critical Constraint: Single Admission Authority

`MemoryBudgetBroker` is the existing admission authority for all memory-intensive operations in JARVIS (local Mac). Any new quantization system **must not** create a parallel authority. The new modules are **advisors** that feed into a single **executor** which coordinates with the broker.

### Repository Boundary

| Component | Repository | Runtime Location |
|-----------|-----------|-----------------|
| All 6 new modules (`quantization_intelligence.py`, etc.) | `jarvis-prime` | GCP VM |
| `LlamaCppExecutor` (existing) | `jarvis-prime` | GCP VM |
| `GCPModelSwapCoordinator` (existing) | `jarvis-prime` | GCP VM |
| `run_server.py` (existing) | `jarvis-prime` | GCP VM |
| `MemoryBudgetBroker` (existing) | `JARVIS-AI-Agent` | Local Mac |
| `MemoryQuantizer` (existing) | `JARVIS-AI-Agent` | Local Mac |
| `unified_model_serving.py` (existing) | `JARVIS-AI-Agent` | Local Mac |
| `unified_supervisor.py` (existing, NO changes) | `JARVIS-AI-Agent` | Local Mac |

### Critical: VRAM Budget Authority (Cross-Process Boundary)

The `MemoryBudgetBroker` on the Mac manages **system RAM**. VRAM on the GCP VM is a **completely different resource** on a **different machine**. Therefore:

- J-Prime instantiates its own lightweight **`VRAMBudgetAuthority`** — a simplified broker that manages GPU VRAM on the GCP VM.
- `VRAMBudgetAuthority` respects the same grant lifecycle (`GRANTED → ACTIVE → RELEASED | ROLLED_BACK`) but operates on VRAM, not system RAM.
- It is **not** the `MemoryBudgetBroker` singleton — it is a purpose-built VRAM admission controller.
- The Mac-side `MemoryBudgetBroker` is **not involved** in GCP model transitions.
- The supervisor learns about model state changes via the `/v1/capability` HTTP endpoint (pull, not push).

---

## 2. Architecture Overview

```
                    ADVISORY LAYER (pure, side-effect-free)
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │  QuantizationIntelligence    AdaptiveModelSelector      │
  │  (rate-distortion scoring)   (inventory + proposals)    │
  │         │                           │                   │
  │         └─────────┬─────────────────┘                   │
  │                   │                                     │
  │  VRAMPressureMonitor         KVCacheOptimizer           │
  │  (pressure events)          (feasible KV profiles)      │
  │         │                           │                   │
  │         └─────────┬─────────────────┘                   │
  └───────────────────┼─────────────────────────────────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │  ModelTransitionManager │  ◄── THE SINGLE EXECUTOR
         │  (serialized FSM)       │      Coordinates all changes
         │  PREPARE→DRAIN→CUTOVER  │      via MemoryBudgetBroker
         │  →VERIFY→COMMIT/ROLLBACK│
         └────────────┬───────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
   VRAMBudget     LlamaCpp     GCPModelSwap
   Authority      Executor     Coordinator
   (VRAM admit)   (load/unload) (existing)
```

### Authority Hierarchy

| Role | Component | Responsibility |
|------|-----------|----------------|
| **Admission Authority** | `VRAMBudgetAuthority` (on GCP VM) | Sole gatekeeper for VRAM allocation on GCP. Issues/denies `VRAMGrant`. Mac-side `MemoryBudgetBroker` manages local RAM separately. |
| **Executor** | `ModelTransitionManager` | Serializes all model changes. Acquires grants from broker before any load. |
| **Advisors** | `QuantizationIntelligence`, `AdaptiveModelSelector`, `VRAMPressureMonitor`, `KVCacheOptimizer` | Propose plans, emit events, compute scores. **Zero side effects.** |

---

## 3. Module Specifications

### 3.1 `quantization_intelligence.py` — Rate-Distortion Scoring Engine

**Location:** `jarvis_prime/core/quantization_intelligence.py`
**Purity:** Deterministic, side-effect-free, no I/O

#### 3.1.1 Mathematical Foundation

Each quantization level Q maps to a point on the **rate-distortion curve** R(D):

```
R(D) = minimum bit-rate to achieve distortion ≤ D
```

For LLM weight quantization:
- **Rate** = bits-per-weight (bpw): IQ2_XXS=2.06, IQ2_M=2.70, Q2_K=2.96, Q3_K_S=3.50, Q4_K_M=4.83, Q8_0=8.50
- **Distortion** = perplexity increase relative to FP16 baseline

The perplexity-vs-bpw relationship follows an empirical power law:

```
ppl(bpw) ≈ ppl_fp16 × (1 + α × (fp16_bpw / bpw)^β)

Where for Qwen2.5-Coder-32B family:
  α ≈ 0.015 (model-family coefficient)
  β ≈ 2.1   (distortion exponent, from calibration data)
  fp16_bpw = 16.0
```

**Fisher Information Scaling** (IQ quantizations only):
IQ variants use per-layer importance matrices derived from the Fisher Information:

```
I(θ_i) = E[(∂/∂θ_i log p(x|θ))²]
```

Weights with higher Fisher Information receive more bits. This achieves the **Cramer-Rao lower bound** for estimation variance, making IQ quantizations information-theoretically optimal for a given bit budget.

#### 3.1.2 Data Model

```python
@dataclass(frozen=True)
class QuantizationProfile:
    """Immutable descriptor for a quantization method."""
    name: str                    # e.g., "IQ2_M", "Q4_K_M"
    bits_per_weight: float       # e.g., 2.70, 4.83
    compression_ratio: float     # relative to FP16 (0.0-1.0)
    uses_importance_matrix: bool # True for IQ variants
    quality_floor: float         # Minimum quality estimate (0.0-1.0)
    quality_ceiling: float       # Maximum quality estimate (0.0-1.0)

@dataclass(frozen=True)
class QuantizationQualityScore:
    """Computed quality assessment for a specific model+quant combination."""
    profile: QuantizationProfile
    model_family: str            # e.g., "qwen2.5-coder-32b"
    estimated_perplexity_ratio: float  # ppl(quant) / ppl(fp16), ≥1.0
    quality_score: float         # 0.0-1.0, derived from perplexity ratio
    vram_bytes: int              # Model weight footprint
    estimated_tok_s: float       # Throughput estimate for this hardware
    context_headroom_tokens: int # Max context with f16 KV cache
    fitness_score: float         # Composite: quality × throughput × context
    scoring_basis: str           # "empirical" | "interpolated" | "extrapolated"
```

#### 3.1.3 Key Functions

```python
def score_quantization(
    profile: QuantizationProfile,
    model_family: str,
    model_size_bytes: int,
    total_vram_bytes: int,
    target_context: int = 8192,
    task_complexity: str = "medium",  # "trivial"|"light"|"medium"|"heavy"|"complex"
    calibration_data: Optional[CalibrationData] = None,
) -> QuantizationQualityScore:
    """
    Score a quantization variant for the given hardware and task.
    Pure function — no I/O, no state mutation.
    """

def rank_quantizations(
    available: list[tuple[QuantizationProfile, int]],  # (profile, file_size_bytes)
    model_family: str,
    total_vram_bytes: int,
    target_context: int = 8192,
    task_complexity: str = "medium",
) -> list[QuantizationQualityScore]:
    """
    Rank all available quantizations by fitness_score.
    Returns sorted list, best first. Excludes variants that won't fit.
    """

def estimate_throughput(
    model_params_billions: float,
    bits_per_weight: float,
    gpu_memory_bandwidth_gbps: float,  # L4 = 300 GB/s
    gpu_compute_tflops: float,         # L4 = 30.3 TFLOPS FP16
) -> float:
    """
    Estimate tok/s using roofline model.
    LLM decode is memory-bandwidth-bound:
      tok/s ≈ memory_bandwidth / (model_params × bpw / 8)
    """
```

#### 3.1.4 Calibration Data

```python
@dataclass(frozen=True)
class CalibrationData:
    """Empirical measurements that override theoretical estimates."""
    model_family: str
    measurements: dict[str, CalibrationPoint]  # quant_name → point

@dataclass(frozen=True)
class CalibrationPoint:
    quant_name: str
    measured_tok_s: float
    measured_perplexity: Optional[float]
    measured_vram_bytes: int
    context_size: int
    timestamp: float
```

Calibration data is stored as JSON alongside models:
```
models/
  calibration/
    qwen2.5-coder-32b.json    # Per-family calibration
    qwen2.5-coder-7b.json
```

---

### 3.2 `adaptive_model_selector.py` — Proposal Engine

**Location:** `jarvis_prime/core/adaptive_model_selector.py`
**Purity:** Read-only I/O (filesystem scan, env vars). Proposes plans — never executes them.

#### 3.2.1 Model Family Grouping

Scans model directory and groups files by base model:

```python
@dataclass(frozen=True)
class ModelVariant:
    """A single GGUF file with parsed metadata."""
    path: Path
    size_bytes: int
    base_model: str           # "qwen2.5-coder-32b-instruct"
    quant_name: str           # "IQ2_M", "Q4_K_M"
    quant_profile: QuantizationProfile
    sha256: Optional[str]     # Verified hash (None if not yet checked)
    provenance: str           # "local" | "huggingface" | "gcs"

@dataclass(frozen=True)
class ModelFamily:
    """All quantization variants of one base model."""
    base_model: str
    variants: tuple[ModelVariant, ...]  # Immutable, sorted by quality desc
    parameter_count: float              # Billions (parsed from name)
```

**Filename Parsing Pattern:**
```
{BaseModel}-{QuantName}.gguf
e.g., Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf
      → base_model="qwen2.5-coder-32b-instruct", quant_name="IQ2_M"
```

#### 3.2.2 Selection Proposals

```python
@dataclass(frozen=True)
class ModelSelectionProposal:
    """A proposed model change — advisory only, not executed."""
    proposal_id: str                    # "prop-{uuid7}"
    selected_variant: ModelVariant
    quality_score: QuantizationQualityScore
    kv_cache_profile: KVCacheProfile    # From KVCacheOptimizer
    reason: str                         # Human-readable justification
    trigger: str                        # "startup" | "pressure" | "task_upgrade" | "task_downgrade"
    inventory_digest: str               # SHA256 of sorted model inventory
    timestamp: float

@dataclass(frozen=True)
class DownloadProposal:
    """A proposed model download — advisory only."""
    proposal_id: str
    target_variant: str                 # e.g., "Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf"
    source_url: str                     # HuggingFace URL
    expected_size_bytes: int
    expected_sha256: str                # For verification
    reason: str
    priority: int                       # 0=critical, 1=high, 2=normal, 3=background
```

#### 3.2.3 Key Functions

```python
async def scan_inventory(model_dir: Path) -> list[ModelFamily]:
    """Scan model directory, group by family. Read-only."""

async def propose_optimal(
    families: list[ModelFamily],
    vram_budget_bytes: int,
    target_context: int,
    task_complexity: str,
    current_model: Optional[ModelVariant],
    calibration: Optional[CalibrationData],
) -> ModelSelectionProposal:
    """Propose the best model for current conditions. Does NOT execute."""

async def propose_download(
    families: list[ModelFamily],
    vram_budget_bytes: int,
    target_context: int,
    download_registry: DownloadRegistry,
) -> Optional[DownloadProposal]:
    """Propose a download if a better variant exists remotely. Does NOT download."""
```

#### 3.2.4 Download Trust Chain

Downloads go through a quarantine pipeline:

```
PROPOSED → PRE_CHECK → DOWNLOADING → QUARANTINED → HASH_VERIFIED → GGUF_VALIDATED → AVAILABLE
                │           │                            │
                │           └→ TIMED_OUT (retry up to 3x)
                │                                        └→ REJECTED (hash mismatch or malformed)
                └→ REJECTED (insufficient disk space)
```

- **Pre-check:** Verify `disk_free >= expected_size × 1.1` (10% safety margin) before starting
- **Download timeout:** 30 minutes max per download. Stalled downloads (no bytes for 60s) are cancelled.
- **Retry policy:** Up to 3 retries with exponential backoff (30s, 60s, 120s) for transient network errors
- **Concurrent limit:** Max 1 concurrent download (serialized queue)
- **Hash verification:** SHA256 compared against HuggingFace model card checksums. If no published checksum exists, log WARNING and skip hash check (provenance-only verification via URL + size match)
- **GGUF validation:** Header parsing to confirm valid GGUF format, parameter count matches expected
- **Quarantine directory:** `models/.quarantine/` — files here are never loaded
- **Provenance tag:** Stored in `models/provenance.json` mapping filename → source URL + hash + timestamp + download_time

---

### 3.3 `vram_pressure_monitor.py` — GPU Memory Watchdog

**Location:** `jarvis_prime/core/vram_pressure_monitor.py`
**Purity:** Read-only (nvidia-smi/pynvml queries). Emits events — never triggers swaps directly.

#### 3.3.1 Pressure Model

```python
class VRAMPressureZone(Enum):
    GREEN = "green"       # < 70% VRAM used — healthy, upgrade possible
    YELLOW = "yellow"     # 70-85% — nominal, no action needed
    RED = "red"           # 85-92% — elevated, prepare for downgrade
    CRITICAL = "critical" # > 92% — imminent OOM, urgent downgrade

@dataclass(frozen=True)
class VRAMPressureEvent:
    """Emitted when pressure zone changes. Advisory only."""
    zone: VRAMPressureZone
    previous_zone: VRAMPressureZone
    total_bytes: int
    used_bytes: int
    free_bytes: int
    fragmentation_estimate: float  # 0.0-1.0, heuristic
    model_resident_bytes: int      # Estimated model weight footprint
    kv_cache_bytes: int            # Estimated KV cache footprint
    timestamp: float
    sustained_seconds: float       # How long in this zone
```

#### 3.3.2 Fragmentation Estimation

Free VRAM does not equal allocatable VRAM due to fragmentation. However, standard APIs (`cuMemGetInfo`, `nvidia-smi`) report only `(free, total)` — not the largest contiguous block.

**Practical approach:**

```python
def estimate_effective_free(
    free_bytes: int,
    model_loaded: bool,
) -> int:
    """
    Conservative estimate of allocatable VRAM.

    After model unload, CUDA memory is typically returned as one contiguous block
    (llama.cpp uses a single large allocation). Fragmentation is primarily a risk
    when multiple models or processes share GPU memory simultaneously.

    For our single-model-at-a-time architecture:
    - Post-unload: assume ~95% of free is allocatable (minimal fragmentation)
    - With model loaded: assume ~80% of remaining free is allocatable
    """
    if not model_loaded:
        return int(free_bytes * 0.95)  # Nearly contiguous after full unload
    return int(free_bytes * 0.80)      # Conservative with model resident
```

**Note:** True fragmentation detection would require binary-search allocation probing (expensive, side-effectful) or CUDA memory pool introspection APIs not available via standard NVML. The 0.80/0.95 multipliers are empirically conservative. If pynvml v12+ with `nvmlDeviceGetMemoryInfo_v2` is available, it can provide more accurate data; this is detected at runtime and used if present.

#### 3.3.3 Monitoring Configuration

```python
@dataclass
class VRAMMonitorConfig:
    poll_interval_s: float = 5.0          # Normal polling rate
    critical_poll_interval_s: float = 1.0 # Accelerated in RED/CRITICAL
    zone_thresholds: dict = field(default_factory=lambda: {
        "yellow": 0.70,
        "red": 0.85,
        "critical": 0.92,
    })
    sustained_threshold_s: float = 10.0   # Zone must hold this long before event
    backend: str = "pynvml"               # "pynvml" | "nvidia_smi" | "mock"
```

All thresholds configurable via env vars:
- `JARVIS_VRAM_YELLOW_THRESHOLD=0.70`
- `JARVIS_VRAM_RED_THRESHOLD=0.85`
- `JARVIS_VRAM_CRITICAL_THRESHOLD=0.92`
- `JARVIS_VRAM_POLL_INTERVAL_S=5.0`

#### 3.3.4 Cross-Node Safety

**Critical invariant:** VRAM pressure on the GCP VM must NOT trigger swap policies intended for the local Mac. The monitor tags every event with `node_id`:

```python
@dataclass(frozen=True)
class VRAMPressureEvent:
    # ... other fields ...
    node_id: str  # "gcp-jarvis-prime-stable" | "local-mac"
```

The `ModelTransitionManager` filters events by `node_id` before acting.

---

### 3.4 `kv_cache_optimizer.py` — Context Window Maximizer

**Location:** `jarvis_prime/core/kv_cache_optimizer.py`
**Purity:** Deterministic, side-effect-free

#### 3.4.1 KV Cache Math

KV cache memory per token:
```
kv_per_token = 2 × n_layers × n_kv_heads × head_dim × bytes_per_element

For Qwen2.5-Coder-32B:
  n_layers = 64, n_kv_heads = 8 (GQA), head_dim = 128
  f16: 2 × 64 × 8 × 128 × 2 = 262,144 bytes/token (256 KB)
  q8_0: 2 × 64 × 8 × 128 × 1 = 131,072 bytes/token (128 KB)
  q4_0: 2 × 64 × 8 × 128 × 0.5 = 65,536 bytes/token (64 KB)
```

#### 3.4.2 Data Model

```python
class KVCacheType(Enum):
    F16 = "f16"     # Full precision, best quality
    Q8_0 = "q8_0"   # 50% savings, ~0.1% quality loss
    Q4_0 = "q4_0"   # 75% savings, ~0.5% quality loss

@dataclass(frozen=True)
class KVCacheProfile:
    """Feasible KV cache configuration for given constraints."""
    cache_type_k: KVCacheType
    cache_type_v: KVCacheType
    max_context_tokens: int
    vram_bytes: int              # Total KV cache VRAM at max_context
    quality_impact: float        # 0.0 = no impact, 1.0 = severe
    recommendation: str          # Human-readable
```

#### 3.4.3 Key Functions

```python
def compute_feasible_profiles(
    model_params: ModelArchitectureParams,
    model_weight_bytes: int,
    total_vram_bytes: int,
    overhead_bytes: int = 500_000_000,  # 500MB CUDA/framework overhead
    target_context: int = 8192,
    min_context: int = 2048,           # Below this, model is useless
) -> list[KVCacheProfile]:
    """
    Compute all feasible KV cache profiles.
    Returns profiles sorted by quality (best first), filtered to those
    that achieve at least min_context tokens.
    """
```

#### 3.4.4 Architecture Parameter Detection

Model architecture params are detected from GGUF metadata:

```python
@dataclass(frozen=True)
class ModelArchitectureParams:
    n_layers: int
    n_heads: int
    n_kv_heads: int    # For GQA models (< n_heads)
    head_dim: int
    vocab_size: int

    @classmethod
    def from_gguf_metadata(cls, model_path: Path) -> 'ModelArchitectureParams':
        """Parse GGUF header for architecture params without loading full model."""
```

---

### 3.5 `model_transition_manager.py` — The Single Executor

**Location:** `jarvis_prime/core/model_transition_manager.py`
**Role:** THE executor. All model changes go through this serialized FSM.

#### 3.5.1 Transition State Machine

```
                          ┌─────────────────────┐
                          │                     │
    IDLE ─── accept() ──► PREPARE ──► DRAIN ──► CUTOVER ──► VERIFY ──► COMMIT
      ▲                     │           │          │           │          │
      │                     │           │          │           │          │
      └─── any failure ─────┴───────────┴──────────┴───────────┘          │
      │                                                                    │
      └────────────────────────────────────────────────────────────────────┘
                              (on COMMIT: return to IDLE)
```

**States:**

| State | Description | Actions |
|-------|-------------|---------|
| `IDLE` | No transition in progress | Accept new proposals |
| `PREPARE` | Validate proposal, acquire `BudgetGrant` from broker | Grant acquisition, inventory digest check |
| `DRAIN` | Wait for in-flight requests to complete | Epoch barrier, reject new requests with old model_epoch |
| `CUTOVER` | Unload old model, load new model | `LlamaCppExecutor.unload()` → `LlamaCppExecutor.load()` |
| `VERIFY` | Confirm new model serves correctly | Smoke-test inference, VRAM check |
| `COMMIT` | Finalize transition, release old grant | Update `model_epoch`, emit telemetry |
| `ROLLBACK` | Revert to previous model on any failure | Reload previous model, release new grant |

#### 3.5.2 Epoch System

```python
@dataclass
class TransitionEpoch:
    """Monotonic epoch counters for consistency enforcement."""
    model_epoch: int = 0       # Incremented on each successful model swap
    cache_epoch: int = 0       # Incremented on each KV cache config change
    inventory_epoch: int = 0   # Incremented when model inventory changes

    def advance_model(self) -> int:
        self.model_epoch += 1
        return self.model_epoch

    def advance_cache(self) -> int:
        self.cache_epoch += 1
        return self.cache_epoch
```

**Request epoch enforcement (within J-Prime process):**

Epochs are process-local to J-Prime. The supervisor on the Mac does NOT stamp epochs — it learns the current epoch from `/v1/capability` responses.

```python
# J-Prime stamps epoch on request intake (inside the HTTP handler):
@dataclass(frozen=True)
class InferenceRequest:
    # ... existing fields ...
    model_epoch: int           # Stamped at intake from TransitionEpoch.model_epoch

# In generation path (still inside J-Prime):
if request.model_epoch < current_epoch.model_epoch:
    raise StaleEpochError(
        f"Request epoch {request.model_epoch} < model epoch {current_epoch.model_epoch}"
    )
```

**Cross-HTTP epoch propagation:**

The supervisor learns the epoch via the `/v1/capability` endpoint response (see Section 4.1):
- `transition_epoch.model_epoch` is included in every capability response
- The supervisor does NOT need to stamp requests with epoch — J-Prime handles this internally
- If the supervisor calls `/v1/chat/completions` and gets a `503 Model Transitioning` response, it should retry or fallback to Claude API

**Epoch response header for observability:**
```
X-Model-Epoch: 3
X-Cache-Epoch: 1
```
These headers are informational — the supervisor logs them but does not enforce them.

#### 3.5.3 Drain Protocol

```python
async def _drain_in_flight(self, timeout_s: float = 30.0) -> bool:
    """
    Wait for all in-flight requests to complete.

    1. Set drain_epoch = current model_epoch + 1
    2. New requests stamped with drain_epoch are held in a queue
    3. Wait for active_request_count to reach 0 (or timeout)
    4. Returns True if drained, False if timed out

    On timeout: ROLLBACK (do not proceed with stale requests in flight)
    """
```

#### 3.5.4 Hysteresis & Cooldown

```python
@dataclass
class TransitionPolicy:
    """Prevents control-loop oscillation."""
    min_cooldown_s: float = 90.0          # Minimum time between swaps
    max_swaps_per_hour: int = 4           # Hard cap
    quality_dead_zone: float = 0.05       # Don't swap if <5% quality improvement
    upgrade_sustained_s: float = 30.0     # GREEN must hold 30s before upgrade
    downgrade_sustained_s: float = 10.0   # RED must hold 10s before downgrade
    cold_start_lockout_s: float = 120.0   # No swaps for 2 min after startup

    # Exponential backoff on repeated swaps
    backoff_base_s: float = 90.0
    backoff_multiplier: float = 2.0
    backoff_max_s: float = 600.0          # Cap at 10 min
```

All configurable via env vars:
- `JARVIS_MODEL_SWAP_COOLDOWN_S=90`
- `JARVIS_MODEL_MAX_SWAPS_PER_HOUR=4`
- `JARVIS_MODEL_QUALITY_DEAD_ZONE=0.05`

#### 3.5.5 Hard Invariants

These invariants are enforced at the executor level and CANNOT be overridden:

1. **Single writer:** At most one transition in progress at any time (enforced by asyncio.Lock)
2. **Broker authority:** No model load without an ACTIVE `BudgetGrant`
3. **Epoch monotonicity:** `model_epoch` only increments, never decrements
4. **Cooldown enforcement:** Swap rejected if within cooldown window
5. **Swap budget:** Swap rejected if `swaps_this_hour >= max_swaps_per_hour`
6. **Context SLA:** Never shrink context below `JARVIS_MIN_CONTEXT_TOKENS` (default 2048) without explicit `DEGRADE` event
7. **Inventory binding:** `inventory_digest` is re-computed at `accept()` time, not carried from proposal. If inventory changed since proposal (e.g., download completed), the proposal is re-scored against current inventory. If re-scored proposal still selects the same variant → proceed. If a better variant is now available → reject proposal and generate new one. This prevents livelock where downloads invalidate pending proposals.
8. **Download isolation:** Downloads modify a separate `.quarantine/` directory. Only `GGUF_VALIDATED → AVAILABLE` moves a file into the main inventory. Inventory digest excludes `.quarantine/`.

#### 3.5.6 Integration with VRAMBudgetAuthority

J-Prime uses its own `VRAMBudgetAuthority` (not the Mac-side `MemoryBudgetBroker`) for VRAM admission control on the GCP VM:

```python
class VRAMBudgetAuthority:
    """Lightweight VRAM admission controller for GCP VM.
    Same grant lifecycle as MemoryBudgetBroker but for GPU VRAM."""

    async def request(
        self,
        component: str,              # e.g., "model-iq2_m"
        bytes_requested: int,         # VRAM bytes needed
        priority: VRAMPriority,       # CRITICAL | NORMAL | BACKGROUND
        *,
        ttl_seconds: float = 300.0,   # Longer TTL for model swaps (5 min)
    ) -> VRAMGrant:
        """Issue or deny a VRAM grant based on nvidia-smi free VRAM."""

@dataclass
class VRAMGrant:
    grant_id: str
    component: str
    granted_bytes: int
    state: LeaseState  # GRANTED → ACTIVE → RELEASED | ROLLED_BACK
    ttl_seconds: float
    created_at: float

    async def commit(self, actual_bytes: int) -> None: ...
    async def rollback(self, reason: str = "") -> None: ...
    async def release(self) -> None: ...
    async def heartbeat(self) -> None: ...
```

**Two-Grant Lifecycle During Model Swap:**

During CUTOVER, both old and new model grants exist simultaneously:

```
PREPARE:
  new_grant = await vram_authority.request("model-iq2_m", new_model_size)
  # Old grant still ACTIVE — both counted against VRAM budget
  # Authority checks: old_grant.granted_bytes + new_model_size <= total_vram
  # If insufficient: deny new_grant → ROLLBACK

DRAIN:
  # Both grants active, old model serving remaining requests
  # Heartbeat both grants to prevent TTL expiry

CUTOVER:
  await executor.unload()          # Free old model VRAM
  await old_grant.release()        # Release old grant (ACTIVE → RELEASED)
  await executor.load(new_model)   # Load new model into freed VRAM
  await new_grant.commit(actual_vram_used)  # GRANTED → ACTIVE

VERIFY:
  # Only new_grant active, old_grant released
  # Run smoke test

COMMIT:
  # new_grant remains ACTIVE until next transition

ROLLBACK (on any failure):
  await new_grant.rollback("transition failed")
  # If old model was unloaded, reload it:
  await executor.load(old_model)
  # old_grant must be re-acquired if it was released
```

**Note:** The `VRAMBudgetAuthority` must handle the transient state where both grants are active. The total VRAM budget accounts for: `model_A + model_B + kv_cache + overhead`. In practice on the L4, only one model fits at a time, so the authority must allow a brief overlap during CUTOVER where the old model is being unloaded.

---

### 3.6 `quality_regression_tester.py` — A/B Benchmarking

**Location:** `jarvis_prime/core/quality_regression_tester.py`
**Purpose:** Measure actual quality/speed of quantization variants for calibration data

#### 3.6.1 Benchmark Protocol

```python
@dataclass(frozen=True)
class BenchmarkSuite:
    """Standard prompts for regression testing."""
    prompts: tuple[BenchmarkPrompt, ...]
    version: str                          # Suite version for reproducibility

@dataclass(frozen=True)
class BenchmarkPrompt:
    name: str
    prompt: str
    expected_patterns: tuple[str, ...]    # Regex patterns expected in output
    max_tokens: int
    temperature: float = 0.1             # Low for reproducibility

@dataclass(frozen=True)
class BenchmarkResult:
    variant: ModelVariant
    suite_version: str
    mean_tok_s: float
    p50_tok_s: float
    p95_first_token_ms: float
    quality_score: float                  # Pattern match rate
    vram_peak_bytes: int
    context_tested: int
    timestamp: float
```

#### 3.6.2 Online Calibration

The tester runs **asynchronously in background** after model load, using idle GPU cycles:

1. Load model → serve production traffic
2. During idle periods (no pending requests for >5s), run one benchmark prompt
3. Accumulate results into `CalibrationData`
4. Feed back to `QuantizationIntelligence` to improve scoring accuracy
5. Store results to `models/calibration/{model_family}.json`

This corrects for **classifier drift** — if the theoretical quality score diverges from measured quality, calibration data overrides theory.

**Preemption rules:**
- Benchmarks are **always preemptible** by production traffic. If a request arrives mid-benchmark, the benchmark is abandoned (not queued).
- Maximum single benchmark prompt duration: 30 seconds. Longer benchmarks are split into multiple prompts.
- Benchmark results are used for **calibration curve fitting only** — never for pass/fail gating on model selection.
- Pattern matching at low temperature is inherently noisy across quantizations. Quality score is computed as ratio of coherent output tokens, not exact pattern match.

---

## 4. Contract Schema

### 4.1 Capability Negotiation

The existing `/v1/capability` endpoint is extended:

```json
{
    "schema_version": "2.0",
    "contract_version": "2.0.0",
    "model_loaded": true,
    "context_window": 8192,
    "compute_class": "gpu_l4",
    "model_id": "qwen2.5-coder-32b-instruct",
    "model_artifact": "Qwen2.5-Coder-32B-Instruct-IQ2_M.gguf",
    "quantization": {
        "method": "IQ2_M",
        "bits_per_weight": 2.70,
        "uses_importance_matrix": true,
        "quality_score": 0.87
    },
    "kv_cache": {
        "type_k": "q8_0",
        "type_v": "q8_0",
        "max_context": 12288
    },
    "transition_epoch": {
        "model_epoch": 3,
        "cache_epoch": 1,
        "inventory_epoch": 2
    },
    "gpu_layers": -1,
    "tok_s_estimate": 12.5,
    "vram_pressure_zone": "green",
    "host_id": "jarvis-prime-stable"
}
```

### 4.2 Compatibility Matrix

| Feature | JARVIS Supervisor | J-Prime (GCP) | Reactor |
|---------|-------------------|---------------|---------|
| Epoch awareness | Required (v2.0+) | Required | Optional |
| Quantization scoring | N/A | Required | N/A |
| VRAM monitoring | N/A | Required | N/A |
| Model transition events | Consumer | Producer | Consumer |
| Capability contract v2.0 | Consumer | Producer | N/A |

**Backwards compatibility:** If JARVIS supervisor doesn't understand contract v2.0, the extra fields are ignored. The existing v1.0 fields remain unchanged.

---

## 5. Integration Points

### 5.1 Where Decisions Are Made

Integration targets the **decision paths**, not launch wrappers:

| Integration Point | File | Change |
|-------------------|------|--------|
| Model loading at startup | `run_server.py` `_load_model()` | Replace static symlink resolution with `AdaptiveModelSelector.propose_optimal()` → `ModelTransitionManager.accept()` |
| Model swap on task change | `gcp_model_swap_coordinator.py` | Wire `AdaptiveModelSelector` as proposal source, `ModelTransitionManager` as executor |
| KV cache config | `llama_cpp_executor.py` `LlamaCppConfig` | Accept `KVCacheProfile` recommendations for `cache_type_k`/`cache_type_v` |
| Health reporting | `run_server.py` `/v1/capability` | Extend with quantization + epoch + pressure data |
| Supervisor awareness | `unified_model_serving.py` `ModelRouter` | Consume capability contract v2.0 for task routing decisions |

### 5.2 Supervisor Blast Radius

**No changes to `unified_supervisor.py`.** The supervisor consumes the extended capability contract via the existing `/v1/capability` HTTP endpoint. Quantization logic lives entirely in jarvis-prime.

---

## 6. Failure Modes & Recovery

### 6.1 Failure Injection Tests

| Test | Trigger | Expected Behavior |
|------|---------|-------------------|
| OOM during model load | Load model larger than VRAM | `CUTOVER` → `ROLLBACK`, reload previous model, emit `ModelLoadOOM` event |
| Partial download | Kill wget mid-download | Quarantine detects truncated file, hash mismatch → `REJECTED` |
| Stale inventory | Model file deleted while proposal pending | `inventory_digest` mismatch → proposal rejected |
| Monitor outage | pynvml crashes | Fallback to nvidia-smi subprocess, then to "assume YELLOW" safe default |
| Split-brain signals | Local says GREEN, broker says CONSTRAINED | Broker authority wins — broker denial → ROLLBACK |
| Rapid task oscillation | Alternate trivial/complex every 5s | Cooldown + dead zone prevent thrashing, sticky routing holds current model |
| Concurrent transition attempts | Two pressure events during drain | asyncio.Lock serializes — second attempt waits or is rejected |

### 6.2 State Restoration After Restart

**Default model definition:** The smallest model variant that fits in VRAM with minimum context (2048). This is computed dynamically from inventory — not a hardcoded filename. For the current L4 inventory, the fallback chain is:
1. `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf` (4.4GB — always fits)
2. `Llama-3.2-1B-Instruct-Q4_K_M.gguf` (771MB — emergency fallback)

On startup, `ModelTransitionManager`:
1. Computes `inventory_digest` from current model directory
2. Loads last-known state from `models/.transition_state.json`
3. If `inventory_digest` differs from stored → discard stale state, fresh proposal
4. If state was mid-transition (PREPARE/DRAIN/CUTOVER) → treat as ROLLBACK, load default model
5. **Crash counter:** Increment `consecutive_rollback_count` in state file. If ≥ 3 consecutive startup rollbacks → load emergency fallback (smallest model), emit `DEGRADED_STARTUP` alert
6. Reset `consecutive_rollback_count` to 0 on any successful `COMMIT`
7. Transition state file is deleted AFTER successful model load (not before)
8. Calibration data persists across restarts (file-backed)

---

## 7. SLO Guardrails

| Metric | Target | Enforcement |
|--------|--------|-------------|
| Max swaps per hour | 4 | Hard cap in `TransitionPolicy`, logged + alerted on hit |
| Swap latency (PREPARE→COMMIT) | < 60s for 7B, < 120s for 32B | Timeout on each phase, ROLLBACK if exceeded |
| p95 generation latency impact during swap | < 2x baseline | Drain protocol ensures no mixed-epoch requests |
| Rollback success rate | > 99% | Previous model path cached, tested on every COMMIT |
| Context floor | 2048 tokens minimum | Hard invariant, DEGRADE event if violated |
| Download verification | 100% hash-checked | Quarantine pipeline, no bypass |
| Monitor availability | > 99.5% uptime | Fallback chain: pynvml → nvidia-smi → safe default |

---

## 8. Observability

### 8.1 Bounded Label Dimensions

To prevent cardinality explosion:

| Metric | Labels | Max Cardinality |
|--------|--------|-----------------|
| `model_transition_total` | `trigger`, `outcome` | 4×3 = 12 |
| `model_load_seconds` | `model_family`, `quant_tier` | 6×4 = 24 |
| `vram_pressure_zone` | `zone` | 4 |
| `inference_tok_s` | `model_family`, `quant_tier` | 6×4 = 24 |

**Quant tiers** (not individual quant names): `2bit`, `3bit`, `4bit`, `8bit`
**Model families** (not individual files): `qwen-7b`, `qwen-14b`, `qwen-32b`, `deepseek-7b`, `llama-1b`, `phi-3.5`

### 8.2 Structured Events

All transitions emit structured JSON events to the existing telemetry pipeline:

```python
@dataclass(frozen=True)
class ModelTransitionEvent:
    event_type: str              # "transition_started" | "transition_completed" | "transition_failed"
    transition_id: str           # "trans-{uuid7}"
    trigger: str                 # "startup" | "pressure" | "task" | "manual"
    from_model: Optional[str]
    to_model: str
    from_quant: Optional[str]
    to_quant: str
    model_epoch: int
    duration_ms: Optional[float]
    outcome: str                 # "commit" | "rollback" | "denied"
    reason: str
    timestamp: float
```

---

## 9. File Layout

```
jarvis-prime/
  jarvis_prime/
    core/
      quantization_intelligence.py    # ~400 lines — rate-distortion scoring
      adaptive_model_selector.py      # ~600 lines — inventory + proposals
      vram_pressure_monitor.py        # ~350 lines — GPU memory watchdog
      kv_cache_optimizer.py           # ~250 lines — context window math
      model_transition_manager.py     # ~700 lines — THE executor FSM
      quality_regression_tester.py    # ~300 lines — A/B benchmarking
```

**Total new code:** ~2,600 lines across 6 focused modules.

---

## 10. Implementation Priority

| Phase | Module | Why First |
|-------|--------|-----------|
| 1 | `quantization_intelligence.py` | Pure math, no dependencies, enables scoring |
| 2 | `kv_cache_optimizer.py` | Pure math, informs model selection |
| 3 | `adaptive_model_selector.py` | Depends on Phase 1+2 for scoring |
| 4 | `model_transition_manager.py` | Core executor, depends on Phase 3 for proposals |
| 5 | `vram_pressure_monitor.py` | Feeds events to Phase 4 executor |
| 6 | `quality_regression_tester.py` | Background calibration, lowest priority |
| 7 | Integration patches | Wire into run_server.py, capability endpoint |

---

## 11. Go/No-Go Checklist

- [ ] Deterministic state machine for swap/upgrade/downgrade (Section 3.5.1)
- [ ] Formal conflict resolution between memory broker and selector (Section 3.5.6)
- [ ] Epoch-based cutover and in-flight drain semantics (Section 3.5.2, 3.5.3)
- [ ] Contract schema with compatibility matrix (Section 4)
- [ ] Failure injection tests defined (Section 6.1)
- [ ] SLO guardrails with hard caps (Section 7)
- [ ] Observability with bounded cardinality (Section 8)
- [ ] No changes to unified_supervisor.py (Section 5.2)
- [ ] Download trust chain with quarantine (Section 3.2.4)
- [ ] Cross-node safety (Section 3.3.4)

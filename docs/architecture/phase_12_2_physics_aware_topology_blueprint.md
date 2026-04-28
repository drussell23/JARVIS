# Phase 12.2 — Physics-Aware Topology Routing

**Status:** BLUEPRINT (DRAFT) — implementation pending Phase 12 Slice E graduation
**Mandate:** [ARCHITECTURAL DIRECTIVE: PHYSICS-AWARE TOPOLOGY ROUTING] (2026-04-27)
**Premise:** DoubleWord is a decentralized hostile mesh of GPU spot instances, not a monolithic endpoint. Sentinel must physically model the infrastructure dynamically.

## 1. Why this exists

Phase 12 (Dynamic Catalog Discovery) gives us *which* models exist. Phase 12.2 gives us *which models are actually usable right now* against the physical reality of DW's infrastructure:

- **VRAM evictions** masquerade as stream-stalls (the L2 Cache Illusion)
- **Cold-storage NVMe loads** masquerade as random latency spikes
- **Multi-tenant queuing collapses** masquerade as transient transport errors

Static `block_mode` decisions and exponential-backoff retries treat all three as the same failure mode. They are not. Phase 12.2 distinguishes them via live telemetry, then routes around each one differently.

## 2. Three mechanisms

### 2.1 Deep VRAM Probing (defeats the L2 Cache Illusion)

**The flaw**: A 1-token sentinel ping verifies network connectivity. It tests TCP, TLS, auth, request validation, the SSE handshake — and almost nothing else. A 1-token response fits in the GPU's L2 cache; the model weights don't even need to be loaded into HBM. A node that's been evicted from VRAM (because another tenant's payload took priority) **still passes the 1-token probe** while being completely unable to serve a real PLAN-EXPLOIT payload.

**The fix**: A staggered "Heavy Probe" — synthetic 500-token completion request — issued asynchronously to a random subset of registered DW models during idle cadences. Heavy probes verify VRAM allocation, not just connectivity.

**Specification**:

| Knob | Default | Purpose |
|---|---|---|
| `JARVIS_TOPOLOGY_HEAVY_PROBE_RATIO` | `0.2` | Fraction of probes that are heavy (already in YAML monitor block) |
| `JARVIS_TOPOLOGY_HEAVY_PROBE_TOKENS` | `500` | Output token count for synthetic payload |
| `JARVIS_TOPOLOGY_HEAVY_PROBE_INPUT_CHARS` | `2000` | Input prompt size (synthetic, fixed-content lorem-style filler so cache hits don't cheat the test) |
| `JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S` | `30` | Hard timeout — heavy probe must produce ≥500 tokens within this window |
| `JARVIS_TOPOLOGY_VRAM_CONSTRAINED_FAILURE_WEIGHT` | `5.0` | Failure source weight for `LIVE_VRAM_CONSTRAINED` (higher than `LIVE_STREAM_STALL=3.0` so a single heavy-fail trips the breaker) |

**New `FailureSource` value**: `LIVE_VRAM_CONSTRAINED`. A node that passes a 1-token light probe within the same 60-second window but fails the heavy probe is **proven** VRAM-constrained — the contradiction itself is the evidence. The breaker for that `model_id` flips to OPEN with `failure_source=LIVE_VRAM_CONSTRAINED`. Distinguished from `LIVE_STREAM_STALL` so post-incident review can tell "the network broke" from "the GPU evicted us."

**Probe scheduling**:
- Heavy probes are NOT every-cycle — that would be wasteful (~$0.001 per probe on Qwen3.5-9B). At `heavy_probe_ratio=0.2`, every 5th sentinel probe is heavy
- Per-model heavy-probe cadence: minimum 5 minutes between consecutive heavy probes against the same `model_id`. Prevents accidentally DOSing a recovering node
- Heavy probes only fire during sentinel's existing `probe_interval_healthy_s=30` cadence — no new background task needed; just an upgraded payload class

**Code seam**: extends `topology_sentinel.py:_probe_endpoint()`. The existing probe path issues a 1-token request via the DW provider; Phase 12.2 adds a `_probe_endpoint_heavy()` variant that's selected randomly per cycle. The async dispatch is already there — only the request shape changes.

**Cost guard**: Heavy probes are gated by `cost_governor.py` like any other DW call. A misbehaving probe loop can't burn the daily budget because the heavy probe inherits the SPECULATIVE cost cap (~$0.001/op).

### 2.2 TTFT Cold-Storage Detection (Dynamic Quarantine)

**The flaw**: When DW's scheduler routes us to a model whose weights haven't been used recently, the inference node has to load 30–400 GB from NVMe SSD into HBM before it can produce the first token. This is a one-time per-cold-start ~30–120 second penalty that masquerades as "the request hung." Static timeout thresholds either fail-fast (incorrectly evicting a model that would have worked in 60s) or fail-slow (giving up after 180s when the model never had a chance).

**The fix**: Per-`model_id` Time-To-First-Token tracking with a moving-average + standard-deviation gate. When TTFT exceeds the dynamic statistical threshold, the model is **mathematically proven** to be in cold storage — and we don't speculate about why. We demote.

**Specification**:

| Knob | Default | Purpose |
|---|---|---|
| `JARVIS_TOPOLOGY_TTFT_WINDOW_N` | `20` | Rolling sample size — needs enough data for stable statistics |
| `JARVIS_TOPOLOGY_TTFT_STDDEV_THRESHOLD` | `2.0` | Deviation in σ above mean that triggers cold-storage demotion |
| `JARVIS_TOPOLOGY_TTFT_MIN_SAMPLES` | `5` | Cold-start guard — won't demote until window has at least N samples |
| `JARVIS_TOPOLOGY_TTFT_DEMOTION_DURATION_S` | `300` | How long a cold-storage demotion lasts before re-evaluation |
| `JARVIS_TOPOLOGY_TTFT_RECOVERY_RATIO` | `0.5` | Demotion lifts when latest TTFT ≤ 50% of pre-demotion mean (warm-up confirmed) |

**New module**: `dw_ttft_observer.py`

```python
@dataclass(frozen=True)
class TtftSample:
    model_id: str
    ttft_ms: int
    sample_unix: float
    op_id: str

class TtftObserver:
    """Per-model TTFT statistical tracker. Owns the rolling sample
    buffer, computes mean + stddev on demand, fires demotion events
    when statistical threshold crosses.

    Pure observer — does NOT route, does NOT demote. Emits events
    that the dispatcher consumes via TrinityEventBus."""

    def record_ttft(self, model_id: str, ttft_ms: int, op_id: str) -> None: ...
    def stats(self, model_id: str) -> Optional[TtftStats]: ...
    def is_cold_storage(self, model_id: str) -> bool: ...
    def cold_storage_models(self) -> Tuple[str, ...]: ...
```

**Statistical gate** (re-evaluated on every record_ttft):

```
if window_size >= MIN_SAMPLES:
    mean, stddev = compute(window[-WINDOW_N:])
    threshold = mean + STDDEV_THRESHOLD * stddev
    if latest_ttft > threshold:
        emit_event(dw_cold_storage_detected, model_id, ttft_ms, threshold)
        demote_until = now + DEMOTION_DURATION_S
```

**Why standard deviation, not absolute threshold**: a 2-second TTFT on a 4B model is cold-storage; on a 397B model it's normal warm operation. Statistical thresholds adapt to each model's baseline automatically — exactly what Zero-Order static thresholds can't do.

**Demotion semantics**:
- Cold-storage demotion ≠ promotion-ledger demotion. The promotion ledger is for ambiguous-metadata graduation (Phase 12 Slice B). Cold-storage demotion is a **temporary route override** — the model goes to SPECULATIVE for `DEMOTION_DURATION_S` seconds, then auto-recovers if its post-demotion TTFT samples normalize
- Distinguished events: `dw_model_cold_storage_demoted` vs `dw_model_promotion_demoted`. Different SSE event types; observers can react differently
- The classifier (Phase 12 Slice B) receives a `cold_storage_models` set when classifying; those models are temporarily excluded from BG/STANDARD/COMPLEX assignments and pinned to SPECULATIVE — same surface as quarantine but with an expiration timestamp

**Code seam**: TTFT measurement already exists in `doubleword_provider.py` SSE stream — first chunk arrival timestamp minus request send timestamp. Currently we discard it; Phase 12.2 wires it through to `TtftObserver.record_ttft()`. Single-line change at the provider; the observer module is the new code.

### 2.3 Full-Jitter Backoff (defeats Little's Law thundering herd)

**The flaw**: Exponential backoff with exact base intervals (10s, 20s, 40s, 80s, 160s) means every client retrying the same endpoint synchronizes at the same offsets. After a DW outage, a thousand instances of similar agentic systems all retry at exactly t+10s, then t+30s, then t+70s — creating retry pulses that crash the recovered endpoint immediately. This is Little's Law's revenge: queue depth = arrival rate × service time, and synchronized arrival pulses mean the queue depth blows past the server's capacity instantly.

**The fix**: Full-jitter exponential backoff. Every retry delay is a random uniform sample from `[0, base * 2^attempt]`. The waveform is desynchronized — our retries fall into the micro-gaps of DW's queue backlog instead of stacking on the herd's wavefronts.

**Specification**:

```python
def full_jitter_backoff_s(
    attempt: int,
    *,
    base_s: float = 10.0,
    cap_s: float = 300.0,
    rng: Optional[random.Random] = None,
) -> float:
    """Returns a random delay in [0, min(cap_s, base_s * 2**attempt)].
    Pure function. No state. NEVER raises."""
    rng = rng or random
    upper = min(cap_s, base_s * (2 ** max(0, attempt)))
    return rng.uniform(0.0, upper)
```

**Where it applies (every retry site)**:
1. `topology_sentinel.py` — `CircuitBreaker` HALF_OPEN probe schedule (currently `probe_backoff_base_s=10.0` exact-exponential, Phase 12.2 replaces with full-jitter)
2. `doubleword_provider.py` — provider-level retries on transient HTTP 5xx / 429 / connection reset
3. `candidate_generator.py` — between sentinel-driven model rotation attempts (today: no inter-attempt delay; Phase 12.2 inserts a small full-jittered delay between attempts on the same op to avoid rapid-fire pounding when DW is under load)
4. `batch_future_registry.py` — DW Tier 1 webhook adaptive poll fallback (already has retry; just upgrade to full-jitter)

**Single source-of-truth helper**: `backend/core/ouroboros/governance/full_jitter.py` (~30 lines including tests). Every callsite imports + calls one function. Eliminates the chance that one component drifts to exact-backoff while another is jittered.

**Determinism for tests**: `full_jitter_backoff_s` accepts an optional `rng=random.Random(seed)` parameter so test suites can pin specific delay sequences without mocking `random.uniform`.

**Knob**:

| Knob | Default | Purpose |
|---|---|---|
| `JARVIS_TOPOLOGY_BACKOFF_BASE_S` | `10.0` | Base delay (already in YAML monitor block) |
| `JARVIS_TOPOLOGY_BACKOFF_CAP_S` | `300.0` | Maximum delay regardless of attempt count |
| `JARVIS_TOPOLOGY_FULL_JITTER_ENABLED` | `false` → `true` at graduation | Master flag for hot-revert if jitter introduces unforeseen issues |

## 3. Slicing plan (4 slices, all defaults false until graduation)

### Slice 12.2.A — Full-jitter helper + retrofit (lowest risk, highest leverage)
- New module `full_jitter.py` (~30 lines + 25 tests)
- Replace exact-exponential backoff at all 4 retry sites
- Master flag `JARVIS_TOPOLOGY_FULL_JITTER_ENABLED` (default false)
- Behavioral test: with seed=1, generated delay sequence matches a pinned expected list (determinism contract)
- **Why first**: simplest mechanism, no new state, no statistical complexity. Lands the desync win immediately

### Slice 12.2.B — TTFT observer module
- New module `dw_ttft_observer.py` (~200 lines + 35 tests)
- Provider wiring: 1-line change in `doubleword_provider.py` SSE stream to call `observer.record_ttft()` on first chunk
- TTFT samples persisted to disk via atomic write (mirrors posture_store pattern; survives restart for stable statistics across boots)
- Master flag `JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED` (default false)
- **No demotion yet** — Slice 12.2.B is observer-only. Slice 12.2.C consumes the observer's output

### Slice 12.2.C — TTFT cold-storage demotion wiring
- Classifier extension: accepts cold_storage_models() from observer, excludes them from BG/STANDARD/COMPLEX assignments
- Sentinel extension: emits `dw_model_cold_storage_demoted` / `dw_model_cold_storage_recovered` events
- Auto-recovery: when post-demotion TTFT samples normalize (latest ≤ 50% of pre-demotion mean), demotion lifts
- Master flag `JARVIS_TOPOLOGY_TTFT_DEMOTION_ENABLED` (default false)

### Slice 12.2.D — Heavy probe (defeats L2 Cache Illusion)
- Sentinel extension: `_probe_endpoint_heavy()` variant, gated by heavy_probe_ratio (already in YAML — just operationalize the existing knob)
- New `FailureSource.LIVE_VRAM_CONSTRAINED` enum value
- Per-model heavy-probe cadence ledger (5-minute minimum between consecutive heavy probes)
- Cost guard via existing `cost_governor.py`
- Master flag `JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED` (default false)

### Slice 12.2.E — Graduation flip
- All four flags flip default false → true
- Graduation pin suite (mirrors Phase 11 Slice 11.7 pattern):
  - Hot-revert per-flag matrix
  - Defaults-true assertions
  - Master-off legacy preservation
- 3 forced-clean soak sessions before flip

## 4. Failure modes & cost contract

| Scenario | Phase 12.2 behavior | Cost contract |
|---|---|---|
| All DW models in cold storage | Statistical observer demotes all to SPECULATIVE; sentinel falls through to YAML or cascades per `fallback_tolerance` | BG/SPEC stay queue-only; no Claude cascade |
| Heavy probe budget overrun | `cost_governor` caps the probe; sentinel skips heavy probe for that cycle | No surprise spend; light probes still run |
| Full-jitter introduces a longer-than-exact delay | Single retry takes longer; total recovery time still bounded by `cap_s=300s` | No cost impact; latency variance increases |
| TTFT observer disk-cache corrupt | Observer treats as missing → samples re-accumulate from boot; statistical gate uses fewer samples until window fills | Soft degradation; no cost impact |
| Heavy probe payload triggers a new VRAM eviction | The breaker that caught the eviction is the same one that would have caught the eviction during a real PLAN-EXPLOIT payload — heavy probe IS the canary it claims to be | Heavy probe cost capped by SPECULATIVE budget |

## 5. Why these three together

Each mechanism, alone, partially fixes one failure mode:

- VRAM probing alone: catches L2 cache illusion but doesn't distinguish cold-storage from genuine outages
- TTFT observer alone: catches cold-storage but routes to nodes that pass TTFT yet stream-stall mid-response (VRAM eviction during generation)
- Full-jitter alone: prevents thundering-herd amplification but doesn't help when the single endpoint we're hitting is genuinely cold

Together they form a **physics-faithful model of the DW infrastructure**: connectivity (light probe) + VRAM allocation (heavy probe) + warm-vs-cold weights (TTFT observer) + queue desync (full-jitter). The dispatcher reasons about each layer separately and routes around each failure mode with the correct response.

## 6. Out of scope (deferred)

- **Multi-region DW endpoint discovery**: today we hit one base URL. If DW exposes a regional health endpoint, we'd add per-region load balancing. Not in 12.2.
- **Tenant-aware backoff**: full-jitter is desync-by-randomness. Coordinating with DW to expose explicit retry-after headers would be deterministic but requires their cooperation. Not in 12.2.
- **GPU memory pressure SSE stream**: if DW exposes a server-pushed pressure indicator, we could pre-emptively demote without waiting for stream-stall evidence. Not in 12.2.
- **Adaptive heavy-probe ratio**: today static at 0.2. A future arc could dynamically increase the ratio when a node has recent failures and decrease when stable. Not in 12.2.

## 7. Verification protocol

Each slice closes with:

1. Unit tests green
2. Combined regression (existing topology_sentinel + provider_topology + candidate_generator suites)
3. Live-fire battle test session with the new flag enabled, observing the new event types in the debug log
4. Cost contract verification: BG/SPEC must stay $0.00 on Claude cascade across the soak

Slice 12.2.E graduation requires:
- 3 consecutive sessions with at least 1 `dw_model_cold_storage_recovered` event (proves the auto-recovery path actually fires)
- 3 consecutive sessions with at least 1 `LIVE_VRAM_CONSTRAINED` breaker trip OR sustained pass (proves heavy probe is exercising the path, not silently no-op)
- Zero `full_jitter_invariant_violation` events (any retry without jitter is a regression)

## 8. The honest architectural read

The TopologySentinel was built assuming a stable, monolithic endpoint. Phase 12.2 is the operator's correction: DW is a hostile mesh of spot instances, and the sentinel needs to model that physics natively. The three mechanisms (VRAM probe, TTFT statistical gate, full-jitter backoff) are not features — they are the architectural debt of pretending GPU clusters are ATMs that always have cash. They were Zero-Order workarounds in disguise. Phase 12.2 makes them First-Order.

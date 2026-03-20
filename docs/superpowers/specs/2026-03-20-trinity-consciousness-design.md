# Trinity Consciousness — Design Specification

**Date**: 2026-03-20
**Status**: Draft
**Branch**: `feat/trinity-consciousness`

---

## 1. Problem Statement

Ouroboros has a production-grade governance pipeline with proactive iteration (Autonomy
Iteration Mode), parallel execution (SubagentScheduler L3), cross-repo sagas, trust
graduation, and 1508 passing tests. However, it lacks **self-awareness** — it doesn't
know its own health trends, doesn't learn from its operation history, doesn't predict
failures, and doesn't use idle GPU time for speculative analysis.

Trinity Consciousness is the self-awareness layer that makes the Trinity (JARVIS/Prime/
Reactor) a self-understanding, self-improving entity.

## 2. Decision Locks

1. **Read-mostly.** Consciousness observes, aggregates, predicts, and pre-computes. It does NOT execute changes — that's AutonomyIterationService's job.
2. **Local-first with cloud acceleration.** HealthCortex + MemoryEngine run without GPU. DreamEngine + ProphecyEngine use J-Prime GPU when available, degrade gracefully when not.
3. **Dream Mode never wakes the VM.** It only uses GPU that's already warm from user activity. Never extends VM uptime. Never triggers wakeups.
4. **Interactive always preempts.** User requests immediately preempt Dream/Prophecy workloads with cancellation + resume semantics.
5. **Separate concurrency budgets.** Interactive (8192 tokens), Dream (2048), Prophecy (1024). Background speculation cannot starve foreground reasoning.
6. **Speculative outputs are advisory only.** Blueprints never auto-promote to execution without passing the full governance path (trust tier, blast radius, preflight).

## 3. Architecture

```
TrinityConsciousness (Zone 6.11 in supervisor)
|
+-- HealthCortex
|   +-- Polls all .health() endpoints every 10s
|   +-- Aggregates into TrinityHealthSnapshot
|   +-- Detects state transitions (HEALTHY -> DEGRADED -> CRITICAL)
|   +-- Emits to CommProtocol + VoiceNarrator
|
+-- MemoryEngine
|   +-- Subscribes to operation outcomes (via ledger)
|   +-- Builds MemoryInsight per completed op
|   +-- Indexes patterns: "this file breaks when X changes"
|   +-- Feeds strategic context to IterationPlanner + DreamEngine
|   +-- Persists to ~/.jarvis/ouroboros/consciousness/memory/
|
+-- DreamEngine
|   +-- Activates during idle periods (user inactive + GPU warm)
|   +-- Runs speculative analysis via J-Prime qwen_coder_14b/32b
|   +-- Pre-computes ImprovementBlueprint objects (plans, NOT execution)
|   +-- Stores ranked blueprints for instant retrieval
|   +-- Respects ResourceGovernor (yields immediately on user activity)
|
+-- ProphecyEngine
    +-- Analyzes changes for risk signals (blast radius, complexity delta)
    +-- Computes regression probability per file/module
    +-- Uses J-Prime deepseek_r1 when available, heuristic fallback when not
    +-- Emits warnings before failures happen
    +-- Feeds high-risk predictions to HealthCortex
```

### Ownership

| Component | Owns | Does NOT Own |
|-----------|------|-------------|
| TrinityConsciousness | Lifecycle of all 4 engines, morning briefing, Zone 6.11 | Iteration execution, code generation, file writes |
| HealthCortex | Real-time health aggregation, state transition detection, trend storage | Remediation actions |
| MemoryEngine | Cross-session learning, file reputation, pattern indexing | Governance decisions |
| DreamEngine | Speculative analysis, blueprint pre-computation, idle GPU scheduling | Blueprint execution (advisory only) |
| ProphecyEngine | Risk prediction, failure probability scoring | Test execution, change blocking |

### J-Prime Integration

```
HealthCortex ------- polls -------> J-Prime /health endpoint
MemoryEngine --- classifies via --> J-Prime phi3_lightweight (1B, CPU, cheapest)
DreamEngine ---- speculates via --> J-Prime qwen_coder_14b/32b (GPU, idle window)
ProphecyEngine -- reasons via ----> J-Prime deepseek_r1 (7B, reasoning)
```

GPU-dependent engines (Dream, Prophecy) require J-Prime READY with model loaded.
When J-Prime is stopped, they go dormant with explicit reason codes.

## 4. Engine Contracts

### 4.1 HealthCortex

**Interface:**
```python
class HealthCortex:
    async def start() -> None
    async def stop() -> None
    def get_snapshot() -> TrinityHealthSnapshot    # latest cached, never blocks
    def get_trend(window_minutes: int) -> HealthTrend  # rolling history
```

**Readiness gate:** None (runs unconditionally).

**Inputs:**
| Signal | Source | Frequency |
|--------|--------|-----------|
| GLS health | governed_loop_service.health() | 10s |
| ILS health | intake_layer_service.health() | 10s |
| Iteration health | iteration_service.health() | 10s |
| Scheduler health | subagent_scheduler.health() | 10s |
| Oracle staleness | oracle.index_age_s() | 10s |
| J-Prime VM state | gcp_vm_manager + /health endpoint | 10s |
| J-Prime model | /health -> model_loaded, phase, tok/s | 10s |
| Reactor consumer | reactor_event_consumer.health() | 10s |
| Host resources | psutil CPU/RAM/disk | 10s |
| Budget state | brain_selector.daily_spend + budget_guard | 10s |
| Trust tier | trust_graduator.get_config() | 10s |

**Timeout:** 5s per health poll (fault-isolated per subsystem).

**Fallback:** If a subsystem's health() raises, mark it UNKNOWN. Three consecutive
UNKNOWN transitions to DEGRADED.

**Event schema:**
```python
CommMessage(msg_type=HEARTBEAT, payload={
    "source": "consciousness.health_cortex",
    "overall_verdict": "HEALTHY",
    "overall_score": 0.94,
    "transition": "DEGRADED->HEALTHY",  # only on state changes
})
```

**Failure class:** Subsystem health poll timeout, psutil unavailable, J-Prime
endpoint unreachable. All fault-isolated — never crashes the cortex.

**Trend storage:** Rolling 720 snapshots (2 hours at 10s). In-memory with periodic
flush to `~/.jarvis/ouroboros/consciousness/health_trend.jsonl`.

### 4.2 MemoryEngine

**Interface:**
```python
class MemoryEngine:
    async def start() -> None       # load from disk
    async def stop() -> None        # flush to disk
    async def ingest_outcome(op_result, ctx) -> None
    def query(query: str, max_results: int) -> List[MemoryInsight]
    def get_file_reputation(file_path: str) -> FileReputation
    def get_pattern_summary() -> PatternSummary
```

**Readiness gate:** None (local-only, reads ledger files on disk).

**Inputs:**
| Signal | Source | Trigger |
|--------|--------|---------|
| Operation outcome | OperationResult from GLS/Iteration | Per completion |
| Ledger history | ~/.jarvis/ouroboros/ledger/*.jsonl | On startup scan |
| Existing MemoryFacts | AdvancedAutonomyService | On startup |
| Git blame | git log --follow | On file reputation query |

**Timeout:** Ledger scan capped at 30s on startup. Individual queries < 100ms.

**Fallback:** If ledger files are corrupted, skip them (log warning). If git is
unavailable, file reputation uses ledger-only data (reduced accuracy).

**Event schema:**
```python
CommMessage(msg_type=PLAN, payload={
    "source": "consciousness.memory_engine",
    "event": "pattern_detected",
    "pattern": "auth.py changes break ECAPA tests 60% of the time",
    "confidence": 0.85,
    "evidence_count": 5,
})
```

**Failure class:** Corrupted ledger, disk full, git subprocess failure. All gracefully
degraded — memory works with whatever data is available.

**TTL + Invalidation:**
- Default TTL: 168 hours (1 week).
- Confidence decays 10% per day after TTL.
- Archived at confidence < 0.2.
- Invalidated on repo HEAD change if references stale line numbers/function names.

**Persistence:**
- `~/.jarvis/ouroboros/consciousness/memory/insights.jsonl`
- `~/.jarvis/ouroboros/consciousness/memory/file_reputations.json`
- `~/.jarvis/ouroboros/consciousness/memory/patterns.json`

### 4.3 DreamEngine

**Interface:**
```python
class DreamEngine:
    async def start() -> None
    async def stop() -> None
    def get_blueprints(top_n: int) -> List[ImprovementBlueprint]
    def get_blueprint(blueprint_id: str) -> Optional[ImprovementBlueprint]
    def discard_stale() -> int
```

**Readiness gates (ALL must pass):**
1. J-Prime READY with model loaded (not just VM running)
2. No user activity for >= IDLE_ENTRY_THRESHOLD_S (default 600s = 10 min)
3. VM was NOT woken by DreamEngine (dream_must_not_wake_vm invariant)
4. ResourceGovernor.should_yield() == False
5. Daily dream budget not exhausted (dream_minutes_today < max)

**Inputs:**
| Signal | Source | Trigger |
|--------|--------|---------|
| Codebase state | Oracle semantic search + graph | Per dream job |
| Miner findings | OpportunityMinerSensor.scan_once() | Per dream cycle |
| File reputations | MemoryEngine.get_file_reputation() | Per candidate file |
| Health snapshot | HealthCortex.get_snapshot() | Per gate check |
| User activity | Last interaction timestamp | Per gate check |

**Timeout:** Per dream job: 180s. Per J-Prime call within a job: 60s.

**Fallback:** When J-Prime unavailable: dormant with reason code JPRIME_UNAVAILABLE.
Does NOT run local heuristics as substitute. Emits DREAM_DORMANT event.

**Event schema:**
```python
CommMessage(msg_type=PLAN, payload={
    "source": "consciousness.dream_engine",
    "event": "blueprint_computed",
    "blueprint_id": "bp-abc123",
    "title": "Reduce voice auth complexity",
    "priority_score": 0.87,
    "model_used": "qwen_coder_14b",
})
```

**Failure class:** J-Prime timeout, preemption mid-job, model not loaded,
resource pressure. All result in job abandonment (resume on next idle window).

**Economic guardrails:**
- Never triggers VM wakeup
- Never extends VM uptime (does not reset idle timer)
- Never runs if VM was started specifically for dreaming
- Only uses GPU already warm from user activity

**Preemption semantics:**
- `asyncio.Event` flag checked between every J-Prime call
- User activity sets the flag immediately
- DreamEngine abandons current job, saves partial state
- Resume on next idle window (idempotent job keys prevent re-work)

**Concurrency budget:** 2048 tokens max per dream prompt.

**Idempotent job keys:**
```python
job_key = sha256(f"{repo_sha}:{policy_hash}:{prompt_family}:{model_class}")
```
Same inputs produce same key. Skip if already computed and not stale.

**Staleness + invalidation:**
- TTL expired (default 24h)
- Repo HEAD changed since computation
- Policy hash changed since computation
- Oracle dependency graph drifted for target files

**Flap damping:**
- IDLE_ENTRY_THRESHOLD_S = 600 (10 min idle before dreaming)
- IDLE_REENTRY_COOLDOWN_S = 300 (5 min after user return before re-entering)

**Cost observability:**
```python
@dataclass
class DreamMetrics:
    opportunistic_compute_minutes: float
    preemptions_count: int
    blueprints_computed: int
    blueprints_discarded_stale: int
    blueprint_hit_rate: float
    jobs_deduplicated: int
    estimated_cost_saved_usd: float
```

### 4.4 ProphecyEngine

**Interface:**
```python
class ProphecyEngine:
    async def start() -> None
    async def stop() -> None
    async def analyze_change(files_changed, diff_summary) -> ProphecyReport
    def get_risk_scores() -> Dict[str, float]
```

**Readiness gates:**
- Without J-Prime: heuristic-only (Oracle graph + MemoryEngine). Confidence capped at 0.6.
- With J-Prime: deepseek_r1 reasoning for higher-confidence predictions.

**Inputs:**
| Signal | Source | Trigger |
|--------|--------|---------|
| File changes | Git diff | Per commit/operation |
| Dependency graph | Oracle.get_file_neighborhood() | Per change |
| Historical failures | MemoryEngine.get_file_reputation() | Per file |
| Complexity delta | AST comparison | Per file |

**Timeout:** 30s per analysis (with J-Prime), 5s heuristic-only.

**Fallback:** Heuristic scoring with reason code JPRIME_UNAVAILABLE_HEURISTIC_ONLY.

**Event schema:**
```python
CommMessage(msg_type=PLAN, payload={
    "source": "consciousness.prophecy_engine",
    "event": "risk_assessment",
    "risk_level": "high",
    "predicted_failures": [...],
    "confidence": 0.78,
})
```

**Failure class:** J-Prime timeout, Oracle stale, git unavailable. Degrades to
reduced-confidence heuristics.

**Concurrency budget:** 1024 tokens max per prophecy prompt.

**Heuristic risk scoring (no GPU):**
```
score = (1 - success_rate) * 0.3      # historical failure rate
      + fragility_score * 0.3          # from MemoryEngine
      + min(dependents/20, 1.0) * 0.2  # blast radius
      + min(complexity/50, 1.0) * 0.2  # cyclomatic complexity
```

## 5. Unified State Model

### TrinityHealthSnapshot (computed every 10s)

```python
@dataclass(frozen=True)
class TrinityHealthSnapshot:
    timestamp_utc: str
    overall_verdict: str          # HEALTHY | DEGRADED | CRITICAL
    overall_score: float          # 0.0-1.0 weighted composite

    jarvis: SubsystemHealth       # GLS, ILS, Oracle, Iteration, sensors
    prime: SubsystemHealth        # VM state, model readiness, tok/s, brains
    reactor: SubsystemHealth      # event consumer, training queue depth

    resources: ResourceHealth     # CPU, RAM, disk, pressure level
    budget: BudgetHealth          # daily spend, iteration spend, remaining
    trust: TrustHealth            # current tier, graduation progress
```

### Morning Briefing

On startup, after all engines initialize:

```python
async def _announce_briefing(self):
    snapshot = self._health_cortex.get_snapshot()
    blueprints = self._dream_engine.get_blueprints(top_n=3)
    patterns = self._memory_engine.get_pattern_summary()

    briefing = compose_briefing(snapshot, blueprints, patterns)
    await safe_say(briefing, source="consciousness")
```

Example output:
```
"Good morning, Derek. Trinity health is 94%.
 J-Prime is loading models - ready in about 2 minutes.
 Overnight, I pre-analyzed 12 improvement opportunities.
 Top priority: voice auth confidence dropped 3% this week.
 3 items in your backlog. No critical alerts."
```

## 6. Safety Invariants

1. **Dream never wakes VM.** Checks vm_state == RUNNING, not just health endpoint.
2. **Interactive preempts all.** asyncio.Event flag checked between every LLM call.
3. **Separate token budgets.** Interactive: 8192, Dream: 2048, Prophecy: 1024.
4. **State gating.** GPU engines require READY + model_loaded, not just VM running.
5. **Blueprint TTL.** 24h default, invalidated on HEAD/policy/dependency change.
6. **Idempotent jobs.** sha256(repo_sha + policy_hash + prompt_family + model_class).
7. **Explicit fallback.** Dormant with reason codes, not silent no-ops.
8. **Cost observability.** Track compute minutes, preemptions, stale discards, hit rate.
9. **Advisory only.** Blueprints go through full governance path before execution.
10. **Flap damping.** 10 min entry + 5 min reentry cooldown prevents thrashing.
11. **Disk rotation.** health_trend.jsonl max 10MB, insights.jsonl max 50MB. On rotation, oldest entries archived/pruned.
12. **Dream bypasses idle timer.** DreamEngine calls J-Prime via direct HTTP (not PrimeRouter), so `record_jprime_activity()` is NOT invoked and the VM idle-stop timer continues counting down. This is the critical economic invariant.
13. **VM start-reason tracking.** `ensure_static_vm_ready()` records `start_reason` ("user_request", "iteration", "health_check"). DreamEngine gate #3 checks `start_reason != "dream"` AND `vm_uptime > IDLE_ENTRY_THRESHOLD_S`.

## 10. Review Fixes

### C1: Health Adapter Normalization

Each subsystem returns ad-hoc `Dict[str, Any]` from `.health()`. HealthCortex uses
per-subsystem adapter functions to normalize into `SubsystemHealth`:

```python
@dataclass(frozen=True)
class SubsystemHealth:
    name: str
    status: str          # "healthy" | "degraded" | "unknown" | "offline"
    score: float         # 0.0-1.0 normalized
    details: Dict[str, Any]  # raw health dict preserved for debugging
    polled_at_utc: str

def _adapt_gls_health(raw: Dict) -> SubsystemHealth:
    """Normalize GovernedLoopService.health() -> SubsystemHealth."""
    state = raw.get("state", "unknown")
    score = 1.0 if state == "active" else 0.5 if state == "degraded" else 0.0
    return SubsystemHealth(name="gls", status=state, score=score, details=raw, ...)
```

One adapter per subsystem. All adapters are pure functions (no side effects).
Unknown keys in raw dicts are preserved in `details` for debugging.

### C2: DreamEngine Idle Timer Isolation

DreamEngine calls J-Prime via **direct `aiohttp.ClientSession`** to the J-Prime
HTTP endpoint, bypassing `PrimeRouter` and `PrimeClient`. This means:
- `record_jprime_activity()` is NOT called
- The VM idle-stop timer continues counting down during Dream activity
- If the idle timer fires while Dream is running, Dream detects the VM going
  offline and abandons the current job (checked between every LLM call)

This is enforced by DreamEngine constructing its own HTTP session:
```python
self._dream_session = aiohttp.ClientSession(
    base_url=os.getenv("JARVIS_PRIME_URL"),
    timeout=aiohttp.ClientTimeout(total=60),
)
# This session is NOT registered with PrimeRouter
```

### C3: VM Start-Reason Tracking

Add `_vm_start_reason: str` field to `GcpVmManager`, populated by callers of
`ensure_static_vm_ready()`. DreamEngine gate #3 checks:
```python
start_reason = self._vm_manager.get_start_reason()
uptime = self._vm_manager.get_uptime_s()
if start_reason == "dream" or uptime < self._idle_threshold:
    return False, "vm_not_warm_from_user"
```

### I1: MemoryEngine.ingest_outcome Input Type

`ingest_outcome` consumes terminal `LedgerEntry` objects filtered to states:
APPLIED, FAILED, ROLLED_BACK, BLOCKED. The MemoryEngine scans all entries for
a given `op_id` to reconstruct the full lifecycle:

```python
async def ingest_outcome(self, op_id: str) -> None:
    """Read all ledger entries for op_id, extract outcome pattern."""
    entries = self._ledger.read_entries(op_id)  # existing method
    terminal = [e for e in entries if e.state in _TERMINAL_STATES]
    if not terminal:
        return
    # Build MemoryInsight from terminal state + operation metadata
```

### I2: ProphecyEngine Caller

ProphecyEngine is called from two places:
1. **AutonomyIterationService._do_planning()** — before submitting an execution graph,
   call `prophecy.analyze_change(target_files, description)` and include the risk
   assessment in the PlannerOutcome metadata.
2. **GovernedOrchestrator at CLASSIFY phase** — optionally, call prophecy for
   interactive operations too. This is gated by `JARVIS_PROPHECY_ENABLED`.

### I3: Cross-Engine Type Definitions

Key types with field-level definitions:

```python
@dataclass(frozen=True)
class ImprovementBlueprint:
    blueprint_id: str             # sha256(repo_sha + policy_hash + prompt_family + model_class)
    title: str
    description: str
    category: str                 # "complexity"|"test_coverage"|"security"|"performance"|"debt"
    priority_score: float         # 0.0-1.0
    target_files: Tuple[str, ...]
    estimated_effort: str         # "small"|"medium"|"large"
    estimated_cost_usd: float
    repo: str
    repo_sha: str
    computed_at_utc: str
    ttl_hours: float
    model_used: str
    policy_hash: str
    oracle_neighborhood: Dict[str, Any]
    suggested_approach: str
    risk_assessment: str

@dataclass(frozen=True)
class ProphecyReport:
    change_id: str
    risk_level: str               # "low"|"medium"|"high"|"critical"
    predicted_failures: Tuple[PredictedFailure, ...]
    confidence: float
    reasoning: str
    recommended_tests: Tuple[str, ...]

@dataclass(frozen=True)
class PredictedFailure:
    test_file: str
    probability: float
    reason: str
    evidence: str
```

### I4: Disk Rotation

Added as safety invariant #11. Both JSONL files rotate at size thresholds:
- `health_trend.jsonl`: max 10MB, rotate to `health_trend.{date}.jsonl.gz`
- `insights.jsonl`: max 50MB, rotate to `insights.{date}.jsonl.gz`

### I5: Startup Ordering

```
Phase 1 (parallel): HealthCortex.start() + MemoryEngine.start()
Phase 2 (parallel): DreamEngine.start() + ProphecyEngine.start()
    (both depend on HealthCortex + MemoryEngine being ready)
Phase 3: Morning briefing (depends on all 4 engines)
```

Enforced in `consciousness_service.py`. DreamEngine and ProphecyEngine check
`self._health_cortex is not None` and `self._memory_engine is not None` at start.

### Additional Test Scenarios

| ID | Test | Verifies |
|----|------|----------|
| TC29 | test_dream_does_not_reset_idle_timer | Dream HTTP calls don't invoke record_jprime_activity |
| TC30 | test_dream_preemption_saves_partial_state | Preempted job saves progress for resume |
| TC31 | test_health_cortex_handles_subsystem_exception | Exception in .health() -> UNKNOWN status |
| TC32 | test_memory_engine_disk_full | Disk exhaustion -> holds in memory, no crash |
| TC33 | test_consciousness_stop_flushes_all | Stop -> memory flushed + trend flushed |
| TC34 | test_prophecy_concurrent_with_dream | Both engines can use J-Prime simultaneously |

### UserActivityMonitor Protocol

```python
class UserActivityMonitor(Protocol):
    def last_activity_s(self) -> float:
        """Seconds since last user interaction."""

# Production: reads unified_supervisor._last_activity_mono
# Test: injectable mock
```

DreamEngine depends on this protocol, not on the supervisor directly.

## 7. Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| JARVIS_CONSCIOUSNESS_ENABLED | false | Master feature flag |
| JARVIS_HEALTH_POLL_INTERVAL_S | 10 | HealthCortex poll frequency |
| JARVIS_DREAM_ENABLED | true | Dream Mode sub-flag |
| JARVIS_DREAM_IDLE_THRESHOLD_S | 600 | Idle time before dreaming (10 min) |
| JARVIS_DREAM_REENTRY_COOLDOWN_S | 300 | Cooldown after user return (5 min) |
| JARVIS_DREAM_MAX_MINUTES_PER_DAY | 120 | Daily dream compute cap |
| JARVIS_DREAM_BLUEPRINT_TTL_HOURS | 24 | Blueprint validity window |
| JARVIS_PROPHECY_ENABLED | true | ProphecyEngine sub-flag |
| JARVIS_MEMORY_TTL_HOURS | 168 | Memory insight expiry (1 week) |
| JARVIS_BRIEFING_ON_STARTUP | true | Morning briefing announcement |

## 8. File Manifest

| File | Purpose | Lines |
|------|---------|-------|
| backend/core/ouroboros/consciousness/__init__.py | Package init | ~5 |
| backend/core/ouroboros/consciousness/types.py | All dataclasses | ~250 |
| backend/core/ouroboros/consciousness/health_cortex.py | HealthCortex | ~200 |
| backend/core/ouroboros/consciousness/memory_engine.py | MemoryEngine | ~300 |
| backend/core/ouroboros/consciousness/dream_engine.py | DreamEngine | ~350 |
| backend/core/ouroboros/consciousness/prophecy_engine.py | ProphecyEngine | ~200 |
| backend/core/ouroboros/consciousness/consciousness_service.py | Zone 6.11 service | ~250 |
| backend/core/ouroboros/consciousness/dream_metrics.py | Cost observability | ~80 |
| **Total** | | **~1635** |

**Modified:** unified_supervisor.py (Zone 6.11 wiring)

## 9. Go/No-Go Test Matrix

### Tier 0: Unit Tests

| ID | Test | Verifies |
|----|------|----------|
| TC01 | test_snapshot_aggregates_all_subsystems | All health sources polled and merged |
| TC02 | test_snapshot_degrades_on_three_unknowns | 3 consecutive UNKNOWN -> DEGRADED |
| TC03 | test_state_transition_emits_event | HEALTHY->DEGRADED fires CommMessage |
| TC04 | test_trend_stores_rolling_window | 720 entries max, oldest evicted |
| TC05 | test_memory_ingests_outcome | OperationResult -> MemoryInsight |
| TC06 | test_file_reputation_tracks_success_rate | 5 ops -> correct success_rate |
| TC07 | test_memory_ttl_decay | Insight decays after TTL |
| TC08 | test_memory_invalidates_on_head_change | Repo HEAD change -> stale insights removed |
| TC09 | test_dream_gate_rejects_vm_not_ready | J-Prime not READY -> cannot dream |
| TC10 | test_dream_gate_rejects_user_active | User active < 10min -> cannot dream |
| TC11 | test_dream_gate_rejects_vm_woken_by_dream | VM woken by dream -> cannot dream |
| TC12 | test_dream_idempotent_job_key | Same inputs -> same key -> skip |
| TC13 | test_blueprint_staleness_on_head_change | Repo HEAD changed -> blueprint stale |
| TC14 | test_blueprint_staleness_on_policy_change | Policy hash changed -> stale |
| TC15 | test_prophecy_heuristic_scoring | Risk score matches expected formula |
| TC16 | test_prophecy_confidence_capped_without_jprime | Heuristic-only -> max 0.6 |

### Tier 1: Integration Tests

| ID | Test | Verifies |
|----|------|----------|
| TC17 | test_dream_preemption_on_user_activity | User activity -> dream job abandoned |
| TC18 | test_dream_flap_damping | Rapid idle/active cycles -> no thrashing |
| TC19 | test_memory_feeds_planner | MemoryInsight available to IterationPlanner |
| TC20 | test_prophecy_feeds_health_cortex | High-risk prediction -> HealthCortex warning |
| TC21 | test_consciousness_startup_recovery | Load trend + memory from disk on restart |
| TC22 | test_dream_metrics_track_roi | Compute minutes + hit rate tracked correctly |
| TC23 | test_separate_token_budgets | Dream prompt capped at 2048, prophecy at 1024 |
| TC24 | test_dream_dormant_reason_code | J-Prime unavailable -> DREAM_DORMANT event |

### Tier 2: E2E Tests

| ID | Test | Verifies |
|----|------|----------|
| TC25 | test_morning_briefing_announced | Startup -> voice briefing with health + blueprints |
| TC26 | test_full_consciousness_lifecycle | Start all engines -> poll -> dream -> stop |
| TC27 | test_dream_blueprint_retrieved_by_iteration | Blueprint from dream -> used by iteration service |
| TC28 | test_regression_detected_via_memory_plus_prophecy | Memory pattern + prophecy -> regression alert |

### Go/No-Go Decision Rules

| Tier | Required | Decision |
|------|----------|----------|
| Deploy to OBSERVE | TC01-TC16 pass | Consciousness runs, collects data, no actions |
| Enable Dream Mode | TC01-TC24 pass | Dream pre-computes blueprints during idle |
| Enable Prophecy | TC01-TC24 pass | Prophecy warns about risky changes |
| Full Activation | TC01-TC28 pass | Morning briefing + blueprint-to-iteration pipeline |

# Trinity Consciousness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a self-awareness layer to Ouroboros with 4 engines: real-time health aggregation, cross-session memory, idle GPU speculative analysis (Dream Mode), and failure prediction (Prophecy).

**Architecture:** TrinityConsciousness (Zone 6.11) composes 4 engines that share a unified state model. HealthCortex + MemoryEngine are local-only. DreamEngine + ProphecyEngine use J-Prime GPU when available, degrade gracefully when not. Startup order: (HealthCortex + MemoryEngine) parallel, then (DreamEngine + ProphecyEngine) parallel, then morning briefing.

**Tech Stack:** Python 3.9+, asyncio, psutil, aiohttp (for Dream direct HTTP), existing Ouroboros governance infrastructure (CommProtocol, OperationLedger, TheOracle, TrustGraduator)

**Spec:** `docs/superpowers/specs/2026-03-20-trinity-consciousness-design.md`

---

## File Structure

**New files (8 source + 6 test):**

| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/consciousness/__init__.py` | Package init |
| `backend/core/ouroboros/consciousness/types.py` | All dataclasses: snapshots, blueprints, insights, reports |
| `backend/core/ouroboros/consciousness/health_cortex.py` | HealthCortex: poll, aggregate, trend, transition detection |
| `backend/core/ouroboros/consciousness/memory_engine.py` | MemoryEngine: learn from outcomes, file reputation, pattern index |
| `backend/core/ouroboros/consciousness/dream_engine.py` | DreamEngine: idle GPU speculation, blueprint pre-computation |
| `backend/core/ouroboros/consciousness/prophecy_engine.py` | ProphecyEngine: risk prediction, heuristic + J-Prime scoring |
| `backend/core/ouroboros/consciousness/consciousness_service.py` | TrinityConsciousness: Zone 6.11 lifecycle, morning briefing |
| `backend/core/ouroboros/consciousness/dream_metrics.py` | DreamMetrics: cost observability + ROI tracking |

**Modified files (1):**

| File | Change |
|------|--------|
| `unified_supervisor.py` | Zone 6.11: create + start TrinityConsciousness after Zone 6.10 |

**Key design decisions from spec review:**

- **C1**: Per-subsystem health adapters normalize heterogeneous `.health()` returns into `SubsystemHealth`.
- **C2**: DreamEngine uses direct `aiohttp.ClientSession` (not PrimeRouter) to avoid resetting VM idle timer.
- **C3**: VM start-reason tracking via `_vm_start_reason` field on GcpVmManager (not implemented in this plan — documented as future integration point, DreamEngine uses uptime-based heuristic instead).
- **I5**: Startup ordering enforced: Phase 1 (HealthCortex + MemoryEngine), Phase 2 (DreamEngine + ProphecyEngine), Phase 3 (briefing).

---

## Task 1: Types (all dataclasses)

**Files:**
- Create: `backend/core/ouroboros/consciousness/__init__.py`
- Create: `backend/core/ouroboros/consciousness/types.py`
- Test: `tests/test_ouroboros_governance/test_consciousness_types.py`

**Go/No-Go: TC12, TC13, TC14**

- [ ] Step 1: Create package directory and `__init__.py` (empty, just a docstring).

- [ ] Step 2: Write failing test file with ~12 tests covering: SubsystemHealth creation, TrinityHealthSnapshot overall_verdict computation, ImprovementBlueprint staleness checks (TC13: HEAD change, TC14: policy change), idempotent job key (TC12: same inputs = same key), ProphecyReport creation, PredictedFailure creation, MemoryInsight TTL decay, FileReputation fragility score, DreamMetrics defaults, HealthTrend rolling window eviction, UserActivityMonitor protocol compliance.

- [ ] Step 3: Run tests, verify fail.

- [ ] Step 4: Implement `types.py` (~250 lines) containing all dataclasses from the spec: `SubsystemHealth`, `ResourceHealth`, `BudgetHealth`, `TrustHealth`, `TrinityHealthSnapshot`, `HealthTrend`, `MemoryInsight`, `FileReputation`, `PatternSummary`, `ImprovementBlueprint` (with `is_stale()` method), `DreamJob`, `DreamMetrics`, `ProphecyReport`, `PredictedFailure`, `UserActivityMonitor` Protocol, `ConsciousnessConfig.from_env()`, `compute_job_key()`, `compute_blueprint_id()`.

- [ ] Step 5: Run tests, verify all pass.

- [ ] Step 6: Commit: `feat(consciousness): add Trinity Consciousness type definitions (TC12-TC14)`

---

## Task 2: DreamMetrics (cost observability)

**Files:**
- Create: `backend/core/ouroboros/consciousness/dream_metrics.py`
- Test: `tests/test_ouroboros_governance/test_dream_metrics.py`

**Go/No-Go: TC22**

- [ ] Step 1: Write failing tests (~6 tests): record_compute_time increments minutes, record_preemption increments count, record_blueprint_computed/discarded updates counters, hit_rate computation (TC22), persist/load from JSON, reset on new day.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `dream_metrics.py` (~80 lines): `DreamMetricsTracker` class with `record_compute_time()`, `record_preemption()`, `record_blueprint_computed()`, `record_blueprint_discarded()`, `record_blueprint_hit()`, `get_metrics() -> DreamMetrics`, `persist(path)`, `load(path)`. Uses JSON persistence to `~/.jarvis/ouroboros/consciousness/dreams/metrics.json`.

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add DreamMetrics cost observability (TC22)`

---

## Task 3: HealthCortex

**Files:**
- Create: `backend/core/ouroboros/consciousness/health_cortex.py`
- Test: `tests/test_ouroboros_governance/test_health_cortex.py`

**Go/No-Go: TC01, TC02, TC03, TC04, TC31**

- [ ] Step 1: Write failing tests (~12 tests): snapshot aggregates all subsystem health dicts (TC01), three consecutive UNKNOWN -> DEGRADED (TC02), state transition emits CommMessage via HEARTBEAT (TC03), trend stores rolling 720 entries with oldest evicted (TC04), subsystem .health() exception -> UNKNOWN status (TC31), J-Prime /health poll timeout -> prime marked UNKNOWN, psutil failure -> resources marked UNKNOWN, all healthy -> HEALTHY verdict + score 1.0, one degraded -> DEGRADED verdict, get_trend returns historical window, stop flushes trend to disk, start loads trend from disk.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `health_cortex.py` (~200 lines): `HealthCortex` class with `start()` (loads trend from disk, starts poll loop), `stop()` (flushes trend to disk, cancels poll), `get_snapshot()` (returns cached, never blocks), `get_trend(window_minutes)` (filters rolling window). Internal `_poll_once()` calls all health adapters with 5s timeout each, fault-isolated. Per-subsystem adapter functions (`_adapt_gls`, `_adapt_ils`, `_adapt_prime`, etc.) normalize raw dicts to `SubsystemHealth`. State transition detection compares previous verdict to current, emits CommMessage only on change. Trend storage: deque(maxlen=720) in-memory, periodic flush to health_trend.jsonl with 10MB rotation (per spec I4).

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add HealthCortex real-time health aggregation (TC01-TC04, TC31)`

---

## Task 4: MemoryEngine

**Files:**
- Create: `backend/core/ouroboros/consciousness/memory_engine.py`
- Test: `tests/test_ouroboros_governance/test_memory_engine.py`

**Go/No-Go: TC05, TC06, TC07, TC08, TC32**

- [ ] Step 1: Write failing tests (~12 tests): ingest_outcome creates MemoryInsight from ledger entries (TC05), file reputation tracks success rate across 5 ops (TC06), insight TTL decay reduces confidence (TC07), insights invalidated on HEAD change (TC08), disk full holds in memory without crash (TC32), query returns relevant insights sorted by confidence, get_pattern_summary aggregates patterns, start loads from disk, stop flushes to disk, malformed JSONL entries skipped, file reputation with no history returns defaults, git unavailable -> ledger-only reputation.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `memory_engine.py` (~300 lines): `MemoryEngine` class with `start()` (scan ledger files, load persisted insights; optionally ingest existing MemoryFacts from AdvancedAutonomyService if available), `stop()` (flush to disk), `ingest_outcome(op_id)` (call `await self._ledger.get_history(op_id)` — NOT `read_entries()`, which does not exist — filter to terminal states APPLIED/FAILED/ROLLED_BACK/BLOCKED, build MemoryInsight), `query(query, max_results)` (keyword search over insights), `get_file_reputation(file_path)` (aggregate success/failure per file from ledger), `get_pattern_summary()` (top patterns by evidence count). Persistence: insights.jsonl (append-only, rotation at 50MB), file_reputations.json (recomputed on ingest), patterns.json (recomputed on ingest).

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add MemoryEngine cross-session learning (TC05-TC08, TC32)`

---

## Task 5: ProphecyEngine

**Files:**
- Create: `backend/core/ouroboros/consciousness/prophecy_engine.py`
- Test: `tests/test_ouroboros_governance/test_prophecy_engine.py`

**Go/No-Go: TC15, TC16, TC20, TC34**

- [ ] Step 1: Write failing tests (~10 tests): heuristic risk score matches formula (TC15: `(1-success_rate)*0.3 + fragility*0.3 + dependents*0.2 + complexity*0.2`), confidence capped at 0.6 without J-Prime (TC16), prophecy feeds HealthCortex via callback (TC20), prophecy concurrent with dream doesn't interfere (TC34), analyze_change returns ProphecyReport with predicted failures, risk_level computed from score thresholds, J-Prime timeout -> heuristic fallback with reason code, Oracle stale -> reduced confidence, get_risk_scores returns per-file scores, empty change list returns low-risk report.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `prophecy_engine.py` (~200 lines): `ProphecyEngine` class with `start()`, `stop()`, `analyze_change(files_changed, diff_summary) -> ProphecyReport`, `get_risk_scores() -> Dict[str, float]`. Internal `_heuristic_risk(file_path)` uses formula from spec. Optional `_jprime_risk(files, diff)` sends to deepseek_r1 via direct HTTP (1024 token budget). Concurrency: asyncio.Lock to prevent duplicate analysis. Callback `_on_high_risk` feeds HealthCortex.

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add ProphecyEngine failure prediction (TC15-TC16, TC20, TC34)`

---

## Task 6: DreamEngine

**Files:**
- Create: `backend/core/ouroboros/consciousness/dream_engine.py`
- Test: `tests/test_ouroboros_governance/test_dream_engine.py`

**Go/No-Go: TC09, TC10, TC11, TC17, TC18, TC23, TC24, TC29, TC30**

This is the most complex task — the idle GPU speculative analysis engine with 5 readiness gates, preemption, idempotent jobs, staleness, and flap damping.

- [ ] Step 1: Write failing tests (~16 tests): gate rejects VM not ready (TC09), gate rejects user active < 10min (TC10), gate rejects VM woken by dream (TC11), preemption on user activity abandons job (TC17), flap damping prevents thrashing (TC18), separate token budget enforced (TC23), J-Prime unavailable -> DREAM_DORMANT event (TC24), dream HTTP doesn't invoke record_jprime_activity (TC29: mock aiohttp session used instead of PrimeClient), preemption saves partial state for resume (TC30), idempotent job key skips recomputation, blueprint is_stale on HEAD change, get_blueprints returns sorted by priority, discard_stale removes expired blueprints, dream budget cap enforced, dream respects ResourceGovernor, stop cancels in-flight job cleanly.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `dream_engine.py` (~350 lines): `DreamEngine` class with `start()`, `stop()`, `get_blueprints(top_n)`, `get_blueprint(id)`, `discard_stale()`. Internal `_dream_loop()` async task: check `_can_dream()` (5 gates), select candidate from Oracle/miner, compute blueprint via direct `aiohttp.ClientSession` to J-Prime (NOT PrimeRouter), check preemption `asyncio.Event` between every HTTP call, store blueprint to `~/.jarvis/ouroboros/consciousness/dreams/blueprint_{id}.json`, track metrics via `DreamMetricsTracker`. Flap damping: `_last_user_return_time` + `REENTRY_COOLDOWN_S`. Idempotent keys: `job_keys.json` with SHA256 lookup.

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add DreamEngine idle GPU speculative analysis (TC09-TC11, TC17-TC18, TC23-TC24, TC29-TC30)`

---

## Task 7: ConsciousnessService (Zone 6.11 orchestrator)

**Files:**
- Create: `backend/core/ouroboros/consciousness/consciousness_service.py`
- Test: `tests/test_ouroboros_governance/test_consciousness_service.py`

**Go/No-Go: TC19, TC21, TC25, TC26, TC27, TC28, TC33**

- [ ] Step 1: Write failing tests (~11 tests): memory feeds iteration planner with insights (TC19), startup recovery loads trend + memory from disk (TC21), morning briefing announced via safe_say (TC25), full lifecycle start -> poll -> stop (TC26), dream blueprint retrievable by iteration service (TC27), regression detected via memory + prophecy cross-engine (TC28), stop flushes all engines (TC33), startup ordering: Phase 1 (health + memory parallel), Phase 2 (dream + prophecy parallel), Phase 3 (briefing), feature flag disabled -> no engines start, health() returns composite of all engine health dicts, start idempotent (second call no-op).

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `consciousness_service.py` (~250 lines): `TrinityConsciousness` class with `start()` (enforced ordering: Phase1 parallel, Phase2 parallel, Phase3 briefing), `stop()` (reverse order, flush all), `health()` (composite dict), `_announce_briefing()` (compose from snapshot + blueprints + patterns, call safe_say). Holds references to all 4 engines. Feature-flag gated by `JARVIS_CONSCIOUSNESS_ENABLED`. Morning briefing gated by `JARVIS_BRIEFING_ON_STARTUP`.

- [ ] Step 4: Run tests, verify all pass.

- [ ] Step 5: Commit: `feat(consciousness): add TrinityConsciousness Zone 6.11 service (TC21, TC25-TC28, TC33)`

---

## Task 8: Supervisor Wiring + Final Validation

**Files:**
- Modify: `unified_supervisor.py` (after Zone 6.10 block)
- Modify: `.env` (add consciousness env vars)

**All remaining Go/No-Go tests validated here.**

- [ ] Step 1: Add `self._consciousness: Optional[Any] = None` near line 67021 (alongside `_iteration_service`).

- [ ] Step 2: Add Zone 6.11 block after Zone 6.10 in the supervisor startup:
Feature-flag gated by `JARVIS_CONSCIOUSNESS_ENABLED`. Creates TrinityConsciousness with all dependencies from existing supervisor state (GLS, ILS, iteration_service, Oracle, trust_graduator, ledger, comm, governance_stack). 10s timeout. Graceful degradation on failure.

- [ ] Step 3: Add shutdown cleanup for consciousness service (before iteration service stop).

- [ ] Step 3a: Wire ProphecyEngine caller into AutonomyIterationService._do_planning() (spec I2). After IterationPlanner.plan() returns a planned graph, call `prophecy.analyze_change(target_files, description)` and attach the ProphecyReport to PlannerOutcome metadata. This is gated by `JARVIS_PROPHECY_ENABLED`. Modify `backend/core/ouroboros/governance/autonomy/iteration_service.py` to accept an optional `prophecy_engine` parameter and call it during PLANNING state.

- [ ] Step 3b: Create `SupervisorActivityMonitor` adapter implementing `UserActivityMonitor` protocol. This reads `unified_supervisor._last_activity_mono` and converts to seconds-since-last-activity. Lives in `consciousness_service.py` as a small inner class. DreamEngine receives this via constructor injection.

- [ ] Step 4: Add env vars to `.env`:
```
JARVIS_CONSCIOUSNESS_ENABLED=false
JARVIS_HEALTH_POLL_INTERVAL_S=10
JARVIS_DREAM_ENABLED=true
JARVIS_DREAM_IDLE_THRESHOLD_S=600
JARVIS_DREAM_REENTRY_COOLDOWN_S=300
JARVIS_DREAM_MAX_MINUTES_PER_DAY=120
JARVIS_DREAM_BLUEPRINT_TTL_HOURS=24
JARVIS_PROPHECY_ENABLED=true
JARVIS_MEMORY_TTL_HOURS=168
JARVIS_BRIEFING_ON_STARTUP=true
```

- [ ] Step 5: Run full Go/No-Go matrix: `python3 -m pytest tests/test_ouroboros_governance/test_consciousness_*.py tests/test_ouroboros_governance/test_health_cortex.py tests/test_ouroboros_governance/test_memory_engine.py tests/test_ouroboros_governance/test_dream_*.py tests/test_ouroboros_governance/test_prophecy_engine.py -v`. Expected: all TC01-TC34 pass.

- [ ] Step 6: Run full regression: `python3 -m pytest tests/test_ouroboros_governance/ --timeout=60 -q`. Expected: 0 new failures.

- [ ] Step 7: Commit: `feat(consciousness): wire TrinityConsciousness into supervisor Zone 6.11`

---

## Execution Order + Parallelism

```
Task 1 (types)          -- foundation, no deps
Task 2 (dream_metrics)  -- depends on Task 1 (uses DreamMetrics type)
                         |
Task 3 (health_cortex)  -- depends on Task 1
Task 4 (memory_engine)  -- depends on Task 1
                         |  Tasks 3+4 can run in parallel
Task 5 (prophecy)       -- depends on Tasks 1, 3, 4 (uses MemoryEngine + HealthCortex callback)
Task 6 (dream)          -- depends on Tasks 1, 2, 3, 4 (uses all)
                         |  Tasks 5+6 can run in parallel after 3+4
Task 7 (service)        -- depends on Tasks 3-6 (composes all engines)
Task 8 (wiring)         -- depends on Task 7
```

Total new code: ~1635 lines across 8 files + 2 modified files (iteration_service.py, unified_supervisor.py). Total tests: ~80 across 6 files covering all 34 Go/No-Go items (TC01-TC34).

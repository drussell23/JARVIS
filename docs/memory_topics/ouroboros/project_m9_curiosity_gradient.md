---
title: Project M9 Curiosity Gradient
modules: []
status: merged
source: project_m9_curiosity_gradient.md
---

**Status (2026-05-04)**: **CLOSED** — Slices 1-5 complete, 217/217 tests green, master flag graduated default-TRUE.

**Per-slice deliverables**:

- **Slice 1** — `curiosity_gradient.py` ~620 LOC contract layer (58 tests):
  - 5-value closed enums: `CuriositySource` (LOGPROB_ENTROPY / PROPHECY_ERROR / POSTMORTEM_RECURRENCE / INSUFFICIENT_DATA / DISABLED), `CuriosityDecayReason` (NONE / STALE_FOCUS / RECURRENCE_LOOP / OPERATOR_RESET / DISABLED)
  - Frozen dataclasses: CuriosityObservation (5 fields), CuriosityScore (10 fields incl. `source_breakdown` for operator explainability)
  - Pure functions: `compute_curiosity()` decision tree (master flag → cold-start → multi-source weighted mean with recency decay → confidence×diversity → dominant source picker → decay reason override) + `curiosity_multiplier_from_score()` consumer-side multiplier with cold-start/decayed/low-confidence → 1.0 short-circuits
  - 8 env knobs zero-hardcoding; bounded multiplier `[floor, ceiling]` (default [0.5, 2.0])
  - Decision E1: `recency_weight` deferred to `_scoring_primitives` (zero duplication)
  - Defensive empty-`per_source_mean` fix added (recency underflow edge case)

- **Slice 2** — `curiosity_collector.py` ~620 LOC observer (36 tests):
  - `CuriosityCollector` with atomic frozen-swap via `threading.RLock`; 5-thread × 20-record race verified
  - 3 record_* methods (logprob/prophecy/recurrence with weight_score normalization)
  - Pull-side `score_for_cluster()` with fresh stale-focus check on every call
  - Auto-decay via `_resolve_decay_reason()` closed-enum dispatch
  - Per-cluster JSONL persistence at `.jarvis/curiosity/{cluster_id}.jsonl` via `cross_process_jsonl.flock_*` (Decision A1)
  - `read_observations_for_cluster()` JSONL replay with garbage-line tolerance
  - `resolve_cluster_id()` Decision A3 (explicit label / SemanticIndex sem-N / `_global` fallback)
  - `reset_cluster()` operator-explicit one-shot decay surface
  - Process-singleton `get_default_collector()`

- **Slice 3** — `sensor_governor.py` + `sensor_governor_seed.py` extensions (22 tests):
  - `SensorBudgetSpec.curiosity_aware: bool = False` opt-in field
  - `BudgetDecision.curiosity_multiplier` + `curiosity_cluster_id` observability fields
  - `_curiosity_multiplier_for()` lazy-import helper (Decision X) — try/except → (1.0, None) on M9-off / cold-start / decay / ImportError / any exception
  - `_weighted_cap()` extended with `curiosity_multiplier` (composes BEFORE topology-backpressure + emergency-brake)
  - `request_budget()` extended with `cluster_id: Optional[str] = None` keyword param
  - 3 graduated curiosity-aware sensors: `OpportunityMinerSensor` / `ProactiveExplorationSensor` / `CapabilityGapSensor`. Other 14 stay neutral.
  - Pre-existing 54 governor regression tests stay green

- **Slice 4** — 3 modules + 1 SSE event (29 tests):
  - `ide_observability_stream.py` extension: `EVENT_TYPE_CURIOSITY_CHANGED` + `publish_curiosity_event()` single event for all transitions (`threshold_crossed` / `decay_applied` / `operator_reset` / `samples_milestone`)
  - `curiosity_observability.py` (~280 LOC): `GET /observability/curiosity[/region/{id}]` with `register_routes()` helper; AST-pinned read-only
  - `curiosity_repl.py` (~410 LOC): `/curiosity {top, region, config, reset, help}`; `register_verbs()` auto-discovery; `/curiosity reset` is the SOLE mutation surface

- **Slice 5** — Graduation + producer bridge (18 tests):
  - Master flag flipped: `curiosity_gradient_enabled()` default false → **true**; asymmetric env semantics (explicit `false` for instant revert)
  - **Producer bridge** (`curiosity_producer_bridge.py` ~250 LOC) — 3 entry points (`feed_logprob_entropy` / `feed_prophecy_error` / `feed_recurrence_drift`) with prophecy-error abs-diff math + log-scale recurrence normalization; SSE publication on significant transitions only (chatter suppression); fully exception-isolated; lazy-imports M9 modules so caller stays decoupled
  - **CoherenceAuditor RECURRENCE_DRIFT wire-up** — initial producer; lazy-imports + master-flag-gated; no-op when M9 disabled. (GENERATE / VERIFY producer wire-ups deferred to Slice 5b for tight regression scope.)
  - **6 FlagRegistry seeds**: master + halflife_days (14.0) + min_samples (8) + stale_focus_hours (24) + multiplier_floor (0.5) + multiplier_ceiling (2.0)
  - **5 AST shipped-code-invariants pins**: `curiosity_gradient_no_authority_imports` / `curiosity_gradient_master_default_true` / `curiosity_decay_via_shared_primitives` (Decision E1) / `sensor_governor_curiosity_lazy_imported` (Decision X) / `curiosity_collector_uses_flock` (Decision A1)
  - Pre-existing slice tests migrated `delenv` → `setenv("...", "false")` for default-true semantics

**Combined regression**: 217/217 across 6 test files (58+36+22+29+18 graduation + 54 governor pre-existing).

**Architectural locks (operator mandate, all preserved)**:
- Three independent input sources composed via weighted aggregation — no single source can dominate
- Pure substrate, zero LLM cost on hot path (cost contract structurally preserved per §26.6)
- Decision A1: per-cluster JSONL persistence via `cross_process_jsonl.flock_*` (AST-pinned)
- Decision A3: SemanticIndex-optional cluster_id resolution with `_global` fallback
- Decision B1: async observer + atomic frozen-swap mutation (mirrors EpistemicBudgetTracker)
- Decision C1: `SensorBudgetSpec.curiosity_aware` opt-in field; bias is per-sensor opt-in, not blanket
- Decision D1: pull-based consumer (governor lazy-imports + queries collector at request_budget time; collector never pushes)
- Decision E1: shared math via `_scoring_primitives.recency_weight` — M9 NEVER duplicates (AST-pinned)
- Decision X: lazy-import discipline at consumer site (governor + producer bridge)
- Cold-start inertness: `<min_samples` → INSUFFICIENT_DATA → multiplier=1.0
- Stale-focus auto-decay via `JARVIS_CURIOSITY_STALE_FOCUS_HOURS`
- Bounded multiplier `[floor, ceiling]` (default [0.5, 2.0]) — global cap structurally never bypassed

**Production behavior post-graduation**: CoherenceAuditor's RECURRENCE_DRIFT findings now feed CuriosityCollector via the producer bridge. Once enough observations accumulate per cluster (≥ min_samples=8), curiosity-aware sensors (OpportunityMiner / ProactiveExploration / CapabilityGap) get bias-amplified emission slots toward high-curiosity clusters. Until then, multiplier stays at 1.0 (cold-start inertness — pre-graduation behavior byte-identical). `/curiosity top` REPL + `GET /observability/curiosity` give live operator visibility.

**Deferred to Slice 5b follow-up**: wire `feed_logprob_entropy` at GENERATE phase via `phase_capture` adapter + wire `feed_prophecy_error` post-VERIFY at `phase_runners/verify_runner.py`. Both are additive lazy-imports following the same pattern as the CoherenceAuditor wire-up; deferred for safe regression scope.

**Next-up after M9 closure** (PRD §32.8 v3): Upgrade 2 DecisionRecord Causality Graph → M10 ArchitectureProposer.

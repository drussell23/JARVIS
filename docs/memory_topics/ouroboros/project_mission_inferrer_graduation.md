---
title: MissionInferrer (GoalInferenceEngine) Graduation — CLOSED 2026-05-03
modules: [scripts/mission_inferrer_closure_verdict.py, backend/core/ouroboros/governance/goal_inference.py, backend/core/ouroboros/governance/intake/unified_intake_router.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/governance/ide_observability.py, tests/governance/test_goal_inference.py, tests/governance/test_mission_inferrer_graduation.py, tests/governance/intake/test_mission_inferrer_priority_wire_up.py]
status: historical
source: project_mission_inferrer_graduation.md
---

# MissionInferrer (GoalInferenceEngine) Graduation — CLOSED 2026-05-03

3-slice graduation arc closing the Tier 2 #5 gap from the user's roadmap table. Pre-arc state: `goal_inference.py` was 1011 LOC, fully wired into `orchestrator.py:2235-2275` CONTEXT_EXPANSION, had a `/infer` REPL with `accept`/`reject`/`refresh`/`stats`, 46 tests in `test_goal_inference.py`, and `priority_boost_for_signal()` exported but **with zero consumers in production code** — making the engine decorative (prompt-only) rather than goal-directed (intake-influencing). Master flag defaulted False; no FlagRegistry seeds, no SSE event, no GET route, no AST regression pin.

## Slices shipped

- **Slice A** — Module-owned `register_flags()` (8 FlagSpec entries: `_ENABLED`, `_PROMPT_INJECTION`, `_MIN_CONFIDENCE`, `_TOP_K`, `_COMMIT_LOOKBACK`, `_MAX_AGE_S`, `_PRIORITY_BOOST_MAX`, `_REFRESH_S`) + `register_shipped_invariants()` AST pin (`goal_inference_substrate`: 6 extract_*_signal helpers + GoalInferenceEngine class + render/priority/accept/reject exports + frozen InferredGoal/SignalSample + no exec/eval/compile).
- **Slice B** — `priority_boost_for_signal` consumed by `unified_intake_router._compute_priority` next to the existing `goal_boost` + `semantic_boost` composition. Cache-only via `get_current()` (CONTEXT_EXPANSION already triggers `build()` per-op, keeping cache fresh — intake never stalls). Authority invariant preserved: priority ordering ONLY; never URL/risk/route/approval. Cross-file regression pin (`goal_inference_intake_consumer`, target_file=intake router) catches silent deletion of the consumer call site. **11 new wire-up regression tests.**
- **Slice C** — `EVENT_TYPE_GOAL_INFERENCE_BUILT` SSE event + `publish_goal_inference_built()` helper called by `GoalInferenceEngine.build()` on cache miss only (NOT on cache hit — avoids observability storm under hot intake load). `GET /observability/goal-inference` handler in `IDEObservabilityRouter.register_routes()` projecting current InferenceResult. Master flag flipped `default-False → default-True`. FlagSpec default updated. Empirical-closure verdict script `scripts/mission_inferrer_closure_verdict.py`. **9 new graduation regression tests.**

## Architectural decisions worth remembering

- **`int(math.ceil(_raw))` not `int(round(_raw))` in the intake hook**: Python's banker's rounding turns the natural single-match boost (`confidence × 0.5 = 0.5`) into 0, silently neutralizing the wire-up. Caught by Slice B test `test_master_on_cached_match_drops_priority` failing on first run with `matched=2 unmatched=2`. ceil ensures any positive raw boost lands ≥1 priority point. The float `priority_boost_max()` env knob still bounds the raw value; the int projection follows naturally.
- **Cache-only read in hot intake path**: `get_current()` returns `None` if the engine has never built. The hook treats `None` as zero boost. CONTEXT_EXPANSION at `orchestrator.py:2244` already triggers `build()` per-op, so the cache is fresh by the time intake fires. Mirrors `semantic_index.build_async()` non-blocking pattern.
- **SSE publish on cache miss only, NOT on cache hit**: `build()` is invoked frequently (per-op CONTEXT_EXPANSION + per-signal intake) but the actual rebuild happens only when `refresh_s` (default 1800s = 30min) elapses. Publishing on every `build()` would be spam; publishing only on cache miss matches semantic intent ("the engine just produced a new view"). Caught by Slice C test `test_publish_skipped_on_cache_hit`.
- **Cross-file AST pin**: `goal_inference_intake_consumer` invariant has `target_file=unified_intake_router.py` even though it's defined in `goal_inference.register_shipped_invariants()`. The substrate owner explicitly documents what consumer call sites it depends on. The infrastructure already supports per-invariant target_file; no new substrate needed.

## Test counts + AST pins

- **67/67 combined sweep across the arc** (46 pre-existing in test_goal_inference + 11 wire-up in test_mission_inferrer_priority_wire_up + 9 graduation in test_mission_inferrer_graduation + 1 updated default-true assertion); 106/108 broader router family (the 2 failures are pre-existing unrelated to this arc, confirmed reproducible on clean tree without my changes)
- **8 FlagRegistry seeds** in goal_inference.register_flags() — covers every JARVIS_GOAL_INFERENCE_* env knob in the module
- **2 AST pins** in goal_inference.register_shipped_invariants():
  - `goal_inference_substrate` (6 signal extractors + GoalInferenceEngine + render/priority/accept/reject exports + frozen dataclasses + no exec/eval/compile)
  - `goal_inference_intake_consumer` (cross-file: priority_boost_for_signal call + inferred_direction_boost composition both present in unified_intake_router)
- **1 new SSE event**: `goal_inference_built` + best-effort publisher; fires on cache miss only

## Empirical-closure verdict (against live repo)

```
[PASS] C1 Master flag default-true post-graduation
       inference_enabled()=True register_flags_default=True
[PASS] C2 Engine build produces real result against live repo
       build_reason=first_build total_samples=464 hypotheses=46
       top_theme='session [feat(governance): ClusterIntelligence-CrossSession Slice 3 —]'
       top_confidence=0.906
       sources_contributing={'commits': 222, 'memory': 70, 'file_hotspots': 73, 'declared_goals': 99}
[PASS] C3 priority_boost_for_signal consumed by intake
       matched_priority=1 unmatched_priority=2 diff=1
[PASS] C4 AST regression pins hold against current source
       invariants=2 results=[goal_inference_substrate=PASS, goal_inference_intake_consumer=PASS]
[PASS] C5 SSE event fires on cache miss (advisory)
       publisher_called=True top_theme='session [feat(governance): ...]' hypotheses_count=46
```

## Reuse contract honored (no duplication)

- Existing `_compute_priority` composition (`base - urgency + cost_penalty - confidence_bonus - dep_bonus - goal_boost - semantic_boost`) extended by single subtraction term — mirrors `goal_boost` + `semantic_boost` patterns exactly
- Existing `priority_boost_for_signal()` reused as-is (1011-LOC substrate untouched)
- Existing `get_current()` reused for hot-path cache read — no new substrate
- Existing CONTEXT_EXPANSION wire-up at `orchestrator.py:2244` reused as the rebuild trigger
- Existing FlagSpec/Category/FlagType pattern from prior arcs (cluster_intelligence, semantic_index)
- Existing `ShippedCodeInvariant` infrastructure with `target_file` per invariant supports cross-file pins out of the box
- Existing SSE publish helper pattern from `publish_domain_map_update` / `publish_semantic_embedder_fallback`
- Existing GET observability handler pattern from `_handle_codebase_character` / `_handle_posture_current`

## What this unlocks

The user's table flagged this gap as: "O+V responds to detected gaps only — never says 'you should build X' unprompted." Pre-arc, the engine produced hypotheses but they only flowed into the model prompt — operator had to read them and act, OR /infer accept them into GoalTracker. Post-arc, hypotheses **also** boost intake priority for signals that match: a backlog signal whose description matches an inferred theme is dequeued sooner than an unrelated one, so the engine's read of "where the operator is heading" actively steers what O+V works on next, not just what the model thinks about. The accept/reject operator surface stays untouched (cost-contract preservation: cannot synthesize new ops; can only re-rank existing intake).

## Files touched

- `backend/core/ouroboros/governance/goal_inference.py` (+register_flags + register_shipped_invariants + SSE publish hook + master flag flip + spec default flip)
- `backend/core/ouroboros/governance/intake/unified_intake_router.py` (intake hook in _compute_priority)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (EVENT_TYPE_GOAL_INFERENCE_BUILT + publish_goal_inference_built)
- `backend/core/ouroboros/governance/ide_observability.py` (route registration + _handle_goal_inference)
- `tests/governance/test_goal_inference.py` (default-true assertion update)
- `tests/governance/test_mission_inferrer_graduation.py` (NEW)
- `tests/governance/intake/test_mission_inferrer_priority_wire_up.py` (NEW)
- `scripts/mission_inferrer_closure_verdict.py` (NEW)

Closes Tier 2 #5 of the user's roadmap with the structural-then-empirical pattern proven on the ClusterIntelligence-CrossSession arc.

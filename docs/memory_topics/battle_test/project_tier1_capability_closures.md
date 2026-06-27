---
title: Project Tier1 Capability Closures
modules: [backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/jarvis_intelligence.py, backend/core/ouroboros/governance/observability/trajectory_auditor_observer.py, backend/core/ouroboros/governance/governed_loop_service.py, backend/core/ouroboros/governance/ide_observability_stream.py, tests/governance/test_trajectory_auditor_observer.py, backend/core/ouroboros/governance/verification/confidence_capture.py, backend/core/ouroboros/governance/verification/causality_dag.py, backend/core/ouroboros/governance/determinism/decision_runtime.py, backend/core/ouroboros/governance/determinism/phase_capture.py, backend/core/ouroboros/governance/auto_action_router.py]
status: merged
source: project_tier1_capability_closures.md
---

**Status (2026-05-04)**: 4-item Tier-1 batch CLOSED — 233/233 tests green across M9 + governor + new tier-1 spine.

## What landed

### Tier 1.1 — M9 GENERATE/VERIFY producer wire-ups
- `orchestrator.py` post-GENERATE site (~line 3670): reads `ctx.artifacts["confidence_monitor"].current_margin()`, converts to `entropy_normalized = clamp(1.0 - margin, 0, 1)`, feeds `curiosity_producer_bridge.feed_logprob_entropy` once per `ctx.target_files` entry. Lazy-imported, master-flag-gated, exception-isolated.
- `orchestrator.py` post-VERIFY site (~line 7551): reads `consciousness_bridge._prophecy_engine.get_risk_scores()` cached snapshot from CLASSIFY-time `assess_regression_risk` call; feeds `curiosity_producer_bridge.feed_prophecy_error` per (file, predicted_risk) tuple with `verify_passed` boolean. Bridge computes `error_magnitude = abs(predicted_risk - actual_indicator)`.
- M9 bias now actually engages — was cold-start-everywhere with only CoherenceAuditor RECURRENCE_DRIFT producing.

### Tier 1.2 — `jarvis_intelligence.py:447` TODO closure
- Replaced `capabilities_graduated=0  # TODO: wire to GraduationOrchestrator` (which pointed at dead code).
- New honest implementation: counts FlagRegistry SEED_SPECS entries that ship `default=True` + bool type. Production reads 90 capabilities graduated as of 2026-05-04.
- Also closed sibling `constraints_learned=0` — now sums LearningConsolidator rule count across domains in same pass.

### Tier 1.3 — Claude confidence-drop SSE: documented honestly, NOT wire-up'd
- Anthropic Messages API does not expose per-token logprobs (`confidence_capture.py:14`). Heuristic substitute (stop_reason / response-length proxies) rejected as workaround per operator mandate.
- Updated PRD §28.4 hard-gap entry: "DW-path confidence-drop SSE production IS the canonical implementation; Claude path is intentionally signal-blind."
- The original v9 brutal review framed this as a TODO. Reframed correctly: it's a structural API constraint.

### Tier 1.4 — TrajectoryAuditor un-stranding
- `observability/trajectory_auditor.py` was shipped 2026-04-XX with full audit pipeline (codebase walk → snapshot → rolling baseline → 4-metric drift detection → JSONL persistence) but **no producer ever invoked it** until now.
- Built `observability/trajectory_auditor_observer.py` (~270 LOC): async observer with on-boot snapshot + 6h periodic tick (env-tunable `JARVIS_TRAJECTORY_OBSERVER_INTERVAL_S`, default 21600s, clamped [60, 86400]). `asyncio.to_thread` wraps the ~40s codebase walk so boot stays non-blocking.
- Wired into `governed_loop_service.py` boot path alongside ClosureLoopObserver (lines ~1676-1701). Stop-side wired in `_stop_governance_observers`.
- Added `EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED` to `ide_observability_stream.py` + `publish_trajectory_drift_event()` helper. SSE only fires on `drifting`/`alarming` verdicts (chatter suppression — `stable`/`growing` stay silent).
- Master flag `JARVIS_TRAJECTORY_AUDITOR_OBSERVER_ENABLED` default-true (graduated 2026-05-04).
- 16/16 tests green.

## Grade table impact

- **Long-horizon semantic stability**: 🟡 B → 🟢 A−. Per-trajectory drift detection is now LIVE in production.
- M9's 🟢 A− cognitive depth now actually demonstrable in production (producers firing).

## Files touched

- `backend/core/ouroboros/governance/orchestrator.py` (M9 producer hooks at GENERATE + VERIFY sites)
- `backend/core/ouroboros/governance/jarvis_intelligence.py` (capabilities_graduated + constraints_learned wired)
- `backend/core/ouroboros/governance/observability/trajectory_auditor_observer.py` (NEW ~270 LOC)
- `backend/core/ouroboros/governance/governed_loop_service.py` (boot + stop wire-ups)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (EVENT_TYPE_TRAJECTORY_DRIFT_DETECTED + publisher)
- `tests/governance/test_trajectory_auditor_observer.py` (NEW 16 tests)
- `docs/architecture/OUROBOROS_VENOM_PRD.md` (version 2.13 → 2.14, grade-table refresh, §28.4 honesty edit)

## Skipped (deliberately, not deferred)

- Claude provider confidence-drop SSE producer — Anthropic API doesn't expose per-token logprobs. Documented as structural API constraint, not TODO.

## Next per §32.8 v3 sequencing

Upgrade 2 — DecisionRecord Causality Graph (§31.3): substrate already shipped (`causality_dag.py`, `decision_runtime.py`, `phase_capture.py`, `auto_action_router.py`); missing pieces are `DecisionRecord` primitive + flock'd `decisions.jsonl` + 8 decision-site instrumentation hooks + `DeterminismReplay` job + graduation surfaces. ~7-9 days. Foundation for safe RSI; precedes M10 ArchitectureProposer.

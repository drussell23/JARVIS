---
title: Project Roadmap Orchestrator Phase2
modules: [backend/core/ouroboros/governance/roadmap_orchestrator.py]
status: historical
source: project_roadmap_orchestrator_phase2.md
---

§41.4 Phase 2 — Roadmap-Orchestrator composer SHIPPED 2026-05-16, commit `7c62857ef4` (pushed via `e8ba708100`).

**Why:** PRD §41.4 Phase 1 shipped 9 substrates 2026-05-11, but no production-tree file chained roadmap_reader + goal_decomposition_planner + multi_step_orchestrator. The only end-to-end proof was test_phase2_roadmap_to_goals_integration.py. This commit closes the composition gap.

**How to apply:** When future work touches §41.4 Phase 1 substrates, the composer at `backend/core/ouroboros/governance/roadmap_orchestrator.py` is the canonical chainer. Production callers use `await execute_roadmap(yaml_path, router=router)` returning a frozen `RoadmapExecutionReport`. NEVER raises.

Substrate facts (verified during build, kept for future sessions):

- Env var plurality is **`JARVIS_MULTI_STEP_ORCHESTRATION_*`** (singular "ORCHESTRATION"), not `_ORCHESTRATOR_`. Applies to the master flag, ledger path, and completion-ledger reader path. Easy to get wrong; check substrate sources.
- The orchestrator's completion-status reader path env (`JARVIS_MULTI_STEP_ORCHESTRATION_COMPLETION_LEDGER_PATH`) is what it READS when no override is given. To wire production end-to-end, alias this to the goal_decomposition writer path so reader sees the same JSONL the writer produces.
- DecomposedPlan carries `parent_goal_id` — `advance_orchestration` short-circuits to NO_PLAN when it's empty.
- `_TeeRouter` is the structural fix for "external routers don't expose what they captured." Wraps upstream, captures internally, forwards on. Use for any production composer that needs envelope visibility without requiring upstream to expose state.

What's IN scope (shipped + tested):
- Composer module + 4 AST pins (verdict taxonomy / composes canonical / authority asymmetry / master default-false)
- 26 integration tests across happy path / DAG gating / wall-clock / cancellation / NEVER-raises / idempotency
- 297/297 full §41.4 cross-suite green
- Master flag `JARVIS_ROADMAP_ORCHESTRATOR_ENABLED` default-FALSE per §33.1

What's explicitly OUT of scope (operator-gated, do NOT add without slice authorization):
- Cadence / autonomous roadmap polling
- `/roadmap` REPL verb
- Battle-test / soak with master flag enabled
- Tier D or autonomous-dev readiness claims
- Bundling with SWE discriminator or $2 spend

P1 (operator-gated) — Phase 2.5 dry-run: one fixture roadmap.yaml, master + substrate masters flipped, real `UnifiedIntakeRouter`, bounded wall-clock cap, single `RoadmapExecutionReport.verdict == COMPLETED` proof (or structured non-COMPLETED with envelopes captured via _TeeRouter). Not a battle-test graduation. Wait for explicit operator pick.

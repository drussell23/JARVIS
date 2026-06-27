---
title: Project Upgrade 1 Bounded Epistemic Loop
modules: []
status: merged
source: project_upgrade_1_bounded_epistemic_loop.md
---

**Status (2026-05-04)**: **CLOSED** — Slices 1-5 complete, 172/172 tests green, master flag graduated default-TRUE.

**Architectural locks (operator mandate, all preserved)**:
- One authoritative per-op budget consulted at every Venom tool-round boundary
- Async at right layer = B1 synchronous probe await (no background probes / B2)
- No hardcoding — env-driven for max_rounds (12), drop_threshold (0.25), branch_cap (3), tracker_ttl (3600s)
- Reuse substrate, zero vanity modules — defers HypothesisProbe call cap via `get_max_calls_per_probe()` AST-pinned
- Lifecycle A1: per-op_id dict + RLock + TTL
- Decision X: tracker is passive consumer; tool_executor pushes events (NOT ConfidenceMonitor subscriber)

**Per-slice deliverables**:

- **Slice 1** — `epistemic_budget.py` ~600 LOC contract layer: BudgetOutcome (7 values), BudgetAction, EpistemicBudget (16 fields incl. ConfidenceTrajectory), ConfidenceSample, pure `compute_budget_action()`. Cost-gated routes (BG/SPEC) refuse PROBE/SBT structurally. **54 tests.**

- **Slice 2** — same module ~560 LOC tracker: `EpistemicBudgetTracker` with atomic frozen-swap via threading.RLock. **38 tests** (incl. 100-increment race test).

- **Slice 3** — `epistemic_budget_executor_hook.py` ~410 LOC: async `apply_budget_decision()` pure dispatch returning frozen `BudgetDispatchResult`. Protocol injection (ProbeRunner/SBTRunner/OrangeQueue). Decision C1 escalation via canonical primitives (`apply_floor_to_name` + `get_active_tier_order` rank comparison). **26 tests.**

- **Slice 4** — 4 modules + extensions:
  - `epistemic_budget.py` extensions: `EpistemicBudget.to_dict()` + `EpistemicBudgetTracker.snapshot_all()`
  - `epistemic_budget_observability.py` (~250 LOC): `GET /observability/budget[/{op_id}]` + `register_routes()` helper
  - `budget_repl.py` (~280 LOC): `/budget {status,op,config,help}` + `register_verbs()` auto-discovery
  - `epistemic_budget_provider_bridge.py` (~170 LOC): `attach_to_provider_run()` returns per-round async observer; `close_op()` for finally cleanup
  - `ide_observability_stream.py`: `EVENT_TYPE_BUDGET_ACTION_TAKEN` + `publish_budget_action_event()` (single event for all 7 outcomes, matches posture_changed pattern)
  - **33 tests.**

- **Slice 5** — Graduation + production wire-up:
  - `tool_executor.run()` extended: `per_round_observer: Optional[Callable[[int], Awaitable[Any]]] = None` parameter, awaited after compaction at the round boundary, exception-isolated. Default None preserves byte-identical pre-graduation behavior.
  - `providers.py` (Claude provider, line 4273) + `doubleword_provider.py` (line 1643): both wire `attach_to_provider_run()` lazy-import + pass `per_round_observer` to `tool_loop.run()` + call `close_op()` in `finally`.
  - Master flag flipped: `epistemic_budget_enabled()` default false → **true** (asymmetric env semantics — explicit `false`/`0`/`no`/`off` for instant revert).
  - **5 FlagRegistry seeds** in `flag_registry_seed.py`: master + max_rounds + drop_threshold + sbt_branch_cap + tracker_ttl.
  - **4 AST shipped-code-invariants pins** in `meta/shipped_code_invariants.py`: `epistemic_budget_no_authority_imports`, `epistemic_budget_master_default_true`, `epistemic_budget_probe_cap_no_duplication`, `tool_executor_per_round_observer_wired`.
  - **21 new graduation tests** + 5 falsy-variant tests; pre-existing slice tests migrated `delenv` → `setenv("...", "false")` for default-true semantics.

**Combined regression**: 172/172 green (54+38+26+33+21 across 5 test files). Provider/tool_executor regression sweep (19 tests across `inline_permission`, `intent_classifier`, `posture_repl`, `w2_4`, `w3_7` graduation pins): all green.

**Production behavior post-graduation**: every Claude/DW provider call to `tool_loop.run()` opens a tracker for the op; per-round observer fires after each round body, dispatches budget decision (probe/SBT/escalation as warranted), publishes `budget_action_taken` SSE, increments tracker. Bridge auto-disables on master-off (returns None, observer stays None, byte-identical pre-graduation behavior). `/budget` REPL + `GET /observability/budget` give live operator visibility.

**Why no orchestrator-level wire-up**: bridge sits at provider boundary because (a) providers are where `tool_loop.run()` is invoked, (b) `ctx.provider_route` + `ctx.op_id` + `ctx.risk_tier` are all already in scope at that site, (c) bridge module is the SOLE production-side entry to budget machinery — providers import bridge, NOT the executor-hook directly. Keeps hook a pure dispatch primitive.

**Next-up per PRD §32.8**: M9 CuriosityGradient → Upgrade 2 DecisionRecord Causality Graph → M10 ArchitectureProposer.

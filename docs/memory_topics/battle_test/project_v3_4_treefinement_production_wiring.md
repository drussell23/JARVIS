---
title: Project V3 4 Treefinement Production Wiring
modules: [backend/core/ouroboros/governance/repair_tree_production.py, backend/core/ouroboros/governance/repair_engine.py, tests/governance/test_repair_engine_generate_primitive.py, tests/governance/test_repair_tree_production_diff_applier.py, tests/governance/test_repair_tree_production_generator.py, tests/governance/test_repair_tree_production_factory.py, tests/governance/test_repair_tree_production_hardening.py, backend/core/ouroboros/governance/repair_tree.py, backend/core/ouroboros/governance/repair_tree_archive.py]
status: merged
source: project_v3_4_treefinement_production_wiring.md
---

May 12 2026 — production-wiring follow-on to v3.3's six-phase Treefinement L2 substrate arc. **Closes the deferred gap explicitly noted at the end of v3.3**: when Phase 5 shipped the strategy gate at `RepairEngine.run()`, tree mode was structurally REACHABLE but BEHAVIORALLY a no-op (no production factory registered). Without v3.4 wiring, the 243-test Treefinement substrate sat dormant and Phase 9 graduation soaks could not start.

## Six-phase arc summary

| Phase | Scope | Tests | Files |
|---|---|---|---|
| A | Extract `RepairEngine._generate_repair_candidate` from inline `_run_inner` GENERATE block as single-source provider primitive | 11 | `repair_engine.py` (+CandidateGenerationResult frozen dataclass) |
| B | `GitApplyDiffApplier` — production `DiffApplier` Protocol impl wrapping `git apply` via safe asyncio.create_subprocess_exec | 27 | `repair_tree_production.py` (new file) |
| C | `ProductionBranchGenerator` — production `BranchGenerator` Protocol impl composing Phase A primitive + Phase 3 cross-branch substrate | 26 | `repair_tree_production.py` |
| D | `production_tree_runner_factory` (zero-arg closure) + `tree_result_to_repair_result` adapter + Phase 5 `_invoke_tree_factory` real body | 20 | `repair_tree_production.py` + `repair_engine.py` |
| E | `register_production_factory_at_boot` lazy registration + 8 AST pins + 14 defense-in-depth tests | 23 | `repair_tree_production.py` + `repair_engine.py` |
| F | PRD §40.7.7-op + this memory artifact + MEMORY.md index + soak-readiness checklist | 0 (docs) | `OUROBOROS_VENOM_PRD.md` + `MEMORY.md` |

**Cumulative**: 1 new substrate file (`repair_tree_production.py` ~1700 LOC) + edits to `repair_engine.py` (Phase A extraction + Phase D gate body + Phase E lazy registration) / **107 new regression tests** / **8 new AST pins** / **2 new FlagRegistry seeds** / **0 surrounding-substrate regressions** / **0 changes to `_run_inner` source bytes** (sha256 still `9e881fdde25ec5b1` after Phase A re-lock).

## Architecture decisions

**Root problem solved at source — no shortcut**:

The shortcut would have been: stub the production factory with a "tree mode just calls _run_inner" passthrough — gives appearance of wiring without actually exercising tree-search. That would have defeated the entire 243-test substrate.

The other shortcut: hardcode prompt construction + provider plumbing inline in the factory, duplicating ~50 LOC of repair_engine's existing generation logic. Forbidden by the composition mandate.

**Structural fix**: Phase A extracts the inline GENERATE block from `_run_inner` as a clean reusable primitive (with explicit Phase tag updating the sha256 bytes-pin from the v3.3 self-bootstrap placeholder). Both `_run_inner` (LINEAR FSM) AND the production `BranchGenerator` (tree mode) compose the SAME primitive — single source of truth for "how a repair candidate gets generated."

**The factory contract refinement (Phase D)**:

v3.3 Phase 5 typed the factory as `Callable[..., RepairTreeRunner]`. Phase D refined the contract to `Callable[..., Callable[[], Awaitable[RepairTreeResult]]]` — the factory returns a zero-arg async **invocation closure** that captures all dependencies. Single-call invariant: one factory call → one tree-result awaitable. This keeps `_invoke_tree_factory` operator-readable + AST-pinnable without leaking factory internals into the gate path.

**The 7 canonical surfaces composed by the factory**:

1. `worktree_manager.WorktreeManager` — branch isolation (canonical L3; reap-orphans on boot covers SIGKILL recovery)
2. `GitApplyDiffApplier` (Phase B) — DiffApplier Protocol impl (the ONLY `git apply` invocation in the codebase; AST-pinned)
3. `test_runner.TestRunner` — pytest invocation + `resolve_affected_tests`
4. `semantic_guardian.SemanticGuardian` — 10-pattern semantic detector (canonical)
5. `repair_tree.CanonicalBranchValidator` (Phase 2) — per-branch pruning oracle composing ascii_strict_gate + Guardian + TestRunner
6. `ProductionBranchGenerator` (Phase C) — provider invocation composing Phase A primitive + Phase 3 substrate
7. `repair_tree.RepairTreeRunner` (Phase 1) — BFS/BEAM_K layer dispatch

## §1 Boundary preserved across all 6 production-wiring phases

**Authority asymmetry — strict read-only consumers (AST-pinned)**:

- `repair_tree_production.py` MUST NOT import: `orchestrator` / `iron_gate` / `change_engine` / `candidate_generator` / `policy_engine` / `risk_tier`
- `repair_engine.py` MUST NOT have top-level import of `repair_tree_production` — lazy import inside `_invoke_tree_factory` only (circular-import safety + zero-cost for non-tree-mode callers)
- The ONLY `git apply` subprocess invocation is in `GitApplyDiffApplier.__call__` (AST-pinned)

**Worktree isolation under production wiring**:

`Phase 1`'s isolation discipline (worktree_create_failed → `failure_class=infra` quarantine, NEVER falls back to shared tree) holds under production wiring. Phase E defense-in-depth test `test_partial_worktree_failure_does_not_poison_other_branches` verifies: branch 2 of 3 fails worktree creation → branches 1 and 3 still execute and appear in the layer with `outcome=PROMOTED`.

## Phase 5 strategy gate — operationally reachable end-to-end

The gate's 3-stage pipeline (Phase D replaces the v3.3 stub):

1. **Construct closure**: `factory(*, budget, ctx, repair_engine, pipeline_deadline)` — raises ValueError if `ctx.repo_root` missing AND deps not injected; caught by stage-1 try/except → return None → fall through to LINEAR
2. **Await closure**: `await invocation()` → `RepairTreeResult` — broker / provider / worktree / validator failures quarantine internally; only `CancelledError` propagates
3. **Adapt**: `tree_result_to_repair_result(tree_result, op_id=...)` → `RepairResult` — pure-function deterministic mapping over closed `LayerVerdict × BranchOutcome` taxonomies; degraded inputs produce `treefinement_adapter_failed:<exc>` rather than crashing

Any stage failure (except `CancelledError`) returns None → caller falls through to legacy `_run_inner` byte-identically (sha256-pinned at `9e881fdde25ec5b1`).

## Phase E lazy boot registration — the operator-respect invariant

`register_production_factory_at_boot()` is **not** called at module import time, **not** at process boot. It's called by the gate's `_maybe_run_treefinement` ONLY when:
- master flag is ON
- branching_strategy is non-LINEAR
- no factory is currently registered

Three invariants enforced by tests:
1. **Idempotent**: second/third calls return False without re-registering
2. **Operator-override-respecting**: if any custom factory is already registered, the canonical factory does NOT overwrite it
3. **Fail-open**: any internal exception returns False → caller falls through to LINEAR

This means: operators who register custom factories before any tree-mode op fires see boot registration become a no-op (their factory wins). The bootstrap is the *default*, not the *authority*.

## 8 production-wiring AST pins (Phase E)

1. **Composition pin**: `maybe_inject_sibling_outcomes` top-level import from `repair_tree` (Phase 3 substrate composition)
2. **Composition pin**: `WorktreeManager` lazy-imported inside factory body (canonical isolation)
3. **Composition pin**: `CanonicalBranchValidator` lazy-imported inside factory body (no parallel validator)
4. **Composition pin**: `TestRunner` + `SemanticGuardian` lazy-imported inside factory body
5. **Anti-pattern pin**: no inline `git apply` subprocess invocation outside `GitApplyDiffApplier.__call__`
6. **§1 Boundary pin**: no `orchestrator`/`iron_gate`/`change_engine`/`candidate_generator`/`policy_engine`/`risk_tier` imports
7. **Closed-taxonomy pin**: `_TREE_VERDICT_TO_STOP_REASON` covers exactly `{EXHAUSTED, BUDGET_TERMINAL}` (WON_TERMINAL handled separately as L2_CONVERGED; EXPANDED never reaches terminal branch)
8. **Closed-taxonomy pin**: `_TREE_OUTCOME_TO_ITERATION_OUTCOME` covers exactly the 5 `BranchOutcome` members (drift detector)

Plus 12 consolidated AST pins from v3.3 Phase 5 (taxonomies + composition imports + strategy gate position + `_run_inner` bytes-pin + SSE registration + register_flags presence) — total **20 AST pins across the Treefinement arc**.

## 2 new FlagRegistry seeds (auto-discovered via §33.3 walker)

- `JARVIS_L2_TREE_GIT_APPLY_TIMEOUT_S` (default 15s, mirrors `RepairSandbox.apply_patch` discipline)
- `JARVIS_L2_TREE_GENERATOR_COST_USD_ESTIMATE` (default 0.005, matches IMMEDIATE route envelope)

Combined with v3.3's 19 seeds: **21 total Treefinement FlagRegistry seeds** across `repair_tree.py` (15) + `repair_tree_archive.py` (4) + `repair_tree_production.py` (2).

## Phase 9 graduation soak — readiness checklist

The strategy gate is now operationally reachable. To start Phase 9 graduation soaks:

1. **Flip master flag**: `JARVIS_L2_TREEFINEMENT_ENABLED=true`
2. **Select tree strategy**: `JARVIS_L2_BRANCHING_STRATEGY=bfs` (or `beam_k`)
3. **Optional knobs** (defaults are operator-approved Phase 0 values):
   - `JARVIS_L2_MAX_BRANCHES_PER_LAYER=3`
   - `JARVIS_L2_BEAM_WIDTH=2`
   - `JARVIS_L2_TREE_GIT_APPLY_TIMEOUT_S=15`
   - `JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED=true` (default; the AlphaVerus delta)
4. **Optional archive surface** for telemetry:
   - `JARVIS_L2_TREE_ARCHIVE_ENABLED=true` for `/repair_tree` REPL + `b-N` refs
   - `JARVIS_L2_TREE_PERSISTENCE_ENABLED=true` for JSONL audit at `.jarvis/ouroboros/repair_tree.jsonl`
5. **Telemetry to watch**:
   - `[RepairTreeProduction] production factory registered lazily on first tree-mode op` log line (first invocation)
   - 4 SSE events: `repair_branch_promoted` / `repair_branch_pruned` / `repair_layer_completed` / `repair_tree_won`
   - Per-branch cost field in `/repair_tree branches`
   - IDE GET `/observability/repair-tree/{recent,op/{id},branch/{ref}}` for live monitoring
6. **Graduation criterion (Phase 9 ladder)**: master flag stays default-FALSE until **EITHER**:
   - ≥10% L2 success-rate lift over LINEAR baseline, OR
   - ≥20% wall-clock reduction at parity success rate
   - on ≥2 consecutive clean soaks
7. **Rollback paths** (all bytes-identical to pre-v3.3):
   - **Soft**: `JARVIS_L2_BRANCHING_STRATEGY=linear` — immediate fall-through to legacy `_run_inner`
   - **Hard**: `JARVIS_L2_TREEFINEMENT_ENABLED=false` — master kill; gate exits before any tree-mode code path
   - **Bytes invariant**: legacy `_run_inner` sha256 pinned at `9e881fdde25ec5b1` (verified after every code change)

## Composition discipline — what was deliberately NOT done

- **No parallel provider invocation** — Phase A extracted ONE primitive both `_run_inner` and `ProductionBranchGenerator` compose
- **No parallel patch primitive** — Phase B composes canonical `git apply` (AST-pinned: only one subprocess invocation in the codebase)
- **No parallel block formatter** — Phase C composes Phase 3's `maybe_inject_sibling_outcomes` (no duplicated cross-branch gating)
- **No parallel context type** — Phase C wraps canonical `RepairContext` additively via `_AugmentedRepairContext`
- **No parallel ring or persistence** — production wiring reuses v3.3's `TreeArchive` substrate end-to-end
- **No parallel SSE broker** — adapter publishes via canonical `publish_task_event` through v3.3's 4 events
- **No top-level import of `repair_tree_production` from `repair_engine`** — lazy import inside `_invoke_tree_factory` (circular-import safety + zero-cost for non-tree-mode callers)
- **No parallel registration registry** — `register_production_factory_at_boot` composes v3.3's `register_production_tree_runner_factory`
- **No changes to `_run_inner` source bytes** — Phase A extraction preserved byte-equivalent LINEAR FSM behavior; sha256 re-locked atomically with the extraction

## Files

- `backend/core/ouroboros/governance/repair_tree_production.py` (~1700 LOC, NEW)
- `backend/core/ouroboros/governance/repair_engine.py` (Phase A extraction + Phase D gate body + Phase E lazy registration)
- `tests/governance/test_repair_engine_generate_primitive.py` (Phase A, 11 tests)
- `tests/governance/test_repair_tree_production_diff_applier.py` (Phase B, 27 tests)
- `tests/governance/test_repair_tree_production_generator.py` (Phase C, 26 tests)
- `tests/governance/test_repair_tree_production_factory.py` (Phase D, 20 tests)
- `tests/governance/test_repair_tree_production_hardening.py` (Phase E, 23 tests)

## Cross-arc cumulative — Treefinement L2 end-to-end (v3.3 substrate + v3.4 production wiring)

- **Files**: 5 new substrate files + edits to 4 canonical files
- **LOC**: ~7,800 (6,114 substrate from v3.3 + ~1,700 production wiring from v3.4)
- **Tests**: 350 (243 v3.3 substrate + 107 v3.4 production wiring)
- **AST pins**: 20 (12 v3.3 consolidated + 8 v3.4 production wiring)
- **FlagRegistry seeds**: 21 (19 v3.3 + 2 v3.4)
- **SSE events**: 4 (v3.3)
- **IDE GET routes**: 3 (v3.3)
- **REPL verb**: `/repair_tree` with 5 subcommands + `/expand b-N` as 7th canonical prefix (v3.3)
- **Master flag**: default-FALSE per §33.1 — graduation requires ≥10% L2 success-rate lift OR ≥20% wall-clock reduction at parity over ≥2 consecutive clean soaks
- **Surrounding-substrate regressions across both arcs**: 0

## What's next

1. **Phase 9 graduation soaks** — operator-paced. With production wiring complete, the soak ladder can start accumulating evidence per §41.6's "cadence flags graduated 1→20+" Layer 3 metric.
2. **STELLAR structural patch retrieval** (§40.7.2 follow-on candidate) stacks naturally on Treefinement's `_patch_sig` ring — top-M survivors at layer N use AST signature as retrieval key into SemanticIndex, surfacing past patches with matching shape as layer-N+1 priors via the same cross-branch injection point.
3. **Agent0 uncertainty-as-reward** — half-session quick win that boosts ProactiveExploration priority based on Move 6.5 K-roll consensus disagreement.

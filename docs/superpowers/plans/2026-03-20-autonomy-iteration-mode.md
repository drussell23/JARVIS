# Autonomy Iteration Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a proactive self-improvement loop to Ouroboros that selects tasks from backlog/miner, builds ExecutionGraphs, runs them via SubagentScheduler, and creates PRs gated by trust graduation.

**Architecture:** 9-state iteration service (Zone 6.10) composes existing governance infrastructure. Pull-based task selection (backlog-first, miner fallback). Separate SubagentScheduler instance with dedicated graph store. All safety gates (budget, blast radius, trust tier, policy hash) enforced.

**Tech Stack:** Python 3.9+, asyncio, existing Ouroboros governance (SubagentScheduler, ExecutionGraph, TrustGraduator, OperationLedger, CommProtocol)

**Spec:** `docs/superpowers/specs/2026-03-20-autonomy-iteration-mode-design.md`

---

## File Structure

**New files (7 source + 8 test):**

| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/governance/autonomy/path_utils.py` | `canonicalize_path()` + `PathTraversalError` |
| `backend/core/ouroboros/governance/autonomy/iteration_types.py` | All dataclasses: `IterationTask`, `IterationState`, policies, outcomes, keys |
| `backend/core/ouroboros/governance/autonomy/resource_governor.py` | CPU/memory preemption checks |
| `backend/core/ouroboros/governance/autonomy/iteration_budget.py` | `IterationBudgetGuard` + `IterationBudgetWindow` |
| `backend/core/ouroboros/governance/autonomy/iteration_planner.py` | `IterationTaskSource` + `IterationPlanner` (task to graph) |
| `backend/core/ouroboros/governance/autonomy/preflight.py` | Pre-submit invariant checks |
| `backend/core/ouroboros/governance/autonomy/iteration_service.py` | `AutonomyIterationService` (10-state FSM + main loop) |

**Modified files (2):**

| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/ledger.py:54-62` | Add `BUDGET_CHECKPOINT`, `PRE_APPLY_CHECKSUM`, `ITERATION_OUTCOME` to `OperationState` |
| `unified_supervisor.py` (after Zone 6.9) | Zone 6.10: create + start `AutonomyIterationService` |

**Review fix notes (from plan review C1-C4, I1-I6):**

- **C1 (miner instance):** Iteration service constructs its own `OpportunityMinerSensor(router=None)` — `scan_once()` does not use the router. No modification to `intake_layer_service.py` needed.
- **C2 (backlog path):** Uses `{project_root}/.jarvis/backlog.json` (matching existing `BacklogSensor`), NOT `~/.jarvis/backlog.json`.
- **C3 (recovery API):** Uses `graph_store.load_inflight()` (existing API), NOT `list_nonterminal()` (does not exist).
- **C4 (public API detection):** `BlastRadiusPolicy.check_public_api_count()` uses heuristic: files with `__all__` or files in `__init__.py` re-exports. No `has_exported_symbols()` needed on Oracle.
- **I2 (state count):** FSM has 10 states (IDLE, SELECTING, PLANNING, EXECUTING, RECOVERING, EVALUATING, REVIEW_GATE, COOLDOWN, PAUSED, STOPPED).
- **I6 (__init__.py):** New modules are imported directly (e.g., `from backend.core.ouroboros.governance.autonomy.iteration_service import ...`). No `__init__.py` updates needed.

---

## Task 1: Path Canonicalization Utility

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/path_utils.py`
- Test: `tests/test_ouroboros_governance/test_path_utils.py`

**Go/No-Go: T01, T02, T03**

- [ ] Step 1: Write failing test file with 8 tests covering dotslash resolution (T01), symlink resolution (T02), traversal rejection (T03), trailing slash stripping, double slash normalization, absolute-within-repo, nonexistent file canonicalization, empty string handling.

- [ ] Step 2: Run `python3 -m pytest tests/test_ouroboros_governance/test_path_utils.py -v` and verify all fail with import error.

- [ ] Step 3: Implement `canonicalize_path(path: str, repo_root: Path) -> str` and `PathTraversalError`. Use `Path.resolve()` for symlinks, `relative_to()` for containment check. ~40 lines.

- [ ] Step 4: Run tests, verify 8 passed.

- [ ] Step 5: Commit: `feat(iteration): add path canonicalization utility (T01-T03)`

---

## Task 2: Iteration Types (all dataclasses + idempotency keys)

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/iteration_types.py`
- Modify: `backend/core/ouroboros/governance/ledger.py:54-62` (add 3 enum values)
- Test: `tests/test_ouroboros_governance/test_iteration_types.py`

**Go/No-Go: T04, T05, T07, T08, T09**

- [ ] Step 1: Add `BUDGET_CHECKPOINT`, `PRE_APPLY_CHECKSUM`, `ITERATION_OUTCOME` to `OperationState` enum in ledger.py after line 62.

- [ ] Step 2: Write failing test file with ~12 tests covering: plan_id stability (T04), plan_id changes on policy change (T05), poisoned task detection (T07), blast radius file count rejection (T08), blast radius public API rejection (T09), all IterationState values exist, all RecoveryDecision values exist, PlannerOutcome rejected status, task fingerprint determinism.

- [ ] Step 3: Run tests, verify fail.

- [ ] Step 4: Implement `iteration_types.py` (~200 lines). `IterationState` enum (10 states: IDLE, SELECTING, PLANNING, EXECUTING, RECOVERING, EVALUATING, REVIEW_GATE, COOLDOWN, PAUSED, STOPPED), `IterationTask`, `PlannerRejectReason`, `PlannerOutcome`, `PlannedGraphMetadata`, `PlanningContext` (trust_tier: AutonomyTier), `BlastRadiusPolicy` with per-dimension check methods, `IterationStopPolicy` with `from_env()`, `IterationBudgetWindow` with `is_expired()`/`reset_if_expired()`, `TaskRejectionTracker`, `RecoveryDecision`, `compute_task_fingerprint()`, `compute_plan_id()`, `compute_policy_hash()`.

- [ ] Step 5: Run tests, verify ~12 passed.

- [ ] Step 6: Commit: `feat(iteration): add iteration types, idempotency keys, policies (T04-T09)`

---

## Task 3: Resource Governor

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/resource_governor.py`
- Test: `tests/test_ouroboros_governance/test_resource_governor.py`

**Go/No-Go: T21**

- [ ] Step 1: Write failing test file with 4 tests: yields on high CPU (T21), yields on high memory, does not yield when normal, handles psutil error gracefully. All mock `psutil.cpu_percent` and `psutil.virtual_memory`.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `ResourceGovernor` frozen dataclass with `async should_yield() -> bool`. Uses psutil with exception handling (fail-open). ~60 lines.

- [ ] Step 4: Run tests, verify 4 passed.

- [ ] Step 5: Commit: `feat(iteration): add ResourceGovernor for CPU/memory preemption (T21)`

---

## Task 4: Iteration Budget Guard

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/iteration_budget.py`
- Test: `tests/test_ouroboros_governance/test_iteration_budget.py`

**Go/No-Go: T14, T16**

- [ ] Step 1: Write failing test file with ~10 tests: `can_proceed()` returns False on budget exhaustion (T14), returns False on iteration count exhaustion, exponential backoff 60/120/240 (T16), backoff capped at max_cooldown, window resets on new day, `record_spend()` increments, `load_from_ledger()` reconstructs window, empty ledger produces fresh window.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `IterationBudgetGuard` with `can_proceed() -> Tuple[bool, str]`, `record_spend(iteration_id, cost)`, `compute_cooldown(consecutive_failures) -> float`, `load_from_ledger()`. Writes `BUDGET_CHECKPOINT` entries. ~100 lines.

- [ ] Step 4: Run tests, verify ~10 passed.

- [ ] Step 5: Commit: `feat(iteration): add IterationBudgetGuard with ledger-backed tracking (T14, T16)`

---

## Task 5: Preflight Invariant Checks

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/preflight.py`
- Test: `tests/test_ouroboros_governance/test_preflight.py`

**Go/No-Go: T19, T28**

- [ ] Step 1: Write failing test file with ~9 tests: rejects stale snapshot / repo HEAD changed (T19), rejects trust tier demotion, rejects budget exhaustion, rejects blast radius violation, rejects policy hash mismatch (T28), detects path conflict with in-flight graphs (spec check #5), passes when all checks valid, handles git subprocess failure gracefully, handles missing repo gracefully.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `preflight_check()` async function. Gets HEAD via `asyncio.create_subprocess_exec("git", "rev-parse", "HEAD")`. Compares all context fields. Returns first error found or None. ~80 lines.

- [ ] Step 4: Run tests, verify ~8 passed.

- [ ] Step 5: Commit: `feat(iteration): add preflight invariant checks (T19, T28)`

---

## Task 6: Iteration Planner (task to graph)

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/iteration_planner.py`
- Test: `tests/test_ouroboros_governance/test_iteration_planner.py`

**Go/No-Go: T06, T10, T11, T12, T20, T33**

- [ ] Step 1: Write failing test file with ~15 tests covering: `IterationTaskSource.get_backlog_tasks()` reads JSON and filters pending, handles malformed JSON (T33), `get_miner_tasks()` converts StaticCandidate, skips poisoned tasks, miner fairness every Nth cycle (T20), `IterationPlanner.plan()` returns rejected PlannerOutcome not None (T06), `select_acceptance_tests()` is deterministic (T10), metadata includes expansion_proof (T12), planner catches DAG cycles (T11 retest), planner rejects on blast radius, planner handles Oracle returning empty results, planner canonicalizes all paths.

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement two classes:

`IterationTaskSource` (~80 lines): `get_backlog_tasks()` reads `{project_root}/.jarvis/backlog.json` directly, `get_miner_tasks()` calls `miner.scan_once()` (miner constructed with `router=None` since `scan_once()` doesn't use it), `select_task(cycle_count, fairness_interval)` implements hybrid selection.

`IterationPlanner` (~170 lines): 4-step pipeline (expand files via Oracle, analyze deps via Oracle graph, partition into units with owned_paths, assemble ExecutionGraph with stable IDs). All paths canonicalized. Returns `PlannerOutcome` (never None, never raises).

`select_acceptance_tests()` module-level function (~30 lines).

- [ ] Step 4: Run tests, verify ~15 passed.

- [ ] Step 5: Commit: `feat(iteration): add IterationPlanner + TaskSource (T06, T10, T12, T20, T33)`

---

## Task 7: Iteration Service (10-state FSM + main loop)

**Files:**
- Create: `backend/core/ouroboros/governance/autonomy/iteration_service.py`
- Test: `tests/test_ouroboros_governance/test_iteration_service.py`

**Go/No-Go: T13, T15, T17, T18, T22, T27, T30, T31, T32, T34, T35**

This is the largest task. The service composes everything from Tasks 1-6.

- [ ] Step 1: Write failing test file with ~20 tests covering all T13-T35 FSM transitions. All tests mock: SubagentScheduler, Oracle, TrustGraduator, CommProtocol, Ledger. Key tests:
  - `test_idle_to_selecting` (T13)
  - `test_stops_on_error_streak` (T15)
  - `test_recovery_resumes` (T17)
  - `test_recovery_detects_partial` (T18)
  - `test_causal_trace` (T22)
  - `test_kill_switch` (T27)
  - `test_trust_regression` (T30)
  - `test_flag_off_mid_execute` (T31)
  - `test_no_overlap` (T32)
  - `test_state_persisted` (T34)
  - `test_narrator_debounced` (T35)

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Implement `AutonomyIterationService` (~300 lines): 10-state FSM with `_iteration_loop()`, per-state handler methods (`_do_selecting`, `_do_planning`, `_do_executing`, `_do_recovering`, `_do_evaluating`, `_do_review_gate`), stop condition checks, trust regression auto-demotion, kill switch, startup recovery path, health dict.

- [ ] Step 4: Run tests, verify ~20 passed.

- [ ] Step 5: Commit: `feat(iteration): add AutonomyIterationService 9-state FSM (T13-T35)`

---

## Task 8: Supervisor Wiring (Zone 6.10) + E2E Tests

**Files:**
- Modify: `unified_supervisor.py` (after Zone 6.9 block)
- Test: `tests/test_ouroboros_governance/test_iteration_e2e.py`

**Go/No-Go: T23, T24, T25, T26, T29**

- [ ] Step 1: Write E2E test file with ~10 tests. All mock J-Prime and git/gh. Tests:
  - `test_backlog_happy_path` (T23)
  - `test_miner_ack_in_suggest` (T24)
  - `test_review_gate_pr_governed` (T25)
  - `test_review_gate_no_merge_governed` (T26)
  - `test_cross_repo_barriers` (T29)

- [ ] Step 2: Run tests, verify fail.

- [ ] Step 3: Add Zone 6.10 block to `unified_supervisor.py` after Zone 6.9. Feature-flag gated by `JARVIS_AUTONOMY_ITERATION_ENABLED`. Creates `AutonomyIterationService` with all dependencies from existing supervisor state.

- [ ] Step 4: Write E2E test implementations with comprehensive mocking.

- [ ] Step 5: Run all iteration tests: `python3 -m pytest tests/test_ouroboros_governance/test_iteration_*.py tests/test_ouroboros_governance/test_path_utils.py tests/test_ouroboros_governance/test_resource_governor.py tests/test_ouroboros_governance/test_preflight.py -v`. Expected: all pass.

- [ ] Step 6: Run full regression: `python3 -m pytest tests/test_ouroboros_governance/ --timeout=60 -q`. Expected: 0 new failures.

- [ ] Step 7: Commit: `feat(iteration): wire AutonomyIterationService into supervisor Zone 6.10`

---

## Task 9: Activate in .env + Final Validation

**Files:**
- Modify: `.env`

- [ ] Step 1: Add all 10 env vars to `.env`:
```
JARVIS_AUTONOMY_ITERATION_ENABLED=false
JARVIS_AUTONOMY_ITERATION_INTERVAL_S=300
JARVIS_AUTONOMY_MAX_ITERATIONS=10
JARVIS_AUTONOMY_MAX_SPEND_USD=0.50
JARVIS_AUTONOMY_MAX_WALL_TIME_S=3600
JARVIS_AUTONOMY_COOLDOWN_BASE_S=60
JARVIS_AUTONOMY_MINER_FAIRNESS_N=5
JARVIS_AUTONOMY_MAX_FILES=10
JARVIS_AUTONOMY_MAX_LINES=500
JARVIS_AUTONOMY_MAX_PRS=3
```

- [ ] Step 2: Run full Go/No-Go matrix (all T01-T35). Expected: all pass. Decision: **Go for OBSERVE tier.**

- [ ] Step 3: Commit: `feat(iteration): add env vars for Autonomy Iteration Mode (OBSERVE-ready)`

---

## Execution Order + Parallelism

```
Task 1 (path_utils)        ─┐
Task 3 (resource_governor) ─┤── can run in parallel (no deps)
                            │
Task 2 (iteration_types)   ─┘── depends on Task 1
                            │
Task 4 (budget)    ─┐      │
Task 5 (preflight) ─┤──────┘── can run in parallel after Task 2
Task 6 (planner)   ─┘
                    │
Task 7 (service)   ─────────── depends on Tasks 2-6
Task 8 (wiring)    ─────────── depends on Task 7
Task 9 (activation) ────────── depends on Task 8
```

Total new code: ~1030 lines across 7 files. Total tests: ~86 across 8 files covering all 35 Go/No-Go items.

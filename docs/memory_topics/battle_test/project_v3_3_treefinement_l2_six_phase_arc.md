---
title: Project V3 3 Treefinement L2 Six Phase Arc
modules: [backend/core/ouroboros/governance/repair_tree.py, backend/core/ouroboros/governance/repair_tree_archive.py, backend/core/ouroboros/governance/repair_tree_repl.py, backend/core/ouroboros/governance/repair_engine.py, backend/core/ouroboros/governance/ide_observability.py, backend/core/ouroboros/governance/ide_observability_stream.py, backend/core/ouroboros/battle_test/serpent_flow.py, tests/governance/test_repair_tree.py, tests/governance/test_repair_tree_flags.py, tests/governance/test_repair_tree_runner.py, tests/governance/test_repair_tree_validator.py, tests/governance/test_repair_tree_cross_branch.py]
status: historical
source: project_v3_3_treefinement_l2_six_phase_arc.md
---

May 11 2026 — six-phase arc shipping AlphaVerus Treefinement L2 tree-search repair. **First §40.7-citation→implementation closure** in the PRD. Closes the published-research delta over O+V's pre-existing linear FSM `repair_engine`: when L2 iteration N takes a wrong fix-strategy turn, iterations N+1..max inherit the misclassification and the 120s timebox burns out on a dead branch. Treefinement forks at each repair attempt, validator feedback prunes losing branches, surviving branches inform the next layer's GENERATE prompt (cross-branch learning — the actual published delta over naive parallel repair).

## Six-phase arc summary

| Phase | Scope | Commit | Tests | Files |
|---|---|---|---|---|
| 0 | Substrate (closed taxonomies + dataclasses + budget knobs + flag seed) | `00c7944739` | 40 (28+12) | `repair_tree.py` |
| 1 | Tree runner core (BFS layer dispatch + budget composition + posture-K) | bundled in `271c43a24e` | 42 | `repair_tree.py` |
| 2 | Validator integration + pruning (TestRunner + SemanticGuardian + IronGate per branch) | `271c43a24e` | 32 | `repair_tree.py` |
| 3 | Cross-branch learning (StrategicDirection prompt injection — the AlphaVerus delta) | `d62e5218d1` | 34 | `repair_tree.py` |
| 4 | Surfaces (REPL `/repair_tree` + IDE GET + SSE + `/expand b-N` + persistence) | `28fce98d4f` | 68 | `repair_tree_archive.py` + `repair_tree_repl.py` + `serpent_flow.py` + `ide_observability.py` + `ide_observability_stream.py` |
| 5 | Hardening + integration (strategy gate at RepairEngine.run + 12 AST pins + defense-in-depth) | this session | 27 | `repair_tree.py` + `repair_engine.py` |
| 6 | PRD update (§40.7.7) + this memory artifact + MEMORY.md index | this session | 0 | `OUROBOROS_VENOM_PRD.md` + `MEMORY.md` |

**Cumulative**: 6114+ LOC across 4 substrate files / **243 regression tests** / **12 consolidated AST pins** / **19 FlagRegistry seeds** (15 in `repair_tree.py` + 4 in `repair_tree_archive.py`) / **4 SSE events** / **3 IDE GET routes** / **5-subcommand REPL verb** / **b-N joins canonical /expand prefix family** (now 7th: t-N / d-N / o-N / n-N / p-N / q-N / b-N).

## Architecture decisions

**Root problem solved at the source — no shortcut**:

The shortcut would be: bump `JARVIS_L2_MAX_ITERS` from 5 to 15 — burns 3× the tokens for sub-linear lift, doesn't escape blind alleys, blows the 120s timebox. The other shortcut would be: spawn 5 parallel `_run_inner` calls and pick the first winner — race-the-loop, K× cost on every repair, no inter-branch learning, no budget composition with the existing `max_total_validation_runs` envelope.

**Structural fix**: BFS tree-search where the **branching axis is the fix strategy** (failure-class hypothesis × candidate diff), validator feedback is the **pruning oracle**, `_patch_sig` is the **branch-equivalence key**, and survivors at layer N inform the GENERATE prompt at layer N+1 (cross-branch learning, not race-the-loop).

**Composition over duplication — every primitive composed, none duplicated**:

| Concern | Composes (single source) |
|---|---|
| K sizing | `parallel_dispatch.posture_weight_for(posture)` × `TreefinementBudget.max_branches_per_layer` |
| Branch equivalence | `failure_classifier.patch_signature_hash(diff)` (same primitive `repair_engine._patch_sig` wraps) |
| Branch isolation | `worktree_manager.WorktreeManager.create()` (COW git worktree, reap-orphans on boot) |
| ASCII strictness | `ascii_strict_gate.scan_content(diff)` |
| Semantic patterns | `SemanticGuardian.inspect_batch(candidate_files)` |
| Test execution | `TestRunner.run(test_files, sandbox_dir=worktree_dir)` |
| Cost envelope | `RepairBudget.max_total_validation_runs` (shared with LINEAR FSM — no parallel budget bookkeeping) |
| Cross-process flock | `cross_process_jsonl.flock_append_line` (JSONL persistence) |
| SSE broker | `ide_observability_stream.publish_task_event` |
| IDE routes | `IDEObservabilityRouter.register_routes()` (loopback+rate-limit+CORS) |
| REPL auto-discovery | `repl_dispatch_registry` via §33.3 naming-cage (filename → verb) |
| `/expand <ref>` family | `serpent_flow._handle_expand` prefix dispatcher |
| Master-flag-FALSE rollback | byte-identical legacy `_run_inner` (sha256-pinned) |

## §1 Boundary preserved across all 6 phases

**Authority asymmetry — strict read-only consumers**:

- `repair_tree.py` MUST NOT import: `orchestrator` / `iron_gate` / `change_engine` / `candidate_generator` / `policy_engine` / `risk_tier` (AST-pinned in `test_repair_tree.py::test_module_does_not_import_authority_substrates`)
- `repair_engine.py` MUST NOT have top-level import of `repair_tree` (lazy import inside `_maybe_run_treefinement` only — circular-import safety, AST-pinned in `test_repair_tree_hardening.py::test_repair_engine_run_imports_treefinement_lazily`)
- `repair_tree_repl.py` MUST NOT import policy/orchestrator
- IDE GET routes import only public accessors (`get_default_archive`, `archive_enabled`) — never `ArchivedBranch` constructors

**Worktree isolation — no shared-tree fallback**:

When `WorktreeManager.create()` fails (branch collision, disk full, permission denied), the branch returns `failure_class=infra` with `fix_hypothesis="worktree_create_failed:<type>:<msg>"` — NEVER falls back to the shared tree. Mirrors the L3 `subagent_scheduler` discipline. Generator never re-invoked for failed-isolation branches.

## Phase 5 strategy gate — production wiring without production hazard

The gate at `RepairEngine.run()` consults Treefinement BEFORE the legacy `_run_inner`. Decision table:

```
treefinement_enabled() == False              → fall through to LINEAR (default)
budget.branching_strategy == LINEAR          → fall through to LINEAR
get_production_tree_runner_factory() is None → fall through to LINEAR (Phase 5 default)
factory raises                               → fall through with warning log
asyncio.CancelledError                       → propagates (orchestrator handles POSTMORTEM)
otherwise                                    → call factory + run tree + adapt result
```

Phase 5 ships a NULL factory registry — production wiring of generator/validator/applier is deferred to a follow-on arc that finalizes the production Protocol shapes. The gate is structurally REACHABLE but BEHAVIORALLY a no-op until a factory is registered. Operators see a structured info log when tree mode is requested-but-not-wired.

The `_run_inner` legacy path is **bytes-pinned via sha256** of AST-unparsed source — drift detector. Updating `_run_inner` requires explicit Phase tag + soak validation, not opportunistic refactor.

## Operator-visibility surfaces (Phase 4)

**REPL** (`/repair_tree`): 5 subcommands — `recent` / `branches` / `op <id>` / `layers <id>` / `stats` / `help`. `help` always works (bypasses master gate). Auto-discovered via §33.3 naming-cage (filename `repair_tree_repl.py` → verb `repair_tree`).

**`/expand b-N`** dispatcher (7th prefix in canonical family). Composes `get_default_archive().get_by_ref(ref)` — single source of truth for the cross-substrate ring.

**IDE GET** routes:
- `GET /observability/repair-tree[?limit=N]` — recent branches + snapshot
- `GET /observability/repair-tree/op/{op_id}` — branches for op
- `GET /observability/repair-tree/branch/{ref}` — single branch by b-N

All 3 routes: dual master-flag gating (403 with `ide_observability.disabled` OR `ide_observability.repair_tree_disabled`), 404 with `repair_tree_ref_not_found` for unknown b-N, 503 with `repair_tree_unavailable` on substrate failure. Read-only — test verifies GET doesn't mutate `next_seq`.

**4 SSE events** registered in `_VALID_EVENT_TYPES`: `repair_branch_promoted` / `repair_branch_pruned` / `repair_layer_completed` / `repair_tree_won`. Producer-bridge in `_publish_branch_lifecycle_events` dedups WON branches (no double-fire — only `repair_tree_won` for the winner).

**§33.4 JSONL persistence** at `.jarvis/ouroboros/repair_tree.jsonl` (env-overridable). One line per branch, atomic per-line via canonical `flock_append_line`. Independent master flag from the ring (operator may want disk audit without RAM ring).

## 12 consolidated AST pins (Phase 5)

1. `BranchingStrategy` 3-value frozen
2. `BranchOutcome` 5-value frozen
3. `LayerVerdict` 4-value frozen
4. `PruningReason` 6-value frozen
5. composition: `parallel_dispatch.posture_weight_for` import
6. composition: `worktree_manager.WorktreeManager` import
7. composition: `failure_classifier.patch_signature_hash` import
8. composition: `repair_tree_archive.maybe_archive_tree_result` (Phase 5 archive wire)
9. strategy gate position pin (`_maybe_run_treefinement` called BEFORE `_run_inner`)
10. legacy `_run_inner` bytes-pinned (sha256 of AST-unparsed source)
11. SSE 4 events present in `_VALID_EVENT_TYPES`
12. `register_flags()` presence on both `repair_tree.py` AND `repair_tree_archive.py`

Plus scattered earlier-phase pins: `_normalize_hypothesis` purity, no parallel posture-weight table, no inline LLM call in `maybe_inject_sibling_outcomes`, signature pin on cross-branch injector, no parallel pattern detector / pytest invocation in `CanonicalBranchValidator`.

## 19 FlagRegistry seeds (auto-discovered via §33.3 walker)

**`repair_tree.py` (15 seeds)**:
- `JARVIS_L2_TREEFINEMENT_ENABLED` (master, default-FALSE)
- `JARVIS_L2_BRANCHING_STRATEGY` (linear/bfs/beam_k, default linear)
- `JARVIS_L2_MAX_BRANCHES_PER_LAYER` (K=3 default)
- `JARVIS_L2_BEAM_WIDTH` (M=2 default)
- `JARVIS_L2_BRANCH_DEDUP_ENABLED` (default TRUE)
- `JARVIS_L2_CROSS_BRANCH_LEARNING_ENABLED` (default TRUE — the AlphaVerus delta)
- `JARVIS_L2_TREE_EMERGENCY_DEMOTE_THRESHOLD` (0.85 default)
- `JARVIS_L2_TREE_TEST_PASS_WEIGHT` (1.0)
- `JARVIS_L2_TREE_SOFT_FINDING_PENALTY` (0.2)
- `JARVIS_L2_TREE_WON_FLOOR` (0.95)
- `JARVIS_L2_TREE_PROMOTED_FLOOR` (0.4)
- `JARVIS_L2_TREE_VALIDATOR_TEST_TIMEOUT_S` (60s)
- `JARVIS_L2_TREE_SIBLING_MAX_COUNT` (M=2 sibling outcomes in prompt)
- `JARVIS_L2_TREE_SIBLING_MAX_CHARS` (800 chars ≈ 200 tokens)
- `JARVIS_L2_TREE_SIBLING_SKIP_POSTURES` (default "MAINTAIN")

**`repair_tree_archive.py` (4 seeds)**:
- `JARVIS_L2_TREE_ARCHIVE_ENABLED` (master, default-FALSE)
- `JARVIS_L2_TREE_ARCHIVE_SIZE` (30 default ring capacity)
- `JARVIS_L2_TREE_PERSISTENCE_ENABLED` (master, default-FALSE)
- `JARVIS_L2_TREE_PERSISTENCE_PATH` (`.jarvis/ouroboros/repair_tree.jsonl` default)

## Phase 9 graduation criteria

Master flag stays default-FALSE until **3-clean-soak ladder** demonstrates EITHER:
- ≥10% L2 success-rate lift over LINEAR baseline, OR
- ≥20% wall-clock reduction at parity success rate

on ≥2 consecutive soaks. Empirically derived, no hand-tuning. Production-factory wiring (deferred follow-on arc) is the prerequisite for graduation soaks — Phase 5 ships only the gate skeleton.

## Composition discipline — what was deliberately NOT done

- **No parallel signature primitive** — branch_id derives from canonical `patch_signature_hash` (AST-pinned forbidden function names: `*patch_sig*` / `*hash*` on `RepairTreeRunner`)
- **No parallel posture-weight table** — composes `posture_weight_for` (AST-pinned: `_POSTURE_WEIGHTS` symbol forbidden in module source)
- **No parallel ring** — `TreeArchive` mirrors `BoundedDecisionArchive` shape exactly (drop-oldest FIFO + monotonic refs + RLock)
- **No parallel flock primitive** — composes `cross_process_jsonl.flock_append_line`
- **No parallel SSE broker** — composes `publish_task_event` + extends `_VALID_EVENT_TYPES` frozenset
- **No parallel REPL dispatcher** — auto-discovered via `repl_dispatch_registry`
- **No parallel pattern detector** — `CanonicalBranchValidator` composes `SemanticGuardian.inspect_batch` (AST-pinned: forbidden method names `*pattern*` / `*detect*` / `*pytest*`)
- **No parallel test infrastructure** — composes `TestRunner.run` with `sandbox_dir=worktree_dir`
- **No top-level import of repair_tree from repair_engine** — lazy import inside `_maybe_run_treefinement` (circular-import safety, AST-pinned)
- **No diff parsing in validator** — `DiffApplier` Protocol owns "what changed" semantics; validator consumes per-file (path, old, new) tuples

## Files

- `backend/core/ouroboros/governance/repair_tree.py` (~2758 LOC)
- `backend/core/ouroboros/governance/repair_tree_archive.py` (~755 LOC)
- `backend/core/ouroboros/governance/repair_tree_repl.py` (~404 LOC)
- `backend/core/ouroboros/governance/repair_engine.py` (+strategy gate ~125 LOC)
- `backend/core/ouroboros/governance/ide_observability.py` (+199 LOC, 3 routes + handlers)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (+16 LOC, 4 events)
- `backend/core/ouroboros/battle_test/serpent_flow.py` (+75 LOC, `/expand b-N`)
- `tests/governance/test_repair_tree.py` (28 substrate tests)
- `tests/governance/test_repair_tree_flags.py` (12 flag tests)
- `tests/governance/test_repair_tree_runner.py` (42 runner tests)
- `tests/governance/test_repair_tree_validator.py` (32 validator tests)
- `tests/governance/test_repair_tree_cross_branch.py` (34 cross-branch tests)
- `tests/governance/test_repair_tree_archive.py` (37 archive tests)
- `tests/governance/test_repair_tree_repl.py` (17 REPL tests)
- `tests/governance/test_ide_observability_repair_tree.py` (14 IDE GET tests)
- `tests/governance/test_repair_tree_hardening.py` (27 consolidated hardening tests)

## What's next (deferred follow-on arcs)

1. **Production wiring arc**: register a real `BranchGenerator` (composing `repair_engine`'s existing generation path with cross-branch StrategicDirection injection) + `BranchValidator` (composing `CanonicalBranchValidator` with a real `DiffApplier` wrapping `RepairSandbox.apply_patch` semantics) + `RepairTreeResult → RepairResult` adapter. Once registered, the Phase 5 gate becomes operationally reachable.

2. **Phase 9 graduation soaks**: with production wiring in place, run 3-clean-soak ladder; if criteria met, flip `JARVIS_L2_TREEFINEMENT_ENABLED` default-TRUE.

3. **Stack adjacent §40.7 candidates**: STELLAR structural patch retrieval (#2 in original plan) becomes natural after Treefinement — top-M survivors at layer N can use `_patch_sig` as a retrieval key for past patches with matching AST shape, surfacing them as additional layer-N+1 priors via the same cross-branch injection point.

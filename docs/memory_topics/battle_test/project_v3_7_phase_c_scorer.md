---
title: Project V3 7 Phase C Scorer
modules: [backend/core/ouroboros/governance/swe_bench_pro/scorer.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_scorer.py, backend/core/ouroboros/governance/repair_engine.py]
status: historical
source: project_v3_7_phase_c_scorer.md
---

May 12 2026 — SWE-Bench-Pro Phase C scorer shipped on branch `ouroboros/swe-bench-pro/phase-c-scorer`.

## Closing the loop: Phases A → B → C end-to-end

With Phase C shipped, the system is operationally complete end-to-end for single-problem benchmark scoring:

1. **Phase A** — load problem from local cache or upstream dataset
2. **B.1** — clone repo, checkout base_commit, apply test_patch, return PreparedProblem
3. **B.2.0** — worktree-aware OperationAdvisor (consumes envelope.evidence.repo_root)
4. **B.2.0.5** — orchestrator publishes operation_terminal SSE on terminal states
5. **B.2.1** — `build_evaluation_envelope` composes ProblemSpec + PreparedProblem
6. **B.2.2** — `evaluate_problem` async façade with bounded SSE rendezvous + ledger fallback
7. **Phase C (this PR)** — `score_evaluation` reproduces the fix in a fresh worktree and scores it

The system can now answer the load-bearing question: **did the model's fix actually work, as measured by the canonical SWE-Bench rubric?**

## Architectural decisions — Phase C scorer

**Root problem solved at source — no shortcut**:

The shortcut path would have been to score INSIDE the B.2.2 evaluator façade — running the tests before cleanup_prepared fires. That would have:
1. Coupled the side-effect-producing evaluator to the pure-data scoring rubric
2. Made offline re-scoring impossible (the worktree is gone after evaluator returns)
3. Made rubric evolution painful (every rubric change forces a re-evaluation of the original problem instead of a re-score of the captured patch)
4. Conflated "did the orchestrator reach terminal=applied?" (B.2.2) with "did the produced patch actually fix the bug?" (Phase C) — two structurally distinct questions

The structural fix: Phase C is reproducible from `(captured_patch, problem)` alone. It re-prepares a fresh worktree via the SAME `prepare_problem` primitive B.1 / B.2.2 use, applies the captured patch with the SAME safe git-apply pattern B.1 uses, and runs pytest via the SAME canonical `TestRunner` the orchestrator's VALIDATE / Treefinement compose. No parallel implementations anywhere.

**Canonical SWE-Bench cheat-detection (default ON)**:

Real SWE-Bench benchmarks disqualify patches that modify test files — a patch that changes the assertion makes any test pass for the wrong reason. The default for `JARVIS_SWE_BENCH_PRO_SCORE_REJECT_TEST_MODS` is `TRUE` (the only Phase C flag defaulting TRUE because rubric integrity depends on it). Operators evaluating rubric variants can flip via env or per-call argument. The detection uses `extract_diff_targets` (canonical Treefinement primitive) + a closed test-file-marker heuristic (`/tests/`, `/test/`, `test_*.py`, `*_test.py`).

**Test-file selection scoped to test_patch**:

The scorer runs ONLY the failing tests added by `problem.test_patch` — NOT the whole repo's test suite. `prepared.target_paths` (parsed by B.1 from the test_patch's `+++ b/<path>` headers) is filtered to test files and resolved under the worktree. Running the whole test suite would include unrelated regressions and make scoring meaningless.

**Reproducible from `(captured_patch, problem)` alone**:

Critical architectural property: Phase C does not need access to the evaluation's original worktree. This means:
- Offline re-scoring of historic `EvaluationResult` payloads when the rubric evolves
- Cross-validation: third-party SWE-Bench-Pro runs can ingest into the JARVIS report pipeline
- Phase D / F can re-aggregate scores without re-running the expensive Phase B evaluation
- Rubric A/B testing: two scorers with different rubrics can independently score the same captured patches

## Composition discipline — what was deliberately NOT done

- No parallel diff parser — composes `extract_diff_targets` (AST-pinned import)
- No parallel worktree management — composes `prepare_problem` / `cleanup_prepared` (AST-pinned)
- No parallel pytest invocation — composes `TestRunner` (AST-pinned)
- No while-True polling loop (AST-pinned forbidden)
- No naked `asyncio.wait()` without timeout (AST-pinned forbidden)
- No JSONL audit sink in this PR — that's Phase D `result_substrate`'s responsibility
- No graduation flip — the Phase A master flag (`JARVIS_SWE_BENCH_PRO_ENABLED`) governs the whole arc
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/scorer.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + docstring update
- `tests/governance/test_swe_bench_pro_scorer.py` — 34-test spine + 6 AST pins (NEW)

## Master flags (FlagRegistry auto-seeded — 3 specs)

- `JARVIS_SWE_BENCH_PRO_SCORE_TEST_TIMEOUT_S` (INT/CAPACITY, default 600s)
- `JARVIS_SWE_BENCH_PRO_SCORE_REJECT_TEST_MODS` (BOOL/SAFETY, default **TRUE** — rubric integrity)
- `JARVIS_SWE_BENCH_PRO_SCORE_GIT_OP_TIMEOUT_S` (INT/CAPACITY, default 60s)

## What's next — Phase D result_substrate

Phase D bridges per-problem `(EvaluationResult, ScoringResult)` pairs into the cross-problem aggregate store. Architectural shape (preliminary):

1. **`EvaluationResultStore`** — in-memory + optional JSONL persistence at `.jarvis/swe_bench_pro/results.jsonl`
2. **`record_evaluation(eval_result, scoring_result)`** — append-only, dedup by `(instance_id, op_id)` pair
3. **§33.4 audit-ledger contract** — append-only JSONL with flock-protected cross-process append (mirrors `decision_trace_ledger` / `graduation_ledger` patterns shipped earlier)
4. **`query(...)` API** — bounded reads for Phase F report-card aggregation
5. **No new schema** — composes existing `EvaluationResult.to_dict()` + `ScoringResult.to_dict()` for the JSONL row shape

**Phase E** (parallel_eval) and **Phase F** (report_card) follow Phase D:
- Phase E drives N problems concurrently via `subagent_scheduler` with bounded concurrency from existing `JARVIS_PARALLEL_DISPATCH_*` knobs
- Phase F renders the aggregate report card from Phase D's store (pass-rate per repo, per difficulty tier, distribution of ScoreOutcomes, etc.)

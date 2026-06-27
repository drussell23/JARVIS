---
title: Project V3 7 Phase E Parallel Eval
modules: [backend/core/ouroboros/governance/swe_bench_pro/parallel_eval.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_parallel_eval.py]
status: historical
source: project_v3_7_phase_e_parallel_eval.md
---

May 12 2026 — SWE-Bench-Pro Phase E parallel evaluation rig shipped on branch `ouroboros/swe-bench-pro/phase-e-parallel-eval`.

## Phases A → E closure summary

With Phase E shipped, the SWE-Bench-Pro arc is operationally complete end-to-end for parallel benchmark execution:

| Layer | Purpose |
|---|---|
| Phase A | `ProblemSpec` + dataset loader |
| B.1 | `prepare_problem` / `PreparedProblem` / worktree |
| B.2.0 | Worktree-aware `OperationAdvisor` |
| B.2.0.5 | Orchestrator `operation_terminal` SSE |
| B.2.1 | `build_evaluation_envelope` |
| B.2.2 | `evaluate_problem` async façade |
| Phase C | `score_evaluation` pure-data scorer |
| Phase D | `EvaluationResultStore` aggregate substrate |
| **Phase E (this PR)** | **`parallel_evaluate` async generator rig** |

The system can now: **load N problems → fan out concurrent fix attempts → capture each patch → score each deterministically → persist into a queryable aggregate store, all under bounded concurrency from a hot-reload-safe primitive, with live streaming-as-completed semantics.**

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut paths considered + rejected:
1. **Homegrown `asyncio.Semaphore` literal in module body** — would orphan in-flight tasks across module hot-reload boundaries. The canonical `_process_singletons.get_semaphore` solves this by living in a quarantined module that `ModuleHotReloader` never reloads. AST pin forbids the homegrown literal.
2. **`asyncio.gather` batch return** — would force operators to wait for the slowest problem before seeing ANY result. Stream-as-completed via `asyncio.Queue` lets fast problems surface first.
3. **`subagent_scheduler` composition** — wrong concurrency domain. `subagent_scheduler` is for L3 fan-out WITHIN a single op; Phase E's fan-out is ACROSS independent benchmark evaluations. Composing it would couple unrelated subsystems and inherit irrelevant op-context state.
4. **`while True:` poll-and-yield loop** — unnecessary; the rig knows exactly how many records to expect (one per submitted problem) and drains the queue with bounded `queue.get` calls. AST pin forbids the `while True:` pattern.
5. **New master flag for parallel-eval enablement** — Phase A's `JARVIS_SWE_BENCH_PRO_ENABLED` already gates the entire arc via the underlying `evaluate_problem` master gate; a separate parallel-eval flag would be redundant. The rig's only knob is concurrency (orthogonal to the arc's enablement).

The structural fix: compose the canonical hot-reload-safe semaphore + canonical pipeline functions + an internal `asyncio.Queue` for stream-as-completed semantics. AST pins lock every composition seam.

**Single seam to bounded concurrency**:

`_process_singletons.get_semaphore(key, value)` is the canonical primitive for hot-reload-safe asyncio.Semaphore management. The key `"swe_bench_pro_parallel_eval"` is distinct from L3 subagent / candidate-generator / patch-benchmark semaphore keys so Phase E concurrency does not steal slots from unrelated subsystems. AST pin asserts the canonical import; another AST pin forbids any `asyncio.Semaphore(` literal anywhere in the module body. The canonical primitive is the single seam.

**Stream-not-batch semantics**:

`parallel_evaluate` is an `async def ... yield` async generator. Each per-problem task `put`s its `EvaluationRecord` to an internal `asyncio.Queue` upon completion; the iterator `get`s and yields. Operators see live progress under any concurrency level — even `concurrency=1` yields per-problem records as serial work completes, not at the end. This is essential for benchmark eval where the long tail (hard problems running 30 min) shouldn't block visibility into the head (easy problems running 30 seconds).

**Per-task fail-closed contract**:

A defensive `try/except` wraps each task's full pipeline. If `evaluate_problem` or `score_evaluation` violate their fail-closed contract and raise unexpectedly (which should not happen given those contracts but is defensive against future changes), the rig produces a **synthetic record** with `EvaluationOutcome.INGEST_FAILED` (or `ScoreOutcome.SCORING_ERROR` for scorer failures) and a diagnostic carrying the exception class name. Other in-flight tasks continue; the rig does not die from a single contract violation.

**Cooperative cancel**:

Both `async for ... break` early-exits AND full-rig cancellation cancel every in-flight task. The rig's `finally` block calls `task.cancel()` on unfinished tasks and `asyncio.gather(..., return_exceptions=True)` to wait for each task's own `finally` to run. Each task's per-problem `finally` (owned by `evaluate_problem` and `score_evaluation`) handles its own worktree cleanup via B.2.2 + Phase C disciplines. Pending unyielded records are dropped — cancellation is cooperative, not eager.

**Bounded queue.get, not while-True**:

The rig knows exactly how many records to expect: `expected = len(tasks)`. The drain loop iterates exactly `expected` times. There is no speculative polling; every `queue.get()` call is matched 1:1 with a task that WILL put a record (either canonical or synthetic). AST pin asserts no `while True:` loop anywhere.

## Composition discipline — what was deliberately NOT done

- No homegrown `asyncio.Semaphore` literal in module body — composes canonical `get_semaphore` (AST-pinned forbidden)
- No while-True polling loop — bounded `queue.get` sized to submitted-task count (AST-pinned forbidden)
- No new master flag — composes Phase A's master via underlying `evaluate_problem` gate
- No batch-style return — stream-as-completed semantics throughout
- No event-loop-blocking sync I/O — every async surface uses canonical async primitives
- No coupling to `subagent_scheduler` (wrong concurrency domain)
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/parallel_eval.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + docstring update
- `tests/governance/test_swe_bench_pro_parallel_eval.py` — 19-test spine + 4 AST pins (NEW)

## Master flag (FlagRegistry auto-seeded — 1 spec)

- `JARVIS_SWE_BENCH_PRO_PARALLEL_CONCURRENCY` (INT/CAPACITY, default **4**) — bounded concurrency cap

## What's next — Phase F report_card

Phase F is the final milestone of the SWE-Bench-Pro arc. Architectural shape (preliminary):

1. **`render_report_card(store, *, format='markdown', output_path=None) -> str`** — pure-data renderer reading from Phase D's `EvaluationResultStore`
2. **Per-repo pass-rate aggregation** — derived from `record.evaluation.problem_instance_id` (instance_id encodes the repo by convention)
3. **Per-difficulty-tier breakdown** — composes `ProblemSpec.difficulty` (requires keeping problem refs alongside records OR re-loading from cache)
4. **`ScoreOutcome` distribution** — composes Phase D's `aggregate_score_outcomes()` directly
5. **Top-N failing problems with diagnostic clustering** — operator-triage surface; groups by `scoring.diagnostic` prefix for clustered review
6. **Output formats**: `markdown` (default; human-friendly), `json` (machine-readable)
7. **Optional `output_path`** writes the artifact to disk; default is in-memory return only
8. **No master flag** — composes Phase A's master via Phase D's store contents (an empty store renders an empty card cleanly)

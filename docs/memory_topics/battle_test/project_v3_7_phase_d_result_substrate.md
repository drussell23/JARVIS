---
title: Project V3 7 Phase D Result Substrate
modules: [backend/core/ouroboros/governance/swe_bench_pro/result_store.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_result_store.py]
status: historical
source: project_v3_7_phase_d_result_substrate.md
---

May 12 2026 — SWE-Bench-Pro Phase D result substrate shipped on branch `ouroboros/swe-bench-pro/phase-d-result-substrate`.

## Phases A → B → C → D closure summary

Phase D is the persistence + aggregation layer above per-problem evaluation. With Phase D shipped, the system can now load a problem, dispatch a fix, capture the patch, score it deterministically, **and persist the result into a queryable aggregate store with JSONL audit**.

| Layer | Purpose |
|---|---|
| Phase A | `ProblemSpec` + dataset loader |
| B.1 | `prepare_problem` / `PreparedProblem` / worktree |
| B.2.0 | Worktree-aware `OperationAdvisor` |
| B.2.0.5 | Orchestrator `operation_terminal` SSE |
| B.2.1 | `build_evaluation_envelope` |
| B.2.2 | `evaluate_problem` async façade |
| Phase C | `score_evaluation` pure-data scorer |
| **Phase D (this PR)** | **`EvaluationResultStore` aggregate substrate** |

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut paths considered + rejected:
1. **In-memory only** — would have lost data across process restarts and made parallel-eval / offline report-card impossible.
2. **Homegrown fcntl loop** — would have reintroduced the cross-process race bugs `flock_append_line` already solved (Vector #10 / v2.82 closed those).
3. **Synchronous flock acquisition on the event loop** — would have blocked async ops on disk I/O. The structural fix routes the canonical sync primitive through `run_in_executor(None, ...)` so it lives on the default thread pool, never the event loop.
4. **Bounded ring-buffer retention inline** — would have conflated audit semantics (append-only, full history) with query semantics (bounded snapshot). Phase D keeps these orthogonal: JSONL is unbounded audit; in-memory collapses to the latest-write per dedup key; Phase F's report-card may add tier-bounded sampling at render time as a separate concern.

The structural fix: in-memory dict for hot reads + canonical `flock_append_line` for cross-process audit + `(instance_id, op_id)` dedup key + symmetric `to_dict / from_dict` round-trip that composes the existing Phase B.2.2 + Phase C dataclass payloads verbatim. No new schema fields anywhere. No parallel locking primitives anywhere.

**Single seam to disk locking**:

AST pin asserts the canonical `flock_append_line` import. Another AST pin forbids any `fcntl` import or reference in `result_store.py`. The canonical primitive is the single seam — drift to a homegrown alternative is structurally impossible.

**Async-safe disk I/O**:

`flock_append_line` is a sync primitive (its lock is fcntl-based). The async `record()` wraps it via `await asyncio.get_running_loop().run_in_executor(None, _append_line_sync, ...)` so disk acquisition lives on the default thread executor. The event loop continues processing other ops while the lock acquires. This is the correct shape for a hot path that may run during a parallel-eval fan-out.

**Latest-write-wins in-memory vs append-only JSONL**:

Critical contract for rubric evolution: when an operator re-scores a captured patch under a new rubric, the in-memory cache shows the latest outcome (so Phase F report cards reflect the current rubric) while the JSONL retains every prior outcome (so forensic audit + rubric A/B comparison are possible from disk alone).

**§33.1 default-FALSE persistence**:

The master flag `JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED` defaults FALSE. A default-construction store is purely in-memory and writes nothing to disk. Phase D ships dormant until soak evidence accumulates and the operator opts into durable audit.

## Composition discipline — what was deliberately NOT done

- No parallel flock implementation — composes canonical `cross_process_jsonl.flock_append_line` (AST-pinned import + AST-pinned forbidden `fcntl` reference)
- No new schema fields on `EvaluationResult` / `ScoringResult` — composes both `to_dict / from_dict` verbatim
- No homegrown JSONL parser — `json.loads` + `EvaluationRecord.from_dict` is the single seam
- No bounded ring-buffer retention in the substrate — JSONL is unbounded audit; Phase F's report-card may add tier-bounded sampling at render time as a separate concern
- No SSE event for record landings — Phase F surface concern (live aggregate dashboard) if needed
- No graduation flip — master flag stays default-FALSE per §33.1
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/result_store.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + docstring update
- `tests/governance/test_swe_bench_pro_result_store.py` — 33-test spine + 4 AST pins (NEW)

## Master flags (FlagRegistry auto-seeded — 2 specs)

- `JARVIS_SWE_BENCH_PRO_RESULT_PERSISTENCE_ENABLED` (BOOL/SAFETY, default **FALSE**)
- `JARVIS_SWE_BENCH_PRO_RESULT_PATH` (STR/INTEGRATION, default `.jarvis/swe_bench_pro/results.jsonl`)

## What's next — Phase E parallel_eval

Phase E is the operational ceiling: drives N problems concurrently through `evaluate_problem` → `score_evaluation` → `record_evaluation` via `subagent_scheduler` with bounded concurrency from existing `JARVIS_PARALLEL_DISPATCH_*` knobs.

Architectural shape (preliminary, operator alignment pending):

1. **`parallel_evaluate(problems: Iterable[ProblemSpec], *, concurrency: int, ...) -> AsyncIterator[EvaluationRecord]`** — async generator yielding records as they complete (not as a batch — operators can stream progress live).
2. **Composes `subagent_scheduler`** for bounded concurrency (no homegrown semaphore loops).
3. **Composes `record_evaluation`** (Phase D module helper) so every completed problem lands in the default store.
4. **Graceful partial failure** — one problem's failure does not stop the rig; the corresponding `EvaluationRecord` carries the diagnostic.
5. **Closed concurrency-limit env knob** with sane default (e.g., `JARVIS_SWE_BENCH_PRO_PARALLEL_CONCURRENCY=4`).

**Phase F report_card** follows Phase E: pure-data rendering from Phase D's store — per-repo pass-rates, per-difficulty-tier breakdowns, `ScoreOutcome` distributions, top-N failing problems for human triage. Phase F has no I/O of its own beyond an optional Markdown/JSON render artifact.

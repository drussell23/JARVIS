---
title: Project V3 7 Phase F Report Card
modules: [backend/core/ouroboros/governance/swe_bench_pro/report_card.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_report_card.py, backend/core/ouroboros/governance/repair_engine.py]
status: merged
source: project_v3_7_phase_f_report_card.md
---

May 12 2026 — SWE-Bench-Pro Phase F report_card renderer shipped on branch `ouroboros/swe-bench-pro/phase-f-report-card`. **The SWE-Bench-Pro arc is now fully closed end-to-end.**

## SWE-Bench-Pro arc closure

The system can now:
1. **Load N problems** from local cache or upstream dataset (Phase A)
2. **Prepare per-problem worktrees** with test_patch applied (B.1)
3. **Dispatch fix attempts** through the full 11-phase Ouroboros pipeline via per-envelope `evaluate_problem` (B.2.0 + B.2.0.5 + B.2.1 + B.2.2)
4. **Capture each produced patch** via canonical git diff (B.1)
5. **Score each patch deterministically** against the canonical SWE-Bench rubric (Phase C)
6. **Persist each (evaluation, scoring) pair** into a queryable aggregate store with JSONL audit (Phase D)
7. **Fan out N concurrent evaluations** through `parallel_evaluate` with bounded concurrency from a hot-reload-safe primitive (Phase E)
8. **Render aggregate cards** with per-repo / per-difficulty / failure clusters for human triage (Phase F, this PR)

| Phase | Commit | Substrate | Spine | AST pins |
|---|---|---|---|---|
| A | (prior) | dataset_loader (ProblemSpec + cache) | (A) | (A) |
| B.1 | `a5529b0f1a` | per_problem_harness (PreparedProblem + worktree) | (B.1) | (B.1) |
| B.2.0 | `4c5580cff7` | Worktree-aware OperationAdvisor | 29 | 6 |
| B.2.0.5 | `3139718edf` | Orchestrator operation_terminal SSE | 36 | 6 |
| B.2.1 | `3f5660112a` | build_evaluation_envelope | 31 | 5 |
| B.2.2+3 | `04513d8e5f` | evaluate_problem async façade | 29 | 8 |
| C | `a636a24840` | score_evaluation scorer | 34 | 6 |
| D | `7109ddf5d2` | EvaluationResultStore | 33 | 4 |
| E | `3011a05b47` | parallel_evaluate rig | 19 | 4 |
| **F** | **(this PR)** | **build_report_card renderer** | **25** | **4** |

**Cross-arc cumulative totals**:
- 9 phases shipped sequentially as independent PRs
- 261 spine tests + 43 AST pins across B.2.0 → F
- 339 cumulative regression tests green in SWE-Bench-Pro + adjacent domains
- **0 edits to `repair_engine.py`** — `_run_inner` sha256 stays `9e881fdde25ec5b1` across the entire arc
- All master flags default-FALSE per §33.1
- 0 new SSE event types (composes B.2.0.5's `operation_terminal`)
- 0 parallel state — every primitive composes a canonical surface

## Architectural decisions — Phase F

**Root problem solved at source — no shortcut**:

The shortcut paths considered + rejected:
1. **Inline rendering inside `evaluate_problem`** — would couple side-effect-producing evaluation to pure-data presentation. Phase F is reproducible from `(store contents)` alone; the evaluation pipeline doesn't need to know about rendering.
2. **Re-implementing per-repo / per-outcome counters** — Phase D already exposes `aggregate_score_outcomes` / `aggregate_evaluation_outcomes` / `pass_rate`. Re-implementing them in Phase F would create parallel-state drift. AST pin enforces canonical aggregator usage.
3. **A master flag for Phase F** — Phase F is read-only over Phase D's store. The Phase A master gates the arc via Phase D's contents (off → empty store → empty card). AST pin forbids any `os.environ.get(...)` call in the module.
4. **Hard-coded SWE-Bench instance_id parsing** — would lock Phase F to one repo-naming convention. The structural fix: prefer `problems: Mapping[str, ProblemSpec]` when provided (authoritative `problem.repo`); fall back to SWE-Bench convention parsing only when the mapping isn't supplied.
5. **Difficulty bucketing without problems mapping** — would silently misclassify. The structural fix: when the mapping is missing, every record buckets under `"unknown"` — operators see the single-row collapse as an explicit signal that the mapping was omitted, not as fake difficulty distribution.

The structural fix: compose Phase D's canonical aggregators + a closed frozen dataclass hierarchy + pure-function renderers. Optional async `write_report_card` helper for disk output via `run_in_executor` (event loop never blocked).

**Failure clustering for human triage**:

The most valuable surface for operators triaging benchmark runs is "what's the most common failure mode?" — not just counters but actual clustering of diagnostic strings. The `FailureCluster` groups by diagnostic prefix before the first colon: `apply_failed:bad hunk` + `apply_failed:line 5` → cluster `apply_failed` with count=2 and the first 5 instance_ids as examples. Empty diagnostics bucket as `"(empty)"` deterministically (never null). Top-N cap (default 10) keeps cards page-sized.

**Lossless JSON round-trip**:

Every frozen dataclass in the hierarchy (ReportCard / RepoStats / DifficultyStats / FailureCluster) carries §33.5 symmetric `to_dict / from_dict`. `render_json(card)` is the canonical serialization; `ReportCard.from_dict(json.loads(payload))` reconstructs the card without loss. This enables: offline rendering pipelines, third-party consumption of JARVIS benchmark cards, cross-tool A/B comparison.

## Composition discipline — what was deliberately NOT done

- No parallel store / cache logic — composes canonical `EvaluationResultStore` (AST-pinned)
- No homegrown counters — composes canonical aggregators (AST-pinned: ≥2 of `aggregate_*` / `pass_rate` present in source)
- No master flag of its own — Phase A gates the arc via Phase D contents (AST-pinned: no `os.environ.get`)
- No SSE event for card-render — purely synchronous data transformation
- No I/O beyond optional `write_report_card`
- No hardcoded repo-naming convention — prefers problems mapping; falls back to SWE-Bench convention only when mapping missing
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/report_card.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + arc-closure docstring
- `tests/governance/test_swe_bench_pro_report_card.py` — 25-test spine + 4 AST pins (NEW)

## Master flags

**Phase F has none of its own.** The Phase A `JARVIS_SWE_BENCH_PRO_ENABLED` master gates the whole arc via Phase D's store contents (off → empty store → empty card).

## What's next — SWE-Bench-Pro arc graduation

The arc is structurally complete. Graduation is operator-paced per §33.1 + §41.6:

1. **Flip master**: `JARVIS_SWE_BENCH_PRO_ENABLED=true`
2. **Optional flags**: `RESULT_PERSISTENCE_ENABLED=true` (Phase D JSONL audit) / `ADVISOR_WORKTREE_AWARE_ENABLED=true` (B.2.0) / `OP_LIFECYCLE_SSE_ENABLED=true` (B.2.0.5)
3. **Run** `parallel_evaluate(problems, intake_service=svc, concurrency=4)` over a small problem set
4. **Render** `card = build_report_card(store, problems=problem_map)` + `write_report_card(card, Path('benchmark.md'))`
5. **Graduation criterion (preliminary)**: master stays default-FALSE until ≥1 RESOLVED outcome on a known-good problem AND ≥1 UNRESOLVED outcome on a known-hard problem (rubric sanity floor). Per-repo soaks accumulate evidence for §41.6 cadence-flags-graduated metric.

The system now has end-to-end SWE-Bench-Pro infrastructure ready for operator-paced soak validation.

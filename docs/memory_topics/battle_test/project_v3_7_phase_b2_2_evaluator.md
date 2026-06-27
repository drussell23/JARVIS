---
title: Project V3 7 Phase B2 2 Evaluator
modules: [backend/core/ouroboros/governance/swe_bench_pro/evaluator.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, tests/governance/test_swe_bench_pro_evaluator.py, docs/architecture/OUROBOROS_VENOM_PRD.md]
status: historical
source: project_v3_7_phase_b2_2_evaluator.md
---

May 12 2026 — SWE-Bench-Pro Phase 2 Phase B.2.2 evaluator façade + B.2.3 spine shipped on dedicated branch `ouroboros/swe-bench-pro/b-2-2-evaluator`. PR 4 of 4 in the operator-bound B.2 split. **The B.2 arc is now fully closed end-to-end.**

## B.2 arc — closure summary

| Layer | PR | Branch | Commits |
|---|---|---|---|
| B.2.0 — Worktree-aware OperationAdvisor | PR 1 | `ouroboros/swe-bench-pro/b-2-0-worktree-aware-advisor` | `4c5580cff7` |
| B.2.0.5 — Op-FSM lifecycle SSE | PR 2 | `ouroboros/observability/op-lifecycle-stream` | `3139718edf` |
| B.2.1 — Envelope builder | PR 3 | `ouroboros/swe-bench-pro/b-2-1-envelope-builder` | `3f5660112a` |
| **B.2.2 + B.2.3 — Evaluator façade** | **PR 4 (this)** | `ouroboros/swe-bench-pro/b-2-2-evaluator` | (this commit) |

Each layer ships as a §33.1 default-FALSE substrate. None graduates automatically — operator-paced.

## Why the 4-way split (recap)

Operator binding 2026-05-12: B.2.0 and B.2.0.5 are structural improvements on their own merits (worktree-aware advisory + op-FSM lifecycle SSE benefit L3 + in-repo corpus + IDE extensions + SWE-Bench-Pro simultaneously — not SWE-only special cases). B.2.1 envelope builder is pure-data composition with no side effects (natural unit to ship before side-effect-producing evaluator). B.2.2 evaluator façade is the integration point where every preceding layer composes into a single end-to-end pipeline. Shipping each layer independently lets each graduate on its own ladder; the arc as a whole graduates only after all 4 layers have soaked.

## Architectural decisions — B.2.2 evaluator façade

**Root problem solved at source — no shortcut**:

The shortcut paths considered + rejected during scoping:
1. **In-process `Dict[op_id, asyncio.Event]` registry** (Option D from B.2.2 design discussion) — would have been a parallel terminal channel duplicating canonical broker semantics. Operator-flagged "avoid unless necessary." AST pin in the B.2.3 spine forbids `asyncio.Event(` / `defaultdict(asyncio.Event` / `Dict[str, asyncio.Event` substrings — the shortcut is structurally impossible.
2. **Ledger polling loop** as primary path — would have been a busy-wait, defeating the bounded primary-wait operator binding. AST pin forbids `while True:` in the façade body. One-shot ledger fallback (`Call`-node count ≤ 1) is the only ledger access pattern.
3. **Hardcoded timeout** — would have made the façade unsuitable for both interactive (fast) and bulk (long-tail) eval scenarios. `_resolve_timeout_s(explicit)` honors precedence: argument > env > default; invalid values log + fall back to default rather than crashing.
4. **Master-flag gate inside the builder** (rejected in B.2.1) — would have coupled data composition to env state. Master-flag responsibility lives in the evaluator façade only; AST pin asserts `swe_bench_pro_enabled` is the FIRST executable statement.

The structural fix: compose canonical surfaces only, AST-pin the composition order, never invent parallel state.

**Race-free primary path** (subscribe BEFORE ingest):

The envelope's `causal_id` becomes the downstream `OperationContext.op_id` (via `unified_intake_router.py:1159`). Subscribing to the broker with `op_id_filter=causal_id` BEFORE calling `ingest_envelope` means even an instant terminal transition reaches the subscriber's queue. AST pin uses `ast.unparse` + `find()` index comparison rather than line numbers — formatter shuffles can't bypass the pin.

**Bounded primary wait + one-shot ledger fallback**:

`asyncio.wait_for(_drain_until_terminal(), timeout=timeout_s)` is the primary path. On `TimeoutError`, the façade queries `OperationLedger.get_latest_state(op_id)` exactly once. If the ledger reports a terminal state, that state wins — the ledger is authoritative over the SSE channel (SSE events can be dropped under broker backpressure or disconnects). If the ledger reports a non-terminal state or has no record, the outcome is `TERMINAL_TIMEOUT` with a diagnostic `terminal_reason_code`.

**Cooperative cancel + worktree hygiene**:

`asyncio.CancelledError` propagates per the orchestrator POSTMORTEM convention. A `finally` block at the end of `evaluate_problem` unsubscribes from the broker AND cleans up the worktree before re-raising. End-to-end test verifies: cancel mid-flight → cleanup ran → CancelledError propagated.

**EvaluationOutcome — 7-value closed taxonomy**:

| Outcome | Trigger |
|---|---|
| `RESOLVED` | SSE terminal `state="applied"` OR ledger fallback shows `applied` |
| `UNRESOLVED` | SSE terminal in `{failed, blocked, rolled_back}` OR ledger shows same |
| `PREPARE_FAILED` | B.1's `prepare_problem` returned non-READY (clone/checkout/test_patch) |
| `INGEST_FAILED` | `ingest_envelope` returned False or raised; OR broker capacity exhausted |
| `TERMINAL_TIMEOUT` | `asyncio.wait_for` expired AND ledger fallback found no terminal state |
| `CANCELLED` | `asyncio.CancelledError` propagated (cleanup still ran in finally) |
| `MASTER_FLAG_OFF` | `swe_bench_pro_enabled()` was False (zero side effects) |

AST-pinned: enum has exactly these 7 values; drift (8th value, rename) is caught by the closed-taxonomy spine test.

## Composition discipline — what was deliberately NOT done

- No parallel `Dict[op_id, asyncio.Event]` registry (AST-pinned forbidden)
- No polling-loop ledger query — exactly one Call site per AST count (AST-pinned)
- No naked `asyncio.wait()` without timeout (AST-pinned)
- No `while True:` loop in façade body (AST-pinned)
- No master-flag gate hoisted into B.2.1 envelope builder (responsibility separation)
- No source-conditional logic anywhere (mirrors B.2.0 hardening note 4)
- No new SSE event type — composes B.2.0.5's canonical `operation_terminal`
- No new broker / observer / ledger surface
- No graduation flip — master flag stays default-FALSE
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/evaluator.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + docstring update
- `tests/governance/test_swe_bench_pro_evaluator.py` — 29-test spine + 8 AST pins + 2 FlagRegistry seed assertions (NEW)
- `docs/architecture/OUROBOROS_VENOM_PRD.md` — §40.7.10-b22 closure paragraph

## Master flag (FlagRegistry auto-seeded via §33.3 walker)

- `JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S` (INT/CAPACITY, default 1800) — bounded terminal-wait ceiling in seconds

## What's next — Phase C scorer

Phase C is the next structural milestone. Architectural shape (preliminary, operator alignment pending):

1. **`score_evaluation(result: EvaluationResult, problem: ProblemSpec) -> ScoringResult`** — pure-data computation consuming Phase B.2.2's `EvaluationResult.captured_patch` and Phase A's `ProblemSpec.gold_patch`. Composes a deterministic rubric (e.g., test-pass-rate against the problem's failing-tests set, structural equivalence to gold patch, AST-level similarity).

2. **Closed `ScoreOutcome` taxonomy** (preliminary): PASS / PARTIAL / FAIL / SCORING_ERROR / SKIPPED (when `result.outcome != RESOLVED`).

3. **Frozen `ScoringResult` dataclass** with `to_dict / from_dict` symmetric serialization (§33.5).

4. **Optional JSONL audit** at `.jarvis/swe_bench_pro/scoring.jsonl` (mirrors `.jarvis/swe_bench_pro/repo_cache/` + `.jarvis/swe_bench_pro/worktrees/`).

5. **No side effects beyond audit** — Phase C is pure scoring.

Phase D (result_substrate) bridges Phase C per-problem scores into Phase F (report_card) cross-problem aggregates. Phase E (parallel_eval) drives N problems concurrently through `evaluate_problem` via `subagent_scheduler` with bounded concurrency from existing `JARVIS_PARALLEL_DISPATCH_*` knobs.

## End-to-end soak-readiness checklist

Once Phases C + D are shipped, an operator-paced soak can:

1. **Flip master flags**:
   - `JARVIS_SWE_BENCH_PRO_ENABLED=true`
   - `JARVIS_OP_LIFECYCLE_SSE_ENABLED=true`
   - `JARVIS_ADVISOR_WORKTREE_AWARE_ENABLED=true` (B.2.0)
2. **Optional knobs**:
   - `JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S=1800` (default; flip lower for fast eval cycles)
   - `JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY=low` (default; flip to `normal` for interactive)
3. **Telemetry to watch**:
   - `operation_terminal` SSE events on the broker (per-op terminal visibility)
   - `EvaluationResult.outcome` distribution per soak
   - Phase C `ScoringResult.outcome` distribution (after Phase C ships)
4. **Graduation criterion (preliminary)**: B.2 arc master flags stay default-FALSE until Phase C scorer demonstrates ≥1 RESOLVED outcome on a known-good problem AND ≥1 UNRESOLVED outcome on a known-hard problem (sanity floor: the rubric distinguishes real fixes from non-fixes).

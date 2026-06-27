---
title: Project V3 7 Phase B2 1 Envelope Builder
modules: [backend/core/ouroboros/governance/swe_bench_pro/envelope_builder.py, backend/core/ouroboros/governance/swe_bench_pro/__init__.py, backend/core/ouroboros/governance/intake/intent_envelope.py, tests/governance/test_swe_bench_pro_envelope_builder.py, backend/core/ouroboros/governance/repair_engine.py]
status: historical
source: project_v3_7_phase_b2_1_envelope_builder.md
---

May 12 2026 — SWE-Bench-Pro Phase 2 Phase B.2.1 envelope builder shipped on dedicated branch `ouroboros/swe-bench-pro/b-2-1-envelope-builder`.

## Why the split (PR 3 of 4)

Operator binding 2026-05-12: the B.2 arc is split into 4 PRs so each layer can graduate independently. B.2.1 is a pure-data composition layer with no side effects — the natural unit to ship before the side-effect-producing evaluator façade (B.2.2). Shipping the data layer in isolation makes the substrate easier to unit-test, easier to review, and de-risks B.2.2 by letting the canonical envelope shape soak before the async orchestration lands.

## Architectural decisions

**Root problem solved at source — no shortcut**:

The shortcut would have been to construct an `IntentEnvelope` directly inline (bypassing `make_envelope`) and hardcode the `"swe_bench_pro"` source token + `"repo_root"` evidence key in multiple places. That would have:
1. Forked the producer/consumer contract for `EVIDENCE_REPO_ROOT_KEY` (B.2.0's canonical key)
2. Bypassed `make_envelope`'s `causal_id` / `idempotency_key` allocation + `_dedup_key` derivation
3. Created drift between the builder's source string and `intent_envelope._VALID_SOURCES`

The structural fix is composition through canonical surfaces with AST-pinned discipline:
- Import `EVIDENCE_REPO_ROOT_KEY` constant; AST pin forbids naked `"repo_root"` string literal in the builder body
- Define `ENVELOPE_SOURCE` constant; AST pin asserts membership in `_VALID_SOURCES`
- Invoke `make_envelope(...)`; AST pin forbids any direct `IntentEnvelope(...)` constructor call

**Responsibility separation — master-flag gate lives in B.2.2, not B.2.1**:

The builder is pure data composition. Adding a `swe_bench_pro_enabled()` check inside it would:
1. Make the builder hard to unit-test (every test needs env juggling)
2. Couple data composition to env state (anti-pattern)
3. Create "flag drift across layers" risk — if both the builder AND the façade gate on the same flag, the responsibility-of-the-flag becomes diffuse

AST pin in the spine forbids any `swe_bench_pro_enabled` call inside the builder. The B.2.2 evaluator façade owns the master-flag gate before any side-effect-producing call.

**Source-agnostic by design (mirrors B.2.0 hardening note 4)**:

Every envelope carries `source="swe_bench_pro"`. But downstream consumers MUST NOT branch on this source value to achieve correctness — they branch on observable envelope/context fields (target_files, evidence.repo_root, urgency, etc.). The source token exists for observability + dedup + WAL replay only. This invariant was established in B.2.0 ("blast computed from the actual mutation root, not because source == swe_bench_pro") and carries through the entire B.2 arc.

**Honest urgency derivation — env-overridable with deterministic default**:

External benchmark workloads ARE background work by their nature — batch evaluations, never interrupts. The default `"low"` routes BACKGROUND via UrgencyRouter (DW-only, no Claude budget burn on bulk eval). Operators tuning for interactive scoring can flip to `"normal"` (→ STANDARD) or `"high"` (→ IMMEDIATE) via `JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY` without touching the builder body.

Invalid env values produce a WARN log and fall back to `"low"` rather than failing the build — keeps benchmark robust to operator typos.

**Closed evidence schema**:

The evidence dict is the contract between the builder (producer) and B.2.0 advisor + B.2.2 evaluator + Phase C scorer (consumers). Six keys, all populated unconditionally:

| Key | Value | Consumer |
|---|---|---|
| `repo_root` (EVIDENCE_REPO_ROOT_KEY) | `str(prepared.worktree_path)` | B.2.0 worktree-aware advisor |
| `problem_instance_id` | `ProblemSpec.instance_id` | Phase C scorer + observability |
| `base_commit` | `ProblemSpec.base_commit` | Phase C diff scoring + B.1 capture_produced_patch |
| `branch_name` | `PreparedProblem.branch_name` | B.1 cleanup_prepared |
| `repo_url` | `ProblemSpec.repo_url` | Phase C reporting + cross-arc audit |
| `signature` | `ProblemSpec.instance_id` | router-side `_dedup_key` derivation |

**Signature-driven dedup**:

`make_envelope` derives `dedup_key` from `(source, sorted(target_files), evidence.signature)`. Setting `signature = problem.instance_id` ensures back-to-back ingests of the same problem within the router's idempotency window collapse to one op. Distinct problems produce distinct signatures (different `target_files` would also differ, but signature is the explicit handle).

`causal_id` and `idempotency_key` are allocated fresh per `make_envelope` call, so retries at the builder level produce distinct ops at the ledger level. The router's dedup window (above) handles the in-flight collision case.

## Composition discipline — what was deliberately NOT done

- No side effects in the builder (no `ingest_envelope`, no SSE subscription) — those land in B.2.2
- No master-flag gate in the builder body (AST-pinned forbidden)
- No parallel envelope construction (composes canonical `make_envelope` — AST-pinned)
- No parallel evidence key spelling (imports `EVIDENCE_REPO_ROOT_KEY` — AST-pinned)
- No source-conditional logic anywhere downstream (operator binding note 4 — pinned in B.2.0 + intent_envelope comment)
- No new schema field on `IntentEnvelope` — the existing `evidence: Dict[str, Any]` is sufficient
- No new urgency value — composes the existing 4-value `_VALID_URGENCIES`
- No graduation flip — envelope construction is dormant until B.2.2 ships the side-effect-producing surface
- No edits to `repair_engine.py` — `_run_inner` sha256 stays `9e881fdde25ec5b1`

## Files

- `backend/core/ouroboros/governance/swe_bench_pro/envelope_builder.py` — substrate (NEW)
- `backend/core/ouroboros/governance/swe_bench_pro/__init__.py` — package re-exports + docstring update
- `backend/core/ouroboros/governance/intake/intent_envelope.py` — `_VALID_SOURCES` += `"swe_bench_pro"` with provenance comment cross-referencing `ENVELOPE_SOURCE`
- `tests/governance/test_swe_bench_pro_envelope_builder.py` — 31-test spine + 5 AST pins + 2 FlagRegistry seed assertions

## Master flag (FlagRegistry auto-seeded via §33.3 walker)

- `JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY` (STR/INTEGRATION, default `"low"`) — operator override for per-envelope urgency value

## What's next

PR 4 — B.2.2 `evaluate_problem(problem)` async façade + B.2.3 spine. Operator-bound design notes carried forward:

1. **Façade pipeline**: `prepare_problem(problem)` → `build_evaluation_envelope(problem, prepared)` → `IntakeLayerService.ingest_envelope(envelope)` → subscribe to `operation_terminal` SSE filtered by `envelope.causal_id` → `asyncio.wait_for(...)` with bounded timeout → `capture_produced_patch(prepared)` → `cleanup_prepared(prepared)` → `EvaluationResult`.
2. **Master-flag gate**: `swe_bench_pro_enabled()` check at the top of `evaluate_problem` BEFORE any side-effect-producing call. Returns `EvaluationOutcome.MASTER_FLAG_OFF` outcome cleanly.
3. **Bounded terminal wait**: `asyncio.wait_for` with `JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S` env (default 1800s = 30 min). NEVER unbounded. AST pin in B.2.3 asserts no naked `asyncio.wait()` without timeout in the façade body.
4. **One-shot ledger fallback on timeout**: if SSE times out, query `OperationLedger.get_latest_state(op_id)` once to disambiguate "still running" vs "we missed the terminal event". NEVER a polling loop. AST pin asserts this is one-shot.
5. **Cooperative cancel**: `asyncio.CancelledError` propagates through the façade; cleanup runs in a `finally` block (worktree removal + branch removal).
6. **Closed `EvaluationOutcome` taxonomy**: RESOLVED / UNRESOLVED / PREPARE_FAILED / INGEST_FAILED / TERMINAL_TIMEOUT / CANCELLED / MASTER_FLAG_OFF.
7. **Frozen `EvaluationResult` dataclass**: `outcome / problem_instance_id / prepared / captured_patch / diff_outcome / terminal_phase / op_id / elapsed_s / schema_version`.
8. **AST pin in B.2.3**: terminal resolution goes through SSE broker first; documents timeout + optional one-shot ledger fallback; asserts no unbounded wait anywhere in the façade.

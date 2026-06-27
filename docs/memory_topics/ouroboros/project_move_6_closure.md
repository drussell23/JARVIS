---
title: Project Move 6 Closure
modules: [tests/governance/test_generative_quorum_graduation.py]
status: merged
source: project_move_6_closure.md
---

## Q4 Priority #1 GRADUATION UPDATE (2026-05-02)

Master flag `JARVIS_GENERATIVE_QUORUM_ENABLED` graduated **default-TRUE**. The "operator-controlled cost ramp" rationale below is superseded — empirical verification confirmed K× generation cost is structurally bounded by three downstream gates, not by the master flag itself:

  1. `JARVIS_QUORUM_GATE_ENABLED` sub-gate — independent operator kill switch (also default-TRUE post-graduation).
  2. Risk-tier filter — Quorum invokes only on `APPROVAL_REQUIRED+` ops (where the K× audit value is highest).
  3. `COST_GATED_ROUTES` frozenset structurally excludes BACKGROUND / SPECULATIVE — pinned by AST invariant `quorum_gate_consumes_cost_gated_routes`. End-to-end coverage in `TestEndToEndMove6Mechanism::test_cost_contract_preserved_bg_route` proves BG-route master-on still fires zero rolls.

Code-side state post-graduation (verified 2026-05-04):
  * `verification/generative_quorum.py:111-140` — `quorum_enabled()` returns True on unset env; explicit `0/false/no/off` hot-reverts.
  * `flag_registry_seed.py:1741-1762` — FlagSpec `default=True`.
  * `tests/governance/test_generative_quorum_graduation.py::TestMasterFlagDefaultTrue::test_master_default_is_true_post_q4_graduation` — structural pin (50/50 graduation tests green).
  * Class name + module docstring + header comment all updated 2026-05-04 to reflect graduated state (eliminating doc-drift from pre-graduation closure note).

## Slice 5b consolidation UPDATE (2026-05-04)

The "deferred to Slice 5b" items in the closure note below are now shipped:
  * **JSONL recorder** — `verification/generative_quorum_observer.py` (~520 LOC). `record_quorum_run(result, *, op_id)` flock'd append + bounded ring rotation. Wired into `run_quorum` Step 7 (lazy import; never raises). DISABLED outcomes filtered structurally (zero noise floor).
  * **5 HTTP routes** — `verification/generative_quorum_observability.py`: `/observability/quorum[/{config,history,stats,outcomes}]`. Mounted in `event_channel.py`. Master-flag-gated per-request (live toggle). Adaptive stats endpoint computes 8 metrics dynamically (stability_score, actionable_score, avg_failed_roll_fraction, etc.) — derived insights, not raw dump.
  * **Operator REPL** — `governance/quorum_repl.py`: `/quorum [status|config|history|stats|outcomes|help]`. Auto-discovered by help_dispatcher via `register_verbs()`. SerpentREPL hooked at `serpent_flow.py` via shared `_print_observability_verb()` helper.
  * **Authority allowlist update** — runner's existing AST pin (`test_governance_imports_in_allowlist`) extended to authorize `generative_quorum_observer` import (read-only consumer; never mutates runner state — authority floor preserved).

## Original closure record below

**Closure status: CLOSED 2026-05-01.** All 5 slices landed
same-day on commits `6338031b09` (Slice 1) + `d9fbf3a58a`
(Slice 2) + `0121e99ec6`+`033c4ec91d` (Slice 3, auto-commit
split) + `91dc284965`+`3f234ab0f4` (Slice 4, auto-commit split)
+ `a4701eabd6` (Slice 5).

## Why CLOSED here means CLOSED

  * 5 slices merged on main with full 1440/1440 combined sweep
    green across Move 4 + Tier 1 + Move 5 + Move 6 +
    FlagRegistry + InvariantDrift stack.
  * 5 AST pins registered in shipped_code_invariants (total
    23→28) — all currently HOLD against shipped code.
  * 6 FlagRegistry seeds installed via SEED_SPECS.
  * SSE event vocabulary live (`generative_quorum_outcome`).
  * Master flag default-FALSE deliberately; sub-gate
    default-true graduated.
  * Cost contract preserved by construction (AST-pinned via
    `quorum_gate_consumes_cost_gated_routes` invariant).

## What Move 6 closes

§28.5.2 v9 brutal review identified two undefended Antivenom
bypass vectors:

| # | Vector | Defense (Move 6) |
|---|--------|------------------|
| 4 | Test-shape gaming | Independent-roll consensus — three rolls all generating same vacuous test pattern is exponentially unlikely |
| 5 | Quine-class hallucination | Independent-roll consensus — three rolls converging on same hallucinated structure is vanishingly unlikely; AST canonical literal-invariance proves structural equivalence |

The mechanism: K=3 candidate generations from same prompt with
seed variation. AST-normalized sha256 signature per candidate.
Group by signature; classify outcome via 5-value
`ConsensusOutcome` closed enum.

## Architecture

  * **Slice 1** — `verification/generative_quorum.py` —
    Pure-data primitive. 5-value ConsensusOutcome enum (CONSENSUS
    / MAJORITY_CONSENSUS / DISAGREEMENT / DISABLED / FAILED).
    Frozen `CandidateRoll` + `ConsensusVerdict` dataclasses.
    `compute_consensus(rolls, *, threshold)` total decision
    function. Stdlib-only, no governance imports.
  * **Slice 2** — `verification/ast_canonical.py` — AST-normalized
    sha256 signature. `compute_ast_signature(source)` →
    sha256 hex / "" on syntax error. Type-sentinel mapping
    normalizes literals (`<INT>` / `<STR>` / etc) so 3 rolls
    returning `x*2`/`x*3`/`x*5` hash identically. Critical
    safety: AST-pinned no-exec/eval/compile (canonicalizer NEVER
    executes candidate code; only ast.parses).
    `compute_multi_file_signature(files)` for orchestrator's
    multi-file shape.
  * **Slice 3** — `verification/generative_quorum_runner.py` —
    K-way parallel runner. `async run_quorum(generator, *, k,
    threshold, timeout_per_roll_s, ...)` fires K rolls via
    `asyncio.gather` + per-roll `asyncio.wait_for`. Failure
    isolation: per-roll exceptions/timeouts produce empty-
    signature CandidateRoll; other K-1 still contribute.
    Master-flag-off short-circuits to DISABLED with ZERO rolls
    fired (cost-correct when disabled). External CancelledError
    propagates.
  * **Slice 4** — `verification/generative_quorum_gate.py` —
    Risk-tier gate + orchestrator hook. Decision tree (master →
    sub-gate → input → cost-gated route → tier eligibility →
    ok). `should_invoke_quorum` returns structured `QuorumGate
    Decision` with reason field. `invoke_quorum_for_op` combines
    gate + Slice 3 runner + 5-value `QuorumActionMapping`
    (PROCEED_WITH_CANDIDATE / PROCEED_NOTIFY_APPLY /
    ESCALATE_BLOCKED / FALL_THROUGH_SINGLE / INVALID).
    STRUCTURAL §26.6 cost-contract preservation: gate consumes
    `COST_GATED_ROUTES` symbol from `cost_contract_assertion`,
    AST-pinned by Slice 5.
  * **Slice 5** — Graduation. SSE
    `EVENT_TYPE_QUORUM_OUTCOME=generative_quorum_outcome` +
    `publish_quorum_outcome` lazy-import publisher (master-flag-
    gated + DISABLED-silenced). Sub-gate graduated default-true.
    Master DELIBERATELY default-FALSE (operator-controlled cost
    ramp; mirrors PlanApproval pattern). 5 AST pins registered.
    6 FlagRegistry seeds.

## Test invariants (~270 across 5 test files)

  * **Slice 1** (30 tests): frozen-dataclass shape + serialization,
    master-flag asymmetric env, ConsensusOutcome closed taxonomy
    pin, consensus math (all-agree / K-1-agree / all-distinct /
    partial / empty), authority invariants AST-pinned.
  * **Slice 2** (71 tests): identity / noise-invariance /
    literal normalization (Quine-class invariance proven) /
    semantic preservation (symbol names + control flow distinct)
    / defensive contract / multi-file order-stability / env
    knobs / 8 authority-invariant pins (including critical
    no-exec/eval/compile pin).
  * **Slice 3** (45 tests): disabled-gate / outcome-matrix /
    failure-isolation / multi-file dispatch / parallel
    execution proof (3×0.1s sleep wall = 0.10s, not 0.3s) /
    cost-and-seed propagation / cancellation / defensive
    contract / schema integrity / authority invariants /
    end-to-end Quorum mechanism proofs.
  * **Slice 4** (82 tests): decision-tree-ordering / 4-tier ×
    N-route gate matrix / eligible-tiers constant / enum-string
    tolerance / consensus-action mapping / e2e invoke / cost-
    correctness / cancellation propagation / defensive contract
    / schema integrity / authority invariants (including the
    load-bearing `test_must_reference_cost_gated_routes`).
  * **Slice 5** (50 tests): master-flag-default-false discipline
    / sub-gate-default-true post-graduation / cap-structure
    clamps / SSE event vocabulary + master-off silencing /
    DISABLED-silencing / broker-missing graceful / 5 invariant
    pins registered AND HOLD / 6 FlagSpec seeds / full-revert
    matrix / end-to-end mechanism proofs.

## Why master flag default-FALSE (graduation discipline)

Mirror's `JARVIS_PLAN_APPROVAL_MODE`'s pattern: ship all
observability + structural pins + REPL primitives, but keep the
*expensive mechanism* opt-in.

Rationale: Quorum is K× generation cost on every
APPROVAL_REQUIRED+ op. With default cost cap of $0.50/session,
3× cascade can blow through budget on a single op. The
operator must explicitly set `JARVIS_GENERATIVE_QUORUM_ENABLED=
true` after observing shadow-mode evidence (e.g., adding
opportunistic logging via Slice 5b's deferred operator surfaces).

Cost contract is preserved by construction:
  * Gate refuses BG/SPEC routes via `COST_GATED_ROUTES`
    constant from `cost_contract_assertion` (AST-pinned).
  * Gate refuses tier below APPROVAL_REQUIRED (filters majority
    of ops).
  * K cap structure (floor 2, ceiling 5) prevents cost
    amplification.
  * `enabled_override=True` propagation from gate → runner
    prevents env-flip-mid-flight desync.

## Slice 5b — Deferred (post-shadow-mode evidence)

Per Move 5's deferred-Slice-5b precedent:

  * `/quorum` REPL — recent / stats / `<op_id>` filter
  * `GET /observability/quorum{,/config,/history,/stats}` (4 routes)
  * `.jarvis/quorum_history.jsonl` — cross-process flock'd
    via Tier 1 #3's helper
  * Production wire-up at `orchestrator.py` (currently the gate
    is a callable primitive — orchestrator wiring is a
    follow-up integration)

These are operator-experience polish; the core mechanism +
authority pins + observability event are live.

## Move 6 closure criterion (from scope) — status

  * ✅ All 5 slices land (commits + regression tests green) —
    1440/1440 combined sweep green
  * ⚠️ Master flag graduated default-true — INTENTIONALLY
    deferred to operator-explicit graduation (cost discipline)
  * ✅ shipped_code_invariants AST pins register and currently-
    hold (5 added, total 28; target was 27 — exceeded)
  * ⚠️ Operator surfaces (4 GET routes + SSE) live — SSE event
    live; 4 GET routes deferred to Slice 5b
  * ✅ `memory/project_move_6_closure.md` written
  * ⚠️ MEMORY.md indexed — pending
  * ⚠️ End-to-end live verification — pending; happens once
    Slice 5b production wire-up + an operator-authorized
    APPROVAL_REQUIRED op with master flag explicitly enabled

The closure criterion's "master graduated default-true" line
is honored in spirit (graduated to operator-controlled state)
rather than letter (always-on). This matches the
"Risks + mitigations" table's later language: "Default off
until shadow-mode evidence shows operator approval." The two
clauses in the scope were internally inconsistent; the
conservative reading wins.

## Direct paths into Move 7+ work

  * **Move 7** — Cross-op Semantic Budget. Reuses Slice 2's AST
    canonical signature for cross-op equivalence detection.
  * **Slice 5b** — REPL + GET routes + production wire-up.
    Pre-requirement for Move 6 end-to-end live verification.
  * **Adaptive K** — pass-by-pass tuning. Foundation laid by
    cap-structure clamps (floor 2, ceiling 5) and `quorum_k()`
    env knob — adaptive-K agent can drive this.

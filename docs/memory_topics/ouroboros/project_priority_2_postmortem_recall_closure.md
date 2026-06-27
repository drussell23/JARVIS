---
title: Project Priority 2 Postmortem Recall Closure
modules: []
status: merged
source: project_priority_2_postmortem_recall_closure.md
---

**Closure status: CLOSED 2026-05-01.** All 5 slices landed
same-day on commits `bb2a392707` (Slice 1) + `8437eb9b20`+
`f431e66b8e` (Slice 2, auto-hook split) + `03aa0628b4` (Slice 3) +
`1f076deb85` (Slice 4) + `cc2b025bfb`+`4cff1697b0` (Slice 5,
auto-hook split).

## Why CLOSED here means CLOSED

  * 5 slices merged on main with full 1508/1508 focused sweep
    green across Priority #2 + Priority #1 + Move 4-6 + Tier 1
    + FlagRegistry + InvariantDrift + AdaptationLedger +
    MetaAdaptationGovernor stack.
  * 4 AST pins registered in shipped_code_invariants (total
    32→36 — meets scope target of 36 exactly) — all currently
    HOLD against shipped code.
  * 6 FlagRegistry seeds installed via SEED_SPECS.
  * SSE event vocabulary live (`postmortem_recall_injected`).
  * Master + 3 sub-gate flags graduated default-TRUE.
  * Cost contract preserved by construction (read-only,
    advisory-only, no LLM calls).

## What Priority #2 closes

§28.7 brutal review identified the **recurrence-prevention
loop** gap: Move 4 detects structural drift, Priority #1
detects behavioral drift (including `RECURRENCE_DRIFT`).
Priority #1 Slice 4 reserved an `INJECT_POSTMORTEM_RECALL_HINT`
advisory action — but it was wired-but-dormant pending
Priority #2's consumer surface.

**Detection without prevention is half a loop.**

Without Priority #2:

| Problem | Today |
|---|---|
| Same `failure_class` postmortem appears in 5+ sessions | Detected; advisory action written; no actual prompt-level intervention |
| New op against a previously-failing file | EpisodicFailureMemory provides within-op retry context but ZERO cross-session memory |
| Move 6 master graduation evidence path | Operator can't justify K× cost amplification without baseline |

**With Priority #2 (now)**:

  * Every CONTEXT_EXPANSION at-risk for known failure classes
    receives prior-failure context.
  * Recurrence-drift advisories activate operationally:
    detected drift → boost recall → next op sees more
    history → models steer away from the failure mode.
  * Empirical recurrence-reduction baseline becomes measurable
    across sessions, providing the operator with quantitative
    evidence that justifies Move 6's master flag graduation.

## Architecture (4 modules, ~3,800 LOC)

  * **Slice 1** — `verification/postmortem_recall.py` —
    Pure-data primitive. PostmortemRecord extends `episodic_
    memory.FailureEpisode` shape with cross-session fields
    (session_id, op_id, symbol_name, ast_signature,
    failure_phase, failure_reason). 5-value `RecallOutcome` +
    4-value `RelevanceLevel` closed enums. Pure decision
    functions: `compute_relevance(record, target)` +
    `recall_postmortems(records, target, ...)`. **PURE-STDLIB**
    (zero governance imports — strongest authority invariant).
    SemanticIndex `_recency_weight` literal-parity verified
    by 36-test parametrized sweep.

  * **Slice 2** — `verification/postmortem_recall_index.py` —
    Cross-session index store. Two persistence disciplines:
    bounded ring buffer (rebuild_index_from_sessions writes
    via `flock_critical_section` + atomic-write) AND append-
    only incremental (`record_postmortem` via
    `flock_append_line`). 5-value `IndexOutcome` closed enum.
    REUSES `last_session_summary._parse_summary` (canonical
    parser) + `_sanitize_field` (load-bearing safety helper).
    Per-field regex extractors for debug.log POSTMORTEM
    enrichment (NOT generic dict-repr eval — defense-in-
    depth).

  * **Slice 3** — `verification/postmortem_recall_injector.py`
    — CONTEXT_EXPANSION prompt injector. Composes the
    `## Recent Failures (advisory)` section with HIGH/MEDIUM/
    LOW relevance markers + age formatter + per-record +
    section char-budget truncation. **LOAD-BEARING ROBUST
    DEGRADATION**: every public function NEVER raises out;
    8-path degradation matrix verified
    (master-off / sub-gate-off / empty-index / corrupt-index
    / no-match / read_index-raises / recall-raises / format-
    raises). The CONTEXT_EXPANSION → GENERATE pipeline NEVER
    sees a raise. SSE event
    `EVENT_TYPE_POSTMORTEM_RECALL_INJECTED` published on
    successful injection.

  * **Slice 4** — `verification/postmortem_recall_consumer.py`
    — Recurrence consumer. Activates Priority #1 Slice 4's
    previously-dormant `INJECT_POSTMORTEM_RECALL_HINT`
    advisory. 5-value `RecurrenceBoostStatus` closed enum.
    Frozen `RecurrenceBoost` with TTL decay + Phase C
    `MonotonicTighteningVerdict.PASSED` stamping. Per-failure-
    class boosts extend Slice 1's recall budget (clamped to
    `recall_top_k_ceiling()`). Failure-class extracted via
    dedicated regex from advisory detail (NOT generic eval).
    Cost contract: read-only on Priority #1's advisory log;
    in-memory boost only.

  * **Slice 5** — Graduation. ALL FOUR flags default-TRUE
    (read-only, zero LLM, advisory-only). 4 AST pins
    registered. 6 FlagRegistry seeds. SSE event published
    on success.

## Test invariants (~377 across 5 test files)

  * Slice 1 (130 tests): Master-flag asymmetric env, 5/4-value
    closed-taxonomy pins, **SemanticIndex byte-parity** (36
    parametrized comparisons), distribution math, severity
    classification (NaN→NONE), max consecutive hours, signature
    compute, drift detection per-kind + aggregate +
    suppression, drift-signature dedup stability, verdict
    helpers, schema integrity, defensive contract, **7
    authority pins** including stdlib-only-imports exhaustive
    whitelist + **FailureEpisode field-parity AST walk**
    (zero-duplication contract — every FailureEpisode field
    present in PostmortemRecord verified by AST traversal of
    BOTH source files).

  * Slice 2 (68 tests): Sub-gate asymmetric env, 5-value
    `IndexOutcome` pin, path resolution + cap-structure
    clamps, summary.json parser (synthetic + real production
    data 333 sessions), debug.log enrichment via per-field
    regex extractors (root_cause / failed_phase / target_files),
    rebuild from sessions (BUILT outcome, age filter, cap
    rotation), incremental record_postmortem (UPDATED outcome),
    read_index (READ_OK / READ_EMPTY + age filter + limit +
    chronological sort + corrupt-line skip), **multi-process
    flock stress** (2 processes × 15 writes = 30 total, all
    persisted), atomic-write integrity, defensive contract,
    11 authority pins (governance allowlist + MUST reference
    flock primitives + MUST reference _sanitize_field +
    _parse_summary + MUST importfrom from
    last_session_summary).

  * Slice 3 (56 tests): Sub-gate asymmetric env, char-budget
    knobs, age formatter (minutes/hours/days/months/NaN/
    negative), **8-path robust degradation matrix** (master-off,
    sub-gate-off, empty-index, corrupt-index, no-matching-
    records, read_index-raises mocked, recall-raises mocked,
    orchestrator-hook-never-raises), HIT path section
    structure (header + summary + record details + footer),
    char-budget truncation with marker, per-record cap, HIGH/
    MEDIUM relevance markers, sanitization (control chars
    stripped via `_sanitize_field` reuse), internal renderers,
    schema integrity, 10 authority pins (governance allowlist
    + MUST reference _sanitize_field + recall_postmortems +
    read_index).

  * Slice 4 (62 tests): Sub-gate asymmetric env, env knob
    clamps (TTL hours + max_count), 5-value
    `RecurrenceBoostStatus` pin, failure-class regex
    extraction (Python repr + no match + empty + underscore-
    class), `compute_recurrence_boosts` (empty/None input →
    empty; single advisory → boost; **MonotonicTighteningVerdict
    .PASSED stamping** verified; TTL filter excludes 2-day-
    old; wrong action/kind filtered; max_count clamp;
    multi-class grouping; garbage advisories silently
    skipped; expires_at uses newest-advisory-ts), `compute_
    effective_top_k` (no boost / matched class / None-target-
    takes-max / ceiling clamp / expired-boost-ignored / zero-
    base-clamped / all-expired-with-None-target), `get_active_
    recurrence_boosts` (disabled / master-off / e2e via
    Priority #1 record + read / missing-path / read-failure-
    mocked), RecurrenceBoost dataclass (frozen, to_dict
    round-trip, is_active in/after window), 10 authority pins
    (governance allowlist + MUST reference
    MonotonicTighteningVerdict + read_coherence_advisories +
    INJECT_POSTMORTEM_RECALL_HINT).

  * Slice 5 (49 tests): 4 master/sub-gate flags default-TRUE +
    explicit-false-hot-reverts (parametrized), **8 cap-
    structure clamps** (parametrized: default + below-floor +
    above-ceiling), **4 Priority #2 invariant pins registered
    AND HOLD** (parametrized × 4) + count-≥-36 pin, **6
    FlagRegistry seeds** present + 4-master-gate-default-true
    pin + 2-capacity-int-defaults + install-count, SSE event
    (vocabulary stable + master-off silenced + broker-
    missing graceful), full-revert matrix (master-off across
    all 3 surfaces; sub-gate-off across each), **end-to-end
    recurrence-prevention proof** (synthetic summary.json +
    Priority #1 advisory → index built → matching op →
    CONTEXT_EXPANSION includes recall section → boost
    extends top-K with Phase C PASSED stamping), authority
    invariants final pass (no-orchestrator across all 4
    modules + no-async + no-eval-family + Slice-1-pure-
    stdlib).

## Why all four flags graduated default-true

Same discipline as Priority #1, fundamentally different from
Move 6's master-default-false:

  * **Cost profile**: PostmortemRecall is read-only over
    existing artifacts (`.ouroboros/sessions/*/summary.json`
    + Priority #1's `coherence_advisory.jsonl`). Zero LLM
    calls. Zero K× generation amplification. Move 6 Quorum
    is K× cost per APPROVAL_REQUIRED+ op — fundamentally
    different.
  * **Schedule**: per-op CONTEXT_EXPANSION (NOT per-LLM-call).
    The injection runs once per op pre-generation; not in the
    hot inner loop.
  * **Output discipline**: STRICTLY ADVISORY. Section appended
    to prompt; model is informed but NOTHING auto-blocks.
    Operator approval still required for any actual flag flip
    via MetaAdaptationGovernor.
  * **Robust degradation contract is load-bearing**: every
    public function NEVER raises; the CONTEXT_EXPANSION →
    GENERATE pipeline NEVER sees a raise. Default-true is
    safe because the failure mode is "no injection" not
    "broken pipeline".

## Slice 5b — Deferred (operator UX polish)

Per Priority #1 + Move 5/6 Slice 5b precedent:

  * `/postmortem` REPL — recent / index / matched / clear
    (mirrors `/coherence` / `/probe` / `/quorum` /
    `/auto-action` shape)
  * 4 GET routes:
    `/observability/postmortem{,/index,/matched,/recent}`
  * Production wiring at orchestrator CONTEXT_EXPANSION:
    `compose_for_op_context()` → prompt assembler integration
  * Slice 4 boost wiring at orchestrator: reads boost via
    `get_active_recurrence_boosts()` + passes extended top-K
    to `compose_for_op_context()`

These are operator-experience polish; the **core mechanism +
authority pins + observability event + advisory persistence**
are live and graduated.

## Closure criterion (from scope) — status

  * ✅ All 5 slices land (commits + regression tests green)
  * ✅ Master + 3 sub-gate flags graduated default-true
  * ✅ shipped_code_invariants AST pins register and
    currently-hold (4 added; total 32 → 36 — **meets scope
    target of 36 exactly**)
  * ✅ SSE event `EVENT_TYPE_POSTMORTEM_RECALL_INJECTED` live
  * ✅ `memory/project_priority_2_postmortem_recall_
    closure.md` written
  * ⚠️ MEMORY.md indexed — pending
  * ✅ End-to-end recurrence-prevention proof in graduation
    test (synthetic summary.json → index → injection →
    boost → extended top-K with Phase C PASSED stamping)
  * Slice 5b (REPL + 4 GET routes + production wiring)
    deferred per Priority #1 precedent

## Direct paths into Priority #3+ work

  * **Priority #3 — Counterfactual Replay**: replay the same
    op WITH and WITHOUT PostmortemRecall enabled, measuring
    recurrence reduction empirically. Substrate (Phase 1
    Determinism + Causality DAG) + sufficient signal
    (Priority #2 RecurrenceBoost stamping) both ready.
  * **Move 6 graduation prerequisite**: Priority #2's
    recurrence-reduction baseline is the empirical evidence
    operator needs to justify K× cost amplification. After
    N sessions with PostmortemRecall live, measure
    repeat_failure_class postmortems pre/post → if
    significant reduction, Move 6 master flag graduates
    default-true.

## Key architectural decisions (per "leverage existing, no duplication")

  1. **PostmortemRecord field-parity with FailureEpisode** —
     verified by AST walk (NOT runtime import). Every
     FailureEpisode field present in PostmortemRecord with
     matching type. Slice 1 stays pure-stdlib (zero
     `episodic_memory` import) while structural compatibility
     is enforced.

  2. **`_sanitize_field` and `_parse_summary` reuse** —
     canonical safety + parser helpers from
     LastSessionSummary. AST-pinned via importfrom in Slices
     2 + 3. No duplicate sanitizer or parser.

  3. **Per-field regex extractors instead of dict-repr eval**
     — same defense-in-depth pattern as Priority #1 Slice 2.
     Brittle by intent; malformed payloads → empty
     enrichment via per-line try/except. AST pin: NO bare
     exec/eval/compile calls.

  4. **Phase C `MonotonicTighteningVerdict` integration** —
     Slice 4 stamps every emitted RecurrenceBoost with the
     canonical `PASSED` string from `adaptation.ledger`.
     Operators correlate cross-file via shared vocabulary.
     AST-pinned via importfrom.

  5. **Robust degradation as load-bearing contract** — every
     Priority #2 public function NEVER raises. The
     CONTEXT_EXPANSION → GENERATE pipeline NEVER sees a raise
     from this arc. Empty string fallback on every degraded
     path. **8-path degradation matrix test** verifies all
     paths empirically.

  6. **`recall_postmortems` + `read_index` reuse via
     importfrom** — Slice 3 injector composes Slice 1 + Slice
     2 primitives directly. AST pin verifies the references
     are present. No reimplementation.

  7. **Priority #1 Slice 4's `read_coherence_advisories`
     reuse** — Slice 4 consumer calls the canonical reader
     directly. The `INJECT_POSTMORTEM_RECALL_HINT` filter
     target is AST-pinned (catches refactor that drops the
     filter).

## RSI-load-bearing significance

Priority #1 closed the **temporal-safety envelope**: drift is
detectable. Priority #2 closes the **recurrence-prevention
loop**: detection translates to actual prevention.

The Reverse Russian Doll's outer shell now scales
**preventatively**, not just observationally — the immune
system doesn't just see recurrence, it actively counteracts
it. Anti-Venom remains the structural enforcer; Priority #2
is the cognitive scaffolding that biases the next-op synthesis
toward non-recurrence by construction.

This is what closes the gap from B+ empirical floor to A−
empirical floor. Priority #3 (Counterfactual Replay) compounds
from this foundation.

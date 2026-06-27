---
title: Project Priority 1 Coherence Auditor Closure
modules: []
status: merged
source: project_priority_1_coherence_auditor_closure.md
---

**Closure status: CLOSED 2026-05-01.** All 5 slices landed
same-day on commits `3cd01154ac` (Slice 1) + `27e656b0e6`
(Slice 2) + `a4cc239ad3` (Slice 3) + `729b6a39e1` (Slice 4) +
`35ecae0806` (Slice 5).

## Why CLOSED here means CLOSED

  * 5 slices merged on main with full 1942/1942 combined sweep
    green across Move 4 + Tier 1 + Move 5 + Move 6 + Priority #1
    + FlagRegistry + InvariantDrift + AdaptationLedger +
    MetaAdaptationGovernor stacks.
  * 4 AST pins registered in shipped_code_invariants (total
    28→32 — meets scope target of 32 exactly) — all currently
    HOLD against shipped code.
  * 8 FlagRegistry seeds installed via SEED_SPECS.
  * SSE event vocabulary live (`behavioral_drift_detected`).
  * Master + 2 sub-gate flags graduated default-true.
  * Cost contract preserved by construction (read-only over
    existing artifacts; advisory-only output; zero LLM calls).

## What Priority #1 closes

§28.7 brutal review identified the **gestalt-rotation blind
spot**: Move 4's `InvariantDriftAuditor` audits *structural*
invariants (a shipped invariant changed since boot snapshot);
Coherence Auditor audits *behavioral* drift (the system's
behavioral mass moving in a direction it shouldn't, integrated
over a rolling window). Without temporal drift detection, RSI
loops have no halting condition rooted in their own behavior —
the immune system catches AST violations but not the slow
boil where everything-is-individually-fine but the gestalt has
rotated.

Move 4 catches the **discrete moment**; Priority #1 catches the
**integral** — the system's behavioral mass moving in a direction
it shouldn't.

## Architecture (4 modules, ~3,500 LOC)

  * **Slice 1** — `verification/coherence_auditor.py` —
    Pure-data primitive. 6-value `BehavioralDriftKind` closed
    enum DISTINCT from Move 4's 9-value structural taxonomy
    (BEHAVIORAL_ROUTE_DRIFT / POSTURE_LOCKED / SYMBOL_FLUX_DRIFT
    / POLICY_DEFAULT_DRIFT / RECURRENCE_DRIFT / CONFIDENCE_DRIFT).
    5-value `CoherenceOutcome` + 4-value `DriftSeverity` closed
    enums. `compute_behavioral_signature` aggregates with
    SemanticIndex's halflife-decay formula (literal byte-parity
    pinned by 36 parametrized comparisons — re-implemented
    inline so module stays PURE-STDLIB, strongest authority
    invariant). `compute_behavioral_drift` is total decision
    over (prev, curr) pair.
  * **Slice 2** — `verification/coherence_window_store.py` —
    Cross-process flock'd window store. Two distinct file
    disciplines for two distinct §8 invariants:
    `coherence_window.jsonl` is bounded ring buffer via
    `flock_critical_section` + read-trim-atomic-write;
    `coherence_audit.jsonl` is structurally append-only via
    `flock_append_line` direct invocation (no read-modify-write
    path → cannot corrupt). 5-value `WindowOutcome` closed enum.
  * **Slice 3** — `verification/coherence_observer.py` —
    Async observer mirroring Move 4 Slice 3's lifecycle exactly.
    Posture-aware cadence (HARDEN 3h / DEFAULT 6h / MAINTAIN
    12h, env-tunable). Adaptive vigilance (drift detected →
    cadence × 0.5 multiplier × N ticks; coherent decays).
    Failure backoff (linear in consecutive_failures, capped at
    ceiling). Drift signature dedup ring (bounded `deque`).
    5-value `ObserverTickOutcome` closed enum. `WindowDataCollector`
    Protocol injectable; default reads posture history via
    Tier 1 #2 safe wrapper. SSE event
    `EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED`.
  * **Slice 4** — `verification/coherence_action_bridge.py` —
    auto_action_router bridge with monotonic-tightening contract
    (Phase C §4.1). 6×6 1:1 mapping `BehavioralDriftKind` →
    `CoherenceAdvisoryAction` (closed enum DISTINCT from Move
    3's per-op `AdvisoryActionType`). REUSES Phase C's
    `MonotonicTighteningVerdict` vocabulary — every advisory
    carries the canonical verdict string. WOULD_LOOSEN proposals
    structurally CONVERTED to NEUTRAL_NOTIFICATION before
    persistence (audit chain cannot contain a loosen-actionable
    advisory by construction). 4-value
    `TighteningProposalStatus` + 5-value `RecordOutcome`.
    Persistence via Tier 1 #3 `flock_append_line`.
  * **Slice 5** — Graduation. ALL THREE flags default-true
    (different from Move 6's master default-false because
    Coherence Auditor is read-only over existing artifacts —
    zero LLM cost, zero K× generation amplification). 4 AST
    pins registered. 8 FlagRegistry seeds.

## Test invariants (~395 across 5 test files)

  * Slice 1 (139 tests): Master-flag asymmetric env, 6/5/4-value
    closed-taxonomy pins, **SemanticIndex byte-parity** (36
    parametrized comparisons), distribution math (TVD), severity
    classification (NaN→NONE), max consecutive hours, signature
    compute, drift detection per-kind + aggregate + suppression
    via APPLY events, drift-signature dedup stability, verdict
    helpers, schema integrity, defensive contract, **7 authority
    pins** including stdlib-only-imports exhaustive whitelist.
  * Slice 2 (49 tests): Path resolution + env clamps, 5-value
    `WindowOutcome` pin, signature record+read round-trip,
    distribution preservation, chronological sort, time-bounded
    read by `window_end_ts` (content-accurate), rotation at cap
    (no-rotation-under, rotation-triggered, keeps-newest), audit
    append-only never rotates, since_ts + limit filters,
    schema-mismatch tolerance, **multi-process flock stress** (40
    concurrent writes all persisted), atomic-write integrity,
    frozen result containers, defensive contract, 8 authority
    pins.
  * Slice 3 (74 tests): Sub-gate asymmetric env, 9 cadence env
    knob clamps, 5-value `ObserverTickOutcome` pin, posture-aware
    cadence dispatch (6 cases), single-cycle outcome matrix
    (INSUFFICIENT_DATA / COHERENT_OK / DRIFT_EMITTED / FAILED),
    drift signature dedup ring (bounded deque eviction +
    explicit-membership controlling outcome), cadence
    composition (default/HARDEN/MAINTAIN baseline, vigilance,
    backoff, ceiling cap, floor enforcement), vigilance
    state machine (escalate-on-drift, decay-on-coherent),
    failure-backoff state machine (increment, reset), SSE
    publisher (master-gated, COHERENT/DISABLED silenced,
    broker-missing graceful), start/stop lifecycle (master-off,
    sub-gate-off, double-start, full lifecycle), defensive
    contract, default collector returns WindowData, frozen
    schema integrity, 7 authority pins.
  * Slice 4 (81 tests): Sub-gate asymmetric env, 4 env knob
    clamps, 3 closed taxonomy pins (6/4/5), **1:1 mapping
    completeness** (every BehavioralDriftKind covered, actions
    unique), monotonic-tightening verification (smaller-is-
    tighter passes/loosens/equal-is-loosen, larger-is-tighter
    same; None=neutral; unknown=failed), default proposer
    (numeric kinds with floor-clamping; non-numeric returns
    None), bridge propose decision tree (10 cases), **WOULD_LOOSEN
    structural reject** (bad proposer attempts loosen → bridge
    converts to NEUTRAL_NOTIFICATION; defensive REJECTED_LOOSEN
    at record), persistence (record disabled/garbage/RECORDED,
    round-trip, drift_kind filter, since_ts filter, limit-keeps-
    newest, corrupt-lines-skipped), schema integrity (frozen,
    to_dict round-trip, schema mismatch None), defensive
    contract, 8 authority pins (including MUST-import-
    MonotonicTighteningVerdict-via-importfrom verification).
  * Slice 5 (43 tests): 3 master+sub-gate flags default-true +
    explicit-false-hot-reverts (parametrized), **10 cap-structure
    clamps** (parametrized: default, below-floor → floor,
    above-ceiling → ceiling), **4 Priority #1 invariant pins
    registered AND HOLD** (parametrized × 4) + count-≥-32, **8
    FlagRegistry seeds** present + 3-master-gate-default-true
    pin + install-count, **full-revert matrix** (master-off,
    sub-gate-off across all 5 surfaces), **end-to-end mechanism
    proofs** (full-pipeline drift→advisory with canonical
    `MonotonicTighteningVerdict.PASSED.value` stamping;
    cost-correctness pin verifying ZERO LLM/provider imports
    across all 4 modules; real observer cycle), authority
    invariants final pass (no-orchestrator across all 4 modules,
    sync vs async by slice, no exec/eval/compile, auditor
    pure-stdlib).

## Why all three flags graduated default-true (different from Move 6)

  * **Cost profile**: Coherence Auditor is read-only over
    existing artifacts (phase_capture / posture_history /
    summary.json / shipped modules). Zero LLM calls. Zero K×
    generation amplification. Move 6 Quorum was K× cost per
    APPROVAL_REQUIRED+ op — fundamentally different.
  * **Schedule**: periodic (default 6h cadence) — NOT per-op.
    Operator approves cadence/budget changes via the future
    `/coherence` REPL.
  * **Output discipline**: STRICTLY ADVISORY. Bridge writes
    `.jarvis/coherence_advisory.jsonl` for operator review; NO
    auto-flag-flip path. Phase C universal-cage-rule integration
    means even "tightening" proposals require operator approval
    via `MetaAdaptationGovernor` to actually change state.
  * **Reverse Russian Doll cost contract**: the immune system
    must scale temporally with the expanding shell. Default-true
    with operator-approval-still-required is the only way to
    actually scale temporal observability without inviting
    bypass via cumbersome operator-toggle ergonomics.

## Slice 5b — Deferred (operator UX polish)

Per Move 5/6 Slice 5b precedent:

  * `/coherence` REPL — recent / signatures / drifts / window /
    `<op_id>` filter (mirrors `/probe` / `/quorum` /
    `/auto-action` shape)
  * 5 GET routes: `/observability/coherence{,/config,/history,
    /signatures,/drifts}`
  * Production wiring at `GovernedLoopService` boot (auto-
    start observer when master+sub-gate true)
  * Default collector extension: full `phase_capture` +
    `summary.json` integration for op_records / recurrence /
    p99 / apply paths

These are operator-experience polish; the **core mechanism +
authority pins + observability event + advisory persistence**
are live and graduated.

## Closure criterion (from scope) — status

  * ✅ All 5 slices land (commits + regression tests green)
  * ✅ Master + 2 sub-gate flags graduated default-true
  * ✅ shipped_code_invariants AST pins register and currently-
    hold (4 added; total 28 → 32 — **meets scope target of 32
    exactly**)
  * ⚠️ Operator surfaces (5 GET routes + `/coherence` REPL) —
    SSE event live (Slice 3); 5 GET routes + REPL deferred to
    Slice 5b
  * ✅ `memory/project_priority_1_coherence_auditor_closure.md`
    written
  * ⚠️ MEMORY.md indexed — pending
  * ⚠️ End-to-end live verification — pending; happens once
    Slice 5b production wire-up + an operator-observed drift
    verdict in `.jarvis/coherence_audit.jsonl`

## Direct paths into Priority #2+ work

  * **Priority #2 — PostmortemRecall**: consumes the
    `RECURRENCE_DRIFT` signal Priority #1 produces. The
    `INJECT_POSTMORTEM_RECALL_HINT` advisory action (Slice 4)
    is the wired-but-dormant trigger surface — when Priority
    #2 ships, it consumes here without architectural change.
  * **Priority #3 — Counterfactual Replay**: the replay engine
    will use coherence drift verdicts as evaluation criteria
    ("would this counterfactual have caused drift?"). Drift
    audit log is the substrate.
  * **Slice 5b**: REPL + GET routes + production observer
    boot. Pre-requirement for end-to-end live verification.

## Key architectural decisions (per "leverage existing, no duplication")

  1. **Pure-stdlib Slice 1** — even stricter than scope
     allowed. SemanticIndex `_recency_weight` formula
     reimplemented inline; **byte-parity pinned by 36 tests**
     (9 ages × 4 halflives). Zero attack surface from upstream
     module changes.
  2. **Distinct vocabulary, shared verdict strings** —
     Coherence has its own 6-value action enum (not Move 3's
     per-op enum), but reuses Phase C's
     `MonotonicTighteningVerdict` strings (`"passed"` /
     `"rejected:would_loosen"`) so operators correlate
     advisories with Pass C ledger entries via shared vocabulary
     across separate files.
  3. **Two persistence disciplines for two §8 invariants** —
     bounded ring buffer (signatures) via
     `flock_critical_section`; structurally append-only audit
     chain (verdicts + advisories) via `flock_append_line`. The
     audit chain CANNOT corrupt because no read-modify-write
     path exists.
  4. **WOULD_LOOSEN structurally rejected** — bridge converts
     bad proposer's loosening intents to NEUTRAL_NOTIFICATION
     before persistence. Audit chain cannot contain a
     loosen-actionable advisory by construction.
  5. **Decoupled collector via Protocol** — Slice 1 takes
     pre-aggregated WindowData; Slice 3's observer is responsible
     for assembly via injectable `WindowDataCollector` Protocol.
     Empty defaults produce semantically-correct
     INSUFFICIENT_DATA verdicts on cold start, NOT a workaround.
  6. **Mirrors Move 4 architectural shape** — same observer /
     store / bridge / graduation discipline. Different schemas,
     different vocabularies, different semantics. The pattern
     is reusable; the content is domain-specific.

## RSI-load-bearing significance

The Reverse Russian Doll's outer shell now scales temporally,
not just spatially:

  * **Before Priority #1**: Move 4 catches AST violations at
    discrete moments; per-op gates catch per-op problems.
    Anti-Venom mathematical safety guarantees are per-op only —
    the integral over many ops can drift unbounded.
  * **After Priority #1**: every RSI loop has a measurable
    behavioral safety envelope. Drift budgets are operator-
    tunable per kind (no hardcoding). Tightening is monotonic
    via Phase C contract. Tightening proposals require Anti-
    Venom approval (`MetaAdaptationGovernor`). The expanding
    shell remains bounded — by construction, not by hope.

This is what closes the gap from B+ empirical floor to A−
empirical floor. Priority #2 (PostmortemRecall) and Priority
#3 (Counterfactual Replay) compound from this foundation.

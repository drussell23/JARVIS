---
title: Project Priority 3 Counterfactual Replay Closure
modules: [backend/core/ouroboros/governance/verification/counterfactual_replay.py, backend/core/ouroboros/governance/verification/counterfactual_replay_engine.py, backend/core/ouroboros/governance/verification/counterfactual_replay_comparator.py, backend/core/ouroboros/governance/verification/counterfactual_replay_observer.py]
status: merged
source: project_priority_3_counterfactual_replay_closure.md
---

5-slice arc closed in a single day (2026-05-02):

**Slice 1** — pure-stdlib primitive (`counterfactual_replay.py`)
  • 3 closed-taxonomy 5-value enums (ReplayOutcome / BranchVerdict /
    DecisionOverrideKind)
  • Frozen dataclasses (BranchSnapshot / ReplayTarget / ReplayVerdict)
    with to_dict/from_dict round-trip + schema-mismatch tolerance
  • 5 env knobs with floor+ceiling clamps
  • Pure decision functions: `compute_branch_verdict` (multi-criteria
    primary→secondary→tertiary→quaternary axis resolution; contradicting
    → DIVERGED_NEUTRAL) + `compute_replay_outcome` (5-value outcome tree)
  • 78 regression tests

**Slice 2** — async engine (`counterfactual_replay_engine.py`)
  • REUSES `causality_dag.build_dag` (Priority 2 Slice 3) for ledger
    parsing — zero re-implementation
  • REUSES `last_session_summary._parse_summary` (Phase 1) for summary
    projection
  • Locates swap point chronologically; detects downstream divergence
    via DAG reverse-edge BFS (capped by env knob)
  • Dynamic kind-keyed inference registry (5 default inferences for
    closed taxonomy: GATE_DECISION reads payload['verdict'] case-
    insensitive; passthrough for POSTMORTEM/RECURRENCE/QUORUM/COHERENCE)
  • `async run_counterfactual_replay()` wraps disk I/O via
    `asyncio.to_thread`
  • Cost-contract structurally enforced via AST: NO providers/
    doubleword/urgency_router/candidate_generator/orchestrator/
    tool_executor imports
  • 78 regression tests + ReplayOutcome.DIVERGED reserved for cached-
    hash-mismatch (NOT counterfactual divergence — that's
    SUCCESS+BranchVerdict)

**Slice 3** — pure-data comparator (`counterfactual_replay_comparator.py`)
  • Aggregates verdict streams → ComparisonReport with 5-value
    ComparisonOutcome (ESTABLISHED / INSUFFICIENT_DATA / DEGRADED /
    DISABLED / FAILED)
  • RecurrenceReductionStats: 18 fields covering counts + percentages +
    postmortem-prevention math
  • BaselineQuality 5-value (HIGH / MEDIUM / LOW / INSUFFICIENT /
    FAILED) — operators see evidence-strength + outcome on independent
    axes
  • DEGRADED takes precedence over ESTABLISHED on tie (safer default)
  • Recurrence-reduction-pct = prevention/actionable * 100 (not /total)
    — avoids degrading the statistic when sessions lack swap points
  • String/bytes/non-iterable input → FAILED (caller-bug guard)
  • Phase C cross-stack vocabulary integration: every report stamps
    `MonotonicTighteningVerdict.PASSED` from `adaptation.ledger`
  • `StampedVerdict` wrapper replaces Slice 2's stamping into detail
    string with a structural typed field
  • 86 regression tests

**Slice 4** — observer + history store + SSE publisher
(`counterfactual_replay_observer.py`)
  • REUSES Tier 1 #3 `cross_process_jsonl.flock_append_line` +
    `flock_critical_section` for the bounded JSONL ring buffer (same
    discipline as InvariantDriftStore + Coherence window store)
  • Per-verdict SSE event `EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE`
  • Per-aggregation SSE event `EVENT_TYPE_COUNTERFACTUAL_BASELINE_UPDATED`
  • Both event types additively registered in
    `ide_observability_stream._VALID_EVENT_TYPES`
  • Async `ReplayObserver` lifecycle (start/stop/idempotent) with
    posture-aware cadence + adaptive vigilance (drift_multiplier × 0.5
    on signature change → twice as fast) + linear failure backoff
    capped at ceiling + liveness pulse every Nth pass
  • Drift-signature dedup via sha256[:16] over bucketed counts
  • 7 env knobs all clamped
  • Slice 1 `ReplayVerdict.from_dict` added — pairs with `to_dict` for
    JSONL round-trip
  • 79 regression tests

**Slice 5** — graduation
  • 4 master/sub-flag defaults flipped from false → true (asymmetric
    env semantics: empty/whitespace = unset = graduated default-true;
    explicit truthy/falsy hot-reverts):
      - JARVIS_COUNTERFACTUAL_REPLAY_ENABLED (master)
      - JARVIS_REPLAY_ENGINE_ENABLED (Slice 2 sub)
      - JARVIS_REPLAY_COMPARATOR_ENABLED (Slice 3 sub)
      - JARVIS_REPLAY_OBSERVER_ENABLED (Slice 4 sub)
  • All four default-true post-graduation matches Priority #1 + #2
    discipline because replay is read-only over cached artifacts
    (zero LLM cost by AST-pinned construction; observational not
    prescriptive — every verdict stamps PASSED)
  • 4 AST pins added in `meta.shipped_code_invariants` (33 → 37 total):
      - counterfactual_replay_pure_stdlib
      - counterfactual_replay_engine_cost_contract
      - counterfactual_replay_comparator_authority
      - counterfactual_replay_observer_uses_flock
  • 6 FlagRegistry seeds added in `flag_registry_seed.SEED_SPECS`:
    master + 3 sub-gates + JARVIS_REPLAY_PREVENTION_THRESHOLD_PCT +
    JARVIS_REPLAY_HISTORY_MAX_RECORDS
  • 19 graduation regression tests covering: flag defaults, AST-pin
    registration + clean validation, 6 seeds present + correctly
    attributed to "Priority #3", 2 SSE event types in
    _VALID_EVENT_TYPES, end-to-end pipeline (synthetic ledger →
    engine → record → aggregator → ESTABLISHED), DIVERGED_WORSE →
    DEGRADED outcome, hot-revert master flag disables full pipeline
    in lockstep, PASSED stamp present in every output surface

**Combined sweep**: 1878/1878 green in 19.78s (Priority #3 Slice 1+2+3+4+5
+ Priority #2 + Priority #1 + Move 4-6 + Tier 1 + Phase 1 determinism +
shipped_code_invariants + ide_observability_stream).

**What this closes** (the §29 gap):
  • Priority #1 detects behavioral drift cross-session.
  • Priority #2 prevents recurrence by injecting recall context.
  • Priority #3 *measures* whether prevention actually reduces
    recurrence empirically — produces a measurable percentage with
    bounded baseline quality, not just correlation.
  • Together: detection → prevention → empirical evidence loop closed.

**Empirical baseline machinery operational**: `compare_replay_history`
+ `compare_recent_history` produce the recurrence-reduction-pct
statistic that retroactively justifies (or contraindicates) Move 6
master flag graduation. Operators can answer "did Quorum actually
reduce postmortems?" with a percentage, not a hunch.

**Deferred to Slice 5b** (post-graduation operator polish, NOT blocking):
  • `/replay` REPL with subcommands {history, baseline, run, status}
  • 4 GET routes: /observability/replay/{history,baseline,verdicts,health}
  • Production wire-up: orchestrator hooks calling
    `record_replay_verdict` after each engine run
  • IDE GET endpoints reading the JSONL store

**Letter grade evolution**:
  • Pre-Priority-#3: A structural / A− empirical (post-#2 closure)
  • Post-Priority-#3: A structural / A empirical — recurrence-reduction
    measurement is now a first-class observable, no longer just
    "correlation looks good"

**Cross-stack vocabulary integration count**: 4 modules now stamp
`MonotonicTighteningVerdict.PASSED` (Move 6 + Priority #1 + Priority #2 +
Priority #3) — operators correlate cross-file via shared canonical token.

**RSI safety envelope**: empirical now provable via deterministic
counterfactual replay over recorded sessions. Zero LLM cost by
AST-pinned construction. Replay never proposes a flag flip — purely
observational. Operator approval still required for any downstream
prescription via MetaAdaptationGovernor.

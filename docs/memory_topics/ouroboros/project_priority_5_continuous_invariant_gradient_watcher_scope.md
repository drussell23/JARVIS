---
title: Project Priority 5 Continuous Invariant Gradient Watcher Scope
modules: [backend/core/ouroboros/governance/verification/gradient_watcher.py, backend/core/ouroboros/governance/verification/gradient_collector.py, backend/core/ouroboros/governance/verification/gradient_comparator.py, backend/core/ouroboros/governance/verification/gradient_observer.py]
status: historical
source: project_priority_5_continuous_invariant_gradient_watcher_scope.md
---

5-slice DRAFT scope post-Priority-#4-closure (May 2). Closes the
long-horizon semantic gradient drift gap identified in §29 brutal
review:

> Move 4 InvariantDriftAuditor takes SNAPSHOTS at discrete moments and
> compares them. Between snapshots, individual edits can cumulatively
> shift the codebase. A 1% shift per op × 100 ops = 100% drift with
> zero alarms.

CIGW watches structural metrics on EVERY APPLY (or every successful
COMPLETE phase) instead of at discrete snapshot boundaries. On each
sample, computes per-metric delta against a rolling baseline; raises
a `GradientBreach` advisory when cumulative drift exceeds env-tunable
severity thresholds. Same observer/window/bridge architecture as
Coherence Auditor + Priority #1/#2/#3/#4 — zero LLM cost on the
detection path; structural code metrics computed via stdlib `ast` +
file size only.

**Slice breakdown** (mirrors Priority #1-4 discipline):

  • Slice 1 — PURE-STDLIB primitive (`gradient_watcher.py`): 3
    closed-taxonomy 5-value enums (MeasurementKind /
    GradientSeverity / GradientOutcome); 4 frozen dataclasses
    (InvariantSample / GradientReading / GradientBreach /
    GradientReport) with to_dict/from_dict round-trip; 5 env knobs
    with floor+ceiling clamps (rolling_window_size +
    low/medium/high/critical threshold pct); pure decision functions
    (compute_baseline_mean / compute_severity /
    compute_gradient_reading / compute_gradient_outcome); ~750 LOC
    + ~80 tests

  • Slice 2 — async metric collector + watcher (`gradient_collector.py`):
    REUSES stdlib `ast` for structural-metric extraction; 5 default
    collectors (LINE_COUNT / FUNCTION_COUNT / IMPORT_COUNT /
    BANNED_TOKEN_COUNT / BRANCH_COMPLEXITY); MetricCollector
    Protocol for operator-injected custom metrics; async
    `sample_target` wraps disk read in to_thread; cost-contract
    AST-pinned; ~700 LOC + ~70 tests

  • Slice 3 — pure-data comparator (`gradient_comparator.py`):
    aggregator over a stream of GradientReports → CIGWReport with
    5-value EffectivenessOutcome (HEALTHY / DRIFTING / DEGRADED /
    DISABLED / FAILED); stats include total_samples / breach_count /
    avg_delta_pct / max_severity / per_kind_drift_summary; Phase C
    PASSED stamping via adaptation.ledger reuse; mirrors Priority
    #1/#3/#4 Slice 3 architecture; ~600 LOC + ~70 tests

  • Slice 4 — observer + history store + SSE (`gradient_observer.py`):
    Tier 1 #3 flock'd JSONL ring buffer; 2 new SSE event types
    EVENT_TYPE_GRADIENT_SAMPLE_RECORDED + EVENT_TYPE_GRADIENT_
    BREACH_DETECTED additively registered; async observer with
    posture-aware cadence + adaptive vigilance + drift-signature
    dedup + liveness pulse; ~700 LOC + ~75 tests

  • Slice 5 — graduation: 4 master/sub-flag defaults flipped to
    TRUE matching Priority #1/#2/#3/#4 discipline (read-only
    structural metrics; zero LLM cost; observational not
    prescriptive); 4 AST pins added (41 → 45 total); 6 FlagRegistry
    seeds; end-to-end graduation regression suite (~20 tests);
    ~250 tests / ~2,000 LOC total

**Reuse contracts** (zero duplication):
  • stdlib `ast` for structural-metric extraction (pure-stdlib;
    same discipline as Move 6's ast_canonical)
  • Tier 1 #3 cross_process_jsonl flock helpers (Slice 4)
  • ide_observability_stream broker (Slice 4 SSE)
  • adaptation.ledger.MonotonicTighteningVerdict (Slice 3 stamping)
  • InvariantDriftAuditor (Move 4) — orthogonal: CIGW watches
    BETWEEN snapshots, IDA watches AT snapshots; both produce
    drift signals consumable by auto_action_router

**Cost contract preserved by construction** (AST-pinned across all
slices): NO providers / doubleword_provider / urgency_router /
candidate_generator / orchestrator / tool_executor / phase_runner /
iron_gate / change_engine / auto_action_router / subagent_scheduler /
semantic_guardian / semantic_firewall / risk_engine imports. Per-
sample cost ≤ Σ(stdlib ast.parse + file.read) — no generation calls.

**Closes the §29 long-horizon drift gap**:
  • Per-APPLY sampling instead of per-snapshot — no inter-snapshot
    blind spot
  • Rolling baseline (default last 50 samples) — adaptive to
    natural codebase growth
  • Severity tiers map to closed-taxonomy GradientSeverity
    (LOW/MEDIUM/HIGH/CRITICAL) — operators see at-a-glance which
    metrics are drifting fastest
  • Empirical baseline produced by Slice 3 comparator — operators
    answer "is the codebase drifting in a structural sense?" with
    a percentage

**Default-true post-graduation matches Priority #1/#2/#3/#4
discipline** because CIGW is read-only over source files (zero LLM
cost on detection path; structural metrics via stdlib ast +
file.read; observational not prescriptive — every reading stamps
PASSED). Operator approval still required for any downstream flag-
flip proposal via MetaAdaptationGovernor.

**Deferred to Slice 5b** (post-graduation operator polish, NOT
blocking): /gradient REPL with subcommands {status, history,
baseline, watch, breaches} + 4 GET routes + orchestrator hook
calling sample_target after each successful APPLY.

**Letter grade target post-Priority-#5**: A structural / A empirical
(returns to A across the board; closes the long-horizon drift blind
spot identified in §29). Remaining gap is Priority #6 Antivenom v2
(Quine-on-BG + tool-output prompt-injection filter).

---
title: Project Priority 5 Continuous Invariant Gradient Watcher Closure
modules: [backend/core/ouroboros/governance/verification/gradient_watcher.py, backend/core/ouroboros/governance/verification/gradient_collector.py, backend/core/ouroboros/governance/verification/gradient_comparator.py, backend/core/ouroboros/governance/verification/gradient_observer.py]
status: merged
source: project_priority_5_continuous_invariant_gradient_watcher_closure.md
---

5-slice arc closed in a single day (2026-05-02). Closes the
long-horizon semantic gradient drift gap identified in §29 brutal
review:

> Move 4 InvariantDriftAuditor takes SNAPSHOTS at discrete moments
> and compares them. Between snapshots, individual edits cumulatively
> shift the codebase. A 1% shift × 100 ops = 100% drift with zero
> alarms.

CIGW samples on EVERY APPLY instead of at snapshot boundaries.
Per-metric delta against rolling 50-sample baseline; severity step
function (NONE/LOW/MEDIUM/HIGH/CRITICAL); GradientBreach raised when
delta crosses threshold. Zero LLM cost via stdlib `ast` + `file.read`.

**Slice 1** — pure-stdlib primitive (`gradient_watcher.py`)
  • 3 closed-taxonomy 5-value enums (MeasurementKind /
    GradientSeverity / GradientOutcome)
  • 4 frozen dataclasses (InvariantSample / GradientReading /
    GradientBreach / GradientReport) with to_dict round-trip
  • 5 env knobs with floor+ceiling clamps
  • 4 pure decision functions (compute_baseline_mean +
    compute_severity + compute_gradient_reading +
    compute_gradient_outcome)
  • Zero-baseline → CRITICAL via 1000% delta heuristic (catches
    "banned token appeared in primitive" headroom case)
  • 105 regression tests

**Slice 2** — async metric collectors + on-APPLY hook
(`gradient_collector.py`)
  • 5 default concrete collectors via stdlib `ast` (LINE_COUNT /
    FUNCTION_COUNT / IMPORT_COUNT / BANNED_TOKEN_COUNT /
    BRANCH_COMPLEXITY)
  • Per-target context caches file.read + ast.parse across the 5
    collectors (saves parse cost)
  • Dynamic MetricCollector Protocol + registry
  • Async public API (sample_target / sample_targets /
    sample_on_apply) wraps sync collection via asyncio.to_thread
  • Default banned-tokens list mirrors 14 governance imports
    pinned across SBT/Replay/Coherence/Postmortem
  • 72 regression tests

**Slice 3** — pure-data comparator (`gradient_comparator.py`)
  • Aggregates GradientReport streams → CIGWComparisonReport with
    5-value EffectivenessOutcome (HEALTHY / INSUFFICIENT_DATA /
    DEGRADED / DISABLED / FAILED)
  • CIGWAggregateStats: 14 fields + per-severity Dict +
    per-MeasurementKind drift Dict (operators see WHICH metrics
    drift)
  • CIGWBaselineQuality 5-value (HIGH / MEDIUM / LOW /
    INSUFFICIENT / FAILED)
  • DEGRADED takes precedence over HEALTHY (any breach escalates)
  • Phase C MonotonicTighteningVerdict.PASSED stamping (6th module
    after Move 6 + Priority #1/#2/#3/#4)
  • 77 regression tests

**Slice 4** — observer + history store + SSE publisher
(`gradient_observer.py`)
  • REUSES Tier 1 #3 cross_process_jsonl flock primitives
  • Per-report SSE event EVENT_TYPE_CIGW_REPORT_RECORDED
  • Per-aggregation SSE event EVENT_TYPE_CIGW_BASELINE_UPDATED
  • Both event types additively registered in
    ide_observability_stream._VALID_EVENT_TYPES
  • Async CIGWObserver lifecycle with posture-aware cadence +
    adaptive vigilance + drift-signature dedup + liveness pulse
  • Drift-signature dedup via sha256[:16] over bucketed counts
  • 7 env knobs all clamped
  • Minimal-shape GradientReport reconstruction at read time
    (Slice 1 doesn't ship from_dict; observer reconstructs only
    fields Slice 3's aggregator reads)
  • 69 regression tests

**Slice 5** — graduation
  • 4 master/sub-flag defaults flipped from false → true:
      - JARVIS_CIGW_ENABLED (master)
      - JARVIS_CIGW_COLLECTOR_ENABLED (Slice 2 sub)
      - JARVIS_CIGW_COMPARATOR_ENABLED (Slice 3 sub)
      - JARVIS_CIGW_OBSERVER_ENABLED (Slice 4 sub)
  • All four default-true post-graduation matches Priority
    #1/#2/#3/#4 discipline because CIGW is read-only over source
    files (zero LLM cost on detection path; observational not
    prescriptive — every reading stamps PASSED)
  • 4 AST pins added in `meta.shipped_code_invariants` (41 → 45):
      - gradient_watcher_pure_stdlib
      - gradient_collector_cost_contract
      - gradient_comparator_authority
      - gradient_observer_uses_flock
  • 6 FlagRegistry seeds added in `flag_registry_seed.SEED_SPECS`:
    master + 3 sub-gates + JARVIS_CIGW_HEALTHY_THRESHOLD_PCT +
    JARVIS_CIGW_HISTORY_MAX_RECORDS
  • 21 graduation regression tests covering: flag defaults,
    AST-pin registration + clean validation, 6 seeds present +
    correctly attributed to "Priority #5", 2 SSE event types
    in _VALID_EVENT_TYPES, end-to-end pipeline (real .py file →
    collect → record → aggregator → HEALTHY with no env-flag
    overrides), BREACHED report → DEGRADED aggregate,
    sample_on_apply orchestrator hook, hot-revert master flag
    disables full pipeline in lockstep, PASSED stamp on every
    output

**Combined sweep**: 1074/1074 green in 24.55s.

**What this closes** (the §29 long-horizon-drift gap):
  • Move 4 IDA compares snapshots; CIGW watches BETWEEN snapshots.
  • Per-APPLY sampling instead of per-snapshot — no inter-snapshot
    blind spot.
  • Per-MeasurementKind drift surface lets operators see WHICH
    metrics are drifting fastest (not just THAT something is).
  • Banned-token gradient catches structural drift toward
    authority-violation BEFORE the binary AST validator triggers
    (e.g., 0 → 1 banned-token count in a primitive module is
    CRITICAL severity even though the binary check would only
    fire AFTER the import lands).

**Cross-stack vocabulary count**: 6 modules now stamp
`MonotonicTighteningVerdict.PASSED` (Move 6 + Priority #1 + #2 +
#3 + #4 + #5) — operators correlate cross-file via shared
canonical token.

**Letter grade evolution**:
  • Pre-Priority-#5: A structural / A− empirical (post-#4 closure)
  • Post-Priority-#5: A structural / A empirical — long-horizon
    drift blind spot closed; remaining gap is Priority #6
    Antivenom v2 (Quine-on-BG + tool-output prompt-injection
    filter — partial work already shipped via background agent
    in commit 91fe520cbb)

**RSI safety envelope**: temporal-safety + structural-safety now
both provable via continuous structural-metric sampling. CIGW
never proposes a flag flip — purely observational. Operator
approval still required for any downstream prescription via
MetaAdaptationGovernor.

**Reverse Russian Doll progress**: Antivenom (the AST-pinned
constraint layer) now scales temporally AND structurally. 45
invariants, 6-module canonical PASSED vocabulary, structural
cost-contract preservation across all 5 graduated arcs (Move 6 +
Priority #1-5). The expanding outer shell carved itself one drift-
detection level larger; the immune system caught up
proportionally.

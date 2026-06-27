---
title: Project Priority 4 Speculative Branch Tree Closure
modules: [backend/core/ouroboros/governance/verification/speculative_branch.py, backend/core/ouroboros/governance/verification/speculative_branch_runner.py, backend/core/ouroboros/governance/verification/speculative_branch_comparator.py, backend/core/ouroboros/governance/verification/speculative_branch_observer.py]
status: merged
source: project_priority_4_speculative_branch_tree_closure.md
---

5-slice arc closed in a single day (2026-05-02). Closes the cognitive
gap identified in §29 brutal review — extends HypothesisProbe (Phase
7.6) from one read-only probe to N parallel typed-evidence branches
at any decision point with sha256-fingerprint majority convergence
and depth-bounded tie-breaker spawn on DIVERGED.

**Slice 1** — pure-stdlib primitive (`speculative_branch.py`)
  • 3 closed-taxonomy 5-value enums (BranchOutcome / EvidenceKind /
    TreeVerdict)
  • 4 frozen dataclasses (BranchEvidence / BranchResult /
    BranchTreeTarget / TreeVerdictResult) with to_dict/from_dict
    round-trip + schema-mismatch tolerance
  • 5 env knobs with floor+ceiling clamps (max_depth / max_breadth /
    max_wall_seconds / dim_returns_threshold /
    min_confidence_for_winner)
  • Pure decision functions: canonical_evidence_fingerprint (sha256
    over sorted (kind, content_hash) — Quine-class-resistant);
    compute_tree_verdict (11-input → 5-outcome decision tree);
    compute_tree_outcome (top-level resolver with
    highest-confidence-in-winning-group winner selection)
  • 99 regression tests

**Slice 2** — async tree executor (`speculative_branch_runner.py`)
  • REUSES Move 5's READONLY_TOOL_ALLOWLIST (9-tool frozenset) +
    is_tool_allowlisted from readonly_evidence_prober — re-exported,
    NEVER re-implemented
  • REUSES Move 6's K-way parallel pattern via `asyncio.create_task`
    + `asyncio.as_completed`
  • REUSES HypothesisProbe's three-termination guarantees (K-call cap
    + monotonic-clock + sha256 diminishing-returns) extended to tree
    topology
  • Tree topology: spawn `max_breadth` parallel branches at level 0;
    if DIVERGED + level < max_depth → spawn ONE tie-breaker level
    with prior_evidence aggregated (8-item cap, sorted by confidence
    desc); cap at effective_max_depth
  • Per-level wall budget split (level 0 gets 50%, each tie-breaker
    halves remaining); per-branch cap = max(1.0, total /
    (depth × breadth))
  • Defense-in-depth: drops evidence whose source_tool isn't in
    READONLY_TOOL_ALLOWLIST
  • BranchProber Protocol with _NullBranchProber default (empty
    evidence → INCONCLUSIVE; safer than asserting caller MUST supply)
  • 57 regression tests

**Slice 3** — pure-data comparator
(`speculative_branch_comparator.py`)
  • Aggregates verdict streams → SBTComparisonReport with 5-value
    EffectivenessOutcome (ESTABLISHED / INSUFFICIENT_DATA /
    INEFFECTIVE / DISABLED / FAILED) — INEFFECTIVE distinguishes
    "burns budget without converging" from "not enough data"
  • SBTEffectivenessStats: 14 fields (counts + per-outcome buckets +
    averages + derived rates + baseline_quality)
  • SBTBaselineQuality 5-value (HIGH / MEDIUM / LOW / INSUFFICIENT
    / FAILED) — same value vocabulary as Priority #3 Slice 3 for
    cross-module operator consistency
  • INEFFECTIVE precedes ESTABLISHED on tie (safer default — same
    discipline as Priority #3 Slice 3's DEGRADED)
  • Resolution rate = converged / actionable * 100 (TRUNCATED +
    FAILED excluded from actionable denominator — they reflect
    budget exhaustion, not resolution capability)
  • Phase C cross-stack vocabulary integration: every report stamps
    `MonotonicTighteningVerdict.PASSED` from `adaptation.ledger`
  • StampedTreeVerdict wrapper replaces string-in-detail stamping
    with structural typed field
  • 82 regression tests

**Slice 4** — observer + history store + SSE publisher
(`speculative_branch_observer.py`)
  • REUSES Tier 1 #3 `cross_process_jsonl.flock_append_line` +
    `flock_critical_section` for the bounded JSONL ring buffer
    (same discipline as InvariantDriftStore + Coherence + Postmortem
    + Priority #3 observer)
  • Per-tree SSE event `EVENT_TYPE_SBT_TREE_COMPLETE`
  • Per-aggregation SSE event `EVENT_TYPE_SBT_BASELINE_UPDATED`
  • Both event types additively registered in
    `ide_observability_stream._VALID_EVENT_TYPES`
  • Async `SBTObserver` lifecycle (start/stop/idempotent) with
    posture-aware cadence + adaptive vigilance (drift_multiplier ×
    0.5 on signature change) + linear failure backoff capped at
    ceiling + liveness pulse every Nth pass
  • Drift-signature dedup via sha256[:16] over bucketed counts
  • 7 env knobs all clamped
  • 70 regression tests

**Slice 5** — graduation
  • 4 master/sub-flag defaults flipped from false → true (asymmetric
    env semantics: empty/whitespace = unset = graduated default-true;
    explicit truthy/falsy hot-reverts):
      - JARVIS_SBT_ENABLED (master)
      - JARVIS_SBT_RUNNER_ENABLED (Slice 2 sub)
      - JARVIS_SBT_COMPARATOR_ENABLED (Slice 3 sub)
      - JARVIS_SBT_OBSERVER_ENABLED (Slice 4 sub)
  • All four default-true post-graduation matches Priority #1/#2/#3
    discipline because SBT is read-only over typed evidence (zero
    LLM cost on convergence path; observational not prescriptive —
    every verdict stamps PASSED)
  • 4 AST pins added in `meta.shipped_code_invariants` (37 → 41 total):
      - speculative_branch_pure_stdlib
      - speculative_branch_runner_cost_contract (also enforces
        Move 5 READONLY_TOOL_ALLOWLIST reuse contract)
      - speculative_branch_comparator_authority
      - speculative_branch_observer_uses_flock
  • 6 FlagRegistry seeds added in `flag_registry_seed.SEED_SPECS`:
    master + 3 sub-gates + JARVIS_SBT_RESOLUTION_THRESHOLD_PCT +
    JARVIS_SBT_HISTORY_MAX_RECORDS
  • 20 graduation regression tests covering: flag defaults, AST-pin
    registration + clean validation, 6 seeds present + correctly
    attributed to "Priority #4", 2 SSE event types in
    _VALID_EVENT_TYPES, end-to-end pipeline (synthetic agreeing
    prober → CONVERGED → record → aggregator → ESTABLISHED with no
    env-flag overrides), DIVERGED handling, hot-revert master flag
    disables full pipeline in lockstep, PASSED stamp + READONLY tool
    allowlist verified

**Combined sweep**: 1296/1296 green in 18.19s (Priority #4 Slice 1+2+3+4+5
+ Priority #3 Slice 1+2+3+4+5+5b + Priority #2 + Priority #1 + Move 4-6
+ Tier 1 + Phase 1 determinism + shipped_code_invariants +
ide_observability stack).

**What this closes** (the §29 cognitive gap):
  • CC has interleaved thinking between tool calls; O+V had only
    HypothesisProbe (one probe per ambiguity) and Move 5 PROBE_ENVIRONMENT
    (fires on confidence drop — a symptom, not the source).
  • SBT runs at ANY decision point with N parallel typed-evidence
    branches converging via deterministic sha256-fingerprint majority,
    with depth-bounded tie-breaker spawn on DIVERGED.
  • Closes CC's plan-mode-replan via tie-breaker spawn pattern —
    when level 0 disagrees, tie-breaker level receives aggregated
    prior evidence and asks sharper follow-ups.
  • Closes CC's speculative branching via Quorum-style K-parallel
    extended from GENERATE-only-APPROVAL_REQUIRED-only to ANY
    decision point + read-only tool budget.

**Antivenom alignment** (load-bearing):
  • Read-only by AST-pinned construction — NO mutation tools
    (READONLY_TOOL_ALLOWLIST 9-tool frozenset enforced)
  • Typed evidence (5-value EvidenceKind closed taxonomy) —
    prompt-injection-resistant
  • Sha256-fingerprint majority — Quine-class-resistant via
    Move-6-style canonicalization
  • Three-termination contract preserved (K-call + monotonic-clock
    + diminishing-returns)
  • Phase C MonotonicTighteningVerdict.PASSED stamping — never
    loosens
  • Cost contract preserved by AST construction (NO providers /
    doubleword / urgency_router / candidate_generator / orchestrator
    / tool_executor / phase_runner / iron_gate / change_engine /
    auto_action_router / subagent_scheduler / semantic_guardian /
    semantic_firewall / risk_engine imports)

**Empirical effectiveness machinery operational**:
`compare_tree_history` + `compare_recent_tree_history` produce the
ambiguity_resolution_rate statistic — operators can answer "does
SBT actually resolve ambiguity, or does it just burn cost?" with a
percentage and a bounded baseline-quality grade, plus the
INEFFECTIVE outcome catches the budget-exhaustion case directly.

**Cross-stack vocabulary count**: 5 modules now stamp
`MonotonicTighteningVerdict.PASSED` (Move 6 + Priority #1 + #2 + #3
+ #4) — operators correlate cross-file via shared canonical token.

**Letter grade evolution**:
  • Pre-Priority-#4: A structural / A empirical (post-#3 closure)
  • Post-Priority-#4: A structural / A− empirical (closing
    cognitive gap; Priority #5 CIGW closes long-horizon drift,
    Priority #6 Antivenom v2 closes Quine-on-BG + tool-output
    injection — full A reachable in 2 more arcs)

**RSI safety envelope**: cognitive shape now matches CC's
interleaved-thinking + plan-mode + speculative-branching paradigm
with Antivenom-aligned constraints (read-only by construction,
typed evidence, bounded structurally). Replay never proposes a flag
flip — purely observational. Operator approval still required for
any downstream prescription via MetaAdaptationGovernor.

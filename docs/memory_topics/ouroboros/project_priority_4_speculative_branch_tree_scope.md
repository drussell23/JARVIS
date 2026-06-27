---
title: Project Priority 4 Speculative Branch Tree Scope
modules: [backend/core/ouroboros/governance/verification/speculative_branch.py, backend/core/ouroboros/governance/verification/speculative_branch_runner.py, backend/core/ouroboros/governance/verification/speculative_branch_comparator.py, backend/core_contexts/observer.py]
status: historical
source: project_priority_4_speculative_branch_tree_scope.md
---

5-slice DRAFT scope post-Priority-#3-closure (May 2). Closes the cognitive
gap identified in §29 brutal review: O+V's HypothesisProbe (Phase 7.6) is
ONE probe per ambiguity, Move 5's PROBE_ENVIRONMENT fires on confidence
drop (a symptom not the source), Move 6's Quorum is K-flat at GENERATE
only and APPROVAL_REQUIRED-tier-only. None of these match CC's
interleaved-thinking + plan-mode + speculative-branching shape.

**SBT extends HypothesisProbe to a tree** — N parallel read-only branches
at ANY decision point (not just GENERATE), each producing typed evidence,
converging via deterministic scoring. When branches diverge, optionally
spawn one tie-breaker sub-branch (depth permitting). Bounded structurally
by depth × breadth × wall-time × diminishing-returns — same triple-
termination contract as HypothesisProbe extended to tree topology.

**Antivenom alignment** (load-bearing):
  • Read-only by AST-pinned construction — NO mutation tools, NO
    exec/eval/compile, READONLY_TOOL_ALLOWLIST (Move 5) reused
  • Evidence is TYPED (5-value EvidenceKind closed taxonomy) not free
    text — prompt-injection-resistant; matches Slice 1 primitive
    discipline from Priority #1/#2/#3
  • Branch verdicts are TYPED (5-value TreeVerdict closed taxonomy)
  • Phase C MonotonicTighteningVerdict.PASSED stamped on every verdict
    (observational not prescriptive — branches NEVER propose mutations)
  • Cost contract preserved by AST construction — no providers/
    doubleword/urgency_router/candidate_generator/orchestrator/
    tool_executor imports

**Slice breakdown** (mirrors Priority #1/#2/#3 discipline):

  • Slice 1 — PURE-STDLIB primitive (`speculative_branch.py`): 3
    closed-taxonomy 5-value enums (BranchOutcome, EvidenceKind,
    TreeVerdict); 4 frozen dataclasses (BranchEvidence,
    BranchResult, BranchTreeTarget, TreeVerdictResult) with to_dict/
    from_dict round-trip; 5 env knobs with floor+ceiling clamps
    (max_depth / max_breadth / max_wall_seconds / dim_returns_threshold
    / min_confidence_for_winner); pure decision functions
    `compute_tree_verdict` (convergence detection via sha256
    fingerprint majority) + `compute_tree_outcome` (5-value outcome
    resolution); ~900 LOC + ~80 tests

  • Slice 2 — async runner (`speculative_branch_runner.py`): REUSES
    Move 6's K-way parallel pattern + Move 5's READONLY_TOOL_ALLOWLIST
    + HypothesisProbe's three-termination guarantees; `async
    run_speculative_tree(target, *, prober) -> TreeVerdictResult`;
    Protocol-typed prober for test injection; sequential-with-
    early-stop via asyncio.as_completed; cost-contract AST-pinned;
    ~700 LOC + ~70 tests

  • Slice 3 — pure-data comparator (`speculative_branch_comparator.py`):
    aggregator over many TreeVerdictResult streams → ComparisonReport
    with 5-value EffectivenessOutcome (ESTABLISHED /
    INSUFFICIENT_DATA / DEGRADED / DISABLED / FAILED); stats include
    total_trees / converged_count / diverged_count / avg_branch_count
    / avg_evidence_count / ambiguity_resolution_rate; Phase C PASSED
    stamping via adaptation.ledger reuse; mirrors Priority #3 Slice
    3 architecture exactly; ~600 LOC + ~70 tests

  • Slice 4 — observer + history store + SSE (`speculative_branch_
    observer.py`): Tier 1 #3 flock'd JSONL ring buffer; 2 new SSE
    event types EVENT_TYPE_BRANCH_TREE_COMPLETE +
    EVENT_TYPE_BRANCH_TREE_BASELINE_UPDATED additively registered;
    async ReplayObserver-pattern lifecycle with posture-aware
    cadence + adaptive vigilance + drift-signature dedup + liveness
    pulse; ~700 LOC + ~75 tests

  • Slice 5 — graduation: 4 master/sub-flag defaults flipped to TRUE
    matching Priority #1/#2/#3 discipline (read-only over cached
    evidence; zero LLM cost on the convergence path; observational
    not prescriptive); 4 AST pins added (37 → 41 total invariants);
    6 FlagRegistry seeds (master + 3 sub-gates +
    JARVIS_SBT_MAX_DEPTH + JARVIS_SBT_MAX_BREADTH); end-to-end
    graduation regression suite (~20 tests) covering full Slice 1→4
    pipeline with synthetic ambiguity scenario; ~250 tests / ~2,200
    LOC total across all 5 slices

**Reuse contracts** (zero duplication):
  • READONLY_TOOL_ALLOWLIST from Move 5 (9-tool frozenset,
    AST-pinned)
  • HypothesisProbe's canonical_fingerprint (sha256 dedup pattern)
  • Move 6's ast_canonical for evidence normalization
  • subagent_scheduler primitive (if branches need isolation —
    likely not since SBT is read-only)
  • Tier 1 #3 cross_process_jsonl flock helpers (Slice 4)
  • ide_observability_stream broker (Slice 4 SSE)
  • adaptation.ledger.MonotonicTighteningVerdict (Slice 3 stamping)
  • causality_dag (optional Slice 4 cluster_kind classification)

**Cost contract preserved by construction** (AST-pinned across all
slices): NO providers / doubleword_provider / urgency_router /
candidate_generator / orchestrator / tool_executor / phase_runner /
iron_gate / change_engine / auto_action_router / subagent_scheduler /
semantic_guardian / semantic_firewall / risk_engine imports.
Per-branch cost ≤ Σ(read-only tool costs) — no generation calls.

**Convergence semantics** (tree topology):
  • Each branch produces BranchResult with sha256 fingerprint over
    canonical evidence
  • Branches grouped by fingerprint; majority wins → CONVERGED
  • No majority but ≥2 distinct fingerprints → DIVERGED (operator
    sees the disagreement; tie-breaker spawn deferred to Slice 2
    runner if depth permits)
  • All branches TIMEOUT → TRUNCATED
  • Any branch FAILED while others succeed → INCONCLUSIVE
    (deterministic scoring deferred to Slice 3 comparator if pattern
    repeats)
  • All branches FAILED → FAILED

**Closes the cognitive gap with CC**:
  • CC's interleaved-thinking → SBT's read-only branches with typed
    evidence (structurally safer than free-form thinking; AST-pinned)
  • CC's plan-mode-replan → SBT's tree spawning sub-branches on
    DIVERGED (depth-bounded, structurally safer than open-ended
    replan)
  • CC's speculative branching → SBT's K-parallel branches at ANY
    decision point (Quorum was GENERATE-only, APPROVAL_REQUIRED+
    only)

**Empirical signal Slice 3 produces**: ambiguity_resolution_rate =
converged_trees / total_trees * 100. High value (>70%) → operators
trust SBT's autonomous resolution. Low value (<30%) → ambiguity is
genuinely hard, escalate to operator approval (no auto-mutation).

**Default-true post-graduation matches Priority #1/#2/#3 discipline**
because SBT is read-only over cached evidence (zero LLM cost on the
convergence path; the LLM costs are entirely on the BRANCH-execution
path which is bounded by max_breadth × max_depth × per-tool budget).
The convergence + observability path runs at zero LLM cost.

**Deferred to Slice 5b** (post-graduation operator polish, NOT
blocking): /sbt REPL with subcommands {status, history, baseline,
run} + 4 GET routes + orchestrator hook for autonomous trigger on
Move 5 PROBE_ENVIRONMENT decisions.

**Letter grade target post-Priority-#4**: A− structural / A
empirical / A− edge-case (composite A−). Closes the cognitive gap
identified in §29; remaining gap is long-horizon gradient drift
(Priority #5 CIGW) and Quine-on-BG (Priority #6 Antivenom v2).

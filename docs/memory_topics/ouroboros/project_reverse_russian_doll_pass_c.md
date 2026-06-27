---
title: Reverse Russian Doll — Pass C Design Draft (2026-04-26)
modules: [backend/core/ouroboros/governance/adaptation/ledger.py]
status: merged
source: project_reverse_russian_doll_pass_c.md
---

# Reverse Russian Doll — Pass C Design Draft (2026-04-26)

> *"Anti-Venom (The Constraint): As O+V expands the shell, our deterministic AST-validation and Iron Gate must scale proportionally. It is the immune system ensuring the expanding outer doll does not collapse, hallucinate, or crush the core."*
>
> — Derek J. Russell, operator binding (2026-04-26)

## 0. Status

**STRUCTURALLY COMPLETE 2026-04-26 — all 6 slices shipped same day; defaults still false pending per-slice graduation cadence.** Pass B Slice 1+2 prerequisites met; operator-authorized; full Pass C executed in a single session arc.

Slice landings:
- Slice 1 — `AdaptationLedger` substrate (PR #22801): append-only JSONL audit log + 5-value AdaptationSurface enum + 3-value OperatorDecisionStatus + 2-value MonotonicTighteningVerdict + frozen AdaptationProposal/AdaptationEvidence dataclasses + pluggable per-surface validator registry + monotonic-tightening invariant validator that REFUSES TO PERSIST loosening proposals (cage rule per §4.1). 60 regression tests + sha256 tamper-detect + append-only state-transition pin. `JARVIS_ADAPTATION_LEDGER_ENABLED` default false.
- Slice 2 — `adaptation/semantic_guardian_miner.py` (PR #22821): POSTMORTEM-mined detector pattern proposer. Stdlib-only longest-common-substring synthesizer + group-by-(root_cause, failure_class) + existing-pattern duplicate check + window filter + threshold/window env knobs + idempotent proposal_id (hash of group+pattern). Auto-registers a per-surface validator with the substrate that enforces add_pattern-only kind + sha256-prefix hash + observation_count-above-threshold. 54 regression pins + 114/114 combined with Slice 1. `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` default false. The miner is a PURE function over caller-supplied `PostmortemEventLite` lists — Slice 6 MetaGovernor will wire the actual postmortem source at the right cadence (window-scheduled background analyzer per §4.3).
- Slice 6 — `adaptation/meta_governor.py` (this PR; CLOSES Pass C): operator-facing `/adapt {pending,show,approve,reject,history,stats,help}` REPL dispatcher. 12-status DispatchStatus enum + frozen DispatchResult; mirrors Pass B's `/order2` REPL pattern. compute_stats() aggregator counts pending/approved/rejected per surface + totals from the AdaptationLedger append-only log (latest-record-per-proposal-id reduction). Render helpers: pending list / full proposal detail / history with --surface filter / stats summary. `--surface` filter on history accepts the 5 enum values (validated; INVALID_ARGS for unknown). help bypasses master flag (discoverability). Substrate master-off short-circuit: even with REPL on, returns LEDGER_DISABLED for read+write subcommands. AST-pinned: NO imports of the 4 surface mining modules (each registered its own validator at its own import; substrate stays acyclic). 55 regression pins covering 12-value status enum + DispatchResult frozen + parse_argv + master-off-blocks-except-help + ledger-master-off-blocks-except-help + 6 read-side path matrices (pending empty/populated, show MISSING/NOT_FOUND/OK, history default/custom/invalid/clamp/--surface filter+invalid+missing-arg, stats empty/aggregated, compute_stats direct) + 8 approve paths + 8 reject paths + reason-truncation + reader-raises + end-to-end (mining surface → propose → REPL approve → APPLIED) + 5 authority invariants. Combined regression spine: **349/349 tests green** across all 6 Pass C slices. `JARVIS_ADAPT_REPL_ENABLED` default false. Deferred follow-ups (tracked in module docstring): observability GET endpoints / SSE event emission / weekly background analyzer scheduling / actual gate-state mutation on approve (each surface's `.jarvis/adapted_<surface>.yaml` writer per §6.3/§7.3/§8.4/§9.3 — "/adapt approve" + the activation wirings together close the full adaptation cycle; same split-pattern as Pass B's "/order2 amend" → replay executor).
- Slice 5 — `adaptation/category_weight_rebalancer.py` (PR #22901): the only Pass C surface where the proposal *appears* to lower something — mass-conservation makes it net-tighten. Pure stdlib Pearson correlation kernel (Py 3.9 compat — `statistics.correlation` was 3.10+). Per-category correlation between exploration score and verify_passed binary; identifies high-value (highest-correlation) and low-value (lowest-correlation) categories; if `(high - low) >= JARVIS_ADAPTATION_CORRELATION_DELTA` (default 0.3) AND ≥ JARVIS_ADAPTATION_REBALANCE_THRESHOLD (default 10) ops in window, proposes raise-high (DEFAULT_RAISE_PCT=20%) + lower-low (DEFAULT_LOWER_PCT=10%, hard-floored at JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT=50% of original AND MIN_WEIGHT_VALUE=0.01 absolute). Defensive mass-conservation check at mine-time: if Σ(new) < Σ(old) somehow, refuse to propose. If caller passes lower_pct >= raise_pct (loosening!), miner clamps lower to raise//2. Surface validator: kind=rebalance_weight + sha256-hash + threshold + summary contains BOTH ↑ AND ↓ tokens + summary contains "net +" indicator (defense-in-depth — actual structural mass-conservation pin lives at the miner). Idempotent proposal_id (sha256 of high+low+new_weights vector rounded 6dp). 62 regression pins + 294/294 combined Pass C green. `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` default false.
- Slice 4 — combined per-Order mutation budget + risk-tier ladder extender (PR #22866; per §8 design "combined slice graduates when both sub-surfaces have 5 clean each"). Slice 4a = `per_order_mutation_budget.py`: pure stdlib analyzer of MutationUsageLite events, proposes lowering the per-Order budget when ops consistently used fewer mutations than budgeted (max-observed becomes the safe new budget; Order-2 hard floor MIN_ORDER2_BUDGET=1). Surface validator: kind=lower_budget + sha256-hash + threshold + summary-→. Slice 4b = `risk_tier_extender.py`: pure stdlib analyzer of PostmortemEventLite events, identifies novel failure_class values (not in DEFAULT_KNOWN_FAILURE_CLASSES) accumulating ≥ threshold occurrences, classifies blast_radius into a band (4 bands, HARDENED at 3 levels + CRITICAL at top), synthesizes deterministic tier name (`<insert_after>_<SUFFIX>_<FAILURE_CLASS>` truncated at 64 chars), proposes new tier insertion between two existing tiers (strictly tightening — existing ladder unchanged, only grows). Surface validator: kind=add_tier + sha256-hash + threshold + summary-contains-insert/between. Both sub-surfaces auto-register their validators at module-import. 63 regression pins (4a: 22, 4b: 24, cross-surface: 5, plus 12 shared) + 232/232 combined Pass C green. `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` + `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` both default false.
- Slice 3 — `adaptation/exploration_floor_tightener.py` (PR #22841): IronGate exploration-floor auto-tightener. Pure stdlib analyzer of (exploration-score, verify-outcome) tuples per op. Detects bypass-failures (floor_satisfied=True AND verify_outcome IN {regression, failed}) → identifies weakest category by per-op argmin → proposes 10% raise per cycle (bounded; capped at MAX_FLOOR_RAISE_PCT=100). `compute_proposed_floor(current, pct=10)` is the testable math kernel: ceil-based + min_nominal_raise=1 + handles zero/negative current. Threshold default 5 (higher than Slice 2's 3 — floor-raise has broader impact). Auto-registers a surface validator: kind=raise_floor + sha256-prefix hash + observation_count-above-threshold + summary-contains-→-indicator (defense against doctored proposals). Idempotent proposal_id (sha256 of category + current + proposed floor). 55 regression pins + 169/169 combined Pass C green. `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` default false.
- Slices 4-6 pending.

## 1. Problem statement (recap from Pass A §23.6)

Today's immune system is uniformly *static*: every Anti-Venom layer (Iron Gate exploration-first, ASCII strict, multi-file coverage; SemanticGuardian's 10 hand-written patterns; SemanticFirewall's 11 detectors + 5 credential shapes; `ScopedToolBackend` mutation budget; risk-tier ladder; ExplorationLedger category floors) is calibrated at code-time. Patterns are hand-written. Floors are env-tuned. Budgets are operator-set.

The Anti-Venom thesis is that **as O+V's outward (Order-1) reach grows, the inward (Order-2) cage must scale proportionally** — the system that wrote 4 test files in Sessions U–W (2026-04-15) creates new attack surfaces the Iron Gate didn't know about when it was built. If gates don't learn, the cage gets relatively weaker as the shell expands. Eventually the cage collapses or the shell hallucinates through it.

Pass C designs how each gate grows adaptive without ever loosening itself.

## 2. Scope

**In scope (Pass C):**
- The `AdaptationLedger` substrate — append-only audit log of every gate calibration change, queryable via REPL + observability stream
- **Five adaptive surfaces**, one per Anti-Venom thesis bullet from §23.6:
  1. SemanticGuardian patterns mined from POSTMORTEM corpus (failure → new detector)
  2. Iron Gate exploration-floors auto-tightening on category-bypass detection
  3. Mutation budget calibration **per Order** (Order-1 vs Order-2)
  4. Risk-tier ladder extension on new attack-surface emergence
  5. ExplorationLedger category-weight rebalancing on diminishing-marginal-value
- The `MetaAdaptationGovernor` — cross-cutting controller enforcing the monotonic-tightening invariant and providing the operator-visibility surface
- Hot-revert path per adaptive surface (mirrors W2(5) and Pass B)

**Out of scope (future, no Pass yet):**
- Subagent-kind generation (Pass A Gap 2.4) — still hand-written
- Cross-Trinity adaptation *enforcement* — design is Trinity-aware (`AdaptationLedger` is `(repo, surface)`-keyed) but initial deployment is Body-only, mirroring Pass B
- ML-based pattern induction — the cage stays zero-LLM (§4.4)
- Adaptive *response* to attacks — Pass C is adaptive *prevention*; incident-response automation is a separate concern
- Self-modification of *how* gates adapt — that is itself an Order-2 governance change going through Pass B amendment protocol

## 3. The five adaptive thesis claims, sharpened

| Bullet (§23.6) | Concrete operationalization | Slice |
|---|---|---|
| SemanticGuardian patterns mined from POSTMORTEM corpus | Deterministic rule-based extractor reads POSTMORTEM JSON; on N occurrences of a `root_cause` shape that doesn't match any existing detector, proposes a new detector to the AdaptationLedger; operator approves before activation | Slice 2 |
| Iron Gate exploration-floors auto-tightening on category-bypass | When the ExplorationLedger detects an op that satisfied the floor in 4-of-5 categories but failed VERIFY anyway, the floor for the missing category is proposed for tightening; operator approves | Slice 3 |
| Mutation budgets calibrated per-Order | Order-1 ops keep current `ScopedToolBackend` budget; Order-2 ops (manifest hits) get a much smaller budget (initial proposal: 1, vs. current Order-1 norm of 5–10); per-Order budget is itself adaptive on observed Order-2 op count | Slice 4 |
| Risk-tier ladder extension on new attack surfaces | When a novel postmortem `failure_class` accumulates ≥ N occurrences, MetaAdaptationGovernor proposes a new tier slot between existing tiers (e.g., `NOTIFY_APPLY_HARDENED` between `NOTIFY_APPLY` and `APPROVAL_REQUIRED` for a specific shape); operator approves | Slice 5 |
| ExplorationLedger category-weight rebalancing on diminishing-marginal-value | When category X scores have not correlated with verify-pass for ≥ N ops, the category weight is proposed for reduction *only after raising another category's weight* (net mass conservation; total cage strictness never drops) | Slice 5 (combined) |

Note: every bullet ends with **"operator approves"**. Pass C does not introduce autonomous adaptation. It introduces **adaptation proposals** that traverse the same operator-only amendment protocol Pass B established for the Order-2 manifest.

## 4. Core design principles

### 4.1 Monotonic-tightening invariant

**Adaptive gates may only become more strict, never less.** This is the load-bearing safety claim. Concretely:

- A new SemanticGuardian pattern can be added; existing patterns cannot be removed via adaptation
- An ExplorationLedger floor can be raised; floors cannot be lowered via adaptation
- A mutation budget can be reduced; budgets cannot be raised via adaptation
- A new risk tier can be added between existing tiers; existing tiers cannot be removed via adaptation
- A category weight can rise; weight reductions are admissible only when paired with an offsetting rise (mass conservation, §3 row 5)

Violation of monotonic-tightening is structurally impossible — the `MetaAdaptationGovernor` (§9) AST-validates every proposal against the invariant before it reaches the operator queue. Any *loosening* operation (deprecating a detector, lowering a floor, raising a budget, removing a tier) requires going through Pass B's manifest-amendment protocol — it is by definition an Order-2 governance change.

### 4.2 All loosening goes through Pass B

This is the dual to §4.1. The Pass B manifest-amendment protocol (`/order2 amend <op-id>`) is the **only** mutation surface for *loosening* the immune system. Pass C cannot loosen via any path. Even an operator-approved Pass C "adaptation" that turned out to be wrong gets rolled back through Pass B, not through Pass C.

This means: Pass C is the *grow-stricter* surface; Pass B is the *grow-stricter-or-loosen-with-explicit-authority* surface. They compose.

### 4.3 Bounded adaptation rate (windowed, not per-op)

Gates do not adapt at op time. They observe at op time and adapt at window time. Pass C uses a **weekly adaptation window** as the default cadence, configurable via `JARVIS_ADAPTATION_WINDOW_DAYS` (default 7). Per-op overhead of Pass C: zero — adaptation proposals run as a scheduled background analysis, not in the hot path.

This decouples cage strictness from session noise. A single anomalous op cannot tighten the cage; it takes a *pattern* across a window.

### 4.4 Zero-LLM in the cage

The cage stays deterministic. Pass C extracts patterns via:
- POSTMORTEM JSON shape-matching (`root_cause`, `failure_class`, `error_type` fields)
- Time-series analysis of ExplorationLedger category scores vs. verify-pass outcomes
- Frequency analysis of `failure_class` distributions

No LLM call inside the gate logic. No generative pattern induction. The deterministic-cage promise from `SemanticFirewall` and `SemanticGuardian` extends to Pass C — only existing GENERATE / Venom paths use LLMs, and Pass C does not run during GENERATE.

### 4.5 Operator-visible adaptation log (append-only)

Every adaptation proposal — accepted, rejected, or pending — lands in `.jarvis/adaptation_ledger.jsonl` (append-only, never rewritten). Schema:

```json
{
  "schema_version": "1.0",
  "proposal_id": "adapt-019d…",
  "surface": "semantic_guardian.patterns" | "iron_gate.exploration_floors" | "scoped_tool_backend.mutation_budget" | "risk_tier_floor.tiers" | "exploration_ledger.category_weights",
  "proposal_kind": "add_pattern" | "raise_floor" | "lower_budget" | "add_tier" | "rebalance_weight",
  "evidence": {
    "window_days": 7,
    "observation_count": 12,
    "source_event_ids": ["postmortem-…", "…"],
    "summary": "..."
  },
  "current_state_hash": "sha256:...",
  "proposed_state_hash": "sha256:...",
  "monotonic_tightening_verdict": "passed" | "rejected:would_loosen",
  "operator_decision": "pending" | "approved" | "rejected",
  "operator_decision_at": "...",
  "operator_decision_by": "...",
  "applied_at": "..." (only on approve),
  "rollback_via": "pass_b_manifest_amendment" (always)
}
```

Three operator surfaces:
- REPL: `/adapt {pending,show,approve,reject,history,stats}` (mirrors Pass B's `/order2`)
- Observability GET: `/observability/adaptations{,/<proposal_id>}`
- SSE: `adaptation_proposed` / `adaptation_approved` / `adaptation_rejected` / `adaptation_applied`

### 4.6 Shadow-mode-first graduation discipline

Mirrors W2(5) and Pass B. Every adaptive surface ships behind a per-slice env flag defaulting `false`. Per-slice graduation requires N clean sessions where the proposal-stream observed in shadow mode would not have produced false-positive proposals (proposals that the operator rejects on review). Initial cadence: **5 clean sessions per slice** (vs. Pass B's 3, because Pass C is more architecturally novel and the false-positive cost is operator-attention burn).

## 5. Slice 1 — AdaptationLedger substrate (foundational)

### 5.1 Deliverable

`backend/core/ouroboros/governance/adaptation/ledger.py` — the universal substrate every other Pass C slice writes to.

```python
@dataclass(frozen=True)
class AdaptationProposal:
    proposal_id: str
    surface: AdaptationSurface  # enum: 5 values per §3
    proposal_kind: str
    evidence: AdaptationEvidence  # bounded JSON shape
    current_state_hash: str  # sha256 of current gate state
    proposed_state_hash: str  # sha256 of proposed gate state
    monotonic_tightening_verdict: Literal["passed", "rejected:would_loosen"]
    proposed_at: str  # ISO8601
    operator_decision: Literal["pending", "approved", "rejected"]
    operator_decision_at: Optional[str]
    operator_decision_by: Optional[str]
    applied_at: Optional[str]

class AdaptationLedger:
    def propose(self, proposal: AdaptationProposal) -> str: ...
    def approve(self, proposal_id: str, by: str) -> None: ...  # no-op if rejected/applied
    def reject(self, proposal_id: str, by: str) -> None: ...
    def list_pending(self) -> tuple[AdaptationProposal, ...]: ...
    def history(self, surface: Optional[AdaptationSurface] = None) -> tuple[AdaptationProposal, ...]: ...
    def state_at(self, surface: AdaptationSurface, ts: str) -> dict: ...  # replay any past state
```

### 5.2 Authority invariants

- Append-only file (`.jarvis/adaptation_ledger.jsonl`); never rewritten
- `propose()` must call `_validate_monotonic_tightening()` before write — fails loudly if violated
- `approve()` is the ONLY path that flips `applied_at` non-null and mutates the gate's actual state
- `reject()` lands the proposal as terminal; it does not delete or hide
- AST-grep CI invariant: any import of `AdaptationLedger` outside `adaptation/` and the 5 adaptive-surface modules is a CI failure

### 5.3 Env flag

`JARVIS_ADAPTATION_LEDGER_ENABLED` (default `false`). When off: ledger does not load, no proposals get written, no REPL surface; behavior identical to today's static cage.

### 5.4 Graduation criteria

5 clean sessions where ledger loads at boot, accepts shadow-mode proposals from §6–§9, and never violates the append-only invariant. No actual gate state change in shadow mode (Slice 1 is substrate only).

## 6. Slice 2 — SemanticGuardian POSTMORTEM-mined patterns

### 6.1 The mining surface

`adaptation/semantic_guardian_miner.py` — a scheduled background analyzer that runs at the adaptation window cadence (§4.3, default 7 days). For each POSTMORTEM event in the window:

1. Extract `root_cause`, `failure_class`, `error_type`, and the specific `code_snippet_excerpt` field if present.
2. Group events by `(root_cause, failure_class)` tuple.
3. For each group with `count >= JARVIS_ADAPTATION_PATTERN_THRESHOLD` (default 3):
   - Synthesize a candidate detector pattern (regex or AST shape) from the common substring/structure of the `code_snippet_excerpt` fields
   - Verify the candidate pattern doesn't already match any existing SemanticGuardian pattern (avoid duplicates)
   - Emit `AdaptationProposal(surface=semantic_guardian.patterns, proposal_kind=add_pattern, ...)`

### 6.2 Why deterministic, not LLM-based

The pattern synthesis uses **longest common substring** (regex case) or **AST template matching** (structural case). These are stdlib-only deterministic operations. An LLM-based synthesizer would be more powerful but would breach the §4.4 zero-LLM invariant — no path. If the LCS approach proves too narrow over time, the operator can extend the synthesizer module via a normal Pass B Order-2 amendment (it's governance code).

### 6.3 The detector activation path

Approved patterns land in `.jarvis/adapted_guardian_patterns.yaml` (loaded by SemanticGuardian on next boot, alongside the hand-written pattern set). At boot, SemanticGuardian merges:
- Hand-written patterns (`semantic_guardian.py:_PATTERNS`)
- Adapted patterns from yaml (only those with `applied_at` set)

The merge rule: **adapted patterns are additive, never substitutive.** No adapted pattern can disable a hand-written pattern. If an adapted pattern is later proven counterproductive, it is removed via Pass B manifest amendment, not via Pass C.

### 6.4 Env flags

- `JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED` (default `false`) — master gate
- `JARVIS_ADAPTATION_PATTERN_THRESHOLD` (default 3) — minimum group size to propose
- `JARVIS_ADAPTATION_WINDOW_DAYS` (default 7) — analyzer cadence (shared across all slices)

### 6.5 Graduation criteria

5 clean sessions where:
- Miner runs at window cadence without raising
- Proposals it generates pass the §4.1 monotonic-tightening invariant
- Operator review of generated proposals shows ≥ 80% acceptance rate (false-positive cost low enough to justify keeping the surface)

## 7. Slice 3 — Iron Gate exploration-floor auto-tightening

### 7.1 The bypass-detection surface

`adaptation/exploration_floor_tightener.py` — analyzes ExplorationLedger entries vs. VERIFY outcomes. For each op in the adaptation window:

1. Read the per-category exploration scores at GENERATE time
2. Read the VERIFY outcome (pass / regression / L2-recovered / failed)
3. Group ops where `floor_satisfied=True AND verify_outcome IN {regression, failed}` — i.e., the op passed the exploration gate but the patch was wrong
4. For these ops, identify the *weakest* category (lowest score among the 5) — this is the candidate for floor tightening
5. If `weakest_category_count >= JARVIS_ADAPTATION_FLOOR_THRESHOLD` (default 5) within the window:
   - Compute a proposed new floor: `current_floor + ceil((current_floor * 0.1))` (bounded 10% raise per cycle)
   - Emit `AdaptationProposal(surface=iron_gate.exploration_floors, proposal_kind=raise_floor, ...)`

### 7.2 Why bounded 10%

Adaptation must not whiplash. A 10% per-cycle raise gives the operator visibility-and-veto for each tightening step rather than a single large jump. Across 7-day windows, the cage tightens at most ~10% per category per week — enough to track shell expansion, not enough to catastrophically over-correct.

### 7.3 Activation path

Approved floor changes land in `.jarvis/adapted_iron_gate_floors.yaml`. At GENERATE time, the ExplorationLedger reads max(env_floor, adapted_floor) — the static env-tuned floor stays as a hard lower bound; adapted floors can only raise above it.

### 7.4 Env flags

- `JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED` (default `false`)
- `JARVIS_ADAPTATION_FLOOR_THRESHOLD` (default 5)
- `JARVIS_ADAPTATION_FLOOR_RAISE_PCT` (default 10)

### 7.5 Graduation criteria

5 clean sessions where the tightener proposals correlate with operator-approved category extensions (≥ 70% acceptance rate is the criterion — slightly lower than Slice 2 because some bypass events are legitimate noise).

## 8. Slice 4 — Per-Order mutation budgets + risk-tier ladder extension

### 8.1 Per-Order mutation budget surface

`adaptation/per_order_mutation_budget.py` — distinct from Slice 5 because it operates on `ScopedToolBackend` directly.

The current `ScopedToolBackend._mutations_count` budget is a single value (env-tuned via `JARVIS_GENERAL_MAX_MUTATIONS`, default 5–10 depending on subagent kind). Pass C splits this into:

- `JARVIS_ORDER1_MAX_MUTATIONS` (default = current value)
- `JARVIS_ORDER2_MAX_MUTATIONS` (default 1 — proposed initial value, far stricter)

The split is deterministic at op classification time (GATE phase, after Pass B's manifest classifier runs):
- If `risk_tier == ORDER_2_GOVERNANCE`: budget = `JARVIS_ORDER2_MAX_MUTATIONS`
- Else: budget = `JARVIS_ORDER1_MAX_MUTATIONS`

The adaptation surface here proposes **lowering** `JARVIS_ORDER2_MAX_MUTATIONS` if Order-2 ops are observed using fewer mutations than budgeted (over-budget waste = wasted attack surface).

### 8.2 Risk-tier ladder extension surface

`adaptation/risk_tier_extender.py` — analyzes POSTMORTEM events for `failure_class` values that don't fit cleanly into the current 4-tier ladder. Specifically:

1. For each POSTMORTEM in the window, extract `failure_class`
2. Group by `failure_class` — known classes map to tiers; unknown classes are candidates
3. For each unknown class with `count >= JARVIS_ADAPTATION_TIER_THRESHOLD` (default 5):
   - Determine the appropriate ladder slot (heuristic: severity score from `blast_radius` field) → propose insertion between two existing tiers
   - Emit `AdaptationProposal(surface=risk_tier_floor.tiers, proposal_kind=add_tier, ...)`

### 8.3 Why a ladder *extension*, not a *replacement*

Replacing existing tiers would loosen — that's a Pass B amendment. Inserting a new tier between two existing ones strictly tightens (an op that previously got `NOTIFY_APPLY` might now get the new `NOTIFY_APPLY_HARDENED` if it matches the new failure class). Existing tier behavior is preserved for ops not matching the new class.

### 8.4 Activation path

Approved budget changes land as env-flag override in `.jarvis/adapted_risk_floors.yaml`. The risk-tier-floor composition rule (current: 3 knobs, strictest wins) is extended to a 4th knob (adapted_floor), still strictest-wins. This means: even if `JARVIS_PARANOIA_MODE=0` and `JARVIS_MIN_RISK_TIER=safe_auto`, the adapted floor still applies. This is correct — adaptation is one-way tighten-only, and a less-strict env config cannot accidentally loosen what adaptation has tightened.

Approved tier extensions land in `.jarvis/adapted_risk_tiers.yaml`, loaded at boot by `risk_tier_floor.py` and merged into the canonical ladder enum.

### 8.5 Env flags

- `JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED` (default `false`)
- `JARVIS_ORDER1_MAX_MUTATIONS` (default = current `JARVIS_GENERAL_MAX_MUTATIONS`)
- `JARVIS_ORDER2_MAX_MUTATIONS` (default 1)
- `JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED` (default `false`)
- `JARVIS_ADAPTATION_TIER_THRESHOLD` (default 5)

### 8.6 Graduation criteria

5 clean sessions per sub-surface (budget + ladder), tracked separately. Combined slice graduates when both sub-surfaces have 5 clean each.

## 9. Slice 5 — ExplorationLedger category weighting auto-rebalance

### 9.1 The rebalance surface

`adaptation/category_weight_rebalancer.py` — the only Pass C surface where the proposal *appears* to lower something. Mass conservation makes it net-tighten.

For each category in the 5-category ExplorationLedger:
1. Compute correlation between category score and verify-pass outcome over the window
2. Identify the category with highest correlation (high-value) and lowest correlation (low-value)
3. If `(high_correlation - low_correlation) >= JARVIS_ADAPTATION_CORRELATION_DELTA` (default 0.3):
   - Propose: raise high-value category weight by X%, lower low-value category weight by Y% (Y < X — net total weight rises)
   - Emit `AdaptationProposal(surface=exploration_ledger.category_weights, proposal_kind=rebalance_weight, ...)`

### 9.2 Why this is monotonic-tightening

Mass conservation: `Σ(new_weights) ≥ Σ(old_weights)`. The net cage strictness rises (via the X-Y delta). The high-value category becomes a stricter floor; the low-value category becomes lower-priority but does not vanish (lowest allowed weight: 50% of original, hard-floored).

The §4.1 invariant validator checks `Σ(new_weights) ≥ Σ(old_weights) AND min(new_weights) >= 0.5 * min(old_weights)` — both must hold.

### 9.3 Activation path

Approved weights land in `.jarvis/adapted_category_weights.yaml`, loaded by the ExplorationLedger at boot.

### 9.4 Env flags

- `JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED` (default `false`)
- `JARVIS_ADAPTATION_CORRELATION_DELTA` (default 0.3)
- `JARVIS_ADAPTATION_WEIGHT_FLOOR_PCT` (default 50 — minimum % of original)

### 9.5 Graduation criteria

5 clean sessions; rebalances proposed must pass the mass-conservation + floor-percent invariant 100% (no false proposals reach the operator queue).

## 10. Slice 6 — MetaAdaptationGovernor + Pass C graduation matrix

### 10.1 The cross-cutting controller

`adaptation/meta_governor.py` — the single component that:

- Coordinates the 5 adaptive surfaces (Slices 2–5; Slice 1 is substrate)
- Enforces §4.1 monotonic-tightening before any proposal reaches the AdaptationLedger
- Provides the `/adapt` REPL surface (§4.5)
- Provides the `/observability/adaptations` GET endpoints
- Wires SSE event emission for the 4 adaptation event types
- Owns the weekly background analyzer scheduling (§4.3)

### 10.2 The full-revert matrix

| Trigger | Action |
|---|---|
| `JARVIS_ADAPTATION_LEDGER_ENABLED=false` | Master kill — entire Pass C goes dark; ledger doesn't load; no proposals; no REPL; no SSE |
| Any Slice X env flag flipped to `false` | That slice's adaptive surface goes dark; other 4 unaffected; existing applied adaptations remain in effect (their roll-back is via Pass B) |
| Single-proposal rollback | `/adapt rollback <proposal_id>` triggers a Pass B manifest amendment to restore prior state — adaptation is one-way; the rollback uses the loosening protocol |
| Total Pass C revert | Set master flag false + delete `.jarvis/adapted_*.yaml` files + Pass B manifest amendment to restore static gate state for any applied adaptations |

### 10.3 Pass C graduation matrix

Pass C does not have a single graduation event. Each slice graduates independently. The Pass C *arc closure* is declared when:
- Slices 1–5 all individually graduated (defaults flipped per-slice)
- Slice 6 (meta-governor + matrix) graduated
- 30-day soak period showing ≥ 70% operator-approval rate across all 5 surfaces (proves false-positive cost is sustainable)
- Battle-test landmark: at least one adaptation cycle proven end-to-end (proposal → operator-approve → applied → measurable outcome)

### 10.4 Env flag

`JARVIS_ADAPTIVE_META_GOVERNOR_ENABLED` (default `false`). Slice 6 also owns the master `JARVIS_ADAPTATION_LEDGER_ENABLED` (Slice 1's flag); they ship co-graduating.

## 11. Boundaries (what Pass C does NOT do)

1. **No autonomous adaptation.** Every proposal goes to operator queue. Pass C generates proposals; humans approve them. There is no "auto-approve at threshold" pathway, by design.
2. **No loosening.** Pass C cannot remove a pattern, lower a floor, raise a budget, delete a tier, or shrink a weight beyond the mass-conservation floor. Loosening is a Pass B amendment.
3. **No ML.** The 5 mining/analysis modules use stdlib + `numpy` only. Longest-common-substring, AST template matching, time-series correlation, frequency analysis. No LLM, no embedding model, no learned classifier.
4. **No per-op overhead.** Adaptation analysis runs at window cadence (default 7 days), not per-op. Hot-path cost: zero.
5. **No flag flips at draft time.** All Slice flags ship `false` until per-slice graduation. Pass C is design-only until Pass B Slice 1 graduates.
6. **No cross-Trinity logic.** AdaptationLedger schema is `(repo, surface)`-keyed and Trinity-extensible from day one (mirrors Pass B), but initial deployment is Body-only.
7. **No self-modification of adaptation logic.** The `meta_governor.py` and 5 surface modules are themselves Order-2 governance code. Modifying them is a Pass B amendment, not a Pass C adaptation.

## 12. Dependencies + sequencing

**Hard prerequisites** (Pass C Slice 1 cannot start until):
1. Pass B Slice 1 graduated (`Order2Manifest` schema exists; the manifest itself is in the manifest, so Pass C's adapted-yaml files can be added to it).
2. Pass B Slice 2 graduated (`ORDER_2_GOVERNANCE` risk class exists; Pass C Slice 4's per-Order budget classifier reads it).
3. Operator authorization to begin Pass C Slice 1 (separate from Pass C drafting).

**Soft prerequisites**:
- Pass B Slice 6 (manifest-amendment protocol) graduated — Pass C's rollback path uses it. If not yet graduated, Pass C still works but rollbacks are deferred until Pass B Slice 6 lands.

**Sequencing within Pass C**:
- Slice 1 (AdaptationLedger substrate) is a hard prerequisite for Slices 2–6
- Slices 2, 3, 4, 5 are independent of each other and can graduate in any order
- Slice 6 (MetaGovernor) depends on Slices 1–5 each existing in shadow mode

**Forward dependency** (Pass D, hypothetical):
- A future "Pass D — adaptation-of-adaptation" (the meta-meta-cage) is not currently in scope. If/when it becomes relevant, it has the same relationship to Pass C that Pass B has to the immune system: any change to *how* gates adapt is itself an Order-2 governance change, going through Pass B. This is structurally bounded by the §4 principles and does not require new architecture.

## 13. Open design questions (deliberate, for operator decision)

1. **Adaptation-window cadence default.** §4.3 proposes 7 days. Too slow for fast-moving systems (a new attack surface opens; cage takes a week to notice). Too fast might over-correct on noise. Operator preference; default is mutable via `JARVIS_ADAPTATION_WINDOW_DAYS`.

2. **Operator-approval-rate graduation threshold.** §10.3 proposes ≥ 70% acceptance over 30 days as the Pass C arc-closure criterion. Higher threshold (e.g., 85%) would mean less false-positive operator burn but might never graduate; lower (50%) might graduate Pass C with too-noisy proposals. 70% is the draft compromise.

3. **`JARVIS_ORDER2_MAX_MUTATIONS` initial value.** §8.1 proposes 1. This is aggressive — it means Order-2 patches can mutate exactly one file per op. Alternative: 2 (allow main change + companion test). Battle-test data on Order-2 op characteristics would inform; we don't have that data yet. Operator decision pre-graduation.

4. **POSTMORTEM corpus retention for mining.** Slice 2 mines from POSTMORTEM events in the window. POSTMORTEM events older than the window are not consulted. Should there be a longer-horizon corpus (e.g., 90 days) for *trend* detection, separate from the window-based mining? Pass C v1 says no (avoid scope creep); Pass C v2 might add it.

5. **Adapted-pattern-yaml manifest entry.** The 5 yaml files (`adapted_guardian_patterns.yaml`, etc.) need to be in the Order-2 manifest themselves (since they contain governance code state). Confirm Pass C Slice 1 includes a Pass B manifest amendment that adds `(jarvis, .jarvis/adapted_*.yaml)` to the manifest — otherwise Pass B's cage won't apply to Pass C's outputs.

## 14. Interaction with existing memory + PRD

- **`project_reverse_russian_doll_pass_a.md`**: Pass A surfaced Gap 2.3 ("Anti-Venom is static, not adaptive") with the 5-bullet thesis. Pass C operationalizes that thesis. The mapping table in Pass A §3 (every static gate location) is the implementation surface for Pass C.
- **`project_reverse_russian_doll_pass_b.md`**: Pass C is a downstream consumer. Every Pass C "loosening" goes through Pass B's manifest-amendment protocol; every adapted-state yaml is itself a manifest entry; Pass C Slice 4 reads Pass B's `ORDER_2_GOVERNANCE` risk class.
- **`OUROBOROS_VENOM_PRD.md` §23.6**: The 5-bullet adaptive thesis lives there; this Pass C doc is the design that makes those bullets concrete.
- **`OUROBOROS_VENOM_PRD.md` §23.10**: Pass C is referenced as "deferred; depends on Pass B existing." This memory file IS that deferred design now lifted out of "to be drafted."
- **`project_iron_gate_pushB.md`**: Iron Gate gates as they exist today. Pass C Slice 3 adapts these gates via the auto-tightening floor surface; existing gates stay authoritative — adapted floors can only raise.
- **`project_phase_b_subagent_roadmap.md`**: Phase B subagents are Order-2 governance code. Pass C does not touch their contracts; subagent-kind generation is out of scope.

## 15. Vocabulary landing (post-graduation, separate work item)

When Pass C arc-closes (§10.3 criteria all met), `OUROBOROS_VENOM_PRD.md` §23.6 should be updated:
- Replace the "today static" table with a "today adaptive (auto-tightening)" table
- Move the 5-bullet thesis from "future Pass C scope" to "delivered: see Pass C arc closure"
- Add a §23.6.1 "Adaptive surface inventory" listing the 5 graduated surfaces with their AdaptationLedger surface enum values
- Add a §23.6.2 "Operator-approval rates (rolling 30 days)" — the live evidence the cage is functioning

The PRD update is a post-graduation work item, not a Pass C deliverable.

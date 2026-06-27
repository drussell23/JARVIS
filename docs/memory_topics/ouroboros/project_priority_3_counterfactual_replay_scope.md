---
title: Project Priority 3 Counterfactual Replay Scope
modules: [backend/core/ouroboros/governance/verification/counterfactual_replay.py, backend/core/ouroboros/governance/verification/counterfactual_replay_engine.py, backend/core/ouroboros/governance/verification/counterfactual_replay_comparator.py, backend/core/ouroboros/governance/verification/counterfactual_replay_observer.py]
status: merged
source: project_priority_3_counterfactual_replay_scope.md
---

**Scope status: DRAFT** (post-Priority-#2-closure 2026-05-01,
authorized for execution per architectural directive). After
commits `bb2a392707` + `8437eb9b20`/`f431e66b8e` + `03aa0628b4`
+ `1f076deb85` + `cc2b025bfb`/`4cff1697b0` (Priority #2 full
arc), the structural ceiling sits at **A**, empirical floor at
**A−**. Priority #3 is the policy-evaluation primitive that
converts Priority #1 + Priority #2's detection + prevention
substrate into measurable empirical evidence — closing the loop
from "we can detect drift" to "we can prove the prevention
mechanism reduces it."

## Why Priority #3 (architectural justification)

§28.9 brutal review identified the **policy-evaluation gap**:

  * Priority #1 detects behavioral drift (RECURRENCE_DRIFT
    among 6 kinds).
  * Priority #2 prevents recurrence (cross-session prompt
    injection + recurrence boost).
  * **Both are graduated default-true; both are wired-and-
    operational.**
  * **What's missing**: the ability to *measure* whether
    prevention actually reduces recurrence empirically. Today
    operators can only observe correlation: "after PostmortemRecall
    shipped, recurrences went down." That's not proof —
    confounders abound.

Priority #3 closes this with **counterfactual A/B replay**:
re-run every recorded session WITH and WITHOUT a chosen policy
override. Cached generation hashes from Phase 1's `phase_
capture` mean ZERO LLM cost — the experiment is purely
deterministic.

**The mechanism**:

```
replay_session(session_id, *, swap_at_phase=PostmortemInjection,
               swap_decision_to=DISABLED) → counterfactual_branch

compare(original_branch, counterfactual_branch) →
    BranchVerdict.DIVERGED_BETTER  (original was better)
  | BranchVerdict.DIVERGED_WORSE   (counterfactual was better)
  | BranchVerdict.EQUIVALENT       (no measurable delta)
```

After N sessions:
  * Aggregate: `% sessions where DIVERGED_BETTER` = empirical
    prevention rate
  * If significant (operator-tunable threshold): Move 6 master
    flag graduates default-true with evidence
  * If insignificant: PostmortemRecall calibration knob (top-K,
    halflife, threshold) gets adjusted via Phase C
    MetaAdaptationGovernor

**Why this is THE next priority** (per §28.9 critical path):

  * Substrate exists: Phase 1 Determinism + Causality DAG +
    `--rerun-from` are shipped. The policy-swap path is the
    only missing piece.
  * Compounds with Priority #1 + Priority #2 directly: replay
    measures their prevention delta empirically.
  * Cost contract: ZERO LLM cost (cached hashes). No K×
    amplification. No risk of pathological cost.
  * Move 6 master graduation is bottlenecked on empirical
    evidence — Priority #3 produces it.
  * Anti-Venom mathematical auditability: replay every blocked
    op with relaxed gate, prove the gate didn't false-positive.

## Existing infrastructure to leverage (NO duplication)

The substrate is mostly already shipped — Priority #3 EXTENDS
the policy-swap path, never duplicates:

| Existing | Reuse via | Slice |
|---|---|---|
| **Phase 1 Determinism — `phase_capture`** | Per-phase Merkle nodes (`.ouroboros/sessions/<id>/phases/`) are the source-of-truth replay. Slice 1 reads the existing Merkle node format. | Slice 1 |
| **Causality DAG (`Priority 2`)** | Session-spanning navigable graph + `--rerun-from` CLI. Slice 2 extends the replay path with policy-override injection. | Slice 2 |
| **`record_decision` / `record_provider_call` / `record_phase`** existing capture API | Slice 1's primitive reads these via the canonical accessors; doesn't reimplement | Slice 1 |
| **AdaptationLedger.MonotonicTighteningVerdict** | Phase C canonical strings. When replay produces a counterfactual that would have *tightened* policy, branch verdict stamps `PASSED`. Operators correlate replay outcomes with Pass C ledger entries. | Slice 3 |
| **Cross-process flock** (Tier 1 #3) | `.jarvis/replay_history.jsonl` + `.jarvis/replay_audit.jsonl` follow Move 4 / Priority #1 / Priority #2 disciplines | Slice 4 |
| **Priority #1 Coherence Auditor** | Replay outcomes feed the `behavioral_drift_detected` SSE event for cross-arc correlation | Slice 4 (extension hook) |
| **Priority #2 PostmortemRecall** | Primary subject of replay: "with vs without recall injection." Replay produces empirical data; PostmortemRecall consumes drift detection into prevention. The two compose into a closed loop. | Slice 3 |
| **Priority #2 RecurrenceBoost** | Boost activation events become a replay swap target (replay with `swap_decision_to=NoBoost`). | Slice 3 |
| **`auto_action_router`** (Move 3) | Replay verdicts may produce advisory records via the existing routing surface | Slice 4 |
| **FlagRegistry seed pattern** | 6 FlagSpec entries (master + 3 sub-gates + 2 cap knobs) | Slice 5 |
| **shipped_code_invariants registry** | 4 new AST pins (36+4=40 post-Priority-3) | Slice 5 |
| **EventChannelServer SSE broker** | `EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE` lazy-publish | Slice 4 |

What Priority #3 BUILDS:

  * **Replay primitive** — pure data + closed-enum decisions over
    a recorded session
  * **Policy-override injection** — extension to `phase_capture
    .replay()` that swaps a decision at a chosen phase
  * **Branch comparator** — pure decision over (original_branch,
    counterfactual_branch) producing `BranchVerdict`
  * **Replay history store** — append-only audit log of replay
    outcomes (cross-process flock'd)
  * **SSE event** — fires on replay completion for cross-arc
    correlation
  * **Cost-correctness proof** — replay uses cached generation
    hashes, NEVER triggers fresh LLM calls (AST-pinned)

## The 5-slice arc

### Slice 1 — Replay primitive (pure data + decisions)

**New module**: `verification/counterfactual_replay.py`

* Frozen dataclasses (J.A.R.M.A.T.R.I.X. closed-enum
  discipline):
  - `ReplayTarget(session_id, swap_at_phase, swap_decision_kind,
    swap_decision_value, max_replay_seconds, schema_version)` —
    bounded query
  - `BranchSnapshot(branch_id, phase_results, terminal_phase,
    apply_outcome, postmortem_records, ops_summary,
    schema_version)` — frozen result of one branch (original or
    counterfactual)
  - `ReplayVerdict(outcome, original_branch, counterfactual_branch,
    verdict, divergence_phase, divergence_reason,
    recorded_at_ts, source_advisory_id, schema_version)` —
    aggregate result

* 5-value `ReplayOutcome` closed enum:
  - `SUCCESS`   — both branches replayed cleanly; verdict
                  computed
  - `PARTIAL`   — one branch replayed; the other failed at a
                  recoverable phase
  - `DIVERGED`  — branches diverged structurally before
                  swap_at_phase (cached hash mismatch — replay
                  is non-deterministic for this session)
  - `DISABLED`  — master flag off
  - `FAILED`    — defensive sentinel (corrupt phase_capture,
                  unknown swap target, etc)

* 5-value `BranchVerdict` closed enum:
  - `EQUIVALENT`        — no measurable delta in terminal
                          outcome
  - `DIVERGED_BETTER`   — original branch had better terminal
                          outcome (success without
                          counterfactual policy)
  - `DIVERGED_WORSE`    — counterfactual branch had better
                          terminal outcome (success WITHOUT
                          original policy — flag candidate for
                          Phase C tightening)
  - `DIVERGED_NEUTRAL`  — branches differ but neither is
                          unambiguously better
  - `FAILED`            — defensive sentinel

* 5-value `DecisionOverrideKind` closed enum:
  - `GATE_DECISION`           — risk-tier gate verdict
                                (e.g., notify_apply →
                                approval_required)
  - `POSTMORTEM_INJECTION`    — Priority #2 Slice 3 enable/disable
  - `RECURRENCE_BOOST`        — Priority #2 Slice 4 enable/disable
  - `QUORUM_INVOCATION`        — Move 6 enable/disable
  - `COHERENCE_OBSERVER`       — Priority #1 enable/disable

* `compute_branch_verdict(original, counterfactual) ->
  BranchVerdict` — pure decision; closed taxonomy.

* `compute_replay_outcome(target, original, counterfactual) ->
  ReplayVerdict` — pure decision; aggregates verdict + source
  metadata.

* Schema version `COUNTERFACTUAL_REPLAY_SCHEMA_VERSION =
  "counterfactual_replay.1"`.

* Master flag `JARVIS_COUNTERFACTUAL_REPLAY_ENABLED` default-
  false until Slice 5.

* **Authority invariants**: stdlib ONLY (mirrors Priority #1
  Slice 1 + Priority #2 Slice 1 — strongest authority
  invariant). Zero governance imports. No exec/eval/compile.
  No async (Slice 3+ may introduce async).

**Tests**: ~50 covering frozen-dataclass shape + serialization,
master-flag asymmetric env, ReplayOutcome 5-value pin,
BranchVerdict 5-value pin, DecisionOverrideKind 5-value pin,
verdict math (EQUIVALENT / DIVERGED_BETTER / DIVERGED_WORSE /
DIVERGED_NEUTRAL / FAILED), defensive contract (NEVER raises),
authority invariants AST-pinned.

### Slice 2 — Phase capture extension with policy override

**New module**: `verification/counterfactual_replay_engine.py`

* `replay_session(session_id, *, swap_at_phase,
  decision_override, project_root, max_replay_seconds) ->
  ReplayVerdict` — high-level replay entry. Reads recorded
  session via Phase 1's `phase_capture` API; replays up to
  swap_at_phase using cached generation hashes; injects
  decision_override at swap_at_phase; continues replay; produces
  ReplayVerdict.

* **Cost contract — ZERO LLM CALLS** (load-bearing):
  - Replay uses cached generation hashes from
    `.ouroboros/sessions/<id>/phases/<phase>/cached_response.json`.
  - When a phase's cached_response is missing, replay returns
    PARTIAL outcome rather than triggering fresh LLM call.
  - AST-pinned: replay engine MUST NOT import providers /
    doubleword_provider / urgency_router / candidate_generator.
    Cost-amplification path is structurally impossible.

* **Sub-gate** `JARVIS_REPLAY_ENGINE_ENABLED` default-false
  until Slice 5.

* Internal: `_replay_branch(session, *, swap_target=None) ->
  BranchSnapshot` — replays one branch (original if swap_target
  is None; counterfactual if swap_target is set). Reads
  cached phase results; if cached result is missing, returns
  PARTIAL.

* Bounded:
  - `JARVIS_REPLAY_MAX_DURATION_SECONDS` (default 300, floor
    30, ceiling 1800) — wall-clock cap on a single replay
  - `JARVIS_REPLAY_MAX_PHASES_PER_BRANCH` (default 50, floor
    5, ceiling 500) — phase count cap

* **Authority invariants**: stdlib + Slice 1
  (counterfactual_replay) + Phase 1 phase_capture API ONLY.
  NEVER imports providers / etc (cost contract pin).

**Tests**: ~50 covering replay-with-cached-hashes (verifies
zero LLM calls), policy-override injection at chosen phase,
branch divergence detection, max-duration timeout, max-phases
cap, schema-tolerance (corrupt phase_capture handled
defensively), 8 authority pins (NO providers/urgency_router/
candidate_generator imports — cost contract structurally
impossible).

### Slice 3 — Branch comparator + verdict aggregation

**New module**: `verification/counterfactual_replay_comparator.py`

* `compare_branches(original, counterfactual) -> ReplayVerdict`
  — full pure decision pipeline.

* Comparison criteria (closed taxonomy):
  - Terminal outcome (success vs failed)
  - Apply mode (none / single / multi)
  - Verify pass rate (passed / total)
  - Postmortem count (recurrence indicator)
  - Cost USD (Phase C cost-contract preservation)

* **MonotonicTighteningVerdict integration**:
  - When counterfactual has BETTER outcome (DIVERGED_WORSE for
    original), the proposed adaptation is to KEEP the original
    policy → stamps `PASSED` (no loosening).
  - When counterfactual has WORSE outcome (DIVERGED_BETTER for
    original), the proposed adaptation is also to KEEP the
    original policy → stamps `PASSED`.
  - When EQUIVALENT, stamps `PASSED` (no change).
  - **Replay can never propose a loosening** — replay is
    observational, not prescriptive. AST-pinned.

* **Compose with Priority #2's recurrence-boost loop**:
  - When replay produces DIVERGED_WORSE for "with PostmortemRecall"
    branch, this is empirical evidence that prevention worked.
  - Aggregate: `recurrence_reduction_pct` = % of replays where
    DIVERGED_WORSE for "without prevention" branch.
  - When `recurrence_reduction_pct` exceeds operator-tunable
    threshold, MetaAdaptationGovernor surfaces the proposal to
    flip Move 6 master to default-true.

* **Authority invariants**: stdlib + Slice 1 + Slice 2 +
  adaptation.ledger (MonotonicTighteningVerdict ONLY) ONLY.
  No orchestrator / providers / etc.

**Tests**: ~50 covering verdict math (parametrized over 4
BranchVerdict values), MonotonicTighteningVerdict.PASSED
stamping verified, EQUIVALENT for matching outcomes,
DIVERGED_BETTER / DIVERGED_WORSE / DIVERGED_NEUTRAL distinct
verdicts, comparison criteria correctness, defensive contract,
8 authority pins.

### Slice 4 — Replay history store + SSE event publisher

**New module**: `verification/counterfactual_replay_observer.py`

* `.jarvis/replay_history.jsonl` — append-only ReplayVerdict
  audit log via Tier 1 #3 `flock_append_line`.

* `record_replay_verdict(verdict, *, target_path) ->
  IndexOutcome` — append-only persistence. NEVER raises.

* `read_replay_history(*, since_ts, limit, target_path) ->
  Tuple[ReplayVerdict, ...]` — schema-tolerant chronological
  reader.

* `EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE` SSE event fired
  on every non-DISABLED non-FAILED replay completion. Lazy
  ide_observability_stream import (Move 4/5/6/Priority#1/#2
  pattern).

* `publish_replay_complete(*, verdict, op_id) -> Optional[str]`
  — best-effort publish. Master-flag-gated. NEVER raises.

* **Recurrence-reduction baseline aggregator**:
  - `compute_recurrence_reduction_pct(verdicts, *,
    since_ts) -> float` — pure aggregator over historical
    replay verdicts. Returns % of replays where prevention
    measurably worked.
  - Operator queries via Slice 5's GET routes (deferred).

* Sub-gate `JARVIS_REPLAY_HISTORY_ENABLED` default-false.

* **Authority invariants**: stdlib + Slice 1 + Slice 2 +
  Slice 3 + Tier 1 #3 (cross_process_jsonl) + lazy
  ide_observability_stream.

**Tests**: ~50 covering record + read round-trip, append-only
discipline (50 records → 50 verdicts; never rotates per §8),
since_ts filter, recurrence-reduction-pct aggregation
correctness, SSE event vocabulary stable + master-off silenced
+ broker-missing graceful, **multi-process flock stress**, 8
authority pins.

### Slice 5 — Graduation + operator surfaces

* **Master + 3 sub-gate flags** flip default false → **true**
  (matching Priority #1 + Priority #2 discipline because
  replay is read-only over cached hashes; ZERO LLM cost):
  - `JARVIS_COUNTERFACTUAL_REPLAY_ENABLED`
  - `JARVIS_REPLAY_ENGINE_ENABLED`
  - `JARVIS_REPLAY_HISTORY_ENABLED`
  - `JARVIS_REPLAY_OBSERVER_ENABLED` (auto-replay
    observer scheduled for periodic counterfactual sweep)

* **shipped_code_invariants AST pins** (4 new, total 36→40):
  - `counterfactual_replay_pure_stdlib` — Slice 1 PURE-STDLIB
    + no exec/eval/compile + no async (mirrors Priority #1
    + Priority #2 Slice 1 discipline)
  - `replay_engine_no_provider_imports` — Slice 2 MUST NOT
    import providers / doubleword_provider / urgency_router
    / candidate_generator (cost-amplification path
    structurally impossible)
  - `replay_comparator_uses_adaptation_ledger` — Slice 3 MUST
    import MonotonicTighteningVerdict from adaptation.ledger
    (Phase C universal cage rule integration)
  - `replay_history_uses_flock` — Slice 4 MUST reference
    flock_append_line (cross-process safety)

* **FlagRegistry seeds**: 6 FlagSpec entries (master + 3
  sub-gates + max_duration + max_phases caps).

* **SSE event** `EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE`
  live.

* **Operator surfaces — DEFERRED to Slice 5b** (consistent
  with Priority #1 + Priority #2 pattern):
  - `/replay` REPL — `recent` / `history` / `<session_id>` /
    `compare <session_id> <swap>`
  - 4 GET routes:
    `/observability/replay{,/history,/recent,/recurrence_reduction}`

* **Comprehensive graduation pin suite** (~50 tests):
  - 4 master/sub-gate flags default-TRUE pins
  - 6 cap-structure clamps
  - 4 Priority #3 invariant pins registered AND HOLD
  - 6 FlagRegistry seeds present + defaults pinned
  - Total invariant count ≥ 40 pin
  - Full-revert matrix
  - **End-to-end empirical recurrence-reduction proof**:
    synthetic recorded session with PostmortemRecall + recurrence
    boost → replay WITH and WITHOUT each → comparator produces
    DIVERGED_BETTER for "with-prevention" → recurrence-reduction-
    pct aggregator returns >0
  - Authority invariants final pass

### Slice budget

| Slice | New module | Tests | LOC est |
|---|---|---|---|
| 1 — Replay primitive | counterfactual_replay.py | ~50 | ~500 |
| 2 — Phase capture extension | counterfactual_replay_engine.py | ~50 | ~500 |
| 3 — Branch comparator | counterfactual_replay_comparator.py | ~50 | ~400 |
| 4 — History store + observer | counterfactual_replay_observer.py | ~50 | ~500 |
| 5 — Graduation + AST pins + seeds | (no new module — modifies existing) | ~50 | ~600 |

**Total**: ~5 commits, ~250 tests, ~2,500 net new lines.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Replay non-determinism** — cached hash mismatch indicates session was non-deterministic; replay returns DIVERGED rather than crashing | Slice 1's ReplayOutcome.DIVERGED is first-class; operator can investigate via Slice 5b GET routes |
| **Cost-amplification leak** — bug in replay path triggers fresh LLM call | AST pin: replay engine MUST NOT import providers / doubleword_provider / urgency_router. Structurally impossible by construction. |
| **Stale replay against newer code** — replay of old session with shipped code changed since | Replay reads ONLY cached generation hashes from phase_capture; doesn't re-execute against current code. The phase_capture is the source-of-truth for that session's reality. Replay is *time-travel*, not *re-execution*. |
| **Replay infinite loop** | Slice 2 enforces `max_replay_seconds` (default 300, ceiling 1800) and `max_phases_per_branch` (default 50, ceiling 500) caps |
| **Verdict mis-classification** | 5-value BranchVerdict closed taxonomy; tested with parametrized fixtures over all comparison criteria |
| **Replay history unbounded growth** | Append-only per §8; future Slice 5b can add rotation if needed (defer; today the bounded write rate keeps growth manageable for soaking) |
| **Phase C cage rule bypass** — replay proposes a loosening | Slice 3 stamps `MonotonicTighteningVerdict.PASSED` on every verdict because replay is observational not prescriptive. AST-pinned. |
| **Multi-process replay race** on history file | Tier 1 #3 `flock_append_line` for writes |
| **Recurrence-reduction false signal** — small N produces noise | Slice 4's `compute_recurrence_reduction_pct` requires `min_replays` (env-tunable, default 5) before reporting. Below threshold → returns NaN or `INSUFFICIENT_DATA` outcome. |
| **Backward-compat regression** — Slice 4 boost wiring breaks Priority #2 | Slice 4 is read-only over Priority #2's advisory chain; no orchestrator integration. Backward-compat verified by Priority #2 regression suite still green. |

## Authority invariants (AST-pinned by Slice 5)

  * `counterfactual_replay.py` (Slice 1) — stdlib ONLY.
    Strongest authority invariant.
  * `counterfactual_replay_engine.py` (Slice 2) — stdlib +
    Slice 1 + Phase 1 phase_capture API. **MUST NOT import
    providers / doubleword_provider / urgency_router /
    candidate_generator** (cost-amplification path
    structurally impossible).
  * `counterfactual_replay_comparator.py` (Slice 3) — stdlib
    + Slice 1 + Slice 2 + adaptation.ledger
    (MonotonicTighteningVerdict ONLY).
  * `counterfactual_replay_observer.py` (Slice 4) — stdlib +
    Slice 1 + Slice 2 + Slice 3 + Tier 1 #3 + lazy
    ide_observability_stream.
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / providers / doubleword_provider
    / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian
    / semantic_firewall / risk_engine / candidate_generator.
  * No mutation tools.
  * No bare exec/eval/compile calls.
  * No async (Slice 5 wraps via to_thread at orchestrator).

## Knobs (Slice 5 graduation defaults)

### Master + sub-gates
  * `JARVIS_COUNTERFACTUAL_REPLAY_ENABLED` — master, **graduated true**
  * `JARVIS_REPLAY_ENGINE_ENABLED` — sub-gate, **graduated true**
  * `JARVIS_REPLAY_HISTORY_ENABLED` — sub-gate, **graduated true**
  * `JARVIS_REPLAY_OBSERVER_ENABLED` — sub-gate, **graduated true**

### Replay bounds
  * `JARVIS_REPLAY_MAX_DURATION_SECONDS` (default 300, floor 30,
    ceiling 1800)
  * `JARVIS_REPLAY_MAX_PHASES_PER_BRANCH` (default 50, floor 5,
    ceiling 500)
  * `JARVIS_REPLAY_MIN_REPLAYS_FOR_BASELINE` (default 5,
    floor 1, ceiling 100)
  * `JARVIS_REPLAY_RECURRENCE_REDUCTION_THRESHOLD_PCT` (default
    20.0, floor 5.0, ceiling 80.0) — % below which replay
    aggregator considers prevention insignificant

## Cost contract preservation (PRD §26.6) — load-bearing

Priority #3 is **read-only over cached generation hashes**:

  * Reads `.ouroboros/sessions/<id>/phases/<phase>/cached_
    response.json` (already on disk from Phase 1 capture).
  * Replays purely from cache — when cache is missing, returns
    `ReplayOutcome.PARTIAL` rather than triggering fresh LLM
    call.
  * **Zero LLM calls. Zero K× generation amplification.**
    Verified by AST pin on Slice 2 (no provider imports).
  * Periodic schedule (Slice 4 observer, NOT per-op).
  * AST-pinned: `counterfactual_replay_engine.py` MUST NOT
    import `providers` / `doubleword_provider` /
    `urgency_router` / `candidate_generator` (Slice 5 pin).
  * Replay verdicts are advisory; only operator approval via
    MetaAdaptationGovernor actually changes flag state.

## Slice independence

Each slice independently mergeable:

  * Slice 1 ships primitive — Slices 2-5 not landed → no
    behavior change (primitive unused).
  * Slice 2 ships engine — usable by tests but auto-replay
    not triggered until Slice 5 observer wiring.
  * Slice 3 ships comparator — produces verdicts for tests
    but not auto-recorded until Slice 4.
  * Slice 4 ships observer + history store — sub-gate default-
    false until Slice 5.
  * Slice 5 graduates — flags default-true unlock the full
    pipeline.

## What this Move does NOT prescribe

  * **No re-execution** — replay reads ONLY cached generation
    hashes. The phase_capture is the source-of-truth for that
    session's reality.
  * **No new ENFORCEMENT** — replay produces advisory verdicts.
    Operator approval via MetaAdaptationGovernor is the only
    path to actual flag flip.
  * **No replacement of `--rerun-from`** — Phase 1 Determinism's
    `--rerun-from` is the read-only replay path; Priority #3's
    `replay_session(swap_at_phase=...)` is the policy-swap
    extension. Both coexist.
  * **No Move 6 master flag flip** — replay produces evidence;
    operator decides graduation based on aggregate `recurrence_
    reduction_pct`.
  * **No new ledger surface** — replay verdicts are persisted
    in `.jarvis/replay_history.jsonl`; AdaptationLedger
    integration is read-only via `MonotonicTighteningVerdict`
    stamping.

## Closure criterion

Priority #3 closes when:

  * All 5 slices land (commits + regression tests green)
  * Master + 3 sub-gate flags graduated default-true
  * shipped_code_invariants AST pins register and currently-
    hold (target: 40 total invariants post-Priority-3)
  * SSE event `EVENT_TYPE_COUNTERFACTUAL_REPLAY_COMPLETE` live
  * `memory/project_priority_3_counterfactual_replay_closure.md`
    written
  * MEMORY.md indexed
  * **End-to-end empirical recurrence-reduction proof** in
    graduation test: synthetic recorded session with
    PostmortemRecall + recurrence boost → replay WITH and
    WITHOUT each → comparator produces DIVERGED_BETTER for
    "with-prevention" branch → recurrence-reduction-pct
    aggregator returns >0
  * Slice 5b (REPL + 4 GET routes) deferred per Priority #1 +
    Priority #2 precedent

## Why this is RSI-load-bearing

Priority #1 closed the **temporal-safety envelope** (drift
detectable). Priority #2 closed the **recurrence-prevention
loop** (detection → prevention). Priority #3 closes the
**policy-evaluation primitive** (prevention → empirical
measurement of effectiveness).

Without Priority #3:

  * Operators observe correlation (recurrences down post-
    PostmortemRecall) but cannot prove causation.
  * Move 6 master flag graduation has no empirical baseline.
  * Anti-Venom mathematical auditability is per-op only —
    can't prove gates aren't false-positive in aggregate.
  * RSI safety envelope has no replay-based stress test.

With Priority #3:

  * Every recorded session has a counterfactual sibling
    available on demand.
  * Aggregate `recurrence_reduction_pct` becomes the metric
    that drives Move 6 graduation.
  * Anti-Venom auditable: replay every blocked op with relaxed
    gate → if all replays still produce DIVERGED_BETTER
    (original better), the gate is correctly tight; if any
    produce DIVERGED_WORSE (counterfactual better), operator
    investigates.
  * RSI loops have a stress-test substrate — replay measures
    the safety envelope under different policy compositions.

The Reverse Russian Doll's outer shell now scales
**evaluatively** in addition to detectionally + preventatively.
The immune system not only sees + counteracts; it now
**proves** its counteractions work via deterministic
counterfactual.

This is what closes the gap from A− empirical floor to A
empirical floor. After Priority #3 + Slice 5b consolidation
(across 4 arcs) + Move 6 graduation soak: O+V is at A-level
empirical execution.

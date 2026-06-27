---
title: Project Priority 1 Coherence Auditor Scope
modules: []
status: merged
source: project_priority_1_coherence_auditor_scope.md
---

**Scope status: DRAFT** (post-Move-6-closure 2026-05-01,
awaiting kickoff authorization). After commits `6338031b09`
+ `d9fbf3a58a` + `0121e99ec6` + `033c4ec91d` + `91dc284965` +
`3f234ab0f4` + `a4701eabd6` (Move 6 full arc), the empirical
floor stands at **B+** with structural ceiling **A−**. Per the
§28.7 brutal review, the gap to A is empirical (live
verification + drift detection + recurrence prevention), not
structural. Priority #1 of three closes the **single highest-
impact gap** — long-horizon behavioral drift detection.

## Why Priority #1 (architectural justification)

§28.7 review identified the **gestalt-rotation blind spot**:

> Move 4's `InvariantDriftAuditor` audits *structural* invariants.
> Coherence Auditor audits *behavioral* drift. Without it, the
> immune system catches AST violations but not the slow boil
> where everything-is-individually-fine but the gestalt has
> rotated.

Concrete drift surfaces today have NO detection:

| Drift surface | Today | Without detection |
|---|---|---|
| StrategicPosture locked >24h same value | PostureObserver applies hysteresis but never alerts on *prolonged* lock | Wrong posture biases EVERY downstream decision |
| Route distribution rotation | UrgencyRouter is stateless per-op | Cost profile silently shifts (e.g., COMPLEX route share doubles → 30× claude cost) |
| AST signature of governed modules drifts mid-session | Move 4 catches *invariant* violations only | Module structurally evolved without graduation but no invariant fired |
| Postmortem recurrence | LastSessionSummary v1.1a digests last session | Same `failure_class` recurring 5× in week is invisible |
| p99 confidence drop trend | Tier 1 #1 wired per-op SSE | No rollup; rising trend is invisible until session-end |
| Env-flag default flipped without FlagRegistry update | FlagRegistry seed_truth tests catch at boot | Mid-session env mutation invisible |

Move 4's structural drift catches the **discrete moment**;
Priority #1's coherence drift catches the **integral** —
the system's behavioral mass moving in a direction it
shouldn't.

**Why this is the FIRST priority (not the second or third):**

  * Without coherence drift detection, RSI loops can self-
    corrupt over time without any signal to halt them.
    Reverse Russian Doll's outer shell expands; the immune
    system does not currently scale temporally with that
    expansion (it scales per-op, not per-week).
  * Priority #2 (PostmortemRecall) **consumes** the
    `RECURRENCE_DRIFT` signal Priority #1 produces.
    Building #2 first means #2 has no upstream telemetry.
  * Priority #3 (Counterfactual Replay) requires drift-aware
    branch evaluation — the replay engine asks "would this
    counterfactual have caused drift?" and that answer
    requires a measurement instrument.
  * Move 6 master flag activation requires shadow-mode
    evidence; coherence audit IS the shadow-mode metric.

## Existing infrastructure to leverage (NO duplication)

The substrate is mostly already shipped — Priority #1
extends, never duplicates:

| Existing | Used as | Slice |
|---|---|---|
| `InvariantDriftAuditor` (Move 4) | Pattern blueprint — observer cadence + flock + audit | Slice 3 |
| `InvariantDriftStore` (Move 4 Slice 2) | Atomic-write triplet pattern under `.jarvis/` | Slice 2 |
| `cross_process_jsonl.flock_append_line` (Tier 1 #3) | Cross-process append-safety for window history | Slice 2 |
| `compute_ast_signature` (Move 6 Slice 2) | Per-module behavioral fingerprint | Slice 1 |
| `DirectionInferrer` + `StrategicPosture` (Wave 1 #1) | Posture-distribution input + posture-aware cadence | Slices 1, 3 |
| `PostureObserver` (Wave 1 #1) | Cadence-and-hysteresis pattern | Slice 3 |
| `phase_capture` (Phase 1) | Per-session ground truth — read source for behavioral signatures | Slice 1 |
| `posture_history.jsonl` (Wave 1 #1) | Posture-distribution input | Slice 1 |
| `SemanticIndex` (Phase C v1.0) | Recency-weighted decay math (3d halflife conversation, 14d commits) | Slice 1 (decay math reuse) |
| `auto_action_router` (Move 3) | Advisory action surface — drift kinds map to AdvisoryActionType | Slice 4 |
| `AdaptationLedger` (Phase 7.8) | Monotonic-tightening contract — drift can only tighten | Slice 4 |
| `MetaAdaptationGovernor` (Phase C Pass C) | Operator-approval gate for tightening proposals | Slice 4 |
| `LastSessionSummary` (v1.1a) | Per-session digest — read source for recurrence index | Slice 1 |
| `summary.json` `ops_digest` (v1.1a) | Apply/verify/commit telemetry | Slice 1 |
| `FlagRegistry` + seed pattern (Wave 1 #2) | Env-tunable knobs registration | Slice 5 |
| `EventChannelServer` `/observability/*` (Gap #6 Slice 1) | GET route surface | Slice 5 |
| `StreamEventBroker` SSE (Gap #6 Slice 2) | Drift-detected event surface | Slices 3, 5 |
| `shipped_code_invariants` (Move 4/5/6 Slice 5) | AST pin pattern | Slice 5 |
| `cost_contract_assertion.COST_GATED_ROUTES` | Read-only auditor — N/A but cited in pin description | Slice 5 |

What Priority #1 BUILDS:

  * **BehavioralSignature** — frozen aggregate of (route_dist,
    posture_dist, module_fingerprints, p99_confidence,
    recurrence_index, ops_digest_summary) computed over a
    rolling window.
  * **BehavioralDriftKind** — 6-value closed enum DISTINCT from
    Move 4's 9-value `DriftKind`. Behavioral drift is a
    different vocabulary (route_drift / posture_locked /
    symbol_flux / policy_default / recurrence / confidence)
    not a structural-drift extension.
  * **Coherence window store** — append-only signature history
    + audit log under `.jarvis/`.
  * **CoherenceObserver** — periodic posture-aware async
    auditor producing drift verdicts.
  * **Bridge to auto_action_router** — drift verdicts proposed
    as advisory tightening; routed through Anti-Venom for
    operator approval.

## The 5-slice arc

### Slice 1 — Behavioral primitive (pure data + compute)

**New module**: `verification/coherence_auditor.py`

* Frozen dataclasses (mirror Move 4 / Move 6 J.A.R.M.A.T.R.I.X.
  discipline):
  - `BehavioralSignature(window_start_ts, window_end_ts,
    route_distribution, posture_distribution,
    module_fingerprints, p99_confidence_drop_count,
    recurrence_index, ops_summary, schema_version)`
  - `BehavioralDriftFinding(kind, severity, detail,
    delta_metric, prev_signature_id, curr_signature_id,
    schema_version)`
  - `BehavioralDriftVerdict(outcome, findings,
    largest_severity, drift_signature, schema_version)`
* 6-value `BehavioralDriftKind` closed enum:
  - `BEHAVIORAL_ROUTE_DRIFT` — route_distribution rotated
    beyond budget
  - `POSTURE_LOCKED` — StrategicPosture stuck >threshold
    duration in same value
  - `SYMBOL_FLUX_DRIFT` — tracked module's `compute_ast_
    signature` changed without an APPLY event recording it
  - `POLICY_DEFAULT_DRIFT` — env-flag default observed at
    runtime differs from FlagRegistry-registered default
  - `RECURRENCE_DRIFT` — same `failure_class` postmortem
    appeared >threshold times in window
  - `CONFIDENCE_DRIFT` — p99 confidence-drop count rising
    window-over-window beyond budget
* 5-value `CoherenceOutcome` closed enum (J.A.R.M.A.T.R.I.X.):
  - `COHERENT` — within budget on every kind; no findings
  - `DRIFT_DETECTED` — at least one finding crossed budget
  - `INSUFFICIENT_DATA` — window too short for comparison
  - `DISABLED` — master flag off
  - `FAILED` — defensive sentinel
* `compute_behavioral_signature(window_data) ->
  BehavioralSignature` — pure aggregator over read-only
  inputs (phase_capture, posture_history, summary.json,
  shipped modules).
* `compute_behavioral_drift(prev, curr, *, budgets) ->
  BehavioralDriftVerdict` — pure decision function. NEVER
  raises.
* AST signature canonicalizer reused via Move 6 Slice 2's
  `compute_ast_signature` (no duplication).
* SemanticIndex's recency-decay math reused for window
  weighting (older signatures decay).
* Schema version `COHERENCE_AUDITOR_SCHEMA_VERSION =
  "coherence_auditor.1"`.
* Master flag `JARVIS_COHERENCE_AUDITOR_ENABLED` default false.
* Authority invariants AST-pinned: stdlib + Move 6 Slice 2 +
  Phase C SemanticIndex (decay math).

**Tests**: ~50 covering frozen-dataclass shape + serialization,
master-flag asymmetric env, BehavioralDriftKind closed taxonomy
pin, drift math (no-drift / single-kind / multi-kind / above-
budget / below-budget), recency-decay weighting verified vs
SemanticIndex, defensive contract (NEVER raises), authority
invariants AST-pinned.

### Slice 2 — Window store (cross-process flock'd)

**New module**: `verification/coherence_window_store.py`

* `.jarvis/coherence_window.jsonl` — append-only
  `BehavioralSignature` history. Cross-process safe via Tier 1
  #3's `flock_append_line`.
* `.jarvis/coherence_audit.jsonl` — append-only
  `BehavioralDriftVerdict` audit log per §8 invariant
  (immutable audit chain).
* `.jarvis/coherence_window.lock` — sibling lockfile (mirrors
  Tier 1 #3 pattern + ApprovalStore pattern).
* 5-value `WindowOutcome` closed enum:
  - `RECORDED` — signature appended successfully
  - `WINDOW_ROTATED` — oldest entry evicted to maintain
    bounded window size
  - `READ_OK` — read returned valid window
  - `READ_EMPTY` — window too short / no data
  - `FAILED` — defensive sentinel
* Public API:
  - `record_signature(sig: BehavioralSignature) ->
    WindowOutcome`
  - `read_window(*, window_hours: int) -> Tuple[
    BehavioralSignature, ...]`
  - `record_drift_audit(verdict: BehavioralDriftVerdict) ->
    WindowOutcome`
  - `read_drift_audit(*, since_ts: float) ->
    Tuple[BehavioralDriftVerdict, ...]`
* Bounded window (cap structure):
  - `JARVIS_COHERENCE_WINDOW_HOURS` (default 168 = 7 days,
    floor 24, ceiling 720)
  - `JARVIS_COHERENCE_MAX_SIGNATURES` (default 200, floor 10,
    ceiling 5000)
* Atomic-write triplet under `.jarvis/` — mirrors Move 4
  Slice 2 + ApprovalStore pattern.
* Authority invariants AST-pinned: stdlib + Tier 1 #3
  (`cross_process_jsonl`) + Slice 1.

**Tests**: ~50 covering append+read round-trip, cross-process
flock contention (multi-process stress), window rotation at
cap, atomic-write triplet integrity, schema-mismatch tolerance
(corrupt-line skip), bounded read at window-hours cap, audit-
log immutability (append-only never overwrite), defensive
contract.

### Slice 3 — Async observer (posture-aware adaptive cadence)

**New module**: `verification/coherence_observer.py`

* `async observe_coherence_once() -> BehavioralDriftVerdict`
  — single audit pass: collect window data → compute signature
  → record → compute drift vs prev → return verdict.
* `class CoherenceObserver` — periodic loop (mirrors
  PostureObserver + InvariantDriftAuditorObserver pattern):
  - **Posture-aware cadence**: HARDEN posture → tighter
    cadence (default 3h), MAINTAIN → relaxed (default 12h),
    EXPLORE/CONSOLIDATE → default (6h)
  - **Adaptive vigilance**: drift detected → temporary
    cadence escalation × multiplier for next N cycles
    (mirrors Move 4 Slice 3 adaptive vigilance — REUSE the
    state-machine, parameterize differently)
  - **Failure backoff**: 3 consecutive observe failures →
    exponential backoff (mirrors Move 4 Slice 3 pattern)
  - **Drift signature dedup**: same `drift_signature` within
    dedup window (default 24h) is suppressed to prevent
    repeated alerts on same observation
* Cadence env knobs:
  - `JARVIS_COHERENCE_CADENCE_HOURS_HARDEN` (default 3,
    floor 1, ceiling 24)
  - `JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT` (default 6,
    floor 1, ceiling 48)
  - `JARVIS_COHERENCE_CADENCE_HOURS_MAINTAIN` (default 12,
    floor 1, ceiling 48)
  - `JARVIS_COHERENCE_DEDUP_WINDOW_HOURS` (default 24,
    floor 1, ceiling 168)
  - `JARVIS_COHERENCE_VIGILANCE_MULTIPLIER` (default 0.5,
    floor 0.1, ceiling 1.0)
* Sub-gate `JARVIS_COHERENCE_OBSERVER_ENABLED` default false
  until Slice 5.
* SSE event published per non-DISABLED audit:
  `EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED` (lazy
  ide_observability_stream import — Move 4/5/6 pattern).
* Read-only over: `phase_capture` artifacts, `posture_
  history.jsonl`, latest `summary.json`, shipped governance
  modules (for `compute_ast_signature`), env (for
  POLICY_DEFAULT_DRIFT detection).
* Authority invariants AST-pinned: stdlib + Slice 1 + Slice 2
  + DirectionInferrer (read posture only, no mutation) +
  posture-aware cadence helpers + lazy ide_observability_
  stream.

**Tests**: ~50 covering single-pass audit round-trip, posture-
aware cadence transitions (HARDEN tightens, MAINTAIN relaxes,
verified via mocked `get_current_posture`), adaptive vigilance
escalation on detected drift + de-escalation on coherent
window, failure backoff exponential progression, drift
signature dedup within window, SSE event payload schema
stability, defensive contract (NEVER raises out of the
periodic loop), authority invariants pinned.

### Slice 4 — auto_action_router bridge + monotonic-tightening

**New module**: `verification/coherence_action_bridge.py`

* `BehavioralDriftKind → AdvisoryActionType` mapping (5-value
  closed enum, J.A.R.M.A.T.R.I.X.):
  - `BEHAVIORAL_ROUTE_DRIFT` → `TIGHTEN_RISK_BUDGET`
    (advisory: propose lower BG/SPEC route share)
  - `POSTURE_LOCKED` → `OPERATOR_NOTIFICATION` (advisory:
    posture stuck — operator may want to override)
  - `SYMBOL_FLUX_DRIFT` → `RAISE_RISK_TIER_FOR_MODULE`
    (advisory: tracked module changed off-graduation;
    treat next op against it as APPROVAL_REQUIRED)
  - `POLICY_DEFAULT_DRIFT` → `OPERATOR_NOTIFICATION`
    (advisory: env mismatch with registry)
  - `RECURRENCE_DRIFT` → `INJECT_POSTMORTEM_RECALL_HINT`
    (advisory: forward-compat hook for Priority #2 — when
    PostmortemRecall ships, this becomes the trigger
    surface; until then, log-only)
  - `CONFIDENCE_DRIFT` → `TIGHTEN_CONFIDENCE_BUDGET`
    (advisory: reduce confidence-drop tolerance for next N
    ops)
* `propose_coherence_action(verdict) ->
  AdvisoryActionRecord` — creates an
  `auto_action_router`-compatible advisory record. Uses
  Move 3's existing 5-value `AdvisoryActionType` taxonomy
  where applicable; introduces 3 new values via
  `auto_action_router` extension contract (additive — Move 3
  already supports custom action types via string passthrough,
  verified backward-compat).
* `AdaptationLedger` integration: every coherence drift
  proposal becomes an `AdaptationProposal` with
  `tightening_only=True` flag. `MetaAdaptationGovernor`
  enforces monotonic-tightening contract — drift can only
  TIGHTEN budgets, never loosen them. Inherits Phase 7.8's
  `_file_lock.flock_exclusive` for ledger appends.
* AST-pinned: bridge MUST consume `AdaptationLedger` API
  (catches refactor that bypasses monotonic-tightening
  guarantee).
* Cost-contract preservation: bridge is read-only on phase
  state; tightening proposals are advisory (operator-approval-
  required for any actual flag flip). No K× cost amplification.

**Tests**: ~50 covering 6-kind × action mapping pin, advisory
record schema, AdaptationLedger integration (every proposal
recorded), monotonic-tightening enforcement (loosen attempt
rejected), MetaAdaptationGovernor approval gate fired,
cost-contract preservation (auditor never causes additional
generation calls), defensive fall-through on FAILED verdict.

### Slice 5 — Graduation + operator surfaces

* **Master flag flip** (default false → **true**):
  - `JARVIS_COHERENCE_AUDITOR_ENABLED`
  - `JARVIS_COHERENCE_OBSERVER_ENABLED`
  - `JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED`
* **shipped_code_invariants AST pins** (4 new, total 28→32):
  - `coherence_auditor_no_authority_imports_primitive` —
    Slice 1 must not import orchestrator-tier modules
  - `coherence_observer_no_authority_imports` — Slice 3 must
    not import orchestrator-tier modules
  - `coherence_window_store_uses_flock` — Slice 2 MUST
    reference `flock_append_line` from `cross_process_jsonl`
    (catches refactor that drops cross-process safety)
  - `coherence_action_bridge_consumes_adaptation_ledger` —
    Slice 4 MUST reference `AdaptationLedger` (catches
    refactor that bypasses monotonic-tightening contract)
* **FlagRegistry seeds**: 8 FlagSpec entries
  (master + sub-gates + cap knobs + cadence knobs).
* **Operator surfaces**:
  - `/coherence` REPL — `recent` / `signatures` / `drifts` /
    `window` / `<op_id>` filter (mirrors `/auto-action` /
    `/probe` / `/posture` shape)
  - `GET /observability/coherence{,/config,/history,
    /signatures,/drifts}` (5 routes — mirrors Move 4
    InvariantDrift surfaces)
  - SSE event `EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED` published
    on every non-DISABLED+non-COHERENT verdict
* **Comprehensive graduation pin suite** (~50 tests).

### Slice budget

| Slice | New module | Tests | LOC est |
|---|---|---|---|
| 1 — Behavioral primitive | coherence_auditor.py | ~50 | ~550 |
| 2 — Window store | coherence_window_store.py | ~50 | ~400 |
| 3 — Async observer | coherence_observer.py | ~50 | ~500 |
| 4 — Bridge + tightening | coherence_action_bridge.py | ~50 | ~400 |
| 5 — Graduation + surfaces | + coherence_observability.py | ~50 | ~550 |

**Total**: ~5 commits, ~250 tests, ~2,400 net new lines.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **False positives flood operator** (every minor distribution change reads as drift) | Recency-decay weighting via SemanticIndex math (older signals weigh less). Drift signature dedup (24h default). Severity field on Finding so operator can filter low-severity. Per-kind budget knob env-tunable. |
| **Window-data sparse session** (only 2 sessions in 7 days → not enough for distribution comparison) | INSUFFICIENT_DATA outcome explicit in 5-value `CoherenceOutcome` enum. Observer skips audit when window length < `MIN_SIGNATURES_FOR_DRIFT` (default 5, env-tunable). |
| **Cross-process race** on window JSONL append | Tier 1 #3 `flock_append_line` for writes; `flock_critical_section(SHARED)` for reads. AST-pinned. |
| **Module fingerprint thrashes** when shipped code legitimately evolves | `SYMBOL_FLUX_DRIFT` only fires when fingerprint changed AND no APPLY event in window references that module. APPLY events from `phase_capture` provide the legitimacy proof. |
| **Posture-aware cadence creates feedback loop** (HARDEN posture tightens cadence → more drift detected → action proposes HARDEN) | Cadence is observation-only — does not propose posture overrides. Posture-bridge to action_router ONLY emits `OPERATOR_NOTIFICATION` (advisory), never auto-flips posture. |
| **Window-store unbounded growth** | Cap structure: `JARVIS_COHERENCE_MAX_SIGNATURES` (default 200, ceiling 5000). Window rotation evicts oldest at cap. Bounded by-design. |
| **Cost-contract violation via auditor causing LLM calls** | Auditor is read-only over existing artifacts (zero LLM). AST-pinned by Slice 5: `coherence_auditor_no_provider_imports`. No `providers.py` / `doubleword_provider.py` / `urgency_router.py` imports allowed. |
| **Anti-Venom monotonic-tightening bypass** (drift proposal loosens a budget) | `AdaptationLedger.is_tightening` check at proposal-record time. AST-pinned: bridge MUST call `is_tightening` before record. MetaAdaptationGovernor enforces operator-approval. |
| **Posture-locked false positive on legitimate stable system** | POSTURE_LOCKED budget = consecutive-hours-in-same-value. Default 48h (env-tunable, floor 24h). Stable system staying in MAINTAIN for 48h is a NORMAL signal, not drift — budget calibrated accordingly. |
| **Schema drift across new BehavioralDriftKind values** | Closed taxonomy enum (J.A.R.M.A.T.R.I.X.). `to_dict`/`from_dict` round-trip with schema_version field. Schema mismatch → `from_dict` returns None defensively. Future extensions get new schema_version (`coherence_auditor.2`). |

## Authority invariants (AST-pinned by Slice 5 graduation pins)

  * `coherence_auditor.py` — stdlib + Move 6 Slice 2
    (ast_canonical) + Phase C SemanticIndex (decay math) +
    DirectionInferrer (read-only posture). NEVER imports
    orchestrator / phase_runners / iron_gate / change_engine
    / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router
    / subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall.
  * `coherence_window_store.py` — stdlib + Tier 1 #3
    (cross_process_jsonl) + Slice 1.
  * `coherence_observer.py` — stdlib + Slice 1 + Slice 2 +
    DirectionInferrer (read-only) + lazy
    ide_observability_stream.
  * `coherence_action_bridge.py` — stdlib + Slice 1 + Slice 2
    + AdaptationLedger + MetaAdaptationGovernor +
    auto_action_router (advisory record only).
  * `coherence_observability.py` — stdlib + aiohttp + Slices
    1-4.

  * No mutation tools referenced in code (AST walk verifies).
  * No exec/eval/compile (Slice 1 reuses Move 6's pin
    discipline — auditor NEVER executes any code).
  * Cap structure with floor + ceiling on every numeric env
    knob.

## Knobs (Slice 5 graduation defaults)

### Master + sub-gates
  * `JARVIS_COHERENCE_AUDITOR_ENABLED` — master, **graduated true**
  * `JARVIS_COHERENCE_OBSERVER_ENABLED` — sub-gate, **graduated true**
  * `JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED` — sub-gate, **graduated true**

### Window
  * `JARVIS_COHERENCE_WINDOW_HOURS` (default 168, floor 24,
    ceiling 720)
  * `JARVIS_COHERENCE_MAX_SIGNATURES` (default 200, floor 10,
    ceiling 5000)
  * `JARVIS_COHERENCE_MIN_SIGNATURES_FOR_DRIFT` (default 5,
    floor 2, ceiling 50)

### Cadence (posture-aware)
  * `JARVIS_COHERENCE_CADENCE_HOURS_HARDEN` (default 3, floor 1,
    ceiling 24)
  * `JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT` (default 6, floor 1,
    ceiling 48)
  * `JARVIS_COHERENCE_CADENCE_HOURS_MAINTAIN` (default 12,
    floor 1, ceiling 48)
  * `JARVIS_COHERENCE_DEDUP_WINDOW_HOURS` (default 24, floor 1,
    ceiling 168)
  * `JARVIS_COHERENCE_VIGILANCE_MULTIPLIER` (default 0.5,
    floor 0.1, ceiling 1.0)

### Per-kind drift budgets
  * `JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT` (default 25.0,
    floor 5.0, ceiling 100.0) — % distribution rotation
  * `JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS` (default 48,
    floor 24, ceiling 168)
  * `JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT` (default 3,
    floor 2, ceiling 50)
  * `JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT` (default 50.0,
    floor 10.0, ceiling 500.0)

### Tracked modules
  * `JARVIS_COHERENCE_TRACKED_MODULES` — comma-separated paths
    relative to repo root. Default: all governance verification
    modules (programmatically resolved at boot, not hardcoded).

## Cost contract preservation (PRD §26.6)

Coherence Auditor is **read-only over existing artifacts**:

  * Reads `phase_capture` Merkle nodes (already on disk).
  * Reads `.jarvis/posture_history.jsonl` (already on disk).
  * Reads latest `summary.json` (already on disk).
  * Reads shipped governance modules' source (already on disk).
  * Reads env (no I/O).
  * Computes `compute_ast_signature` per module (pure-stdlib;
    Move 6 Slice 2 already AST-pinned no-exec).
  * **Zero LLM calls**. Zero K× generation amplification.
  * Periodic schedule (default 6h cadence), not per-op.
  * AST-pinned: `coherence_auditor.py` MUST NOT import
    `providers` / `doubleword_provider` / `urgency_router` /
    `candidate_generator` (Slice 5 pin).
  * Action bridge proposals are advisory; only operator
    approval (via MetaAdaptationGovernor) actually changes
    state.

## Slice independence

Each slice is independently mergeable:

  * Slice 1 ships the primitive — Slices 2–5 not landed → no
    behavior change (primitive unused).
  * Slice 2 ships window store — usable by tests but not
    triggered from observer until Slice 3.
  * Slice 3 ships observer — fires only when `JARVIS_COHERENCE_
    OBSERVER_ENABLED=true` (default false in Slice 3).
  * Slice 4 wires the action bridge — sub-gate default-false
    until Slice 5 → drift verdicts logged but no advisory
    records created.
  * Slice 5 graduates — flags default-true unlock the full
    pipeline.

This matches Move 4 + Move 5 + Move 6 substrate-first cadence.

## What this Move does NOT prescribe

  * **No new ENFORCEMENT** of coherence findings — every
    proposal is advisory, gated by Anti-Venom's
    MetaAdaptationGovernor. Operator approves any flag flip.
  * **No replacement of Move 4** — InvariantDriftAuditor
    handles structural drift (boot-snapshot vs current);
    Coherence Auditor handles BEHAVIORAL drift (window-over-
    window). Distinct vocabularies, distinct schemas, distinct
    enums.
  * **No PostmortemRecall implementation** — Priority #1
    produces the `RECURRENCE_DRIFT` signal; Priority #2
    consumes it. Coupling is forward-compatible (advisory
    record kind reserved; consumer wires later).
  * **No counterfactual replay** — that's Priority #3.
    Coherence audit produces drift verdicts; replay engine
    will use those verdicts as evaluation criteria.
  * **No new auto-flags-flipping path** — drift never directly
    flips an env. Operator approves via MetaAdaptationGovernor.

## Closure criterion

Priority #1 closes when:

  * All 5 slices land (commits + regression tests green)
  * Master + 2 sub-gate flags graduated default-true
  * shipped_code_invariants AST pins register and currently-
    hold (target: 32 total invariants post-Priority-1)
  * Operator surfaces (5 GET routes + SSE + `/coherence`
    REPL) live
  * `memory/project_priority_1_coherence_auditor_closure.md`
    written
  * MEMORY.md indexed
  * One end-to-end live verification: at least one observed
    drift verdict (any of 6 kinds) lands in `.jarvis/coherence_
    audit.jsonl` AND its advisory action lands in `auto_action_
    router`'s ledger AND MetaAdaptationGovernor surfaces the
    proposal for operator review.

## Why this is RSI-load-bearing

The Reverse Russian Doll's outer shell must scale temporally,
not just spatially. Move 4 detects when a single AST violates
its boot snapshot (spatial). Coherence Auditor detects when
the system's behavioral mass rotates over a window (temporal).

Without temporal drift detection, RSI loops have no halting
condition rooted in their own behavior. Anti-Venom's
mathematical safety guarantees become per-op only — the
*integral* over many ops can still drift unbounded.

With Coherence Auditor live:

  * Every RSI loop has a measurable safety envelope.
  * Drift budgets are operator-tunable per kind (no
    hardcoding).
  * Tightening is monotonic via AdaptationLedger contract.
  * Tightening proposals require Anti-Venom approval
    (MetaAdaptationGovernor).
  * The expanding shell remains bounded — by construction,
    not by hope.

This is what closes the gap from B+ empirical to A−
empirical. Priority #2 (PostmortemRecall) and Priority #3
(Counterfactual Replay) compound from this foundation.

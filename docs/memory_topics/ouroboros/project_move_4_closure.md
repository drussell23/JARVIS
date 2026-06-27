---
title: Project Move 4 Closure
modules: []
status: merged
source: project_move_4_closure.md
---

**Closed 2026-04-30.** Move 4 of the §27 v6 brutal-review autonomy
roadmap — closes the *temporal* gap left by Move 3.

**Why:** Move 3's auto_action_router closed the verification → action
loop *operationally* (every terminal postmortem produces an explicit
``AdvisoryAction`` proposal). But between boot and shutdown, the
organism *adapts* — Pass C surface miners propose tightenings,
operators approve patches via ``/adapt approve``, env knobs flip via
REPL. Without continuous re-validation, regressions in architectural
*promises* (shipped invariant pins, flag defaults, exploration
floors) accumulate silently for hours/days/weeks. Move 4 is the
*semantic drift* sister-concern to Move 3's *operational* loop.

**Sibling, not duplicate:** ``observability/trajectory_auditor.py``
already tracks **physical** codebase trajectory (LOC, complexity,
public-API count). Move 4 ships **InvariantDriftAuditor** —
*semantic* invariant drift (shipped-code pins, flag defaults,
exploration floors, posture). Orthogonal concerns; both feed the
same operator situational-awareness loop.

**How to apply:** All 3 master flags graduated default-true with
asymmetric env semantics (empty/whitespace = unset = post-graduation
default; explicit ``0``/``false`` hot-reverts independently):

  * ``JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED``          (master)
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED``         (sub-gate)
  * ``JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED`` (sub-gate)

Boot wiring lives in ``event_channel.py`` mirroring the existing
``auto_action_router`` block. Operators consume drift through the
unified Move 3 ledger (``/auto-action`` REPL + GET endpoints) plus
Slice 5's read-only Slice surfaces:

  * ``GET /observability/invariant-drift{,/baseline,/history,/stats}``
  * SSE ``EVENT_TYPE_INVARIANT_DRIFT_DETECTED`` (all novel drift,
    INFO+)
  * SSE ``EVENT_TYPE_AUTO_ACTION_PROPOSAL_EMITTED`` (actionable
    subset only — bridge skips NO_ACTION)

## What graduated (Slice 5)

Three master flags flipped false → true. Asymmetric env semantics:
empty/unset = post-graduation default; explicit truthy/falsy
hot-revert. Each independent — operators can silence one layer
without affecting the other two.

8 ``FlagSpec`` entries seeded in ``flag_registry_seed.SEED_SPECS``
(3 masters with posture-relevance + 5 cadence/tuning knobs).

2 ``shipped_code_invariants`` AST pins:
  * ``invariant_drift_bridge_uses_propose_action`` — bridge MUST
    consume ``_propose_action`` (no direct ``AdvisoryAction(...)``
    construction); §26.6 cost-contract guard inheritance.
  * ``invariant_drift_auditor_no_disk_writes`` — Slice 1 auditor
    primitive MUST stay disk-write-free (token + AST ``open()``
    walk).

## The 5-slice arc

| Slice | Commit       | Tests | Net |
|-------|--------------|-------|-----|
| 1 — Primitive          | `739c08c4e1` (Derek) + `8b030e47ab` (test spine) | 59 | InvariantDriftAuditor module — frozen dataclasses, pure compare engine, defensive capture from 4 read-only surfaces (shipped_code_invariants / flag_registry / exploration_engine / posture_observer); 9-value DriftKind closed taxonomy + 3-value DriftSeverity |
| 2 — Boot capture       | `93c0adf47b` | 61 | InvariantDriftStore — atomic-write triplet (.jarvis/invariant_drift_baseline.json + history.jsonl + audit.jsonl); 5-value BootSnapshotOutcome; sync + async install_boot_snapshot |
| 3 — Periodic observer  | `c68e20a0c0` | 56 | Async InvariantDriftObserver mirroring PostureObserver lifecycle; posture-aware cadence × adaptive vigilance × failure backoff; drift signature ring-buffer dedup; pluggable signal emitter |
| 4 — Auto-action bridge | `c3ede35f93` (Derek) | 49 | InvariantDriftAutoActionBridge translating drift → AdvisoryAction via _propose_action (cost-contract guard inherited); severity-aware mapping (CRITICAL → ROUTE_TO_NOTIFY_APPLY / WARNING → RAISE_EXPLORATION_FLOOR / INFO → NO_ACTION); env-overridable mapping table |
| 5 — Graduation         | `119ecb48f6` | 60 | 3 master flag flips; event_channel.py boot wiring; invariant_drift_observability.py with 4 GET routes; observer-level SSE event for full-drift visibility; FlagRegistry seeds; shipped_code_invariants AST pins |

**Total: 5 commits, 285 new regression tests, ~5,500 net new lines
(modules + tests + integrations). Combined sweep: 415/415 green
across Slices 1-5 + neighbors (TrajectoryAuditor for physical
metrics + auto_action_router + flag_registry).**

## Architecture overview

```
Boot (GovernedLoopService.start → EventChannel)
  ├── install_boot_snapshot()                       [Slice 2]
  ├── install_auto_action_bridge()                  [Slice 4]
  ├── get_default_observer().start()                [Slice 3]
  └── register_invariant_drift_routes(app)          [Slice 5]

Runtime (every cadence-tick):
  capture_snapshot → compare_snapshots vs baseline
    → InvariantDriftObserver.run_one_cycle
        ├── publish_invariant_drift_detected (SSE — all novel drift)
        ├── store.append_history(snap)
        └── InvariantDriftAutoActionBridge.emit
            ├── _propose_action (cost-contract guarded)
            ├── AutoActionProposalLedger.append (Move 3 ledger)
            └── publish_auto_action_proposal_emitted (Move 3 SSE)

Operator surfaces:
  /auto-action REPL  (Move 3 — actionable proposals)
  GET /observability/auto-action[/stats]            (Move 3)
  GET /observability/invariant-drift                (Move 4 Slice 5)
  GET /observability/invariant-drift/baseline
  GET /observability/invariant-drift/history
  GET /observability/invariant-drift/stats
  SSE: invariant_drift_detected (all)               (Move 4)
       auto_action_proposal_emitted (actionable)    (Move 3)
```

## Posture-aware adaptive cadence (Slice 3 + 5)

```
interval = base_interval_s
         × posture_multiplier[current_posture]   (env JSON override)
         × vigilance_factor      (when drift detected — K ticks)
         × (1 + consecutive_failures)             (linear backoff)
         clamped to [interval_floor, backoff_ceiling]
```

Default multipliers (env-overridable):
* HARDEN: 0.5× (tighten cadence — under pressure)
* CONSOLIDATE: 1.0× (steady)
* MAINTAIN: 1.2× (slightly loose — steady-state)
* EXPLORE: 1.5× (loose — calm exploration)

## Drift signature de-duplication (Slice 3)

Same drift signature ``(kind, affected_keys)`` in N consecutive
cycles → ONE SSE emit + ONE ledger append, N-1 deduped. Ring buffer
size env-tunable (default 5). Operators see novel drift, not
repeated signal noise. History JSONL still records every cycle.

## Cost contract preservation (PRD §26.6, structurally inherited)

The bridge consumes ``auto_action_router._propose_action`` directly
rather than constructing ``AdvisoryAction`` itself. The cost-
contract structural guard inside ``_propose_action`` therefore
applies to bridge-emitted proposals automatically:

  * If ``current_route in COST_GATED_ROUTES = (BG_ROUTE, SPEC_ROUTE)``
    AND ``proposed_risk_tier`` would route to APPROVAL_REQUIRED+,
    raises ``CostContractViolation``.
  * The bridge passes ``current_route="drift_bridge"`` — a sentinel
    NOT in ``COST_GATED_ROUTES``, so the guard naturally bypasses
    *by contract* (drift is metadata, out-of-band of any per-op
    route).
  * AST-pinned: ``invariant_drift_bridge_uses_propose_action``
    catches any future refactor that constructs AdvisoryAction
    directly (which would bypass the guard).

## Authority invariants (AST-pinned)

Per-module forbidden-import lists progressively widen:

  * **invariant_drift_auditor.py** — stdlib + 4 read-only surfaces
    (shipped_code_invariants / flag_registry / exploration_engine /
    posture_observer) ONLY. NO orchestrator/iron_gate/etc. AST
    walk + governance-import allowlist test.
  * **invariant_drift_store.py** — stdlib + auditor ONLY. Cannot
    re-implement capture logic; consumes only the auditor's
    snapshot type.
  * **invariant_drift_observer.py** — stdlib + auditor + store +
    posture_observer (cadence) + ide_observability_stream (Slice 5
    SSE).
  * **invariant_drift_auto_action_bridge.py** — stdlib +
    auto_action_router + auditor + observer ONLY.
  * **invariant_drift_observability.py** — stdlib + 4 invariant_drift
    modules + aiohttp.web.

All modules NEVER raise from public methods (defensive everywhere).

## Mutation boundary still locked

Move 4 ships *advisory* signals only. The bridge appends to the
existing Move 3 ledger; Move 3's ENFORCE flag
(``JARVIS_AUTO_ACTION_ENFORCE``) remains default-false. No code
path automatically modifies ``ctx`` based on drift. Operators must
explicitly graduate enforce-mode (separate later authorization,
gated on shadow-mode evidence accumulation).

## Operator binding (J.A.R.M.A.T.R.I.X.)

Two closed-taxonomy enums shape every code path:

  * ``DriftKind`` — 9 values (SHIPPED_INVARIANT_REMOVED,
    SHIPPED_VIOLATION_INTRODUCED, SHIPPED_VIOLATION_SIGNATURE_CHANGED,
    FLAG_REGISTRY_HASH_CHANGED, FLAG_REGISTRY_COUNT_DECREASED,
    EXPLORATION_FLOOR_LOWERED, EXPLORATION_REQUIRED_CATEGORY_DROPPED,
    EXPLORATION_BUCKET_REMOVED, POSTURE_DRIFT)
  * ``DriftSeverity`` — 3 values (CRITICAL, WARNING, INFO)
  * ``BootSnapshotOutcome`` — 5 values (NEW_BASELINE,
    BASELINE_MATCHED, BASELINE_DRIFTED, DISABLED, FAILED — never
    None, never implicit fall-through)

Mirrors Move 3's ``AdvisoryActionType`` 5-value-explicit
discipline.

## Knobs (Slice 5 graduation)

  * ``JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED`` — master, **graduated
    true**
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED`` — sub-gate,
    **graduated true**
  * ``JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED`` — sub-gate,
    **graduated true**
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S`` (default 600,
    floor 30)
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS`` (default 3)
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR`` (default
    0.5, range (0.05, 1.0])
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW`` (default 5)
  * ``JARVIS_INVARIANT_DRIFT_OBSERVER_BACKOFF_CEILING_S`` (default
    1800, floor 60)
  * ``JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS`` (JSON override)
  * ``JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING`` (JSON override —
    severity → AdvisoryActionType)
  * ``JARVIS_INVARIANT_DRIFT_BASE_DIR`` (default ``.jarvis/``)
  * ``JARVIS_INVARIANT_DRIFT_HISTORY_SIZE`` (default 256, floor 16)

## What remains (NOT in this arc)

  * **/invariant REPL command** — operator workflow polish; the
    GET endpoints + SSE + ledger surface cover the load-bearing
    use cases. REPL is Slice 5b follow-up if operators ask.
  * **Enforce-mode graduation** for Move 3 (still locked off) —
    separate authorization, gated on shadow-mode soak evidence.
  * **op_family inference at the orchestrator hook** — same
    deferral as Move 3 closure.

## Net trajectory after Move 4

§27 grade table — Self-tightening immunity dimension lifts from
A toward A+. Recovery from B+ toward A. Move 4 closes the
temporal gap (re-validation cadence) AND the semantic gap (drift
detection) AND the operator-surface gap (unified ledger). The
Reverse Russian Doll Order 2 trajectory now has its load-bearing
safety property: architectural promises survive across boots,
across adaptations, across operator interventions.

## Sibling architecture (NOT duplicated)

``observability/trajectory_auditor.py`` (commit ``5b6e1249a8``,
Apr 26) tracks **physical** codebase trajectory: LOC, complexity,
public_api_count, governance_file_count. Different concern; same
"trajectory" naming. The two auditors are designed to coexist —
operators see codebase volume drift AND semantic invariant drift
through complementary surfaces.

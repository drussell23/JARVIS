---
title: Project Phase Cost Drilldown
modules: [scripts/livefire_phase_cost.py, backend/core/ouroboros/governance/phase_cost.py, backend/core/ouroboros/governance/cost_repl.py, backend/core/ouroboros/governance/cost_governor.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/battle_test/session_recorder.py, backend/core/ouroboros/governance/session_record.py, tests/governance/test_phase_cost.py, tests/test_ouroboros_governance/test_cost_governor.py, tests/governance/test_phase_cost_graduation.py, tests/governance/test_cost_governor_phase.py, tests/governance/test_phase_cost_persistence.py]
status: historical
source: project_phase_cost_drilldown.md
---

Per-Phase Cost Drill-Down — CLOSED 2026-04-21 (5-slice arc).
Closes the CC-parity gap *"no 'why did this op cost $0.80'? — cost
breakdown exists but isn't drillable per-phase."*

**Why instrumentation-first, not UI-only:**
Pre-arc, CostGovernor tracked (op_id, provider, amount) tuples in
``_OpCostEntry`` but never tagged charges with phase. The orchestrator
fired a heartbeat with a phase label at ``_emit_route_cost_heartbeat``
(lines 1053–1083) but that was transient UX only — nothing persisted
to the ledger, summary.json, or any queryable surface. Building a UI
drill-down on top of non-existent data would have been theater.

**What shipped:**
- Slice 1: ``phase_cost.py`` — frozen ``PhaseCostEntry`` +
  ``PhaseCostBreakdown`` value types; pure ``aggregate_entries()`` /
  ``breakdown_from_mappings()`` / ``render_phase_cost_breakdown()``
  helpers. Schema ``phase_cost.v1``. Canonical 17-phase order exposed
  as a module-level tuple so consumers don't import the FSM enum.
  31 tests.
- Slice 2: Extended ``cost_governor.py``:
  - ``_OpCostEntry`` gained ``phase_totals``, ``phase_by_provider``,
    ``unknown_phase_usd`` fields.
  - ``charge()`` gained optional ``phase`` kwarg (backward-compat:
    default None preserves pre-arc behavior byte-for-byte — pinned
    by graduation test).
  - ``get_phase_breakdown(op_id) -> PhaseCostBreakdown`` projection API.
  - ``snapshot_all_phase_breakdowns()`` for session-wide rollup.
  - **Budget cap unchanged**: ``cumulative_usd`` remains the sole
    enforcement axis. Phase data is pure sidecar accounting.
  - Orchestrator call sites (2 of them — generation + demotion) pass
    ``ctx.phase.name``.
  21 tests.
- Slice 3: Persistence via observer pattern:
  - New ``register_finalize_observer()`` / ``reset_finalize_observers()``
    module-level API on ``cost_governor``.
  - ``CostGovernor.finish()`` dispatches to observers before prune
    (observer exceptions swallowed — finalize never breaks).
  - ``SessionRecorder`` subscribes at construction, ingests
    ``summary["phase_totals"]`` / ``phase_by_provider`` into
    ``_cost_by_op_phase`` dict.
  - ``save_summary()`` emits 3 new optional keys (additive — old
    consumers unaffected):
    - ``cost_by_phase``: session rollup
    - ``cost_by_op_phase``: per-op per-phase map
    - ``cost_by_op_phase_provider``: provider matrix within phase
    - ``cost_unknown_phase_by_op``: untagged spend (pre-arc path)
  - ``SessionRecord`` parser reads these into new ``cost_by_phase`` /
    ``cost_by_op_phase`` fields. Malformed values (non-mapping,
    negative, bad types) silently dropped. ``project()`` exposes
    ``has_phase_cost_data`` flag for IDE feature detection.
  - Empty sessions (no finalize events) omit every new key —
    backward-compat with pre-arc summary.json consumers preserved.
  19 tests.
- Slice 4: ``cost_repl.py`` with ``/cost`` dispatcher:
  - ``/cost`` (no args) — session-wide rollup across live ops
  - ``/cost <op-id>`` — live per-phase drill-down for one op
  - ``/cost session <sid>`` — historical drill-down from a past
    session's summary.json (via SessionBrowser)
  - ``/cost help`` / ``/cost ?``
  - ``set_default_governor()`` / ``reset_default_governor()`` module
    singleton for production wiring.
  - **IDE observability surface** was already extended automatically
    — ``GET /observability/sessions/<id>`` now returns ``cost_by_phase``
    / ``cost_by_op_phase`` / ``has_phase_cost_data`` because
    ``SessionRecord.project()`` exposes them.
  17 tests.
- Slice 5: ``test_phase_cost_graduation.py`` (17 pins) + live-fire
  ``scripts/livefire_phase_cost.py`` (10 scenarios, 39 checks).

**Critical graduation pins:**
- ``test_charge_without_phase_produces_identical_budget_state`` —
  dual-governor experiment: one charges with phase, one without;
  every budget field (cumulative_usd, cap_usd, remaining_usd,
  call_count, exceeded, provider_totals) must match byte-for-byte.
- ``test_observer_dispatch_survives_raising_observer`` — a bad
  observer never breaks finalize (cost persistence lifeline).
- ``test_canonical_phase_order_has_expected_members`` — introspects
  ``OperationPhase`` enum against ``CANONICAL_PHASE_ORDER`` to catch
  rename drift (tuple must cover every enum member).
- ``test_summary_omits_cost_keys_when_no_ops_observed`` — pre-arc
  sessions never see the new keys injected (backward-compat).

**Schema versions pinned:** ``phase_cost.v1`` (only new schema;
summary.json stays at v2 with additive keys).

**§1 invariant (grep-enforced):** ``phase_cost.py`` + ``cost_repl.py``
import zero of orchestrator / policy_engine / iron_gate /
risk_tier_floor / semantic_guardian / tool_executor /
candidate_generator / change_engine.

**Why observer pattern (not direct injection):** Orchestrator doesn't
know about ``SessionRecorder`` (battle_test-specific). CostGovernor
lives in governance. Module-level observer registry lets the recorder
subscribe at construction without coupling the modules — mirrors the
``OpsDigestObserver`` pattern that's already in place for APPLY/VERIFY
telemetry.

**Files shipped:**
- ``backend/core/ouroboros/governance/phase_cost.py`` (new)
- ``backend/core/ouroboros/governance/cost_repl.py`` (new)
- ``backend/core/ouroboros/governance/cost_governor.py`` (extended —
  phase kwarg + observer registry + get_phase_breakdown +
  snapshot_all_phase_breakdowns)
- ``backend/core/ouroboros/governance/orchestrator.py`` (2 call sites
  pass ``ctx.phase.name``)
- ``backend/core/ouroboros/battle_test/session_recorder.py`` (observer
  subscription + save_summary extension)
- ``backend/core/ouroboros/governance/session_record.py`` (cost
  fields + parser + project extension)
- ``tests/governance/test_phase_cost.py``,
  ``test_cost_governor_phase.py``, ``test_phase_cost_persistence.py``,
  ``test_cost_repl.py``, ``test_phase_cost_graduation.py``
- ``scripts/livefire_phase_cost.py``

**Test tally:** 105 arc tests green (31 + 21 + 19 + 17 + 17) + 39
live-fire checks across 10 scenarios. 404-test regression sweep
(cost arc + session arc + IDE observability) zero failures.

**Landmines resolved:**
- Late import of ``phase_cost`` from ``cost_governor.get_phase_breakdown``
  — phase_cost is a leaf module, cost_governor is prod-critical. The
  late import keeps the load order stable and lets phase_cost stay
  gate-free.
- ``_dispatch_finalize_observers`` defined after ``finish()`` method
  — works because Python resolves names at call time, not class-body
  time. Pyright flags it; runtime is fine.
- ``_on_cost_finalize`` tolerates non-Mapping summaries — observer
  contract says "best-effort"; bad data never breaks session recording.
- Per-op phase data clipped to 6 decimal places via ``round()`` for
  JSON sanity (matches existing CostGovernor convention).
- Test file name for existing cost_governor tests lives at
  ``tests/test_ouroboros_governance/test_cost_governor.py`` (not
  ``tests/governance/``) — easy to miss in regression sweeps.

---
title: Production Oracle → auto_action_router VERIFY wiring — CLOSED 2026-05-03
modules: [scripts/auto_action_oracle_closure_verdict.py, backend/core/ouroboros/governance/auto_action_router.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/ide_observability_stream.py]
status: merged
source: project_oracle_to_auto_action_wiring_closure.md
---

# Production Oracle → auto_action_router VERIFY wiring — CLOSED 2026-05-03

4-slice arc closing TWO gaps simultaneously discovered during the Production Oracle scope investigation:

1. **Production Oracle had zero downstream consumers** — substrate emitted advisory verdicts, but nothing in the orchestrator read them.
2. **`auto_action_router.propose_advisory_action()` had zero production callers** — the entire 5-value AdvisoryActionType machinery was built (Move 3, 97 tests) but never invoked from any phase hook. Pure dead code.

This arc wires both ends together: Production Oracle observations flow into AutoActionContext via the new `gather_context(include_oracle=True)` hop; AdvisoryAction proposals are produced at orchestrator VERIFY phase (post-cluster-cascade-observer) and surfaced via SSE event for operator review.

## Slices shipped

- **Slice A** — `auto_action_router.RecentOracleObservation` frozen dataclass + extended `AutoActionContext` with `recent_oracle_observation: Optional[RecentOracleObservation] = None` (additive — existing fields untouched). Frozen because consumers treat observations as immutable snapshots; the observer's own ring buffer holds live state.
- **Slice B** — Decision precedence extended with **Rule 1.5** (between master-flag check and ESCALATE rate). Reads `context.recent_oracle_observation` when oracle veto enabled:
  - `FAILED + SAFE_AUTO` → `DEMOTE_RISK_TIER` proposing NOTIFY_APPLY
  - `FAILED + NOTIFY_APPLY+` → `ROUTE_TO_NOTIFY_APPLY`
  - `DEGRADED + has op_family` → `RAISE_EXPLORATION_FLOOR`
  - `HEALTHY` (or no observation) → falls through to existing rules
  - Cost contract preserved structurally — `_propose_action` BG/SPEC guard is inherited; no rule directly proposes route escalation.
  - New env knob `JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED` graduated default-true.
- **Slice C** — `gather_context()` extended with `include_oracle: bool = True` parameter. New `_read_recent_oracle_observation()` helper reads the production_oracle_observer's `current()` and projects into the router's frozen shape (NEVER raises — observer-not-yet-ticked / master-off / any error → returns None). Orchestrator VERIFY phase hook in `Slice4bRunner` (right after the existing cluster_cascade_observer block, ~line 7474) calls `gather_context(include_oracle=True)` → `propose_advisory_action()`; non-NO_ACTION proposals are logged + emit SSE event `auto_action_proposal`. Master env knob `JARVIS_AUTO_ACTION_VERIFY_HOOK_ENABLED` graduated default-true. SSE event constant + `publish_auto_action_proposal()` helper added to `ide_observability_stream.py`.
- **Slice D** — `scripts/auto_action_oracle_closure_verdict.py` covering 5 primary contracts; closure memory + MEMORY.md update.

## Architectural decisions worth remembering

- **Production reality wins** — Rule 1.5 fires BEFORE the existing escalate-rate / failure-rate rules. Rationale: external truth signals (production health, latency, error bursts) are higher-priority than internal observability (postmortem ledger, confidence ledger). When the oracle says FAILED, the router shouldn't wait for internal failure rate to catch up.
- **Defense-in-depth via SAFE_AUTO branching** — FAILED + SAFE_AUTO produces DEMOTE (route to NOTIFY_APPLY for human review) rather than DEFER. Reasoning: SAFE_AUTO ops should auto-apply when safe — when external reality says they're NOT safe, human review is the correct intermediate step (matches the existing op-family failure-rate pattern). FAILED + NOTIFY_APPLY+ uses ROUTE_TO_NOTIFY_APPLY as the targeted action because the op is already past auto-apply.
- **gather_context include_oracle defaults True** — operators who want internal-observability-only behavior pass `include_oracle=False`; the default surface (and orchestrator hook) reads the oracle. The oracle hop is best-effort: observer-not-ready / disabled / errored → returns None, which Rule 1.5 treats as no-veto and falls through.
- **Orchestrator hook is ADVISORY, not gating** — the hook fires AFTER auto-commit + AFTER the cluster_cascade_observer; it logs the proposal and emits SSE but doesn't block COMPLETE. Master flag controls whether it runs at all; sub-flag (`JARVIS_AUTO_ACTION_ENFORCE`) controls whether proposals are auto-applied — that flag stays default-false (the only state the existing arc ships in). This wiring lets operators see what proposals WOULD have fired before flipping enforce on.
- **Dead-code closure via pin-by-existence** — Slice C's verdict static-checks the orchestrator source for the hook marker (`JARVIS_AUTO_ACTION_VERIFY_HOOK_ENABLED`), the propose call, and the publish call. This is a structural regression spine — any future edit that quietly removes the hook will fail the verdict. Lighter-weight than a full AST cross-file pin (which would require updating the meta/_invariant_helpers.py shape), but accomplishes the same regression detection.

## Test counts + AST pins

- **Empirical verdict 5/5 PRIMARY PASS** (no test suite ships in this arc — verdict script is the regression spine):
  - C1 AutoActionContext accepts oracle slot (backwards-compat verified)
  - C2 Oracle veto rule produces correct AdvisoryActionType (4 paths: FAILED+safe_auto / FAILED+notify_apply / DEGRADED+family / HEALTHY)
  - C3 Master env knob default-true; explicit false silences rule
  - C4 gather_context populates oracle when include_oracle=True
  - C5 Orchestrator VERIFY hook + SSE publisher present
- **2 new env flags** (in module bodies, not yet seeded in flag_registry_seed — that's a follow-up):
  - `JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED` (default true)
  - `JARVIS_AUTO_ACTION_VERIFY_HOOK_ENABLED` (default true)
- **1 new SSE event**: `auto_action_proposal` + `publish_auto_action_proposal()` helper in `ide_observability_stream.py`
- **1 new dataclass**: `RecentOracleObservation` (frozen)

## Empirical-closure verdict (against live source)

```
[PASS] C1 AutoActionContext accepts oracle slot (backwards-compat)
       with_obs.observation=True without_obs.observation_is_None=True
[PASS] C2 Oracle veto rule produces correct AdvisoryActionType
       cases=[FAILED+safe_auto->demote_risk_tier,
              FAILED+notify_apply->route_to_notify_apply,
              DEGRADED+family->raise_exploration_floor,
              HEALTHY->no_action]
[PASS] C3 Master env knob default-true; explicit false silences rule
       default_on=True explicit_off=False action_when_off=no_action
[PASS] C4 gather_context populates oracle when include_oracle=True
       with_oracle.verdict=failed without_oracle=None
[PASS] C5 Orchestrator VERIFY hook + SSE publisher present
       hook_marker=True propose_call=True publish_call=True sse_event_ok=True
```

## Reuse contract honored (no duplication)

- Existing `AutoActionContext` extended additively (single new optional field) — every existing test + caller still works
- Existing `_propose_action` reused for new Rule 1.5 outputs — no parallel decision path
- Existing `gather_context()` extended with one new parameter (default-True for graduated behavior)
- Existing orchestrator post-VERIFY observer pattern (cluster_cascade_observer at line 7474) mirrored for the auto_action hook — same try/except / fail-soft / never-raises shape
- Existing SSE publish helper pattern (`publish_domain_map_update` / `publish_production_oracle_signal`) mirrored for `publish_auto_action_proposal`
- `_read_recent_oracle_observation()` is a thin projection helper — substrate work happens in `production_oracle_observer.get_default_observer().current()` (which already existed)

## What this unlocks

Pre-arc, both substrates were dead-code at the orchestrator layer. Post-arc:

1. **Production Oracle has its first downstream consumer** — when StdlibSelfHealthOracle observes degraded harness completion ratio (the example caught in the Production Oracle's empirical closure: 37.5% complete), it now propagates into AdvisoryAction proposals visible to operators via SSE + REPL.
2. **auto_action_router has its first production caller** — the 97-test Move 3 substrate is no longer dormant; every successful VERIFY phase produces a proposal evaluation.
3. **The advisory loop is end-to-end functional** — production reality → oracle signal → AutoActionContext → AdvisoryAction → SSE event → operator review → (optionally, after explicit JARVIS_AUTO_ACTION_ENFORCE flip) auto-applied via Pass C surfaces.
4. **Sentry/Datadog adapters (Arc 2) get a real consumer** — when those adapters emit FAILED/DEGRADED OracleSignals, the AdvisoryAction proposals will surface vendor errors as actionable router proposals at the next op's VERIFY phase.

## Files touched

- `backend/core/ouroboros/governance/auto_action_router.py` (RecentOracleObservation + AutoActionContext slot + Rule 1.5 + auto_action_oracle_veto_enabled + _read_recent_oracle_observation helper + gather_context include_oracle param)
- `backend/core/ouroboros/governance/orchestrator.py` (VERIFY hook block in Slice4bRunner)
- `backend/core/ouroboros/governance/ide_observability_stream.py` (EVENT_TYPE_AUTO_ACTION_PROPOSAL + publish_auto_action_proposal)
- `scripts/auto_action_oracle_closure_verdict.py` (NEW)

Closes the dead-code gap on both ends. Sentry/Datadog adapters land in Arc 2; MetaPhaseRunner soak preparation in Arc 3.

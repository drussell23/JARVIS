---
title: Project Followup F2 Backlog Urgency Hint Schema
modules: [tests/governance/test_parallel_dispatch_reachability_supplement.py, tests/governance/intake/test_backlog_sensor_urgency_hint.py, tests/governance/test_urgency_router_hint_consumption.py, tests/fixtures/wave3_forced_reach_seed.json, backend/core/ouroboros/governance/intake/intent_envelope.py, backend/core/ouroboros/governance/op_context.py, backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py, backend/core/ouroboros/governance/intake/unified_intake_router.py, backend/core/ouroboros/governance/urgency_router.py, backend/core/ouroboros/governance/flag_registry_seed.py, tests/governance/intake/sensors/test_backlog_sensor_urgency_hint.py, tests/governance/intake/sensors/test_backlog_sensor.py]
status: historical
source: project_followup_f2_backlog_urgency_hint_schema.md
---

## Status

- **Authorized 2026-04-23** per operator binding after Wave 3 (6) Slice 5a S3 + reachability supplement landed.
- **Controlling scope** for the F2 arc. Extends / supersedes the earlier placeholder.
- **Slice 1 in progress** — schema + sensor read + envelope stamping behind default-off flag.
- **Cross-reference**: `memory/project_wave3_item6_graduation_matrix.md` §"Verdict" — live_reachability=blocked pending F2.

## Why this exists

Wave 3 (6) Slice 5a live-fire sessions (S1 `bt-2026-04-24-021024`, S2 `bt-2026-04-24-030628`, S3 `bt-2026-04-24-044547`) all finished with `0 [ParallelDispatch]` markers despite correctly-wired post-GENERATE seam (proven by `tests/governance/test_parallel_dispatch_reachability_supplement.py`, 2/2 green at HEAD 92ddb54463).

Root cause traced to two independent disqualifiers on the forced-reach multi-file seed:

1. **Source-type routing**: `source=backlog` → BACKGROUND route default in `UrgencyRouter`, regardless of envelope urgency. F3 (`JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY`) stamps urgency=critical but UrgencyRouter's decision uses source-type mapping not urgency alone. BG route disqualifies parallel_dispatch eligibility.
2. **Plan-shape collapse**: Mid-run log `PlanGenerator Skipping plan ... trivial_op: 1 file(s), short description` — the candidate came back as 1-file not 3-file. Single-file disqualifies parallel_dispatch eligibility independently.

Both conditions must be fixed for the forced-reach seed to exercise live multi-file fan-out. F2 fixes #1 by letting the seed entry declare its own urgency + routing, bypassing source-type mapping. #2 is a separate GENERATE-side plan coalescing issue, tracked in the Slice 5a verdict as a graduation-blocker.

**Why not F1 first**: F1 (intake governor enforcement — upgrade Wave 1 #3 SensorGovernor from advisory to enforcing at `UnifiedIntakeRouter.ingest()`) has a larger blast radius — every sensor's emissions become subject to enforcement, not just backlog. Operator scope-freeze 2026-04-23: F1 scheduled after F2 merges unless explicitly reprioritized.

## Scope (binding per operator 2026-04-23)

- Additive schema fields on `backlog.json` entries: `urgency_hint` + `routing_hint`.
- Backward compatible: absent → byte-identical to pre-F2 behavior.
- BacklogSensor reads per-entry hint, stamps envelope with it (overriding F3 env-wide override and priority-map default — most-specific wins).
- UrgencyRouter respects envelope's explicit `urgency_hint` / `routing_hint` over source-type mapping.
- Master flag gates per-entry override consumption. Default off through graduation; flip to default on only after 3 clean live sessions prove the path.
- FlagRegistry entry for the master flag.
- Authority invariants (grep-pinned on new / modified files):
  - BacklogSensor: no imports of orchestrator / policy / iron_gate / risk_tier / change_engine / candidate_generator / gate.
  - UrgencyRouter additions: same ban (it already stays clean).
- No schema change to envelope itself in Slice 1 — hints flow through the existing `urgency` field on `IntentEnvelope`. Slice 2 may add an optional explicit `routing_override` field on the envelope if the routing hint requires it.

## Non-goals

- F1-style intake governor enforcement (stays separate, larger arc).
- Changing source-type defaults for other sensors (DocStaleness, TodoScanner, etc. — all stay BG default).
- Overriding Iron Gate / SemanticGuardian / risk-tier. Routing hints do NOT bypass any safety check; they only select the route tier.
- Per-request urgency injection from non-backlog sources. F2 scopes to `backlog.json` only.
- GENERATE-side plan coalescing fix (Slice 5a verdict graduation-blocker #2). That's a separate arc.

## Slices

### Slice 1 — Schema + BacklogSensor read + envelope stamping (flag-gated, default off)

**Deliverables:**
- `backlog_sensor.py`: read `item.get("urgency_hint")` per entry. Validate against allow-list (`critical/high/normal/low`). Invalid values → log warning, fall back to pre-F2 behavior.
- Flag `JARVIS_BACKLOG_URGENCY_HINT_ENABLED` (bool, default `false`, SAFETY category in FlagRegistry). Guards consumption of per-entry hints.
- Precedence: per-entry `urgency_hint` > F3 session env override > priority-map default. One INFO log per scan where a hint was applied (ledger-parseable).
- `BacklogTask` dataclass gains optional `urgency_hint: Optional[str] = None` field.
- FlagRegistry seed entry added to `flag_registry_seed.py`.
- Tests in `tests/governance/intake/test_backlog_sensor_urgency_hint.py`:
  - Hint absent → byte-identical to pre-F2 (confidence, urgency, envelope shape)
  - Hint present + flag off → ignored (byte-identical to pre-F2)
  - Hint present + flag on → envelope's `urgency` field reflects the hint, not priority-map default
  - Invalid hint + flag on → warning logged, fallback to priority-map
  - Per-entry hint beats F3 env override (specificity wins)
  - Authority invariant grep: BacklogSensor imports stay clean
- No `routing_hint` consumption yet (Slice 2). No UrgencyRouter changes in Slice 1. Envelope carries the stamp; routing decision still uses source-type mapping. Slice 1 proves the sensor-side read + stamp without affecting routing semantics.

**Behavioral parity** (default off): byte-identical `BacklogSensor.scan_once()` output vs pre-F2.

### Slice 2 — `routing_hint` field + UrgencyRouter consumption (flag-gated, default off)

**Deliverables:**
- Extend `BacklogTask` with `routing_hint: Optional[str]`. Allow-list `immediate/standard/complex/background/speculative`.
- Extend envelope with optional `routing_override` field carrying Slice 2's value (additive, schema-version bumped).
- `urgency_router.py`: prefer envelope's explicit `routing_override` over source-type default when `JARVIS_BACKLOG_URGENCY_HINT_ENABLED=true`. Urgency consumption (Slice 1) flows too.
- Parity tests: hint-absent → byte-identical routing; hint-present + flag-on → explicit wins.
- Tests in `tests/governance/test_urgency_router_hint_consumption.py` covering every (source, hint) combination matrix.
- Still no default flip. Flag stays off.

### Slice 3 — Forced-reach seed uses explicit hints (supplement extension)

**Deliverables:**
- `tests/fixtures/wave3_forced_reach_seed.json`: add `"urgency_hint": "critical"` + `"routing_hint": "standard"` to the seed entry. Backward-compat: pre-F2 BacklogSensor ignores the fields.
- Extend `tests/governance/test_parallel_dispatch_reachability_supplement.py`: add a variant test proving a seed with `urgency_hint=critical` + `routing_hint=standard` flows through BacklogSensor → UrgencyRouter (mocked) → produces a STANDARD-route envelope → reaches the post-GENERATE parallel_dispatch seam with multi-file candidate.

### Slice 4 — Graduation cadence (live-fire) + Slice 5b default flip

**Deliverables:**
- 3 clean live sessions under master flag on, using the updated seed fixture. Assertions: `[ParallelDispatch] > 0`, `enforce_submit_start > 0`, 3 target files written, POSTMORTEM root_cause=none.
- If 3/3 clean: operator-authorized commit flipping `JARVIS_BACKLOG_URGENCY_HINT_ENABLED` default `false → true`.
- Post-flip confirmation soak with no env overrides.
- Matrix row: Wave 3 (6) Slice 5a FINAL.

## Cross-links

- **Graduation blocker**: Wave 3 (6) Slice 5a — `live_reachability=blocked pending F2` (see `project_wave3_item6_graduation_matrix.md` §"Verdict").
- **Sibling F-items**:
  - F1 deferred (`project_followup_f1_intake_governor_enforcement.md`).
  - F3 shipped — session-wide env knob `JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY`.
- **Harness epic**: `project_followup_battle_test_post_summary_hang.md` — 5-item structural fix, orthogonal; unblocks single-flight S* runs but does NOT by itself fix the live_reachability gap F2 addresses.
- **Reachability supplement**: `tests/governance/test_parallel_dispatch_reachability_supplement.py` (HEAD `92ddb54463`) — proves the post-GENERATE seam wiring; F2 produces the live fan-out the supplement simulates.

## Slice 3 status — **SHIPPED 2026-04-23** (commit `4bdc9f58d5`)

- **Commit**: `4bdc9f58d5 test(f2): F2 Slice 3 — forced-reach seed hints + end-to-end supplement variant`
- **Branch**: `ouroboros/battle-test/20260424-035720`
- **Test count**: 5/5 green on supplement (2 Slice 1 + 3 Slice 3); 341/341 green across full F2 + parallel_dispatch + flag_registry regression.
- **Files landed**:
  - `tests/fixtures/wave3_forced_reach_seed.json` — adds `"urgency_hint": "critical"` + `"routing_hint": "standard"` to the forced-reach seed entry. Backward-compatible: pre-F2 BacklogSensor ignores both fields (unrecognized keys pass through `item.get()` cleanly). Description extended with F2 context.
  - `tests/governance/test_parallel_dispatch_reachability_supplement.py` — 3 new tests:
    - `test_f2_slice3_fixture_declares_both_hints` — regression guard against silent drop of F2 hint fields.
    - `test_f2_e2e_seed_to_dispatch_pipeline_standard_route_markers` — the one test proving the F2 causal chain end-to-end. Drives the real BacklogSensor reading the real fixture (copied to tmp_path), verifies Slice 1 stamps `envelope.urgency=critical`, Slice 2 stamps `envelope.routing_override=standard`, intake-router pattern produces `ctx.provider_route=standard` + `reason="envelope_routing_override:standard"`, UrgencyRouter returns `(STANDARD, "envelope_routing_override:standard")`, ctx walks CLASSIFY→ROUTE→GENERATE, stub GENERATE emits 3-file `GenerationResult` matching seed targets, `dispatch_pipeline` fires post-GENERATE hook, `[ParallelDispatch]` eligibility + `enforce_submit_start` emit, scheduler submits + returns COMPLETED, `pctx.extras` holds `FanoutResult(COMPLETED)` with 3-unit `ExecutionGraph`.
    - `test_f2_e2e_flag_off_routes_background_and_seam_skipped` — negative control proving F2 is the causal enabling link. Same fixture, flag off → envelope.urgency falls back to priority-map ("low"), envelope.routing_override empty, UrgencyRouter returns BACKGROUND. Byte-identical to pre-F2.
  - Helpers: `_SEED_FIXTURE_PATH`, `_copy_seed_to_tmp`, `_CapturingRouter` — local to the supplement file.
  - Added `from pathlib import Path` import (not previously imported at module level).

## Slice 2 status — **SHIPPED 2026-04-23** (commit `184eecc58b`)

- **Commit**: `184eecc58b feat(urgency_router): F2 Slice 2 — envelope routing_override + router consumption (default-off)`
- **Branch**: `ouroboros/battle-test/20260424-035720` (unpushed along with Slice 1 + supplement)
- **Test count**: 18/18 green on Slice 2 router-consumption suite; 108/108 green on combined F2 + backlog + supplement regression.
- **Files landed**:
  - `backend/core/ouroboros/governance/intake/intent_envelope.py` — `IntentEnvelope.routing_override: str = ""` field + validation against `_VALID_ROUTING_OVERRIDES` frozenset (empty + 5 ProviderRoute values). `with_lease` / `to_dict` / `from_dict` / `make_envelope` all propagate; `from_dict` uses `.get(..., "")` so pre-F2 persisted envelopes parse cleanly. **SCHEMA_VERSION unchanged** (additive; per narrow-scope guardrail).
  - `backend/core/ouroboros/governance/op_context.py` — `OperationContext.create()` accepts optional `provider_route` + `provider_route_reason` kwargs, threads through `fields_for_hash` dict + final `cls(...)` call.
  - `backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py` — `_validate_routing_hint` helper, `BacklogTask.routing_hint: Optional[str]` field, `scan_once` consumes hint under master flag, stamps `envelope.routing_override`; one INFO per scan on consumption + one WARNING per scan on any invalid hint.
  - `backend/core/ouroboros/governance/intake/unified_intake_router.py` — at `OperationContext.create` call site, forwards `envelope.routing_override` to `ctx.provider_route` with reason `"envelope_routing_override:<value>"`.
  - `backend/core/ouroboros/governance/urgency_router.py` — `_envelope_routing_override_enabled()` helper reads the shared F2 master flag; `classify()` gains priority-0.5 clause between harness priority-0 and IMMEDIATE priority-1. Disambiguated from harness by reason-prefix check `startswith("envelope_routing_override")`.
  - `tests/governance/test_urgency_router_hint_consumption.py` — 18 tests covering flag-off parity (inert even when ctx pre-stamped), flag-on consumption matrix (all 5 ProviderRoute values), priority vs critical urgency / cross-repo / harness flag ordering, invalid value safety (empty / bogus / case-insensitive), Wave 3 (6) Slice 5a forced-reach scenario end-to-end.
- **Behavioral parity (default off)**: verified byte-identical to pre-F2 via the `test_flag_off_*` test family + `test_nothing_pre_stamped_*` tests.
- **Orthogonality to harness knob**: verified by `test_f2_flag_off_but_harness_flag_on_still_respects_pre_stamp` + `test_f2_flag_on_ignores_non_f2_reason` + `test_f2_and_harness_both_on_harness_wins_at_priority_0`. Neither flag can consume the other's pre-stamp.
- **Priority ordering pinned** by tests:
  - 0 (existing) `JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED` harness knob
  - 0.5 (F2)     `envelope_routing_override` (this slice)
  - 1            IMMEDIATE (critical / voice / cross_repo)
  - 2            SPECULATIVE (intent_discovery)
  - 3            BACKGROUND (source-type default)
  - 4            COMPLEX (heavy_code / multi-file)
  - 5            STANDARD (default)

## Pre-existing test-suite flakes (noted, orthogonal to F2)

Three pre-existing failures confirmed via `git stash` diff — all present on pre-F2 baseline, not caused by this arc:
1. `test_urgency_router.py::TestEnvelopeSourceSchemaCoverage::test_every_valid_envelope_source_routes_to_expected_tier` — test's hardcoded expected-route mapping is stale; missing `vision_sensor` added to `_VALID_SOURCES` 2026-04-18.
2. `test_unified_intake_router.py::test_submit_called_with_correct_trigger_source` — SensorGovernor shadow-mode state bleeding across tests (`governor.sensor_cap_exhausted cap=3 count=3`).
3. `test_unified_intake_router.py::test_dead_letter_after_max_retries` — same governor state bleed.

Could be fixed in separate one-line PRs; out of scope for F2.

## Slice 1 status — **SHIPPED 2026-04-23** (HEAD `37642cfbe2`)

- **Commit**: `37642cfbe2 feat(backlog_sensor): F2 Slice 1 — per-entry urgency_hint schema (default-off)`
- **Branch**: `ouroboros/battle-test/20260424-035720`
- **Test count**: 52/52 green on F2 Slice 1 suite; 192/192 green across F2 + F3 + backlog FS events + FlagRegistry + reachability supplement regression.
- **Files landed**:
  - `backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py` — module docstring schema update + `_urgency_hint_enabled()` + `_validate_urgency_hint()` + `BacklogTask.urgency_hint` field + `scan_once` precedence (per-entry > F3 env > priority-map) + one INFO per scan on consumption + one WARNING per scan on any invalid hint.
  - `backend/core/ouroboros/governance/flag_registry_seed.py` — `JARVIS_BACKLOG_URGENCY_HINT_ENABLED` FlagSpec (BOOL, default `False`, SAFETY category).
  - `tests/governance/intake/sensors/test_backlog_sensor_urgency_hint.py` — 52 tests: flag parsing, hint validation, BacklogTask field, scan_once precedence matrix (flag-off parity, flag-on override, hint-vs-F3 specificity), invalid-hint warning telemetry, consumed-hint INFO telemetry, authority-import ban grep-pin.
- **Authority invariant**: BacklogSensor stays grep-clean on orchestrator/policy/iron_gate/risk_tier/change_engine/candidate_generator/gate imports (new test pins this).
- **Behavioral parity (default off)**: verified byte-identical to pre-F2 via the `flag_off_*` test family.
- **Next**: Slice 2 — extend `BacklogTask.routing_hint`, add envelope `routing_override` field, wire `urgency_router.py` to consume both. Requires separate per-slice authorization to start (per standard per-slice cadence).

## Pre-existing test-suite flake (noted, orthogonal to F2)

`tests/governance/intake/sensors/test_backlog_sensor.py::test_sensor_start_stop` fails pre-F2 (pre-existing bug in the test itself: calls `sensor.stop()` synchronously without `await`, but the method is async — the coroutine is never awaited, so `sensor._running` stays True). Not touched by F2 Slice 1. Could be fixed in a separate one-line PR.

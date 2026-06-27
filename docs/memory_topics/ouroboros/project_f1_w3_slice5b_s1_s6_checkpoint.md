---
title: Project F1 W3 Slice5B S1 S6 Checkpoint
modules: [backend/core/ouroboros/governance/phase_dispatcher.py, backend/core/ouroboros/governance/orchestrator.py, backend/core/ouroboros/governance/governed_loop_service.py]
status: historical
source: project_f1_w3_slice5b_s1_s6_checkpoint.md
---

## Status

- **Slice 5b: CLOSED 2026-04-24** on wiring + reachability + observability (structural bar). S8 (`bt-2026-04-25-003533`) on `d378dea968` proved `[ParallelDispatch]` log emits, `dispatch_fanout` is reachable, FanoutResult path exercised (empty_candidate_list is a valid eligibility outcome, not a wiring miss).
- **Seed traversal of Iron Gate exploration ledger: parked as separate arc.** See `project_followup_seed_exploration_arc.md`. Status: OPEN. Next work is offline (design doc + fixture/prompt analysis); no live battle burn until scoped.
- **Patch state on main**: `d378dea968` (orchestrator `_subagent_scheduler` alias — closes W3(6) Slice 4 wiring gap) + `1a495c5ee9` (cancel-attribution telemetry) + `25fdf09d24` (Tier 3 PLAN cap) + `87ccbcee15` (Tier 3 v2 — Tier 0 path) + `08d523d158` (Tier 3 v1 — primary path) + `e97bb9b60d` (F1 S3 integration) + `4bdc9f58d5` (F2 S3 envelope) + earlier W3(6) S1–4.
- **Harness epic continues parallel.** Items 6 (SIGTERM/summary), 7 (wall_clock_cap silent), and 1–5 from the original ticket remain open under the harness epic owner — independent of the W3(6) graduation arc closure.

## Slice 5b graduation ledger (final)

| Row | Status | Evidence |
|---|---|---|
| `[ParallelDispatch]_structural` (wiring + observability) | **PASS (S8)** | `bt-2026-04-25-003533/debug.log:17:45:14 [ParallelDispatch] op=op-019dc212-0f02 ... reason=empty_candidate_list`; 0× `enforce_fanout skipped: ... no _subagent_scheduler reference` |
| `seed_slice5b_traversal` (seed-attributed E2E) | **BLOCKED-BY-INTEGRATION-BUG (identified 2026-04-25, fix in PR)** | Test A audit (S6/S7/S8) refuted all 4 prompt/fixture hypotheses — actual exploration tool calls were 11–21 per session, but `_merge_results` was dropping them. Path 2 fix lands in `plan_exploit._merge_results` — aggregates `tool_execution_records` + `prompt_preloaded_files` from child streams. Path 3 live-fire pending operator's "seed live verify go" after merge. |
| Cost cap option (a) | **APPLIED** | `JARVIS_OP_RETRY_HEADROOM=8.0` → per-op cap $0.45 → $1.20; S8 retry budget held (2/2 attempts ran, $1.02/$1.20 spent) |

## S1→S7 ledger

| Session | session_id | MERGE_HEAD_SHA | Failure class | Bucket | Patch landed |
|---|---|---|---|---|---|
| S1 | bt-2026-04-24-075052 | (pre-F1) | OperationAdvisor blast=50 block on 3 × empty `__init__.py` | product (fixture) | fixture reframe 1 (`46acea87f8` substituted `cancellation_token.py`) |
| S2 | bt-2026-04-24-091016 | F1 + reframe 1 | `risk_engine.classify` `touches_security_surface` substring "token" false positive | product (governance) | F5 stub filed (real fix deferred); fixture reframe 2 (substituted `context_memory_loader.py`) |
| S3 | bt-2026-04-24-204029 | F1 + F2 + reframe 2 | DW 153s GENERATE stall | provider (DW endpoint) | Tier 3 reflex v1 — `_call_primary` path only (`08d523d158`) |
| S4 | bt-2026-04-24-213248 | F1 + F2 + Tier3 v1 | Tier 3 v1 inert: code path bypassed when DW=Tier0=Primary | product (patch scope) | Tier 3 reflex v2 — `_compute_tier0_budget` path (`87ccbcee15`) |
| S5 | bt-2026-04-24-220418 | F1 + F2 + Tier3 v2 | PLAN phase 227.2s burn (DW Connection error → Claude ConnectTimeout) | provider (Anthropic transport) + product (PLAN cap missing) | PLAN Tier 3 cap (`25fdf09d24`) |
| S6 | bt-2026-04-24-225137 | F1 + F2 + Tier3 GENERATE+PLAN | mid-Venom Round 2 Claude `CancelledError` after 12.3s sem_wait, `remaining_s=207.67` | unknown (transport vs internal cancel) | none yet — under investigation per (A) directive |
| S7 | bt-2026-04-25-001939 | + cancel-attribution telemetry (`1a495c5ee9`) | **W3(6) Slice 4 enforce path STRUCTURALLY UNREACHABLE: `getattr(orchestrator, "_subagent_scheduler", None)` returns None — orchestrator stores scheduler as `_config.execution_graph_scheduler`. Wiring miss** | product (W3(6) Slice 4 attribute-name mismatch) | none — fix proposed, not authorized |

## S7 detailed findings (2026-04-24)

### Primary discovery — W3(6) Slice 4 wiring bug

**The graduation marker `[ParallelDispatch]` is structurally unreachable in the current main**, regardless of env config or fixture choice. Six sessions worth of bar-tracking optimization were partially mis-targeted:

- `backend/core/ouroboros/governance/phase_dispatcher.py:608`: `_scheduler = getattr(orchestrator, "_subagent_scheduler", None)`
- `backend/core/ouroboros/governance/orchestrator.py:572`: orchestrator stores it as `_config.execution_graph_scheduler` (passed in via `OrchestratorConfig`)
- `backend/core/ouroboros/governance/governed_loop_service.py:3280`: harness passes `self._subagent_scheduler` to `OrchestratorConfig.execution_graph_scheduler` correctly
- `_subagent_scheduler` does NOT exist as an orchestrator attribute (verified via `grep`)

S7 log signature confirming the miss: `WARNING [PhaseDispatcher] enforce_fanout skipped: orchestrator has no _subagent_scheduler reference` (17:23:27 in `bt-2026-04-25-001939/debug.log`).

### Two-line fix proposal (NOT authorized — awaiting operator)

Either:
- (A) `phase_dispatcher.py:608` change to `getattr(orchestrator._config, "execution_graph_scheduler", None)`, OR
- (B) Add a property on `GovernedOrchestrator` aliasing `_subagent_scheduler → self._config.execution_graph_scheduler` for backward-compat with the dispatcher's call shape.

(A) is cleaner — direct rename — but couples dispatcher to orchestrator's internal config layout. (B) hides the rename behind a stable surface but adds an alias the next reader has to chase. Operator picks.

Tests required: ≥1 unit test in `test_phase_dispatcher.py` (or peer) showing `enforce_fanout` *runs* (not skipped) when orchestrator was constructed with `OrchestratorConfig(execution_graph_scheduler=<mock>)` — pins the wiring path so this regresses loud next time.

### Secondary observation — PLAN-EXPLOIT is a different post-GENERATE seam

Per operator directive: `[PLAN-EXPLOIT]` is documented separately as a **second-class** seam, not a substitute for `[ParallelDispatch]`. Both are post-GENERATE concurrency hooks; they fire at different points and serve different purposes:

| Seam | Module | Manifesto principle | Trigger | Status in S7 |
|---|---|---|---|---|
| `[PLAN-EXPLOIT]` | `plan_exploit.py` | §3 Disciplined Concurrency | PLAN-SHADOW DAG with parallel-able units | ✅ **fired** — `dag_units=3 concurrent_streams=3 merged_files=9` |
| `[ParallelDispatch shadow_*]` | `parallel_dispatch.py` | W3(6) Slice 3 (shadow) | post-GENERATE candidate-files extraction + shadow flag | ❌ skipped — shadow flag default off |
| `[ParallelDispatch] enforce` | `parallel_dispatch.py` | W3(6) Slice 4 (enforce) | post-GENERATE + enforce flag + scheduler attribute | ❌ skipped — wiring bug above |

S7 produced **a real PLAN-EXPLOIT proof on a 3-file multi-file candidate** (cost ≈ $0.55, wall ≈ 119s). That's a §3 deliverable validation; ledger row added per operator directive.

### S7 cost cap finding

Per-op cap is `$0.45` for STANDARD/moderate (`base=$0.10 * route_mult=1.50 * complexity_mult=1.00 * headroom=3.00`). The 3 parallel Claude streams burned $0.5484 collectively — so cap was busted on attempt 1 and retry was blocked. Per operator directive: *don't* soften the `exploration_insufficient` rejection. Two options:
- (a) Raise battle-test per-op cap. E.g., `--cost-cap 5.00` (session) + `JARVIS_COST_HEADROOM_X=6.0` (per-op) so 3-stream multi-file can fit.
- (b) Dial down parallelism for this seed: `JARVIS_PLAN_EXPLOIT_MAX_STREAMS=1` (or similar) so retries fit in single-stream budget.

(a) is more honest: 3-stream multi-file *is* expensive, and the cap should reflect that for this graduation arc. Operator picks.

### Cancel attribution telemetry

`[CancelAttribution]: 0` in S7 — no cancellations. Telemetry is loaded and quiet; will fire next time a cancel happens. No data yet on Class-C disambiguation.

## Patches live on main, all proven on at least one session

- F1 (intake priority scheduling): proven S3 (envelope reached ROUTE under priority-0.5 hint)
- F2 (backlog urgency_hint/routing_hint): proven S3 + S6 (envelope_routing_override:standard active in `DECISION` log every session since)
- Tier 3 reflex v1 + v2 (GENERATE primary cap 30s): structurally inert when DW = Tier 0 = Primary (S4 finding); v2 covers Tier 0 path
- Tier 3 reflex PLAN cap (30s primary, 60s fallback): proven S6 — `Plan Tier3_cap_active: primary_budget=30.0s` logged live, PLAN burn 227.2s → 61.8s (3.7×). Re-proven S7 on independent op.
- §3 Disciplined Concurrency (`plan_exploit.py`): proven S7 — `[PLAN-EXPLOIT] status=completed dag_units=3 concurrent_streams=3 merged_files=9` on the seed op. Second-class seam (not the W3(6) Slice 5 deliverable, but a §3 deliverable in its own right).
- Cancel attribution telemetry (`1a495c5ee9`): loaded S7, quiet (no cancels). Functional contract pinned by 5 unit tests; live observability surface awaits next cancel.

## Bucket A — harness epic items (operator-binding P0)

Source of truth: `project_followup_battle_test_post_summary_hang.md`. Items 1–6 already filed.

**Update — S6 reproduces and adds new items:**

- **Item 6 (filed S5, repro S6)**: SIGTERM during steady-state → `summary.json` not written even on clean exit. SIGKILL needed in S6 to terminate (SIGTERM did NOT exit within 8s). Same artifact pattern: `debug.log` only, no summary.
- **Item 7 (NEW — discovered S6)**: `WallClockWatchdog` did not fire at `max_wall_seconds=2400`. Session armed at 15:51:55, ran to >16:42 (51min wall) without firing `stop_reason=wall_clock_cap`. Operator-impact: harness can't be relied on to bound runaway sessions in headless / agent-conducted soaks. Action item: add fail-fast watchdog telemetry (per-second heartbeat or per-30s tick log) so silent-fire vs not-armed-at-all is observable; root-cause whether the watchdog task itself is being starved by event-loop contention.

These two items remain harness epic; not blockers on the runner-class arcs.

## Bucket B — product/provider items

- **F5 stub** (`project_followup_f5_touches_security_surface_precision.md`): naive substring `risk_engine` rule — out of scope on F1 branch; stub-only.
- **CancelledError @ Round 2** (S6 — under investigation per directive (A)): cancel-source telemetry to be added before S7. Hypothesis: either (a) tool_executor inner sem race, (b) HTTP/2 transport flap (similar `OSStatus -26276` TLS failures present in same session — `gh` CLI broken throughout, suggesting host-level network instability).
- **Patch state**: stable. No further runner-attributable patches needed; the failure-mode escalation curve is now upstream (provider/transport) or harness, not runner.

## Why we kept hitting this

The bar required APPLY landing on an `APPROVAL_REQUIRED` op in `--headless` — which is structurally impossible without an orange-PR path (currently broken on this host) or fixture risk-tier reduction. Six sessions burned proving that *pre-APPLY* upstream patches kept working while the bar kept failing.

The bar is now retired. See `feedback_headless_completion_contract.md` for the proposed two re-scoping paths.

## Recommendation forward

1. Operator picks Path (i), (ii), or (mixed) per `feedback_headless_completion_contract.md`.
2. (A) telemetry pass on the `CancelledError` source — operator authorized as gating S7.
3. Optional cheap datapoint operator suggested: probe `gh` / `api.anthropic.com` connectivity *outside* the harness to disambiguate provider-vs-host network state. Cost: ~1 min, no battle-test session burn.
4. S7 launches on the new bar with telemetry in place.

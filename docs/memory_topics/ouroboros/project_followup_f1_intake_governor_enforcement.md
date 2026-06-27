---
title: Project Followup F1 Intake Governor Enforcement
modules: [backend/core/ouroboros/governance/intake/intake_priority_queue.py, tests/governance/intake/test_intake_priority_queue.py, tests/governance/intake/test_unified_intake_router_f1.py, tests/governance/test_parallel_dispatch_reachability_supplement.py, backend/core/ouroboros/governance/flag_registry_seed.py, backend/core/ouroboros/governance/intake/unified_intake_router.py, tests/governance/intake/test_unified_intake_router_f1_integration.py, backend/core/ouroboros/governance/parallel_dispatch.py, backend/core/ouroboros/governance/worktree_manager.py, backend/core/ouroboros/governance/phase_dispatcher.py]
status: historical
source: project_followup_f1_intake_governor_enforcement.md
---

## Why this exists (evidence-driven)

F2 Slice 4 S1 live cadence (`bt-2026-04-24-062608`, 2026-04-24) established:

1. F2 plumbing fires correctly on live traffic — `JARVIS_BACKLOG_URGENCY_HINT_ENABLED active` + `envelope.routing_override stamped` markers emitted ≥1× live for the first time ever.
2. The seed's envelope was stamped with `urgency=critical` and `routing_override=standard` and handed to `router.ingest(envelope)` which returned `"enqueued"`.
3. **The seed then never reached `UrgencyRouter.classify()`**. Zero `Route: standard (envelope_routing_override:standard)` log lines. All 12 routed ops were DocStaleness / TodoScanner / runtime_health.
4. Session idle-timed at 858.9s with `[ParallelDispatch] = 0` and `enforce_submit_start = 0` — the F2 priority-0.5 UrgencyRouter clause cannot fire on an op that never reaches the router.

**Cause**: `UnifiedIntakeRouter`'s queue is class-partitioned FIFO. `urgency=critical` is a label on the envelope but not a dequeue-priority signal. BG sensors fire burst emissions at session boot (hardcoded initial-scan sleeps not covered by `_INTERVAL_S` env vars), crowding the head of the BG partition and also consuming worker pool slots before the BacklogSensor seed's envelope surfaces.

**Lesson**: F2 fixed the routing DECISION (`envelope.routing_override → ctx.provider_route → UrgencyRouter priority-0.5`). F1 fixes the routing REACHABILITY (`envelope stamped → dequeued in priority order → reaches classify() → decision fires`). Both are required for live multi-file fan-out graduation. Deferring F1 past F2 graduation was a category error; the S1 ledger is the proof.

## Scope (binding per operator 2026-04-24)

**Core primitive**: replace class-partitioned FIFO dequeue in `UnifiedIntakeRouter` with an urgency-priority heap + reserved-slot starvation guard + per-envelope deadline. No new authority on the router — it still defers all content decisions to UrgencyRouter / orchestrator. F1 changes ORDER of dispatch, not DESTINATION.

**Same graduation discipline as F2**:
- Per-slice operator authorization.
- Behavioral parity tests pinning "flag off = byte-identical to pre-F1 dispatch order" before every merge.
- 3 clean live sessions under master flag on + 1 post-flip soak before default flip.
- Reachability supplement must cover the new primitive's contract at unit-level.

**Orthogonal to SensorGovernor enforcement**: F1's core is the priority scheduler. Governor enforcement is a **second-order consumer** — once F1's priority queue exists, plugging `governor.request_budget(...)` into the enqueue path is additive (and graduates separately). F1 does not require governor enforcement; governor enforcement does require F1.

## Non-goals

- Changing UrgencyRouter's classify() logic or F2 priority-0.5 clause. F1 does not touch routing math.
- Changing per-sensor intervals / silencing knobs. F1 does not touch sensor emission rate.
- Changing Iron Gate / SemanticGuardian / risk-tier / Manifesto §6 Antivenom. F1 has zero safety-layer surface.
- Wave 3 (7) mid-token cancel. Separate arc.
- Per-op lifecycle ledger (operator P1 — separate arc; may run in parallel after F1 Slice 1).
- Curiosity / hypothesis / speculative execution loops. Deferred.
- SerpentFlow replay-from-ledger. Deferred.
- SemanticGuardian capability-delta diff. Deferred.

## Architectural contract

### The primitive — `IntakePriorityQueue`

Replaces `UnifiedIntakeRouter`'s current queue structure. Semantics:

- **Heap key**: `(urgency_rank_desc, enqueue_monotonic_ts_asc)`. `urgency_rank` maps `critical → 0`, `high → 1`, `normal → 2`, `low → 3` (heap pops lowest, so critical comes first; FIFO within equal urgency).
- **Reserved slot guarantee**: of every N sequential dequeues, at least M must be from urgency ≥ `normal` if any such envelope exists in queue. Prevents pathological "infinite low urgency burst starves one pending normal" corner case. N and M env-tunable (defaults: `JARVIS_INTAKE_RESERVED_N=5`, `JARVIS_INTAKE_RESERVED_M=1`).
- **Per-envelope deadline**: envelope carries `must_be_routed_by_monotonic: float` computed at ingest as `now + deadline_for_urgency(envelope.urgency)`. Defaults: critical=5s, high=30s, normal=300s, low=∞.
- **Deadline violation → priority inversion emergency dequeue**: if head-of-queue hasn't yielded to a deadlined envelope by its deadline, `IntakePriorityQueue.dequeue()` emits `[Intake] priority_inversion op=<short> waited=Xs deadline=Ys` at WARNING and pops the deadlined envelope out-of-order. §7 Authority Override in kernel form.
- **Queue-depth telemetry**: snapshot per (urgency, source) written to session-scoped `.ouroboros/sessions/<id>/intake_queue.jsonl` on every enqueue + dequeue + 5s periodic. Format: `{"ts":..., "event":"snapshot"|"enqueue"|"dequeue", "depths":{"critical":N, ...}, "oldest_wait_s":...}`.
- **Back-pressure**: if queue depth exceeds `JARVIS_INTAKE_BACKPRESSURE_THRESHOLD` (default 200) OR oldest critical envelope has waited > 2× its deadline, `router.ingest()` refuses further ingestion from BG-class sources with `retry_after_s` + emits `[Intake] backpressure_applied source=<X> reason=<queue_full|critical_starved>` — sensors see the signal and back off.

### The master flag — `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED`

- Default `false` through Slice 3 graduation cadence.
- When off: current class-partitioned FIFO (byte-identical pre-F1).
- When on: `IntakePriorityQueue` is the queue; all semantics above take effect.
- Operator flip happens in Slice 4 after 3 clean live sessions with `[ParallelDispatch] > 0 AND enforce_submit_start > 0 AND priority_inversion_count=0`.

### Authority invariants (grep-pinned)

New files + modified `unified_intake_router.py` must NOT import:
- `orchestrator`, `policy`, `iron_gate`, `risk_tier`, `change_engine`, `candidate_generator`, `gate`, `semantic_guardian`

F1 is a routing-ORDER primitive, not a routing-DECISION primitive. It does not evaluate content.

### Observability contract

Every dequeue decision emits a structured event:
```
[Intake] dequeue op=<16char> urgency=<c/h/n/l> source=<src> waited_s=<X>
         starved_budget_pct=<Y> queue_depth={critical:a, high:b, normal:c, low:d}
```

Priority inversion events:
```
[Intake] priority_inversion op=<16char> waited_s=<X> deadline_s=<Y>
         ahead_of_me=[<source:urgency>, ...]
```

Back-pressure events:
```
[Intake] backpressure_applied source=<src> reason=<X> retry_after_s=<Y>
         queue_depth_total=<N>
```

## Slices

### Slice 1 — `IntakePriorityQueue` primitive + default-off flag + unit tests (no wiring)

**Deliverables**:
- `backend/core/ouroboros/governance/intake/intake_priority_queue.py` — new file. `IntakePriorityQueue` class with heap-based `enqueue` / `dequeue` / `snapshot_depths`, reserved-slot counter, deadline-priority-inversion emergency pop, optional telemetry sink.
- `_intake_priority_scheduler_enabled()` helper reads `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED` at call time.
- `FlagRegistry` seed entry for the master flag (SAFETY category, BOOL, default False).
- Unit tests in `tests/governance/intake/test_intake_priority_queue.py`:
  - FIFO within equal urgency
  - critical pops before normal regardless of enqueue order
  - reserved-slot: M-of-N guarantee for normal+ when any normal+ envelope exists
  - deadline: critical envelope past deadline pops out-of-order even if low envelopes were enqueued first
  - back-pressure: ingest refused when threshold exceeded
  - telemetry: snapshot / enqueue / dequeue events emitted to injected sink
  - parity: when flag off (queue in legacy mode if exposed), equivalent to FIFO
  - authority-import grep pin (no banned imports)
- **No wiring to `UnifiedIntakeRouter` in Slice 1.** Primitive lands standalone + tested.

**Scope**: ~500 LOC primitive + ~300 LOC tests. One commit, default-off, behavioral parity irrelevant (primitive not yet in hot path).

**Proof criterion**: 40+ tests green covering all semantic branches; authority-import grep pin passes; FlagRegistry `/help flag JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED` renders.

### Slice 2 — Wire `UnifiedIntakeRouter` to use `IntakePriorityQueue` when flag on (shadow-mode parallel structure)

**Deliverables**:
- `unified_intake_router.py`: when master flag off → legacy FIFO path (byte-identical pre-F1). When on → new `IntakePriorityQueue` backs the intake queue; dequeue loop honors heap + reserved-slot + deadline semantics.
- Shadow-mode telemetry: even when flag off, optionally build a parallel `IntakePriorityQueue` and log "what would have been dequeued next" vs "what was actually dequeued next" for diagnostic delta. Gated by separate `JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW`.
- Parity tests in `tests/governance/intake/test_unified_intake_router_f1.py` — byte-identical behavior under flag-off across the existing intake test matrix.

**Scope**: ~200 LOC router wiring + ~300 LOC parity tests. Master flag default-off.

**Proof criterion**: full existing intake test suite green on both flag-on and flag-off paths. Shadow mode produces log-delta evidence on real session showing "priority-scheduler would have dequeued seed at T+Xs vs legacy did so at T+∞".

### Slice 3 — Reachability supplement extension + integration test for seed-starvation repro

**Deliverables**:
- Extend `tests/governance/test_parallel_dispatch_reachability_supplement.py` with a new variant: construct burst of 20 BG envelopes + 1 critical seed envelope; run through `UnifiedIntakeRouter` with `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true`; assert seed is dequeued within deadline (5s).
- Integration test that under flag-off + same burst pattern, seed starves past 30s → locks in the S1 repro as a regression guard.
- Optionally: end-to-end test chaining BacklogSensor → priority queue → UrgencyRouter → dispatch_pipeline fan-out, replacing / extending the F2 Slice 3 E2E test with a burst-scenario variant.

**Scope**: ~400 LOC tests. No code changes to production paths.

**Proof criterion**: reproducer test green (flag-on); starvation-regression test green (flag-off → starvation as expected, proves we haven't accidentally broken the repro).

### Slice 4 — Live graduation cadence + operator-authorized default flip

**Deliverables**:
- 3 clean live sessions under master flag on. Success criteria per session:
  - `session_outcome=complete`, `stop_reason ∈ {idle_timeout, budget_exhausted}`
  - `[ParallelDispatch] > 0` ✓
  - `enforce_submit_start > 0` ✓
  - `[Intake] priority_inversion count = 0` (or all logged + not-load-bearing)
  - `[Intake] backpressure_applied` events present if burst conditions met
  - 3 target files actually written (F2 seed completes APPLY)
- If 3/3 clean → separate operator message authorizing default flip.
- Commit flipping `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED` default `false → true` (with FlagRegistry seed updated in the same commit).
- Post-flip confirmation soak with no env overrides.
- Matrix row: Wave 3 (6) Slice 5b FINAL unblocked.

## Cross-links

- **Graduation blocker**: Wave 3 (6) Slice 5b — `live_reachability=blocked_by_intake_starvation` per S1 `bt-2026-04-24-062608`. See `project_wave3_item6_graduation_matrix.md`.
- **Sibling P0**: `project_followup_battle_test_post_summary_hang.md` — harness bounded-shutdown + executor audit. Co-ships or immediately after F1 Slice 1. Kills the 9-zombie Py_FinalizeEx wedge class.
- **Sibling P1** (deferred): per-op lifecycle ledger / temporal observability surface. Ships after F1 Slice 1 or in parallel once P0-2 stabilizes the harness.
- **F2 precedent**: `project_followup_f2_backlog_urgency_hint_schema.md`. Same graduation cadence pattern (scope → Slice 1 primitive → Slice 2 wiring → Slice 3 integration → Slice 4 live + flip).
- **SensorGovernor enforcement interlock** (second-order, not in F1): once F1 Slice 2 lands, plugging `governor.request_budget(...)` check into the enqueue path is an additive Slice 5 candidate. Not covered here.

## Status

- **Scope authorized 2026-04-24** per operator P0 binding after F2 graduation S1 failure classification.
- **Slices 1+2+3 SHIPPED 2026-04-24** on clean branch `feat/f1-intake-priority-scheduler` (forked from `main` @ `4bdc9f58d5` per operator hygiene directive).
  - **Slice 1**: `528358dfdd` (cherry-pick of `21bb9e354c` from the original battle-test branch — clean-branch hygiene applied).
  - **Slice 2**: `9685f2931c` (router wiring, auto-committed by hook) + `3309e5e64e` (parity tests).
  - **Slice 3**: `e97bb9b60d` (integration + starvation-regression tests).
- **Combined test count**: 93 F1 tests (60 primitive + 22 router wiring + 11 integration); 434/434 green across F1 + F2 + parallel_dispatch + flag_registry regression.
- **Slice 1 test count**: 60/60 green on F1 suite; 273/273 green across F1 + F2 + flag_registry + reachability supplement.
- **Slice 1 files landed**:
  - `backend/core/ouroboros/governance/intake/intake_priority_queue.py` — new file, 366 LOC primitive. `IntakePriorityQueue` class with heapq-backed priority heap, reserved-slot starvation guard, per-envelope deadline with priority-inversion emergency pop, queue-depth telemetry, back-pressure. `URGENCY_RANK` map, `_DEFAULT_DEADLINES_S`, `EnqueueResult` + `DequeueDecision` frozen dataclasses, `_HeapEntry` heapq-compatible entry with `field(compare=False)` on non-comparable fields. Env knob helpers: `_intake_priority_scheduler_enabled` / `_reserved_dequeue_n` / `_reserved_dequeue_m` / `_back_pressure_threshold`. **Authority invariant**: zero imports of orchestrator / policy / iron_gate / risk_tier / change_engine / candidate_generator / gate / semantic_guardian — grep-pinned by test.
  - `backend/core/ouroboros/governance/flag_registry_seed.py` — +4 FlagSpec entries: `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED` (SAFETY, BOOL default False), `JARVIS_INTAKE_RESERVED_N` (TUNING, INT default 5), `JARVIS_INTAKE_RESERVED_M` (TUNING, INT default 1), `JARVIS_INTAKE_BACKPRESSURE_THRESHOLD` (CAPACITY, INT default 200). Surfaces via `/help flag <NAME>` and `/help flags --category`.
  - `tests/governance/intake/test_intake_priority_queue.py` — 60 tests. Coverage: urgency rank map, default deadlines, master flag parsing (5 truthy + 6 falsy/unknown), 3 env knob parsers (default / override / invalid-fallback / clamped), empty queue behavior (4 tests), basic enqueue/dequeue (3), heap priority ordering (5 including full 4-way order + FIFO within equal + unknown/missing urgency), reserved-slot guard (4 including M=0 disable + warmup inert + no-op when only-low + force-pop after window fills), deadline inversion (5 including out-of-order + beats priority + inf never fires + explicit override + telemetry field validation), back-pressure (5 including rejects normal/low + always admits critical + below-threshold admits + telemetry), telemetry sink (3 including exception safety), snapshot_depths, oldest_wait_s (3 including urgency filter), starved_budget_pct (2), authority-import grep pin, **S1 repro scenario** (3 tests: FIFO failure-mode documentation + priority-queue version proves seed dequeues FIRST despite enqueue-last + waited_s=0).
- **Zombie collateral from S1**: resolved — PID 57884 killed via signature-match predicate, lock released (2026-04-24). Harness epic ticket contains 9th zombie record.
- **Slice 2 files landed**:
  - `backend/core/ouroboros/governance/intake/unified_intake_router.py` — +181 LOC: imports `IntakePriorityQueue` + `_intake_priority_scheduler_enabled`; adds `_intake_priority_scheduler_shadow_enabled()` helper (SAFETY invariant: read at call time); `__init__` builds `self._priority_queue: Optional[IntakePriorityQueue]` when master OR shadow flag on, with telemetry sink that elevates `priority_inversion` + `backpressure_applied` to WARNING; `ingest()` mirrors envelope to priority queue after legacy `put()`; `_dispatch_loop()` has two branches gated by `_f1_master_on` — primary-mode reads from priority queue + drains legacy as tombstone, shadow-mode reads from legacy + consumes priority queue for delta comparison + logs `[IntakePriority shadow_delta]` on divergence. `_legacy_task_done_owed` local flag ensures task_done() bookkeeping stays balanced across both branches.
  - `backend/core/ouroboros/governance/flag_registry_seed.py` — +42 LOC: new `JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW` FlagSpec (OBSERVABILITY, BOOL default False); updated `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED` description to reflect primary-mode semantics.
  - `tests/governance/intake/test_unified_intake_router_f1.py` — 22 tests: shadow flag helper parsing (default-off + 5 truthy + 5 falsy), flag-off parity (3 tests: priority_queue is None, counters zero, ingest doesn't touch priority queue), shadow-mode wiring (2: built when shadow on, mirrors ingest), primary-mode wiring (3: built when master on, master supersedes shadow, mirrors ingest), live dispatch primary-mode S1 repro (3 BG + 1 critical → critical dispatches first via patched `_dispatch_one`), shadow-delta logging (constructs legacy-vs-shadow divergence scenario via `doc_staleness+critical` source/urgency combo that legacy `_PRIORITY_MAP` ranks lower than `backlog+low` while shadow rank is reversed), authority invariant grep-pin (imports + log strings).
- **Behavioral parity (flag off)**: verified by `test_flag_off_*` family — priority queue is None, no F1 state touched, legacy dispatch path byte-identical to pre-F1.
- **S1 repro inverted**: `test_master_on_dispatch_order_critical_first` — 3 BG envelopes ingested first, 1 critical envelope ingested last, dispatch loop run, first envelope to reach `_dispatch_one` is the critical one regardless of FIFO enqueue order. The exact S1 failure mode (bt-2026-04-24-062608) no longer possible under primary-mode.
- **Slice 3 files landed**:
  - `tests/governance/intake/test_unified_intake_router_f1_integration.py` — 11 tests. Coverage: S1-scale primary-mode repro (3: critical dispatches first, all 21 drain, seed within 5s deadline); S1-scale flag-off structural regression (1: no F1 state, no F1 markers); shadow-mode observation (1: priority queue mirrors all 21); back-pressure integration (2: low-threshold refuses BG but admits critical, WARNING telemetry emitted); primary-mode supersedes shadow (1); coalesce-zero + F1 dispatch order (1); flag-off no F1 back-pressure (1); authority-import grep pin (1).
  - Test helper `_ingest_s1_shape()` reproduces the actual S1 burst: 20 doc_staleness/normal envelopes + 1 backlog/critical seed.
  - `_make_config()` raises router-level `backpressure_threshold=500` and `max_queue_size=500` to isolate the F1-level threshold from the legacy router-level threshold during testing.
- **Behavioral parity (flag off)**: verified via `test_s1_scale_flag_off_f1_state_absent` + `test_flag_off_no_backpressure_refusal` — priority queue is None, no F1 logs, no F1 state mutations.
- **S1 repro inverted at S1 scale**: `test_s1_scale_primary_mode_critical_dispatches_first` drives the full 21-envelope S1 burst through the router's `_dispatch_loop`; first dispatched envelope is the critical seed by dedup_key match.
- **Identity via dedup_key**: router's `envelope.with_lease()` rewrites the envelope before queueing, so the dispatched instance is a fresh object. Identity checks use `dedup_key` equality (stable across lease rewrites) rather than `is`.
- **Slice 4 (live graduation cadence + default flip) PAUSED post-S1b 2026-04-25** — F1+F2 plumbing PROVEN live for the first time, but Slice 4 default flip not eligible (needs ≥1 live session with successful multi-file generation reaching post-GENERATE seam; provider exhaustion gating).
  - **session_id**: `bt-2026-04-25-054256`, MERGE_HEAD_SHA `b068c8a083`
  - **env matrix**: `JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED=true` (F1 master, FIRST EVER LIVE), `JARVIS_BACKLOG_URGENCY_HINT_ENABLED=true` (F2), `JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED=true`, `JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE=true`, `JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY=critical` (F3), 18× sensor mute intervals @ 86400000s
  - **stop**: idle_timeout / **outcome**: complete / **duration**: 1510s / **cost**: $0.16 (well under $2.00 cap)
  - **F1 PROVEN**: 8 `[IntakePriority] primary dequeue` markers across critical/high/normal/low; seed dequeued first in 0.10s waited; 0 `priority_inversion`; 0 traceback frames in extended blocklist (`parallel_dispatch.py`/`worktree_manager.py`/`phase_runners/`/`phase_dispatcher.py`)
  - **F2 PROVEN**: 3 `envelope_routing_override` markers; seed routed `route=standard, route_reason=envelope_routing_override:standard` via priority-0.5 clause (was IMMEDIATE in S1a without F2)
  - **W3(6) reachability BLOCKED downstream of F1+F2**: seed exhausted at GENERATE via `all_providers_exhausted:fallback_failed` (DW primary failed → Claude fallback `CancelledError` after 72.8s sem_wait × 3 retries); post-GENERATE seam never invoked on seed; only `[ParallelDispatch]` line was `empty_candidate_list` from a non-seed op
  - **Block class**: `live_reachability=blocked_by_provider_exhaustion` (was `blocked_by_intake_starvation` in W3(6) S1+S2+S3); promoted upward — structural intake failure mode FIXED, downstream provider transport is the new gate
  - **Operator binding 2026-04-25**: lock in S1b as cadence record; do NOT mask provider latency via Option C (modify seed) or Option D (inflate timeouts); `anthropic_transport` infra waiver class, not cognitive/routing failure within O+V
  - **Cadence verdict**: 1 of 3 sessions recorded; PAUSED — no S2/S3 launched. Slice 4 default flip awaits unblock path (provider transport stability, OR organic accumulation of multi-file generations from any sensor under steady-state operation).

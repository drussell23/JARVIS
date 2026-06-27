---
title: Slice 5 Arc B — Closed
modules: [tests/governance/test_slice5_arc_b_wiring.py, tests/governance/test_posture_observer.py, backend/core/ouroboros/governance/autonomy/subagent_scheduler.py, backend/core/ouroboros/governance/ide_observability_stream.py]
status: merged
source: project_slice5_arc_b.md
---

# Slice 5 Arc B — Closed

**Authorized:** 2026-04-21 (operator grant, narrow-path direct-enforce)
**Landed:** 2026-04-21

## What it does

Makes the `MemoryPressureGate` (Wave 1 #3) actually gate something. Previously advisory-only: graduated + observability surfaces shipped, but no code consulted `can_fanout()`. Arc B wires the single L3 parallel-dispatch site in `SubagentScheduler._run_graph` to consult the gate before `asyncio.gather()` fans out work units.

## The seam

`subagent_scheduler.py:_run_graph` already has a two-stage selection pattern:

1. `_select_ready_batch(graph, ready)` returns `(selected, deferred)` — selected within `graph.concurrency_limit`, path-conflicting units pushed to deferred
2. `_run_selected_units(graph, selected)` dispatches via `asyncio.gather()`

Arc B inserts a gate consultation between stages 1 and 2. If the gate clamps `n_requested → n_allowed`, the scheduler moves the overflow from `selected` to `deferred` and proceeds with a smaller batch. The overflow replays on the next `_run_graph` iteration via the existing deferred-queue pattern — **zero work loss**, no new failure_class, no WorkUnitResult marker.

## Four deterministic dispositions

Classified inside `_consult_memory_gate()` from the `FanoutDecision.reason_code`:

| Disposition | Trigger | Behavior |
|---|---|---|
| `allow` | `memory_pressure_gate.ok` | No clamp, proceed |
| `clamp` | `memory_pressure_gate.capped_to_N_at_LEVEL` | Move overflow to deferred |
| `disabled` | `memory_pressure_gate.disabled` | Pass-through (gate master off) |
| `probe_fail` | `memory_pressure_gate.probe_*` | Pass-through (probe unreliable/failed) |

## Observability — immutable log + SSE

**Log** (every decision):
```
[SubagentScheduler] fanout_gate: graph=<id> disposition=<allow|clamp|disabled|probe_fail>
  requested=<N> allowed=<M> level=<ok|warn|high|critical> reason=<reason_code>
```
INFO level for allow/disabled/probe_fail; WARNING for clamp.

**SSE** — new event type `memory_fanout_decision` via `publish_memory_fanout_decision_event(graph_id, disposition, decision)`:
- Fires on every decision (not just clamps) — operator gets full §8 audit trail
- Payload: `graph_id, disposition, n_requested, n_allowed, level, free_pct, reason_code, source`
- Best-effort, never-raise into scheduler

Reason: scheduler call rate is bounded by graph-execution cadence (~1/s upper bound under sustained L3 load), so per-decision SSE is cheap.

## Authorization adherence

- ✅ Direct-enforce (per operator's "narrow path" ratification)
- ✅ Deterministic reason codes (4-way classification, exhaustive)
- ✅ Immutable log + SSE on allow/deny/clamp (all 4 dispositions both)
- ✅ Tests for WARN/HIGH/CRITICAL clamp + disabled passthrough (18/18 green)
- ✅ Grep-clean authority invariant — no orchestrator/policy/iron_gate/risk_tier/change_engine/candidate_generator imports introduced
- ✅ Graduation note (this file)
- ✅ No new silent parallelism reduction — every clamp is logged + SSE-published

## Tests (18)

`tests/governance/test_slice5_arc_b_wiring.py`:

**Disposition classification (5)** — OK → allow; WARN → clamp(8); HIGH → clamp(3); CRITICAL → clamp(1); gate-disabled → disabled disposition

**OK passthrough (2)** — requested under cap no-clamp; WARN-level requested-under-cap returns n_allowed=n_requested

**Scheduler clamp semantics (3)** — clamp moves overflow to deferred (zero work loss, N-out-of-N preserved); no-clamp at OK; gate-None (outage) means no mutation

**SSE wiring (4)** — event type in whitelist; publish disabled returns None; publish enabled emits + broker receives; full `_consult_memory_gate` → SSE round-trip

**Probe failure safety (2)** — probe.ok=False → pass-through with `probe_fail` disposition; probe raises → consultation returns None (scheduler doesn't break)

**Authority invariant (2)** — grep-clean on `subagent_scheduler.py` Arc B additions + `publish_memory_fanout_decision_event` function body

## Regression status

**606 / 608 on my-touched surface** (Wave 1 + Arc A + Arc B + hygiene fixes)

Full-governance-suite regression surfaced 64 pre-existing failures across 26 test files, 6 of which are in files my Arc A touched. Stash-verified these fail WITH Arc B removed — so Arc B does not regress anything. 2 of those 6 fixed in this commit (hygiene fixes for Track A seed-sync assumptions in `test_posture_observer.py`). The remaining 4 + the 58 in other files are pre-existing or Arc-A-induced and are filed as out-of-scope follow-ups for this PR.

## Bundled hygiene

`tests/governance/test_posture_observer.py` — 2 tests (`test_format_for_prompt_without_posture_when_master_off`, `test_format_for_prompt_no_crash_when_store_empty`) failed post-Track-A-seed-sync because they assumed master=false default. Fixed by:
- First test: `monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "false")` — test name said "master off" but never set it
- Second test: `reset_default_store()` + `get_default_store(tmp_path/.jarvis)` to isolate from the live-fire's real-repo `.jarvis/posture_current.json`

## Live-fire proof (session `bt-2026-04-22-035305`)

Boot sequence clean end-to-end:
```
[GovernedLoop] PostureObserver started (Slice 5 Arc A)
[GovernedLoop] Wave 1 SSE bridges installed (posture + governor + memory-pressure)
[GovernedLoop] L3 SubagentScheduler wired: state_dir=... max_graphs=2
```

`.jarvis/posture_current.json` → **EXPLORE @ 0.987**, hash `c8f1cd07`.

Session duration 159.5s, $0.00 cost, idle_timeout clean exit. No `fanout_gate` log lines because no L3 graph was dispatched in this short idle session (L3 fan-out requires multi-file architectural ops). Clamp/allow/SSE paths exercised via the 18 integration tests; live-fire verifies the module loads + boots cleanly + doesn't break the organism.

## Files changed

- `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py` — `_consult_memory_gate` method (+77 lines) + 8-line clamp block in `_run_graph` (+10 lines)
- `backend/core/ouroboros/governance/ide_observability_stream.py` — `EVENT_TYPE_MEMORY_FANOUT_DECISION` constant + whitelist entry + `publish_memory_fanout_decision_event` helper (+47 lines)
- `tests/governance/test_slice5_arc_b_wiring.py` — new, 18 integration tests (+385 lines)
- `tests/governance/test_posture_observer.py` — 2 hygiene fixes (+14 lines, -5 lines)

## Operator kill switch

```bash
export JARVIS_MEMORY_PRESSURE_GATE_ENABLED=false
```

Reverts to pre-Arc-B behavior. Gate `can_fanout()` returns pass-through with `reason_code="memory_pressure_gate.disabled"`, scheduler `_consult_memory_gate` classifies as `disposition=disabled`, no clamp ever applied. No code change, no restart required.

## What's next

Wave 1 + Slice 5 Arc A + Arc B all merged → **post-Slice-5 enforcement soak** (agent-conducted OK, same template variant) focused on real throttle/deny signals under intake enforce + memory gate stress. Per operator: "optional if time-constrained but preferred."

**Wave 2 / orchestrator refactor** — remains on hold until the post-Slice-5 soak is captured + reviewed.

---
title: Slice 5 Arc A — Closed
modules: [tests/governance/test_slice5_arc_a_wiring.py, backend/core/ouroboros/governance/intake/unified_intake_router.py, backend/core/ouroboros/governance/governed_loop_service.py, backend/core/ouroboros/governance/posture_observer.py, unified_supervisor.py, backend/core/ouroboros/governance/autonomy/subagent_scheduler.py]
status: merged
source: project_slice5_arc_a.md
---

# Slice 5 Arc A — Closed

**Authorized:** 2026-04-21 (operator grant)
**Landed:** 2026-04-21

## What it does

**Wires Wave 1 primitives from graduated-but-silent into the hot path**, while preserving the graduation discipline of "no behavior change on by-default":

1. **`UnifiedIntakeRouter.ingest()`** now consults `SensorGovernor.request_budget()` between the existing backpressure check and the WAL/enqueue steps. Default mode is **shadow** — logs would-be denials but lets envelopes through — so there is no intake-throttling behavior change for operators who don't flip `JARVIS_INTAKE_GOVERNOR_MODE=enforce`.

2. **`GovernedLoopService.start()`** spawns the `PostureObserver` at the single canonical boot site (used by both `battle_test/harness.py` and `unified_supervisor.py`, which delegate here). Without this wiring, `SensorGovernor.default_posture_fn` returns `None` → posture weights collapse to 1.0 → the whole posture-weighting story is inert. The soak finding #1 from the advisory soak is now resolved.

3. **Three SSE bridges installed** at the same boot site: `bridge_posture_to_broker`, `bridge_governor_to_broker`, `bridge_memory_pressure_to_broker`. Live observability for all three Wave 1 primitives.

4. **Graceful shutdown** — `GovernedLoopService.stop()` awaits `PostureObserver.stop()` via `getattr(..., None)` so it's safe even if start() never ran.

## Three env knobs

| Flag | Default | Behavior |
|---|---|---|
| `JARVIS_INTAKE_GOVERNOR_MODE` | `shadow` | `off` = skip consultation; `shadow` = log would-be denies + allow; `enforce` = honor denies (return `"governor_throttled"`) |
| `JARVIS_DIRECTION_INFERRER_ENABLED` | `true` (graduated) | When false, observer startup is no-op; governor falls back to unweighted caps |
| `JARVIS_SENSOR_GOVERNOR_ENABLED` | `true` (graduated) | When false, `request_budget()` returns allow-with-reason so intake passes through |

## Translation layer (small surface bug this revealed + fixed)

`IntentEnvelope.source` uses snake_case (`"test_failure"`) while `SensorBudgetSpec.sensor_name` uses CamelCase (`"TestFailureSensor"`). Neither could be renamed without breaking existing test surface, so Arc A adds two module-level translation maps in `unified_intake_router.py`:

- `_SOURCE_TO_GOVERNOR_SENSOR` — 15 mappings covering every envelope source that has a governor spec; unmapped sources fall through to `"governor.unregistered_sensor"` (always-allow safe default)
- `_URGENCY_STR_TO_GOVERNOR` — `critical → immediate, high → standard, normal → complex, low → background`

## Pre-existing bug surfaced + fixed

`posture_observer.py::get_default_observer()` acquires `_singleton_guard` then nested-calls `get_default_store()` which re-acquires the same non-reentrant `threading.Lock` → deadlock. Arc A integration tests hit this pattern first. Fix: `_singleton_guard = threading.RLock()`. One-line change, no behavior impact except allowing the previously-deadlocking path to succeed.

## Tests

- **26 new tests** in `tests/governance/test_slice5_arc_a_wiring.py` — translation maps, mode env parsing, governor helpers in isolation, full ingest path (shadow / enforce / off), observer singleton + idempotent start, governed-loop stop() safety pattern, authority invariant
- **590/590 combined** across Wave 1 + Arc A after changes (no regressions)

## Live-fire proof (session `bt-2026-04-22-031218`)

First battle-test session where the organism observed its own posture during live ops:

```
2026-04-21T20:12:29 [PostureObserver] started interval=300.0s window=900.0s
2026-04-21T20:12:29 [GovernedLoop] PostureObserver started (Slice 5 Arc A)
2026-04-21T20:12:29 [GovernedLoop] Wave 1 SSE bridges installed (posture + governor + memory-pressure)
2026-04-21T20:12:36 [FSEventBridge] First fs.changed event published:
                    topic=fs.changed.created path=.jarvis/posture_current.json
```

Artifacts after session:
- `.jarvis/posture_current.json` → **EXPLORE @ 0.988 confidence** (hash 52eafed6)
- `.jarvis/posture_history.jsonl` → 1 entry

Compare to the pre-Arc-A soak (sessions `bt-2026-04-22-024636` + `bt-2026-04-22-025106`) where both files never existed and zero `posture_observer` log lines appeared — that was the whole finding that motivated Arc A.

Session stats: 156.8s, $0.00 cost, idle_timeout clean exit. No governor SHADOW/ENFORCE deny lines because no sensor hit its cap during the short window (expected for a <3min run with 200-ops/hour global cap).

## Files changed

- `backend/core/ouroboros/governance/intake/unified_intake_router.py` — +86 lines (2 module-level maps, 1 mode helper, 2 instance helpers, 1 ingest insertion, 1 record_emission call)
- `backend/core/ouroboros/governance/governed_loop_service.py` — +50 lines (observer startup + 3 SSE bridges + stop-path cleanup)
- `backend/core/ouroboros/governance/posture_observer.py` — `Lock()` → `RLock()` (1-line bugfix)
- `tests/governance/test_slice5_arc_a_wiring.py` — new, 26 tests

## What's next

**Arc B** — `subagent_scheduler.py` consults `MemoryPressureGate.can_fanout()` on L3 fan-out. Direct-enforce approved by operator (path is narrow). Same PR discipline: tests + observable deny/metric signals + short graduation note.

**Post-merge short soak** — after Arc A + Arc B land, operator ran or agent-conducted soak (same template variant) focused on real deny/throttle signals under load. Operator marked it "optional if time-constrained but preferred."

**Still held:** Wave 2 / orchestrator refactor — no work until both arcs merge.

## Flipping from shadow to enforce (operator reference)

When operator is satisfied with shadow-mode observations (no unexpected deny patterns in debug.log over N sessions):

```bash
export JARVIS_INTAKE_GOVERNOR_MODE=enforce
```

That's it. Denies start returning `"governor_throttled"` to sensors instead of just being logged. No code change, no restart-sensitive behavior, instantly bidirectional via env flip.

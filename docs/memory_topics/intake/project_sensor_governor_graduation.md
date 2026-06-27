---
title: SensorGovernor + MemoryPressureGate Arc — CLOSED
modules: [backend/core/ouroboros/governance/intake/unified_intake_router.py, backend/core/ouroboros/governance/autonomy/subagent_scheduler.py]
status: merged
source: project_sensor_governor_graduation.md
---

# SensorGovernor + MemoryPressureGate Arc — CLOSED

**Graduated 2026-04-21.** Wave 1 #3 of the A-level-execution roadmap is live. **Wave 1 plan (DirectionInferrer + FlagRegistry + SensorGovernor + MemoryPressureGate) fully graduated.**

## What graduated

Two master flags flipped `false → true`:
- `JARVIS_SENSOR_GOVERNOR_ENABLED` — rolling-window op cap across 16 sensors
- `JARVIS_MEMORY_PRESSURE_GATE_ENABLED` — advisory memory-pressure signal

All surfaces active with zero env setup:

1. **SensorGovernor primitive** — `request_budget(sensor, urgency) → BudgetDecision(allowed, ...)` with posture-weighted caps, urgency multipliers, emergency brake, rolling window.
2. **MemoryPressureGate primitive** — `can_fanout(n) → FanoutDecision` with probe cascade + per-level caps.
3. **/governor REPL** — 6 subcommands (status/explain/history/reset/memory/help).
4. **IDE GET** — `/observability/governor{,/history}` + `/observability/memory-pressure` (loopback + rate-limit + CORS + schema v1.0 + double-gated).
5. **SSE** — `governor_throttle_applied` + `governor_emergency_brake` + `memory_pressure_changed` via two bridges.
6. **13 flags auto-registered in FlagRegistry** (6 governor + 7 gate) at `ensure_seeded()` / `ensure_bridged()` time.

Explicit `=false` per-flag reverts respective surfaces in lockstep (proven bidirectional). `/governor help` still works master-off for discoverability.

## Commits

```
40b354f43a Slice 1 — SensorGovernor primitive + 16-sensor seed (54 tests, 16 live-fire)
4e7996241c Slice 2 — MemoryPressureGate primitive (44 tests, 17 live-fire)
f405780c84 Slice 3 — /governor REPL + GET + SSE (32 tests, 31 live-fire)
8fff33fe50 Slice 3 — addendum (live-fire + script)
<pending>   Slice 4 — graduation + 47 pins (26 live-fire)
```

## Final numbers

| Dimension | Count |
|---|---|
| Python test files | 4 |
| Tests green | **177/177** combined (54 + 44 + 32 + 47) |
| Live-fire scripts | 4, all PASS |
| Live-fire checks | 16 + 17 + 31 + 26 = **90 total** |
| Graduation pins | 47 (8 authority + 14 behavioral + 10 graduation-specific + 4 docstring + 3 schema + 4 integration + 2 full-revert + 4 CLAUDE.md) |
| LoC new | ~2500 Python (governor + seed + gate + REPL) |
| LoC integration | ~400 (ide_observability + ide_observability_stream extensions) |
| Authority pins | 4 arc files + 3 GET handler methods + 5 SSE bridges |
| Seeded sensors | **16** (with 16 × 4 = 64 posture weights) |
| Auto-registered flags (Wave 1 #2 bridge) | **13** |

## First arc to consume BOTH prior Wave 1 graduations

**Wave 1 #1 consumer (DirectionInferrer posture):**
- Default `posture_fn` reads `get_default_store().load_current().posture.value`
- Default `signal_bundle_fn` reads evidence as a dict for emergency brake thresholds
- Slice 1 live-fire on real HARDEN store: TestFailureSensor → cap=36 (20 × 1.8)

**Wave 1 #2 consumer (FlagRegistry):**
- `ensure_seeded()` auto-registers 6 governor flags
- `ensure_bridged()` auto-registers 7 gate flags
- Each flag carries category (safety/capacity/timing/tuning), source_file, description, example, since="v1.0", optional posture_relevance

Downstream `/help flags --search governor` and `/help flags --search memory_pressure` work out of the box.

## Posture-weight matrix (16 × 4 = 64 weights)

Stabilization (HARDEN-favored):
- TestFailureSensor: 1.8 HARDEN, 0.8 EXPLORE
- RuntimeHealthSensor: 1.5 HARDEN
- PerformanceRegressionSensor: 1.5 HARDEN

Discovery (EXPLORE-favored):
- OpportunityMinerSensor: 1.5 EXPLORE, 0.3 HARDEN
- ProactiveExplorationSensor: 1.5 EXPLORE, 0.3 HARDEN
- IntentDiscoverySensor: 1.4 EXPLORE, 0.5 HARDEN
- CapabilityGapSensor: 1.3 EXPLORE
- WebIntelligenceSensor: 1.3 EXPLORE, 0.7 HARDEN

Consolidation (CONSOLIDATE-favored):
- DocStalenessSensor: 1.3 CONSOLIDATE, 0.6 HARDEN
- TodoScannerSensor: 1.4 CONSOLIDATE, 0.8 HARDEN
- BacklogSensor: 1.3 CONSOLIDATE, 0.6 HARDEN
- CrossRepoDriftSensor: 1.2 CONSOLIDATE

Integration / neutral:
- GitHubIssueSensor: 1.2 HARDEN
- VoiceCommandSensor: neutral (user-driven)
- VisionSensor: 1.2 EXPLORE
- ScheduledSensor: neutral (cron)

## 47 graduation pins

| Group | Pins | What |
|---|---|---|
| A. Authority | 8 | grep-enforced zero-imports on 4 arc files + 3 GET handlers + 5 SSE bridges |
| B. Behavioral | 14 | governor/gate disabled paths, brake activation, global cap, rolling window, pressure levels, fanout matrix, probe cascade, probe-raise safety, posture-weight math, urgency multipliers, record_emission, reset, unregistered-sensor allow |
| C. Graduation-specific | 10 | both `True` literals, both flags enabled on default, 16-sensor seed unchanged, 4 postures, default cap/threshold/timing values pinned, key posture weights locked |
| C'. Docstring bit-rot | 4 | Tier 0 + advisory citations in both module docstrings |
| D. Schema version | 3 | governor + gate + SSE frame all `"1.0"` literal |
| E. Integration | 4 | governor + gate auto-registration in FlagRegistry, both GETs double-gated |
| F. Full-revert matrix | 2 | governor revert; gate revert |
| G. CLAUDE.md doc | 4 | both classes + both master flag names mentioned |

## Full-revert matrix proof

```
[graduated] → both primitives + REPL + 3 GETs + SSE all active
[JARVIS_SENSOR_GOVERNOR_ENABLED=false]
  → /governor status rejected, /governor help still works,
    GET /governor 403, GET /governor/history 403
[unset; JARVIS_MEMORY_PRESSURE_GATE_ENABLED=false]
  → /governor memory rejected, GET /memory-pressure 403
[both=false]
  → both GETs 403 in lockstep
[both unset] → all surfaces back to 200/ok bidirectional
```

## What's next

**Wave 1 complete.** Three graduated arcs:
```
Wave 1 #1 DirectionInferrer + StrategicPosture  ✅ 2026-04-21
Wave 1 #2 FlagRegistry + /help dispatcher       ✅ 2026-04-21
Wave 1 #3 SensorGovernor + MemoryPressureGate   ✅ 2026-04-21
```

**Combined Wave 1 totals:**
- 208 + 173 + 177 = **558 tests green**
- 89 + 129 + 90 = **308 live-fire checks**
- 43 + 38 + 47 = **128 graduation pins**
- 11 arc files authority-pinned (6 + 3 + 4 − overlap in ide_observability)

**Slice 5 (deferred) for Wave 1 #3:**
- Wire `unified_intake_router.py` to call `governor.request_budget()` before emitting IntentEnvelope ops
- Wire `subagent_scheduler.py` to call `gate.can_fanout()` before L3 worktree creation
- Each wiring gets its own 3-session clean arc per graduation discipline
- Only lands when integration is proven safe under live ops (not speculative)

**Wave 2 roadmap (not yet planned):**
- Orchestrator refactor — 8.9K-line file past the patch-vs-refactor threshold
- Live-integration of all three Wave 1 arcs into the production op loop
- Sensor-aware cost accounting (posture-weighted budget vs actual $ burn reconciliation)

## Operator reference — complete flag cascade

| Flag | Default | Effect when false |
|---|---|---|
| `JARVIS_SENSOR_GOVERNOR_ENABLED` | `true` | All governor surfaces revert (master kill) |
| `JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR` | `200` | — (tuning) |
| `JARVIS_SENSOR_GOVERNOR_WINDOW_S` | `3600` | — (tuning) |
| `JARVIS_SENSOR_GOVERNOR_EMERGENCY_REDUCTION_PCT` | `0.2` | — (tuning) |
| `JARVIS_SENSOR_GOVERNOR_EMERGENCY_COST_THRESHOLD` | `0.9` | — (tuning) |
| `JARVIS_SENSOR_GOVERNOR_EMERGENCY_POSTMORTEM_THRESHOLD` | `0.6` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_GATE_ENABLED` | `true` | All gate surfaces revert |
| `JARVIS_MEMORY_PRESSURE_WARN_PCT` | `30.0` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_HIGH_PCT` | `20.0` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_CRITICAL_PCT` | `10.0` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_WARN_FANOUT_CAP` | `8` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_HIGH_FANOUT_CAP` | `3` | — (tuning) |
| `JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP` | `1` | — (tuning) |

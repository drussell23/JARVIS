---
title: SensorGovernor + MemoryPressureGate — 5-Slice Arc
modules: [backend/core/ouroboros/governance/]
status: historical
source: project_sensor_governor_plan.md
---

# SensorGovernor + MemoryPressureGate — 5-Slice Arc

**Wave 1 priority #3** — closes the "truly unattended" gap. First production downstream of both prior Wave 1 arcs: SensorGovernor weights per-sensor op budgets by DirectionInferrer posture (#1) and registers its flags through FlagRegistry (#2). MemoryPressureGate provides advisory signal for worktree fan-out decisions.

## Problem

- **16 autonomous sensors** can each emit ops whenever their trigger condition is met. No global throttle exists. Under sustained load (e.g., a test-suite regression triggering TestFailureSensor every 5 minutes AND OpportunityMinerSensor firing in parallel), the BackgroundAgentPool saturates and budget burns regardless of posture.
- **No posture-aware throttling.** When organism is HARDENing (rising postmortems), we want *more* TestFailure budget and *less* OpportunityMiner; under EXPLORE the inverse. Currently everyone gets the same cadence.
- **No emergency brake.** Runaway cost or failure rate has no circuit breaker — budget governors react per-op but not system-wide.
- **Worktree fan-out balloons memory.** Each `unit-*` worktree is a full working copy. Parallel L3 execution under memory pressure can OOM the harness.

## Solution shape

**SensorGovernor** — rolling-window op-emission counter + posture-weighted per-sensor cap + global cap + emergency brake. Sensors call `governor.request_budget(sensor, urgency)` before emitting; governor returns `BudgetDecision(allowed, reason, remaining)`. Reports to SSE + GET + REPL.

**MemoryPressureGate** — consults psutil (or /proc/meminfo / vm_stat / fallback) and returns `FanoutDecision(allowed, reason, n_allowed)` for a requested parallel unit count. Advisory — `subagent_scheduler.py` chooses to honor it; the gate doesn't reach into the scheduler.

**Per-posture sensor weight table (16 × 4 = 64 weights):**

| Sensor | EXPLORE | CONSOLIDATE | HARDEN | MAINTAIN |
|---|---|---|---|---|
| TestFailure | 0.8 | 1.0 | **1.8** | 1.0 |
| RuntimeHealth | 1.0 | 1.0 | **1.5** | 1.0 |
| PerformanceRegression | 1.0 | 1.0 | **1.5** | 1.0 |
| OpportunityMiner | **1.5** | 0.5 | 0.3 | 1.0 |
| ProactiveExploration | **1.5** | 0.8 | 0.3 | 1.0 |
| IntentDiscovery | **1.4** | 0.8 | 0.5 | 1.0 |
| CapabilityGap | **1.3** | 1.0 | 0.8 | 1.0 |
| WebIntelligence | **1.3** | 1.0 | 0.7 | 1.0 |
| DocStaleness | 0.8 | **1.3** | 0.6 | 1.0 |
| TODOScanner | 1.0 | **1.4** | 0.8 | 1.0 |
| Backlog | 1.0 | **1.3** | 0.8 | 1.0 |
| CrossRepoDrift | 1.0 | 1.2 | 1.0 | 1.0 |
| GitHubIssue | 1.0 | 1.0 | 1.2 | 1.0 |
| VisionSensor | 1.2 | 0.8 | 1.0 | 1.0 |
| Scheduled | 1.0 | 1.0 | 1.0 | 1.0 |
| CUExecution | 1.0 | 1.0 | 1.0 | 1.0 |

## Design rulings (carry over + new)

1. **Authority-free** — both primitives are advisory. `request_budget()` returns a decision; callers choose to honor. `can_fanout()` same. Grep-pinned Slice 4.
2. **Master flags** — `JARVIS_SENSOR_GOVERNOR_ENABLED` + `JARVIS_MEMORY_PRESSURE_GATE_ENABLED` (default `false` Slice 1-3, both graduate Slice 4).
3. **Per-slice E2E live-fire** required.
4. **§5 Tier 0** — pure dict + counter ops; memory probe uses stdlib psutil / /proc fallback; zero LLM anywhere.
5. **Integration via pull, not push** — Slice 1-4 ship the primitive + surfaces. Slice 5 (deferred) wires `unified_intake_router.py` + `subagent_scheduler.py` to actually consult them. Graduation gates the surface, not enforcement.
6. **Register own flags in FlagRegistry** (Wave 1 #2 consumer) so `/help flags --search governor` works out of the box.
7. **Default caps generous** — don't hunger the system; posture-weighted adjustments nudge, don't starve. Emergency brake is the only hard cut.

## Categories of flags this arc introduces

- `safety` — master kill switches, emergency brake thresholds
- `capacity` — per-sensor base caps, global cap
- `tuning` — posture weights, urgency multipliers
- `timing` — rolling window seconds

## Location

```
backend/core/ouroboros/governance/
  sensor_governor.py              # SensorGovernor primitive (Slice 1)
  sensor_governor_seed.py         # 16-sensor budget table (Slice 1)
  memory_pressure_gate.py         # MemoryPressureGate primitive (Slice 2)
  governor_repl.py                # /governor REPL (Slice 3)
  ide_observability.py            # +GET /observability/governor (Slice 3)
  ide_observability_stream.py     # +SSE governor_throttle_applied (Slice 3)
```

## Slice 1 — `SensorGovernor` primitive + 16-sensor seed

**Goal:** Rolling-window + posture-weighted emission counter. Pure math, no integration.

**Deliverables:**
- `sensor_governor.py`:
  - `SensorBudgetSpec(dataclass, frozen)` — sensor_name, base_cap_per_hour, urgency_multipliers, posture_weights
  - `BudgetDecision(dataclass, frozen)` — allowed, reason_code, sensor_name, posture, weighted_cap, current_count, remaining
  - `SensorGovernor(class)` — `request_budget(sensor, urgency) → BudgetDecision`, `record_emission(sensor, urgency)`, `snapshot()`, `reset()`
  - Rolling window via deque with timestamp eviction
  - Per-posture weighted cap: `weighted_cap = int(base_cap * posture_weight * urgency_multiplier)`
  - Global cap enforcement
  - Emergency brake: when `signal_bundle.cost_burn > 0.9` or `postmortem_rate > 0.6`, caps drop to 20%
  - Posture reader: optional injectable `current_posture_fn`, defaults to Wave 1 #1 lookup
  - Thread-safe via `threading.Lock`
- `sensor_governor_seed.py`:
  - 16 `SensorBudgetSpec` entries + posture weight table (above)
  - `seed_default_governor(governor)` function
- Kill switch `JARVIS_SENSOR_GOVERNOR_ENABLED` (default `false`)
- Env knobs: `_GLOBAL_CAP_PER_HOUR` (default 200), `_WINDOW_S` (default 3600), `_EMERGENCY_REDUCTION_PCT` (default 0.2)
- Registers own flags in FlagRegistry if available (Wave 1 #2 consumer)

**Tests (~55):**
- SensorBudgetSpec construction + immutability
- Rolling-window eviction (time-based)
- Posture-weighted cap calculation
- Global cap enforcement
- Emergency brake (cost_burn > 0.9)
- Emergency brake (postmortem > 0.6)
- Request allows under cap, denies over
- Record emission grows counter
- snapshot() shape
- reset() clears state
- Thread-safety stress
- Urgency multipliers
- Posture injection (explicit fn vs default)
- 16 seeded sensors registered
- Each posture × sensor weight correct
- Authority-free grep
- Schema version pin

**Live-fire:**
- Seed governor → request_budget under each posture → verify expected allow/deny
- Inject high cost_burn → emergency brake activates
- Record 200 emissions → global cap kicks in
- Snapshot reports per-sensor remaining
- Flags registered in FlagRegistry (bridge validation)

## Slice 2 — `MemoryPressureGate` primitive

**Goal:** Advisory memory-pressure signal. Cross-platform stdlib fallback cascade.

**Deliverables:**
- `memory_pressure_gate.py`:
  - `PressureLevel(Enum)` — OK / WARN / HIGH / CRITICAL
  - `FanoutDecision(dataclass, frozen)` — allowed, reason_code, n_allowed, current_free_pct, level
  - `MemoryPressureGate(class)` — `pressure() → PressureLevel`, `can_fanout(n_units) → FanoutDecision`, `snapshot()`
  - Probe cascade: psutil → /proc/meminfo (linux) → `vm_stat` subprocess (Darwin) → fallback to "OK always"
  - Thresholds: `_WARN_PCT` (default 30% free), `_HIGH_PCT` (20%), `_CRITICAL_PCT` (10%)
  - Per-level fanout caps: OK=unlimited, WARN=8, HIGH=3, CRITICAL=1
- Kill switch `JARVIS_MEMORY_PRESSURE_GATE_ENABLED` (default `false`)

**Tests (~45):**
- PressureLevel thresholds
- Fanout decision matrix (level × n_units)
- Probe cascade: psutil path, /proc path (mocked), vm_stat path (mocked), fallback path
- Malformed probe output → falls back to OK
- Subprocess timeout on vm_stat → fallback
- can_fanout clamps to level cap
- can_fanout returns n_allowed ≤ n_requested
- snapshot() shape
- Authority-free grep
- Schema version pin

**Live-fire:**
- Real psutil probe returns numeric
- can_fanout(16) under current pressure → reasonable decision
- Force mock-CRITICAL path → refuses fanout
- Snapshot matches probe
- Flags registered in FlagRegistry

## Slice 3 — Operator surfaces + IDE + SSE

**Deliverables:**
- `governor_repl.py` — `/governor` dispatcher, 6 subcommands:
  - `/governor status` — current counts vs caps per sensor + current posture + pressure level
  - `/governor explain` — full rolling window + cost_burn + emergency-brake state
  - `/governor history [N]` — last N decisions
  - `/governor reset` — clear counters (operator override, audited)
  - `/governor memory` — PressureLevel + can_fanout(N) for N=1..16
  - `/governor help`
- IDE observability:
  - `GET /observability/governor` — current snapshot
  - `GET /observability/governor/history?limit=N`
  - `GET /observability/memory-pressure` — pressure level + free_pct
- SSE:
  - `EVENT_TYPE_GOVERNOR_THROTTLE_APPLIED` — fires on deny
  - `EVENT_TYPE_GOVERNOR_EMERGENCY_BRAKE` — fires on brake activation
  - `EVENT_TYPE_MEMORY_PRESSURE_CHANGED` — fires on level transition
  - `bridge_governor_to_broker(governor)` + `bridge_memory_pressure_to_broker(gate)`

**~55 tests + live-fire.**

## Slice 4 — Graduation

Flip both master flags `false → true`. ~45 graduation pins:
- Authority (8) — grep-enforced on 5 arc files + 3 GET handlers + 2 SSE bridges
- Behavioral (14) — both gates master off/on, emergency brake, window eviction, cap clamp, probe fallback cascade, thread-safety, posture-weight correctness
- Graduation-specific (10) — 2 flags default True, 16-sensor seed, 4-posture coverage, pressure-level transitions, bridge pins
- Docstring guards (4) — Tier 0, authority-free, advisory, §8 observability
- Schema version (3) — governor + gate + SSE
- Integration (4) — FlagRegistry auto-registration, posture reader wiring, SSE bridges, GET double-gate
- Full-revert matrix (2) — one flip kills REPL + GETs + SSE per gate
- CLAUDE.md doc (4) — SensorGovernor + MemoryPressureGate + both flags + Wave 1 #3 position

## Slice 5 — Production integration (deferred)

- Wire `unified_intake_router.py` to call `governor.request_budget()` before emitting ops
- Wire `subagent_scheduler.py` to call `gate.can_fanout()` before L3 worktree creation
- Lands when integration doesn't destabilize live ops. Each wiring gets its own 3-session clean arc.

## Cross-slice invariants

1. Authority-free — both primitives are advisory; callers choose to honor
2. Kill switches kill surfaces in lockstep (REPL + GET + SSE + typo warnings)
3. §5 Tier 0 — pure math, no LLM
4. Wave 1 #1 + #2 consumers — posture-aware weighting + FlagRegistry auto-registration
5. Emergency brake is the only hard cut; default posture nudges are soft

## Effort

| Slice | LoC | Tests | Sessions |
|---|---|---|---|
| 1 | ~600 | ~55 | 1-2 |
| 2 | ~400 | ~45 | 1 |
| 3 | ~600 | ~55 | 1-2 |
| 4 | ~200 + script | ~45 | 1 |
| 5 | ~300 | deferred | deferred |
| **Total** | **~2100 + deferred** | **~200** | **4-6** |

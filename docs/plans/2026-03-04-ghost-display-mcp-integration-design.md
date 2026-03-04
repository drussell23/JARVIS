# Ghost Display / Phantom Hardware — Memory Control Plane Integration

**Date:** 2026-03-04
**Approach:** Display Lease + Broker Event Subscription (Approach A)
**Scope:** Wire ghost display into MCP as a first-class lease-governed component
**Platform:** Apple Silicon M1 (Unified Memory Architecture — no discrete VRAM)

---

## Context

The Memory Control Plane (MCP) now governs all model memory allocation through
`MemoryBudgetBroker` with transactional leases. However, the ghost display system
operates outside this authority:

- `agi_os_coordinator.py` calls raw `psutil.virtual_memory()` to decide whether
  to skip components — bypassing the broker's `PressureTier` classification.
- `yabai_space_detector.py` reaches into `MemoryQuantizer._thrash_state` (a private
  attribute) rather than using the broker's snapshot API.
- `phantom_hardware_manager.py` has no runtime resolution control and no awareness
  of memory pressure — the ghost display runs at a fixed resolution regardless of
  system state.

On Apple Silicon (M1), there is no discrete VRAM. The same 16GB RAM pool serves
CPU, GPU, and Neural Engine. When macOS allocates a virtual display framebuffer via
BetterDisplay, it consumes unified memory (triple-buffered + compositor overhead).
This is invisible to `psutil` process RSS but reduces the available pool that the
broker's `MemoryQuantizer.snapshot()` observes via `physical_free`.

This makes ghost display compositor memory a first-class concern for the MCP.

---

## 1. Unified Memory Accounting for Display Compositor

### Estimation Model

On UMA, display compositor cost is computed as:

```
base = width * height * bytes_per_pixel * buffer_count * scale_factor * refresh_factor
overhead = base * compositor_overhead_factor
estimated_bytes = base + overhead
```

Where:
- `bytes_per_pixel`: 4 (BGRA)
- `buffer_count`: 3 (triple-buffer)
- `scale_factor`: 1.0 for non-Retina, 2.0 for HiDPI
- `refresh_factor`: 1.0 for 60Hz, 1.5 for 120Hz (additional compositor work)
- `compositor_overhead_factor`: 0.3 (cursor, window server surfaces, mirroring,
  color conversion, capture pipes)

### Resolution Tier Table

| Resolution | Estimated compositor cost | Tier label |
|---|---|---|
| 1920x1080 | ~32MB | Full |
| 1600x900 | ~22MB | Degraded-1 |
| 1280x720 | ~14MB | Degraded-2 |
| 1024x576 | ~9MB | Minimum |
| Disconnected | 0MB | Released |

### Lease Registration

- `component_id`: `display:ghost@v1`
- `priority`: `BudgetPriority.BOOT_OPTIONAL`
- `estimated_bytes`: computed from resolution at creation time
- Lease metadata: `display_id`, `resolution`, `refresh_rate`, `scale_factor`

### Reserved vs Accounted Bytes

- `reserved_bytes`: what the broker budgets pessimistically (from estimation model)
- `accounted_bytes`: what telemetry infers from before/after `MemorySnapshot` deltas

This separation prevents overfitting grant math to noisy compositor measurements.

### Calibration Loop

After each mode change completes:
1. Record `pre_snapshot.physical_free` and `post_snapshot.physical_free` (3-5s apart)
2. Compute `actual_delta = post_free - pre_free`
3. Feed into per-resolution EMA: `ema = 0.8 * ema + 0.2 * actual_delta`
4. Future estimates use calibrated EMA when `delta_confidence` is `high` (5+ obs)
5. `delta_confidence`: `high` (5+), `medium` (2-4), `low` (estimate-only)
6. `delta_method`: `snapshot_diff` or `estimate_only`

---

## 2. Display State Machine & Shedding Ladder

### State Definitions

```
INACTIVE ──► ACTIVE ──► DEGRADING ──► DEGRADED_1 ──► DEGRADING ──► DEGRADED_2
   ▲                                                                    │
   │         RECOVERING ◄── DEGRADED_1 ◄── RECOVERING ◄── DEGRADED_2   │
   │                                                                    ▼
   │                                              DEGRADING ──► MINIMUM
   │                                                               │
   │         RECOVERING ◄── MINIMUM                                │
   │                                                               ▼
   └────────────────────────────── DISCONNECTED ◄── DISCONNECTING ─┘
                                       │
                                       ▼
                                  RECOVERING ──► MINIMUM (reconnect floor)
```

Transitional states: `DEGRADING`, `RECOVERING`, `DISCONNECTING` — commands cannot
overlap, and supervisor restart can resume/rollback deterministically.

| State | Resolution | Lease Status | Entry Tier |
|---|---|---|---|
| `INACTIVE` | None | No lease | Pre-init or released |
| `ACTIVE` | 1920x1080 (preferred) | GRANTED → ACTIVE | ABUNDANT / OPTIMAL |
| `DEGRADED_1` | 1600x900 | ACTIVE (amended) | CONSTRAINED |
| `DEGRADED_2` | 1280x720 | ACTIVE (amended) | CRITICAL |
| `MINIMUM` | 1024x576 | ACTIVE (amended) | CRITICAL + thrash |
| `DISCONNECTED` | None | RELEASED | EMERGENCY (dependency-clear) |

### One-Step-Per-Evaluation Invariant

Never skip multiple states in one evaluation tick, including under EMERGENCY.
Each tick advances at most one step. This prevents overshoot and oscillation.

### Shedding Ladder (Pressure → Action)

| PressureTier | Action | Detail |
|---|---|---|
| ABUNDANT / OPTIMAL | None | Full resolution |
| ELEVATED | Telemetry pre-warn | Log only, no mode change |
| CONSTRAINED | Degrade to DEGRADED_1 | 1600x900, block new creates above this |
| CRITICAL | Degrade to DEGRADED_2 | 1280x720 |
| CRITICAL + thrash | Degrade to MINIMUM | 1024x576 |
| EMERGENCY | Dependency-aware disconnect | Full release if no blocking leases |

### Hysteresis (Trigger vs Clear Thresholds)

Entry and exit thresholds are separated to prevent ping-pong:

| State | Enter at | Clear only at |
|---|---|---|
| DEGRADED_1 | CONSTRAINED | OPTIMAL sustained 60s |
| DEGRADED_2 | CRITICAL | ELEVATED sustained 60s |
| MINIMUM | CRITICAL + thrash | CONSTRAINED sustained 60s |
| DISCONNECTED | EMERGENCY | ELEVATED sustained 60s + hysteresis clear |

### Recovery Ladder

Recovery only triggers when ALL of:
1. `PressureTier` drops below clear threshold for current state
2. `swap_hysteresis_active == False`
3. Dwell timer expired (minimum 60s at current state)
4. `pressure_trend == FALLING` or `STABLE`

Recovery steps back one level at a time. From DISCONNECTED, reconnect to MINIMUM
first, verify stability, then step up. Never reconnect straight to ACTIVE.

### Flap Guards

- Minimum dwell per state: 30s degradation, 60s recovery (env-configurable)
- Cooldown between any two mode changes: 20s
- Maximum transitions per hour per display: 6
- Global transition cap per hour: 12 (multi-display storm protection)
- If max transitions exceeded: lock at current state for 10 minutes, emit warning

### Failure Budget

If `APPLY/VERIFY` fails N times (default 3) for a specific transition, quarantine
that action path:
- Freeze at current stable state
- Emit `DISPLAY_ACTION_FAILED` with `quarantined: true`
- Auto-unquarantine after `JARVIS_DISPLAY_QUARANTINE_DURATION` (default 300s)
- Record `quarantine_reason`, `failure_count_window`, `window_seconds`

### Two-Phase Action Protocol

All state transitions use this protocol:

```
1. PREPARE  — Validate preconditions, take pre-snapshot, compute target,
               check flap guards and failure budget
2. APPLY    — Issue betterdisplaycli command (idempotent, with timeout)
3. VERIFY   — Wait verification_window (3-5s), dual-condition check:
               a) Display mode actually changed (query betterdisplaycli)
               b) Memory pressure trend improved within bounded window
4. COMMIT   — Atomic lease amendment (old_bytes → new_bytes swap in broker),
               emit event, record calibration data
   or
   ROLLBACK — Revert CLI command, emit DISPLAY_ACTION_FAILED,
               stay at previous state, increment failure budget counter
```

### Idempotent & Replay-Safe Commands

- Repeated `set -resolution=...` at same resolution is a no-op (check before issue)
- Repeated `set -connected=off` when already disconnected is a no-op
- Event replay must check current hardware state before issuing CLI commands
- `action_id` + `idempotency_key` prevent duplicate hardware actions

---

## 3. Telemetry Contract & Event Schema

### New Event Types (added to MemoryBudgetEventType)

```python
DISPLAY_DEGRADE_REQUESTED    = "display_degrade_requested"
DISPLAY_DEGRADED             = "display_degraded"
DISPLAY_DISCONNECT_REQUESTED = "display_disconnect_requested"
DISPLAY_DISCONNECTED         = "display_disconnected"
DISPLAY_RECOVERY_REQUESTED   = "display_recovery_requested"
DISPLAY_RECOVERED            = "display_recovered"
DISPLAY_ACTION_FAILED        = "display_action_failed"
DISPLAY_ACTION_PHASE         = "display_action_phase"  # subevent per phase
```

### Event Payload Schema

All display events share this structure:

```python
{
    # --- Identity & ordering ---
    "event_id": "uuid",                    # UUID, unique per event
    "sequence_no": 42,                     # per-display monotonic counter
    "idempotency_key": "act_001:APPLY",    # action_id + phase
    "parent_event_id": "uuid|null",        # links subevent to parent

    # --- Schema versioning ---
    "event_schema_version": "1.0",
    "state_machine_version": "1.0",
    "constraints_schema_version": "1.0",

    # --- State transition ---
    "from_state": "ACTIVE",                # DisplayState enum
    "to_state": "DEGRADING",               # includes transitional states
    "trigger_tier": "CRITICAL",            # PressureTier
    "snapshot_id": "snap_abc123",          # MemorySnapshot.snapshot_id

    # --- Lease ---
    "lease_id": "lease_xyz789",            # BudgetGrant.grant_id
    "action_id": "act_001",                # unique per two-phase action

    # --- Resolution ---
    "from_resolution": "1920x1080",
    "to_resolution": "1280x720",

    # --- Memory impact ---
    "estimated_delta_bytes": -18_000_000,  # negative = freed
    "accounted_delta_bytes": -16_500_000,  # actual measured (may be null)
    "delta_confidence": "medium",          # high|medium|low
    "delta_method": "snapshot_diff",       # snapshot_diff|estimate_only

    # --- Time ---
    "ts_monotonic": 123456.789,            # time.monotonic()
    "ts_wall_utc": "2026-03-04T...",       # ISO 8601
    "event_latency_ms": 3,                 # emit delay
    "phase_duration_ms": {                 # per two-phase step
        "prepare": 12,
        "apply": 1834,
        "verify": 3200,
        "commit": 5
    },
    "dwell_seconds": 45.2,                 # time in from_state

    # --- Flap tracking ---
    "transition_count_1h": 3,              # rolling hour window

    # --- Dependency check ---
    "dependency_check": {
        "blocked": false,
        "blocking_lease_ids": [],
        "blocking_components": [],
        "latched_reason": null,
        "latched_window_remaining_s": 0
    },

    # --- Verification ---
    "verify_result": "pass",               # pass|fail|timeout|skipped

    # --- Failure taxonomy (on DISPLAY_ACTION_FAILED) ---
    "failure_code": null,                  # COMMAND_TIMEOUT|VERIFY_MISMATCH|...
    "failure_class": null,                 # transient|structural|operator
    "retryable": null,                     # bool

    # --- Quarantine (when applicable) ---
    "quarantine_reason": null,
    "quarantine_until": null,
    "failure_count_window": null,
    "window_seconds": null,

    # --- Provenance ---
    "actor": "display_manager",            # broker|display_manager|supervisor_replay
    "host": "hostname",
    "pid": 12345,
    "epoch": 7
}
```

### Phase Subevents

`DISPLAY_ACTION_PHASE` events are emitted for each two-phase step:

```python
{
    "event_id": "uuid",
    "parent_event_id": "parent_action_event_id",
    "phase": "APPLY",  # PREPARE|APPLY|VERIFY|COMMIT|ROLLBACK
    "phase_duration_ms": 1834,
    "phase_result": "success",
    # ... same identity/versioning/provenance fields
}
```

---

## 4. Durability, Crash Recovery & Wire Points

### Lease Persistence

Display leases persist through the existing broker `_persist_leases()` mechanism
(atomic tmp+fsync+rename). On crash recovery, `reconcile_stale_leases()` detects
the `display:ghost@v1` lease and:

1. Queries `betterdisplaycli get -nameLike="JARVIS GHOST"` for actual display state
2. If connected → restore lease as ACTIVE at detected resolution
3. If not connected → release lease, mark as RELEASED

### Transitional State Recovery

Transitional states (`DEGRADING`, `RECOVERING`, `DISCONNECTING`) are persisted in
lease metadata. On supervisor restart mid-transition:

- `DEGRADING` → verify actual display mode → commit downgrade or rollback to previous
- `DISCONNECTING` → verify display state → confirm disconnected or re-attempt
- `RECOVERING` → verify display mode → commit upgrade or stay at previous

### Lease Amendment Semantics

For resolution changes, the broker performs an atomic `old_bytes → new_bytes` swap.
No temporary release window — the lease stays ACTIVE throughout, only the
`reserved_bytes` changes atomically.

### Dependency-Aware Disconnect Protocol

1. Query `broker.get_active_leases()`
2. Filter for leases where `metadata.get("requires_display") == True`
3. Check latched dependency window (30s grace): any lease that released within
   the last 30s with `requires_display` still blocks
4. If blocked → stay at MINIMUM, emit `DISPLAY_DISCONNECT_BLOCKED`
5. If clear → proceed with two-phase disconnect

### Wire Points (files modified, no new files)

| File | Change |
|---|---|
| `backend/core/memory_types.py` | Add 8 `DISPLAY_*` event types, `DisplayState` enum, `DisplayFailureCode` enum |
| `backend/core/memory_budget_broker.py` | Add `register_pressure_observer()`, `amend_lease_bytes()` atomic swap, display lease support in `reconcile_stale_leases()` |
| `backend/system/phantom_hardware_manager.py` | Add `set_resolution_async()`, `disconnect_async()`, `reconnect_async()`, `get_current_mode_async()`. Add `DisplayPressureController` class (state machine + shedding ladder + two-phase actions + calibration). Register as broker pressure observer at init. |
| `backend/vision/yabai_space_detector.py` | Replace `_current_thrash_state()`: use `get_memory_quantizer_instance()` + `snapshot()` for typed `ThrashState`/`PressureTier` via broker snapshot API |
| `backend/agi_os/agi_os_coordinator.py` | Replace raw `psutil.virtual_memory()` with `get_memory_quantizer_instance().snapshot()` for pressure-gated component init |
| `unified_supervisor.py` | Wire display lease request during Phase 6.5 init. Pass broker ref to phantom manager. Integrate display lease into health loop. |

### No New Files

`DisplayPressureController` lives inside `phantom_hardware_manager.py` alongside
`PhantomHardwareManager` — same module, same singleton lifecycle. No architectural
sprawl.

---

## 5. Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_DISPLAY_DEGRADE_DWELL_S` | `30` | Minimum seconds before degradation step |
| `JARVIS_DISPLAY_RECOVERY_DWELL_S` | `60` | Minimum seconds before recovery step |
| `JARVIS_DISPLAY_COOLDOWN_S` | `20` | Cooldown between any mode changes |
| `JARVIS_DISPLAY_MAX_TRANSITIONS_1H` | `6` | Max transitions per hour per display |
| `JARVIS_DISPLAY_GLOBAL_MAX_TRANSITIONS_1H` | `12` | Global transition cap |
| `JARVIS_DISPLAY_LOCKOUT_DURATION_S` | `600` | Lockout after exceeding rate limit |
| `JARVIS_DISPLAY_VERIFY_WINDOW_S` | `5` | Post-action verification wait |
| `JARVIS_DISPLAY_FAILURE_BUDGET` | `3` | Failures before quarantine |
| `JARVIS_DISPLAY_QUARANTINE_DURATION_S` | `300` | Quarantine auto-clear interval |
| `JARVIS_DISPLAY_LATCHED_DEPENDENCY_S` | `30` | Grace period for recently-released deps |
| `JARVIS_DISPLAY_SCALE_FACTOR` | `1.0` | HiDPI scale for memory estimation |
| `JARVIS_DISPLAY_REFRESH_FACTOR` | `1.0` | Refresh rate factor (1.0=60Hz, 1.5=120Hz) |
| `JARVIS_DISPLAY_COMPOSITOR_OVERHEAD` | `0.3` | Extra factor for compositor surfaces |

---

## Definition of Done

### Core Integration
- [ ] Raw `psutil.virtual_memory()` in `agi_os_coordinator.py` replaced with broker snapshot
- [ ] `_current_thrash_state()` in `yabai_space_detector.py` uses typed snapshot API
- [ ] Ghost display registers as `display:ghost@v1` lease during Phase 6.5

### Display State Machine
- [ ] `DisplayState` enum with all states including transitionals
- [ ] `DisplayPressureController` implements full shedding + recovery ladder
- [ ] One-step-per-evaluation invariant enforced
- [ ] Hysteresis separation (trigger vs clear thresholds)
- [ ] Flap guards (dwell, cooldown, rate limit, lockout)
- [ ] Failure budget with quarantine

### Two-Phase Actions
- [ ] `set_resolution_async()` on `PhantomHardwareManager`
- [ ] `disconnect_async()` / `reconnect_async()` on `PhantomHardwareManager`
- [ ] PREPARE → APPLY → VERIFY → COMMIT/ROLLBACK protocol
- [ ] Dual-condition verification (mode changed + memory improved)
- [ ] Idempotent CLI commands (check before issue)

### Broker Extensions
- [ ] `register_pressure_observer()` on `MemoryBudgetBroker`
- [ ] `amend_lease_bytes()` atomic swap on `MemoryBudgetBroker`
- [ ] Display lease persistence and crash recovery
- [ ] Transitional state recovery on restart

### Dependency Awareness
- [ ] Emergency disconnect checks active leases for `requires_display`
- [ ] Latched dependency window (30s grace)
- [ ] `DISPLAY_DISCONNECT_BLOCKED` event when denied

### Telemetry
- [ ] All 8 `DISPLAY_*` event types implemented
- [ ] Full event payload with versioning, ordering, provenance
- [ ] Phase subevents for debugging
- [ ] Failure taxonomy with codes and classes
- [ ] Calibration data recorded per mode change

### Calibration
- [ ] Before/after snapshot delta recording
- [ ] Per-resolution EMA tracking
- [ ] `delta_confidence` and `delta_method` in events
- [ ] `reserved_bytes` vs `accounted_bytes` separation

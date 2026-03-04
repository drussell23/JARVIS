# Autonomy Contract Hardening & MCP Cloud Capacity Integration

## Problem Statement

Three runtime warnings indicate structural sequencing issues:

1. **Autonomy contract mismatch → read_only**: Prime/Reactor haven't finished starting when the contract check runs (Phase 2 timing gap after Phase 1 Trinity spawn).
2. **ServerDisconnectedError (unhandled)**: aiohttp errors from Cloud Run cold starts escape retry logic and aren't classified as transient.
3. **Zone6/VoiceBio barrier timeout**: Voice biometrics initialization (12-25s) exceeds the 8s optional barrier.

Warning #2 and #3 are addressed in Step A (committed as `e0c9f106`). This design covers:
- **Step B**: Fix autonomy contract timing with bounded readiness wait + pending status
- **Step C**: Unify MCP pressure signals with cloud capacity decisions

## Architecture

The fix adds a bounded readiness wait between Trinity spawn and autonomy check, introduces a `pending` status for services still starting, and creates a single cloud capacity decision authority consuming MCP signals.

**Key constraint**: No phase reordering in `unified_supervisor.py`. All changes are additive — inserting a wait, adding status states, and wiring event callbacks.

---

## Step B: Autonomy Contract Timing Fix

### B.1: Three-State Autonomy Model

**Current**: `active` / `read_only` / `disabled`

**New**: `pending` / `active` / `read_only`

| State | Meaning | Write Policy |
|-------|---------|-------------|
| `pending` | Services still starting, check not yet conclusive | Block autonomous writes (same as read_only) |
| `active` | All contracts satisfied | Allow autonomous writes per WorkspaceAutonomyPolicy |
| `read_only` | Hard failure: schema mismatch, service down after timeout | Block autonomous writes |

**Reason codes** (stored in `self._autonomy_reason`):

| Code | Meaning |
|------|---------|
| `pending_services` | Waiting for Prime/Reactor health probes |
| `pending_lease` | Waiting for journal lease acquisition |
| `schema_mismatch` | Version incompatibility after services healthy |
| `health_probe_failed` | Services responded but contract check failed |
| `timeout` | Bounded wait exceeded, services never came online |
| `active` | All contracts satisfied |

**Policy**: `pending` is treated identically to `read_only` for write gates. No gray-zone policy hole.

### B.2: Bounded Readiness Wait

After `start_all_services()` returns, insert a bounded wait before `check_autonomy_contracts()`:

```
Phase 1: Trinity spawn → ProcessOrchestrator.start_all_services()
          ↓
NEW:     _await_autonomy_dependencies(timeout=15s)
          ├─ Poll every 2s: Prime healthy? Reactor healthy? Journal lease held?
          ├─ Early exit: all 3 conditions met → proceed immediately
          ├─ Shutdown signal → abort wait
          └─ Timeout → proceed to check anyway (will result in read_only)
          ↓
Phase 2: check_autonomy_contracts() → sets mode based on result
```

**Files:**
- Modify: `unified_supervisor.py` ~line 81402-81447 (autonomy gate)
- New method: `_await_autonomy_dependencies(timeout: float) -> Dict[str, bool]`

**Environment variable**: `JARVIS_AUTONOMY_READINESS_WAIT_S` (default 15.0, min 1.0)

### B.3: Enhanced Contract Check Return

Modify `check_autonomy_contracts()` to return reason codes:

```python
checks["reason"] = "pending_services"  # or schema_mismatch, active, etc.
checks["pending"] = ["prime", "reactor"]  # which services aren't ready
```

The caller in `unified_supervisor.py` uses the reason:
- `reason.startswith("pending")` → `_autonomy_mode = "pending"`, log at INFO
- `reason == "schema_mismatch"` → `_autonomy_mode = "read_only"`, log at WARNING
- `reason == "active"` → `_autonomy_mode = "active"`, log at INFO

**File:** Modify `cross_repo_startup_orchestrator.py` ~line 24779-24891

### B.4: Event-Driven Fast Promotion

1. **ProcessOrchestrator callback**: When a service transitions to healthy, check if it's Prime or Reactor. If so, trigger an immediate autonomy re-check.

2. **Adaptive monitor interval**: When `_autonomy_mode == "pending"`, re-check every 5s instead of 60s. Once `active`, return to 60s.

3. **Transition logging**: `"Autonomy: pending → active (Prime+Reactor healthy, journal lease held, 12.3s after boot)"`

**Files:**
- Modify: `unified_supervisor.py` ~line 86277-86323 (runtime monitor)
- Modify: `cross_repo_startup_orchestrator.py` ProcessOrchestrator (add service-ready callback)

### B.5: Semantic Version Comparison

Replace string comparison with tuple-based semver:

```python
def _version_gte(a: str, b: str) -> bool:
    """True if semantic version a >= b."""
    def _parse(v: str) -> Tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))
    return _parse(a) >= _parse(b)
```

**File:** Modify `cross_repo_startup_orchestrator.py` ~line 24862-24877

---

## Step C: MCP + Cloud Capacity Integration

### C.1: Cloud Capacity Decision Authority

A `CloudCapacityController` replaces scattered cloud-scaling threshold checks across `gcp_vm_manager.py`, `gcp_oom_prevention_bridge.py`, and `supervisor_gcp_controller.py`.

**Inputs** (from existing MCP infrastructure):
- `PressureTier` from `broker.latest_snapshot`
- Queue depth (inference request backlog from PrimeRouter)
- Latency SLO violations (response time > target)
- Cost budget remaining (from existing `CostTracker`)

**Decision enum** (`CloudCapacityAction`):

| Action | When | Effect |
|--------|------|--------|
| `STAY_LOCAL` | Pressure ≤ ELEVATED, queue short | No cloud action |
| `DEGRADE_LOCAL` | Pressure = CONSTRAINED, queue manageable | Shed non-critical (display, cache) |
| `OFFLOAD_PARTIAL` | Pressure ≥ CONSTRAINED, queue growing | Route non-critical inference to cloud |
| `SPIN_SPOT` | Pressure ≥ CRITICAL, sustained > 30s | Provision Spot VM |
| `FALLBACK_ONDEMAND` | Spot unavailable/preempted | Use Cloud Run (higher cost) |

**Files:**
- Create: `backend/core/cloud_capacity_controller.py`
- Modify: `backend/core/memory_types.py` (add `CloudCapacityAction` enum)

### C.2: Hysteresis & Cooldowns

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Spot create cooldown | 120s | Prevent VM thrash |
| Spot destroy cooldown | 300s | Amortize startup cost |
| Break-even threshold | warmup_time + 60s | Only spin if expected runtime justifies cost |
| Tier deadband | Uses existing PressurePolicy (5% gap) | Prevent tier flapping |

### C.3: Preemption-Aware Routing

- **Non-critical** (profile learning, batch embeddings) → Spot first, Cloud Run fallback
- **Critical** (real-time voice unlock, active inference) → local or Cloud Run, never Spot alone
- **On preemption**: Immediate failover to Cloud Run, 300s cooldown before Spot retry

### C.4: Unified Telemetry

Every cloud decision carries MCP provenance:
- `snapshot_id` from broker
- `decision_id` from coordinator
- `policy_version` from PressurePolicy
- `epoch` / `sequence` for staleness fencing

### C.5: Integration with Existing GCP Manager

`gcp_vm_manager.py` already has `register_with_broker()` from MCP governance migration (Task 6). The controller:
1. Registers as a broker pressure observer
2. Receives tier changes via callback
3. Makes cloud capacity decisions
4. Submits through the existing `MemoryActuatorCoordinator`
5. `gcp_vm_manager.py` becomes an executor — the controller decides, the manager executes

---

## Migration Safety

### Dual-Actuation Prevention

During migration, only one layer actuates per action type:
- **Phase 0**: Legacy actuates, MCP shadows (log-only)
- **Phase 1** (current MCP state): MCP actuates, legacy is `if not self._mcp_active` fallback
- **Phase 2** (future): Remove legacy fallbacks

The coordinator's existing `shadow_mode` flag gates this per-instance.

### Execution-Time Epoch Fencing

Add staleness re-check at drain time (not just submit time):

```python
def drain_pending(self) -> List[PendingAction]:
    with self._lock:
        fresh = [a for a in self._pending
                 if not a.envelope.is_stale(
                     current_epoch=self._current_epoch,
                     current_sequence=self._current_sequence,
                 )]
        stale_count = len(self._pending) - len(fresh)
        self._total_rejected_stale += stale_count
        self._pending = []
        return sorted(fresh, key=lambda a: a.action.priority)
```

**File:** Modify `backend/core/memory_actuator_coordinator.py`

### Observer Backpressure

Wrap broker observer callbacks with timeout to prevent slow subscribers from blocking the bus:

```python
async def _notify_one_observer(self, callback, tier, snapshot):
    try:
        await asyncio.wait_for(callback(tier, snapshot), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning(f"Observer {callback.__qualname__} timed out (>2s), skipped")
```

**File:** Modify `backend/core/memory_budget_broker.py`

### Shutdown Safety

All recovery loops, promotion checks, and background tasks must:
1. Check `_shutdown_event.is_set()` before acting
2. Use `asyncio.shield()` only for cleanup tasks, never for new work
3. Be tracked in `_background_tasks` for orderly drain during shutdown

---

## Implementation Phases

### Phase A: Immediate (Step A) — DONE ✅
- Classify aiohttp errors as transient
- Increase Cloud Run retry budget
- Name fire-and-forget tasks
- Committed as `e0c9f106`

### Phase B: Autonomy Contract Timing (Step B)
1. Add `_autonomy_reason` field and pending status
2. Implement `_await_autonomy_dependencies()` bounded wait
3. Enhance `check_autonomy_contracts()` with reason codes
4. Wire event-driven fast promotion
5. Replace string version compare with semver
6. Tests

### Phase C: MCP Cloud Capacity (Step C)
1. Add `CloudCapacityAction` enum to memory_types
2. Create `CloudCapacityController` with broker observer
3. Add hysteresis, cooldowns, break-even logic
4. Wire to existing `gcp_vm_manager.py` (controller decides, manager executes)
5. Add execution-time epoch fencing to coordinator
6. Add observer backpressure to broker
7. Tests

### Phase D: Migration Safety
1. Shadow mode verification across all actuators
2. Shutdown-awareness audit
3. Integration tests

# Journal-Backed GCP Lifecycle: Unified Hybrid Cloud Control Plane

**Date:** 2026-02-25
**Status:** Approved
**Phase:** Design

## Problem Statement

Six independent decision-makers control GCP VM lifecycle (`GCPHybridPrimeRouter`, `GCPVMManager`, `SupervisorAwareGCPController`, `IntelligentGCPOptimizer`, `GCPOOMPreventionBridge`, `CostTracker`) — each with its own state, polling loop, thresholds, and failure modes. None coordinate through the control plane journal. After crash, recovery is heuristic-based (in-memory booleans, stale signal files, GCP API polling) rather than intent-based (journal replay).

**Root cause:** "Six brains, no spine." These subsystems grew organically from different crises (v93 SIGSTOP, v132 OOM, v153 graceful degradation, v229 golden image, v266 pressure-driven lifecycle). Each solved its crisis well, but they were never unified under a single authority.

**Core principle:** At-least-once orchestration + idempotent reconciliation. No "exactly once" illusions across process boundaries + GCP API calls. Use stable operation/resource keys (`op_id`, `instance_name`, `request_id`) and reconcile actual GCP state before re-issuing side effects.

---

## P0 Requirements (Hard Gates for Implementation)

1. **Single authority** for GCP lifecycle transitions via journaled state machine (no boolean scatter)
2. **Atomic budget reservation + create** (close the check/create TOCTU race)
3. **Lease-safe side effects** (pending intent must include external operation ID; next leader reconciles before retry)
4. **Commit-then-publish event ordering** (outbox pattern: fabric events can't outrun durable journal visibility)
5. **Invincible node as first-class component** in `component_state` with explicit lifecycle semantics
6. **Probe hysteresis + startup ambiguity** (distinguish "never launched" vs "crashed during startup")

---

## Section 1: Control-Plane Ownership Model

### Single Writer + Transition Authority

The orchestration journal's epoch-fenced lease already provides single-writer authority. The missing piece: GCP lifecycle transitions must flow through this authority, not bypass it.

**Current (fragmented):**
```
MemoryQuantizer → GCPHybridPrimeRouter (own polling loop, own thresholds)
                → decides "provision VM"
                → calls GCPVMManager.create_vm() directly
                → no journal entry
                → in-memory boolean updated
                → crash → state lost
```

**Target (unified):**
```
MemoryQuantizer → tier change callback → journal.fenced_write("pressure_event", ...)
                → GCPLifecycleStateMachine processes event
                → guards checked (budget, state, hysteresis)
                → journal.fenced_write("vm_provision_requested", ...)
                → side effect: GCPVMManager.create_vm()
                → journal.mark_result(seq, "committed", payload={instance_id, ip})
                → event fabric publishes (outbox pattern)
```

### High-Level GCP Lifecycle Transition Table

| Current State | Event | Guard | Next State | Journaled Action(s) |
|---|---|---|---|---|
| `IDLE` | `pressure_triggered` | sustained pressure threshold met, lease held | `TRIGGERING` | `pressure_detected`, `trigger_requested` |
| `TRIGGERING` | `budget_approved` | atomic reservation succeeded | `PROVISIONING` | `budget_reserved`, `provision_requested` |
| `TRIGGERING` | `budget_denied` | reservation failed / limit exceeded | `COOLING_DOWN` | `budget_denied`, `cooldown_started` |
| `PROVISIONING` | `vm_create_started` | request accepted by provider | `BOOTING` | `vm_create_accepted` |
| `PROVISIONING` | `vm_create_failed` | retry budget not exhausted | `TRIGGERING` | `vm_create_failed`, `retry_scheduled` |
| `PROVISIONING` | `vm_create_failed` | retry budget exhausted | `COOLING_DOWN` | `vm_create_failed_terminal`, `cooldown_started` |
| `BOOTING` | `health_healthy` | handshake + contract validation pass | `ACTIVE` | `vm_ready`, `routing_switched_to_cloud` |
| `BOOTING` | `health_timeout_or_unreachable` | boot deadline exceeded | `COOLING_DOWN` | `boot_failed`, `budget_release_requested`, `cooldown_started` |
| `ACTIVE` | `pressure_cooled` | cool-pressure hysteresis met for window | `COOLING_DOWN` | `cooldown_started` |
| `ACTIVE` | `health_degraded_consecutive` | degraded threshold exceeded | `ACTIVE` | `health_degraded` (stay active, no route flap) |
| `ACTIVE` | `health_unreachable_consecutive` | unreachable threshold exceeded | `TRIGGERING` | `cloud_lost`, `routing_switched_to_local`, `retrigger_requested` |
| `ACTIVE` | `spot_preempted` | provider preemption signal or inferred | `TRIGGERING` | `preempted`, `routing_switched_to_local`, `retrigger_requested` |
| `COOLING_DOWN` | `cooldown_expired` | no trigger-worthy pressure | `STOPPING` | `stop_requested` |
| `COOLING_DOWN` | `pressure_triggered` | sustained pressure returns | `TRIGGERING` | `retrigger_requested` |
| `STOPPING` | `vm_stopped_confirmed` | stop observed | `IDLE` | `vm_stopped`, `budget_released` |
| `STOPPING` | `stop_timeout` | stop deadline exceeded | `IDLE` | `stop_timeout`, `force_cleanup_recorded` |
| `*` | `lease_lost` | epoch mismatch / fenced | `IDLE` (read-only) | `authority_lost` (best effort), side effects halted |
| `*` | `session_shutdown` | shutdown requested | `STOPPING` | `shutdown_stop_requested` |
| `*` | `fatal_error` | non-recoverable internal fault | `COOLING_DOWN` | `fatal_detected`, `routing_switched_to_local`, `cooldown_started` |

### Who Owns What (After Unification)

| Concern | Current Owner | Target Owner |
|---------|--------------|--------------|
| "Should we provision?" | GCPHybridPrimeRouter + IntelligentGCPOptimizer + OOMBridge | GCPLifecycleStateMachine (single evaluator) |
| "Can we afford it?" | CostTracker.can_create_vm() (non-atomic) | Journal-backed budget reservation (atomic) |
| "Create the VM" | GCPVMManager.create_vm() | GCPVMManager (unchanged, but called only by state machine) |
| "Is it healthy?" | GCPVMManager._ping_health_endpoint() | RecoveryProber (with hysteresis + classification) |
| "Route to it" | PrimeRouter.notify_gcp_vm_ready() | State machine emits ACTIVE → PrimeRouter subscribes via fabric |
| "Stop/delete it" | Scattered across shutdown hooks | State machine: STOPPING → STOPPED journal entries |

---

## Section 1B: Canonical Schema (Implementation Vocabulary)

All journal rows, UDS payloads, state machine internals, and tests use these exact enum values.
Reject unknown strings at boundaries (schema validation). Add a single legacy mapping table
if existing logs use old names.

```python
from enum import Enum


# -------------------------
# Lifecycle State Vocabulary
# -------------------------
class State(str, Enum):
    IDLE = "idle"
    TRIGGERING = "triggering"
    PROVISIONING = "provisioning"
    BOOTING = "booting"
    HANDSHAKING = "handshaking"
    ACTIVE = "active"
    COOLING_DOWN = "cooling_down"
    STOPPING = "stopping"
    LOST = "lost"
    FAILED = "failed"
    DEGRADED = "degraded"


# -------------------------
# Lifecycle Event Vocabulary
# -------------------------
class Event(str, Enum):
    # Pressure / trigger
    PRESSURE_TRIGGERED = "pressure_triggered"
    PRESSURE_COOLED = "pressure_cooled"
    RETRIGGER_DURING_COOLDOWN = "retrigger_during_cooldown"
    COOLDOWN_EXPIRED = "cooldown_expired"

    # Budget
    BUDGET_CHECK = "budget_check"
    BUDGET_APPROVED = "budget_approved"
    BUDGET_DENIED = "budget_denied"
    BUDGET_EXHAUSTED_RUNTIME = "budget_exhausted_runtime"
    BUDGET_RELEASED = "budget_released"

    # Provisioning / VM
    PROVISION_REQUESTED = "provision_requested"
    VM_CREATE_ACCEPTED = "vm_create_accepted"
    VM_CREATE_ALREADY_EXISTS = "vm_create_already_exists"
    VM_CREATE_FAILED = "vm_create_failed"
    VM_READY = "vm_ready"
    VM_STOP_REQUESTED = "vm_stop_requested"
    VM_STOPPED = "vm_stopped"
    VM_STOP_TIMEOUT = "vm_stop_timeout"
    SPOT_PREEMPTED = "spot_preempted"

    # Health / handshake
    HEALTH_PROBE_OK = "health_probe_ok"
    HEALTH_PROBE_DEGRADED = "health_probe_degraded"
    HEALTH_PROBE_TIMEOUT = "health_probe_timeout"
    HEALTH_UNREACHABLE_CONSECUTIVE = "health_unreachable_consecutive"
    HEALTH_DEGRADED_CONSECUTIVE = "health_degraded_consecutive"
    HANDSHAKE_STARTED = "handshake_started"
    HANDSHAKE_SUCCEEDED = "handshake_succeeded"
    HANDSHAKE_FAILED = "handshake_failed"
    BOOT_DEADLINE_EXCEEDED = "boot_deadline_exceeded"

    # Routing / reconcile / audit
    ROUTING_SWITCHED_TO_LOCAL = "routing_switched_to_local"
    ROUTING_SWITCHED_TO_CLOUD = "routing_switched_to_cloud"
    RECONCILE_OBSERVED_RUNNING = "reconcile_observed_running"
    RECONCILE_OBSERVED_STOPPED = "reconcile_observed_stopped"
    AUDIT_RECONCILE = "audit_reconcile"

    # Control-plane / operator
    LEASE_LOST = "lease_lost"
    SESSION_SHUTDOWN = "session_shutdown"
    MANUAL_FORCE_LOCAL = "manual_force_local"
    MANUAL_FORCE_CLOUD = "manual_force_cloud"
    FATAL_ERROR = "fatal_error"


# -------------------------
# Probe / Health Classification
# -------------------------
class HealthCategory(str, Enum):
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


# -------------------------
# Event Fabric Disconnect Reasons
# -------------------------
class DisconnectReason(str, Enum):
    TIMEOUT = "timeout"
    WRITE_ERROR = "write_error"
    EOF = "eof"
    PROTOCOL_ERROR = "protocol_error"
    LEASE_LOST = "lease_lost"
    SERVER_SHUTDOWN = "server_shutdown"
    CLIENT_SHUTDOWN = "client_shutdown"
```

### Schema Rules

- Use enum `.value` strings in journal rows and UDS payloads (not enum names).
- Reject unknown state/event strings at boundaries (schema validation).
- Add a single legacy mapping table if existing logs use old names.
- `State` and `Event` are `str, Enum` so they serialize directly to JSON without converters.

### Schema Notes

The `State` enum includes `HANDSHAKING`, `LOST`, `FAILED`, and `DEGRADED` which are not
in the high-level transition table. These serve specific roles:

- **`HANDSHAKING`**: Sub-state of `BOOTING` — VM is reachable but contract validation
  (API version, capabilities, schema compatibility) is in progress. Separating this from
  `BOOTING` allows the reconciler to distinguish "VM up but not validated" from "VM still starting".
- **`LOST`**: Transient state after `EV_RECONCILE_OBSERVED_STOPPED` — journal thought VM was
  running, probe found it gone. Used by reconciler to trigger re-evaluation (→ `TRIGGERING`
  if pressure still active, → `IDLE` if not).
- **`FAILED`**: Terminal state for non-recoverable errors (e.g., GCP quota exhausted,
  configuration invalid). Requires operator intervention or automated retry with backoff.
- **`DEGRADED`**: VM is reachable but health probes return `SERVICE_DEGRADED` or
  `DEPENDENCY_DEGRADED`. Routing stays active (no flap) but journal records degradation
  for audit. Transitions to `ACTIVE` on recovery or `TRIGGERING` on escalation.

---

## Section 2: Journaled GCP State Machine

### States

Canonical states from `State` enum above. Primary lifecycle path:

```
IDLE → TRIGGERING → PROVISIONING → BOOTING → HANDSHAKING → ACTIVE → COOLING_DOWN → STOPPING → IDLE
```

Auxiliary states: `LOST` (reconciler-discovered absence), `FAILED` (terminal error),
`DEGRADED` (health degradation without route flap).

### Events

```
EV_PRESSURE_TRIGGER          — N-of-M memory readings above trigger threshold
EV_BUDGET_CHECK              — Internal: initiate atomic budget reservation
EV_BUDGET_APPROVED           — Atomic reservation succeeded (journal entry committed)
EV_BUDGET_DENIED             — Reservation failed / limit exceeded
EV_PROVISION_EXECUTE         — Internal: issue GCP API create/start call
EV_VM_CREATE_ACCEPTED        — GCP API returned success + instance ref
EV_VM_CREATE_ALREADY_EXISTS  — Discovered existing matching instance (409 or probe)
EV_VM_CREATE_FAILED          — GCP API returned error (retryable or terminal)
EV_HEALTH_PROBE_OK           — Health probe returned HEALTHY + handshake valid
EV_HEALTH_PROBE_DEGRADED     — Health probe returned DEGRADED category
EV_HEALTH_PROBE_TIMEOUT      — Health probe timed out (single occurrence)
EV_BOOT_DEADLINE_EXCEEDED    — Boot watchdog fired (cumulative timeout)
EV_PRESSURE_COOLED           — Memory below release threshold for hysteresis window
EV_HEALTH_DEGRADED           — Consecutive degraded probes >= k_degraded
EV_HEALTH_UNREACHABLE        — Consecutive unreachable probes >= k_unreachable
EV_SPOT_PREEMPTED            — Preemption detected (metadata or inferred from health)
EV_BUDGET_EXHAUSTED_RUNTIME  — Hard budget cap crossed during active session
EV_COOLDOWN_EXPIRED          — Cooldown grace timer completed
EV_VM_STOPPED                — Provider confirmed VM stopped/terminated
EV_STOP_TIMEOUT              — Stop deadline exceeded
EV_SESSION_SHUTDOWN          — Graceful shutdown requested by operator
EV_LEASE_LOST                — Epoch mismatch detected (fencing violation)
EV_RECONCILE_OBSERVED_RUNNING  — Reconciler found running VM not tracked in journal
EV_RECONCILE_OBSERVED_STOPPED  — Reconciler found journal thinks running, actually gone
EV_MANUAL_FORCE_LOCAL        — Operator override: force local routing
EV_MANUAL_FORCE_CLOUD        — Operator override: force cloud provisioning
```

### Detailed State x Event x Guard x Side-Effect Matrix

> All mutating transitions require valid fence (`epoch == lease.epoch`) and write intent to journal before external side effects.
> Side effects must carry `op_id`/`request_id` for idempotent reconciliation.

| State | Event | Guard (must all pass) | Next | Side Effects (ordered) |
|---|---|---|---|---|
| `IDLE` | `EV_PRESSURE_TRIGGER` | pressure tier >= trigger tier for N-of-M samples; not manual-local-lock; lease held | `TRIGGERING` | `journal(pressure_detected)` -> `journal(trigger_requested)` |
| `TRIGGERING` | `EV_BUDGET_CHECK` | none | `TRIGGERING` | `atomic reserve_budget(op_id)` |
| `TRIGGERING` | `EV_BUDGET_APPROVED` | reservation row committed | `PROVISIONING` | `journal(budget_reserved, op_id)` -> `journal(provision_requested, op_id)` -> enqueue provision command |
| `TRIGGERING` | `EV_BUDGET_DENIED` | reservation failed | `COOLING_DOWN` | `journal(budget_denied)` -> start cooldown timer |
| `PROVISIONING` | `EV_PROVISION_EXECUTE` | idempotency key unused OR prior pending reconciled | `PROVISIONING` | call `ensure_static_vm_ready(request_id)` |
| `PROVISIONING` | `EV_VM_CREATE_ACCEPTED` | provider accepted request | `BOOTING` | `journal(vm_create_accepted, request_id, instance_ref)` -> start boot watchdog |
| `PROVISIONING` | `EV_VM_CREATE_ALREADY_EXISTS` | discovered existing matching instance | `BOOTING` | `journal(vm_adopted_existing, instance_ref)` -> start boot watchdog |
| `PROVISIONING` | `EV_VM_CREATE_FAILED` | retries < max_retries | `TRIGGERING` | `journal(vm_create_failed_retryable)` -> backoff schedule |
| `PROVISIONING` | `EV_VM_CREATE_FAILED` | retries >= max_retries | `COOLING_DOWN` | `journal(vm_create_failed_terminal)` -> `release_budget(op_id)` -> cooldown |
| `BOOTING` | `EV_HEALTH_PROBE_OK` | probe category `HEALTHY`; handshake valid (api/schema/capabilities) | `ACTIVE` | `journal(vm_ready)` -> `journal(routing_switch_requested cloud)` -> switch routing -> `journal(routing_switched cloud)` |
| `BOOTING` | `EV_HEALTH_PROBE_DEGRADED` | consecutive degraded < degrade_limit | `BOOTING` | `journal(boot_probe_degraded)` (stay) |
| `BOOTING` | `EV_HEALTH_PROBE_TIMEOUT` | retries remaining | `BOOTING` | `journal(boot_probe_timeout_retry)` |
| `BOOTING` | `EV_BOOT_DEADLINE_EXCEEDED` | none | `COOLING_DOWN` | `journal(boot_failed_timeout)` -> routing local (if needed) -> `release_budget(op_id)` -> cooldown |
| `ACTIVE` | `EV_PRESSURE_COOLED` | cooled hysteresis met for window T | `COOLING_DOWN` | `journal(cooldown_started_reason_pressure_cleared)` |
| `ACTIVE` | `EV_HEALTH_DEGRADED` | degraded consecutive >= `k_degraded` and unreachable < `k_unreachable` | `ACTIVE` | `journal(cloud_degraded)` (no immediate route flap) |
| `ACTIVE` | `EV_HEALTH_UNREACHABLE` | unreachable consecutive >= `k_unreachable` | `TRIGGERING` | `journal(cloud_lost)` -> `journal(routing_switch_requested local)` -> switch local -> `journal(routing_switched local)` -> retrigger |
| `ACTIVE` | `EV_SPOT_PREEMPTED` | preemption signal OR metadata indicates preempted | `TRIGGERING` | same as cloud_lost path + `journal(preempted)` |
| `ACTIVE` | `EV_BUDGET_EXHAUSTED_RUNTIME` | hard cap crossed | `COOLING_DOWN` | `journal(runtime_budget_exhausted)` -> route local -> cooldown |
| `COOLING_DOWN` | `EV_PRESSURE_TRIGGER` | sustained pressure returns and not cooldown-hardlock | `TRIGGERING` | `journal(retrigger_during_cooldown)` |
| `COOLING_DOWN` | `EV_COOLDOWN_EXPIRED` | no trigger condition | `STOPPING` | `journal(stop_requested)` -> stop VM command |
| `STOPPING` | `EV_VM_STOPPED` | provider reports stopped/terminated | `IDLE` | `journal(vm_stopped)` -> `release_budget(op_id)` -> clear runtime counters |
| `STOPPING` | `EV_STOP_TIMEOUT` | stop deadline exceeded | `IDLE` | `journal(stop_timeout)` -> reconcile cleanup marker |
| `ANY` | `EV_SESSION_SHUTDOWN` | shutdown intent active | `STOPPING` | route local -> stop requested |
| `ANY` | `EV_LEASE_LOST` | epoch mismatch detected | `IDLE` (read-only) | halt side effects immediately; `journal(authority_lost)` best effort; terminate worker loops |
| `ANY` | `EV_RECONCILE_OBSERVED_RUNNING` | journal says non-running; probe says healthy running | state by policy (`BOOTING` or `ACTIVE`) | `journal(reconcile_adopt_running)` -> run handshake if not previously validated |
| `ANY` | `EV_RECONCILE_OBSERVED_STOPPED` | journal says running; probe says absent | `LOST` semantic via `TRIGGERING` or `IDLE` path | `journal(reconcile_mark_lost)` -> route local -> optional retrigger |
| `ANY` | `EV_MANUAL_FORCE_LOCAL` | operator override active | `COOLING_DOWN`/`STOPPING` | `journal(manual_force_local)` -> route local -> stop path |
| `ANY` | `EV_MANUAL_FORCE_CLOUD` | operator override active + budget approved | `PROVISIONING` | `journal(manual_force_cloud)` -> reserve budget -> provision |

### Transition Invariants

- **Invariant 1 — Fence first:** no mutation without valid epoch fence.
- **Invariant 2 — Commit before publish:** persist journal entry before UDS event emit (outbox pattern preferred).
- **Invariant 3 — Idempotent side effects:** all provider calls carry stable `op_id/request_id`.
- **Invariant 4 — No direct `ACTIVE` recovery:** `FAILED/LOST -> STARTING/BOOTING -> HANDSHAKE -> ACTIVE`.
- **Invariant 5 — Hysteresis required:** no single probe timeout can trigger route flap.
- **Invariant 6 — Budget is reserved, not checked-only:** eliminate check/create race.

### Illegal Transitions (explicitly reject)

- `IDLE -> ACTIVE` (must pass provision/boot/handshake path)
- `PROVISIONING -> ACTIVE` (must pass `BOOTING`)
- `ACTIVE -> IDLE` (must pass `COOLING_DOWN/STOPPING`, except forced emergency local-route still records stop path)
- Any transition with stale epoch (`fence_token != lease.epoch`)

### Journal Entry Schema for GCP Lifecycle

Every transition produces a journal entry:

```python
journal.fenced_write(
    action="gcp_lifecycle",
    target="invincible_node",          # or "spot_vm:{name}"
    idempotency_key=f"gcp:{state}:{event}:{op_id}",
    payload={
        "from_state": "EVALUATING",
        "to_state": "RESERVING",
        "event": "PRESSURE_SUSTAINED",
        "op_id": "op_2026-02-25_14:32:01_a3f2",  # Stable operation ID
        "guard_results": {
            "budget_available": True,
            "pressure_readings": [87, 89, 91, 88, 92],
            "hysteresis_satisfied": True,
        },
        "side_effect": None,  # Or {"type": "gcp_create", "instance_name": "..."}
    },
)
```

---

## Section 3: Atomic Budget Reservation Protocol

### The Race We're Closing

```
Current (TOCTU):
  T=0: check budget → $0.50 remaining
  T=1: another coroutine records $0.40 spend
  T=2: create VM → budget now -$0.20 (overdrawn)
```

### Reservation Protocol

```python
async def reserve_budget(self, estimated_cost: float, op_id: str) -> bool:
    """
    Atomically reserve budget via journal write.

    The journal entry IS the reservation. If two coroutines race,
    the second one's fenced_write sees the first's reservation
    in the budget calculation (because journal reads are serialized
    under the write lock).
    """
    # 1. Calculate available budget (includes all prior reservations)
    available = await self._calculate_available_budget()

    if available < estimated_cost:
        return False

    # 2. Journal the reservation (atomic under write lock)
    seq = self._journal.fenced_write(
        action="budget_reserved",
        target="cost_tracker",
        idempotency_key=f"budget_reserve:{op_id}",
        payload={
            "estimated_cost": estimated_cost,
            "available_before": available,
            "op_id": op_id,
        },
    )

    return True

async def commit_budget(self, op_id: str, actual_cost: float):
    """Mark reservation as consumed with actual cost."""
    self._journal.fenced_write(
        action="budget_committed",
        target="cost_tracker",
        idempotency_key=f"budget_commit:{op_id}",
        payload={"op_id": op_id, "actual_cost": actual_cost},
    )

async def release_budget(self, op_id: str):
    """Release unused reservation (e.g., VM creation failed)."""
    self._journal.fenced_write(
        action="budget_released",
        target="cost_tracker",
        idempotency_key=f"budget_release:{op_id}",
        payload={"op_id": op_id},
    )
```

**Budget calculation** replays all `budget_reserved` / `budget_committed` / `budget_released` entries for the current day to compute available budget. This is idempotent and crash-safe.

---

## Section 4: Outbox / Event Ordering Contract

### The Problem

Events emitted via UDS fabric can arrive at subscribers before the journal entry is visible to them (if they read the journal independently).

### Outbox Pattern

```
1. State machine writes journal entry (fenced_write)
   → SQLite WAL flush guarantees durability
2. State machine writes to outbox table (same transaction)
   → Outbox entry: {seq, event_type, target, payload, published: false}
3. Transaction commits (atomic: journal + outbox)
4. Outbox publisher (async loop):
   → Reads unpublished outbox entries
   → Emits via EventFabric
   → Marks published: true
   → If crash between 3 and 4: outbox entries replayed on restart (at-least-once)
```

### Outbox Schema

```sql
CREATE TABLE event_outbox (
    seq             INTEGER PRIMARY KEY,  -- Same seq as journal entry
    event_type      TEXT NOT NULL,
    target          TEXT NOT NULL,
    payload         TEXT,                 -- JSON
    published       INTEGER NOT NULL DEFAULT 0,
    published_at    REAL,
    FOREIGN KEY (seq) REFERENCES journal(seq)
);
```

### Subscriber Contract

Subscribers must be idempotent. They may receive the same event twice (at-least-once delivery from outbox replay). The `seq` field provides dedup.

---

## Section 5: Recovery / Reconcile Contract for External Side Effects

### Lease-Safe Side Effect Protocol

Every external side effect (GCP API call, cost recording, etc.) follows this protocol:

```python
async def execute_side_effect(self, intent_seq: int, op_id: str):
    """
    Execute external side effect with lease-safe semantics.

    Contract:
    1. Intent is already journaled (intent_seq exists)
    2. op_id is stable and deterministic (survives crash)
    3. Side effect is idempotent (same op_id → same result)
    4. Result is journaled only if lease still held
    5. If lease lost, next leader reconciles via op_id
    """
    # Execute the external operation
    try:
        result = await self._gcp_manager.create_vm(
            instance_name=f"jarvis-prime-{op_id}",
            # ... config ...
        )
    except Exception as e:
        # Journal failure (if lease still held)
        if self._journal.lease_held:
            self._journal.mark_result(intent_seq, "failed",
                payload={"error": str(e), "op_id": op_id})
        return None

    # Verify lease before journaling success
    if not self._journal.lease_held:
        logger.warning(
            f"Lease lost after side effect {op_id}. "
            f"Next leader must reconcile."
        )
        # Side effect committed but not journaled.
        # Next leader will discover via reconciliation.
        return None

    # Journal success
    self._journal.mark_result(intent_seq, "committed",
        payload={"op_id": op_id, "instance_id": result.instance_id})

    return result
```

### Reconciliation on Leader Takeover

When a new leader acquires the lease, before processing new events:

```python
async def reconcile_pending_side_effects(self):
    """
    Find journal entries with result='pending' and reconcile
    against actual GCP state.
    """
    pending = await self._journal.replay_from(0)
    pending = [e for e in pending if e["result"] == "pending"]

    for entry in pending:
        op_id = entry["payload"].get("op_id")
        if not op_id:
            # Legacy entry without op_id — mark stale
            self._journal.mark_result(entry["seq"], "stale")
            continue

        # Query GCP for actual state
        actual_state = await self._gcp_manager.get_instance_by_op_id(op_id)

        if actual_state is None:
            # Side effect never executed or was cleaned up
            self._journal.mark_result(entry["seq"], "abandoned")
        elif actual_state.status == "RUNNING":
            # Side effect succeeded, adopt it
            self._journal.mark_result(entry["seq"], "committed",
                payload={"instance_id": actual_state.instance_id,
                         "reconciled": True})
        elif actual_state.status in ("STOPPED", "TERMINATED"):
            # Side effect succeeded but resource is down
            self._journal.mark_result(entry["seq"], "committed_but_stopped",
                payload={"instance_id": actual_state.instance_id})
```

---

## Section 6: Test Matrix

### Fault-Injection Tests (HARD GATE)

All tests must pass before merging. They exercise the full pressure → budget → provision → route path.

| # | Test | Scenario | Expected Behavior |
|---|------|----------|-------------------|
| 1 | `test_pressure_to_provision_full_path` | Sustained pressure (3/5 readings) → budget available → VM created → health passes → routing switches | Journal has entries for every transition; PrimeRouter endpoint updated |
| 2 | `test_budget_race_two_concurrent_requests` | Two coroutines both detect pressure simultaneously | Only one budget reservation succeeds; second gets BUDGET_DENIED |
| 3 | `test_lease_loss_during_vm_creation` | Leader creates VM via GCP API, loses lease before journaling result | New leader reconciles: finds VM in GCP, adopts it, journals "committed_reconciled" |
| 4 | `test_preemption_detection_and_recovery` | VM preempted by GCP mid-operation | State machine: ACTIVE → TRIGGERING via `EV_SPOT_PREEMPTED` (journaled); routing switches to local; re-provision if budget allows |
| 5 | `test_outbox_event_ordering` | Journal write + outbox write + crash before publish | On restart, outbox publisher replays unpublished events; subscribers receive in order |
| 6 | `test_invincible_node_crash_recovery` | Supervisor crashes while invincible node is ACTIVE | New leader: reads component_state → probes invincible node → finds RUNNING → adopts → resumes routing |
| 7 | `test_probe_hysteresis_transient_failure` | Single health probe timeout followed by success | State stays ACTIVE (no transition); consecutive_failures incremented then reset |
| 8 | `test_startup_ambiguity_never_launched` | Component in STARTING state but start_timestamp is null | Reconciler issues START (not FAILED); journal records "never_launched_recovery" |
| 9 | `test_budget_reservation_crash_recovery` | Budget reserved, crash before VM creation | New leader: sees "budget_reserved" without "budget_committed" → releases reservation |
| 10 | `test_session_shutdown_stops_active_vm` | Graceful shutdown while VM is ACTIVE | State: ACTIVE → STOPPING via `EV_SESSION_SHUTDOWN`; route switches to local; VM stop confirmed → IDLE |
| 11 | `test_cooldown_prevents_flapping` | Pressure spikes, VM created, pressure drops, pressure spikes again within 60s | Second spike hits COOLING_DOWN state; no new VM created until cooldown expires |
| 12 | `test_stale_signal_file_ignored` | `/tmp/jarvis_progress.json` exists from prior boot with old epoch | State machine checks file epoch; ignores stale data; starts fresh |

### Integration Tests

| # | Test | What It Validates |
|---|------|-------------------|
| 13 | `test_full_lifecycle_idle_to_active_to_idle` | Complete round-trip with journal replay verification |
| 14 | `test_cost_tracker_reconciles_with_journal` | Cost tracker totals match journal budget entries |
| 15 | `test_event_fabric_never_outruns_journal` | Subscriber always finds journal entry for received event |

---

## Files to Modify

| File | Change |
|------|--------|
| `backend/core/gcp_lifecycle_state_machine.py` | **NEW**: Unified state machine, replaces scattered booleans |
| `backend/core/orchestration_journal.py` | Add `event_outbox` table, `reserve_budget()`, `reconcile_pending()` |
| `backend/core/gcp_hybrid_prime_router.py` | Remove own state management; delegate to state machine |
| `backend/core/gcp_vm_manager.py` | `should_create_vm()` → called only by state machine; `ensure_static_vm_ready()` → journaled |
| `backend/core/supervisor_gcp_controller.py` | Remove local `_spend_today`; use journal budget entries |
| `backend/core/intelligent_gcp_optimizer.py` | Scoring logic extracted into state machine guard evaluation |
| `backend/core/gcp_oom_prevention_bridge.py` | Pre-flight check → publishes pressure event; doesn't directly trigger GCP |
| `backend/core/cost_tracker.py` | `can_create_vm()` → reads journal reservations; `record_vm_created()` → journal-backed |
| `backend/core/recovery_protocol.py` | Add hysteresis buffer, failure classification, startup ambiguity detection |
| `backend/core/uds_event_fabric.py` | Outbox publisher loop; emit from outbox table, not direct |
| `backend/core/control_plane_client.py` | Subscriber dedup via seq (at-least-once safe) |
| `tests/adversarial/test_gcp_lifecycle_fault_injection.py` | **NEW**: 15 HARD GATE tests from matrix above |

---

## Open Questions

1. ~~**Transition table**: Awaiting user-provided state × event table for deterministic implementation~~ **RESOLVED** — full transition table integrated (Sections 1 + 2)
2. **Hysteresis window**: How many consecutive failures before route flap? (Proposed: `k_unreachable=3`, `k_degraded=5`)
3. **Cooldown duration**: How long after VM stop before re-provisioning allowed? (Proposed: 60s, env `JARVIS_GCP_COOLDOWN_SEC`)
4. **Budget reservation TTL**: How long does an uncommitted reservation hold? (Proposed: 300s, then auto-release via background reaper)
5. **Outbox publish interval**: How frequently does the outbox publisher poll? (Proposed: 100ms)
6. ~~**Enum schema**: Awaiting compact enum list (`State`, `Event`, `HealthCategory`, `DisconnectReason`) for schema-aligned implementation + tests~~ **RESOLVED** — canonical schema integrated (Section 1B)

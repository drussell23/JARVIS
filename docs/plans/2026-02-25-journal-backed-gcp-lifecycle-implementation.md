# Journal-Backed GCP Lifecycle Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace six independent GCP decision-makers with a single journal-backed state machine that owns all lifecycle transitions, budget reservations, event ordering, and crash recovery.

**Architecture:** Canonical enums define all states/events. Every transition is journaled before side effects execute. Budget is reserved atomically via journal entries. Events publish only after journal commit (outbox pattern). Recovery reconciles pending side effects against actual GCP state.

**Tech Stack:** Python 3.11+, SQLite (WAL mode), asyncio, pytest + fault injection framework from Phase 2A

**Design Doc:** `docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md`

---

## Execution Rules

- No wave advance without passing prior wave's gate tests.
- Track every transition/action by canonical enum value only (`State.value`, `Event.value`).
- Enforce "commit-before-publish" and "fence-before-mutate" in tests, not just code review.
- TDD: write failing test → implement → pass → commit.
- All new code in `.worktrees/phase2b/` on branch `feature/journal-backed-gcp-lifecycle`.

---

## Wave 1: Schema + Enums + Migration + Validation Guards

**Purpose:** Establish the canonical vocabulary that all subsequent waves build on. No behavior yet — just types, schema, and boundary validation.

---

### Task 1.1: Create canonical enum module

**Files:**
- Create: `backend/core/gcp_lifecycle_schema.py`
- Test: `tests/unit/core/test_gcp_lifecycle_schema.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_gcp_lifecycle_schema.py
"""Tests for GCP lifecycle canonical schema."""
import json
import pytest

from backend.core.gcp_lifecycle_schema import (
    State, Event, HealthCategory, DisconnectReason,
    validate_state, validate_event, validate_health_category,
)


class TestStateEnum:
    def test_all_states_are_str_enum(self):
        for s in State:
            assert isinstance(s, str)
            assert isinstance(s.value, str)

    def test_primary_lifecycle_states_exist(self):
        required = ["idle", "triggering", "provisioning", "booting",
                     "handshaking", "active", "cooling_down", "stopping"]
        for val in required:
            assert State(val) is not None

    def test_auxiliary_states_exist(self):
        for val in ["lost", "failed", "degraded"]:
            assert State(val) is not None

    def test_state_serializes_to_json(self):
        payload = {"state": State.ACTIVE}
        dumped = json.dumps(payload)
        assert '"active"' in dumped

    def test_state_round_trips_json(self):
        original = State.PROVISIONING
        dumped = json.dumps({"s": original})
        loaded = json.loads(dumped)
        assert State(loaded["s"]) is State.PROVISIONING


class TestEventEnum:
    def test_pressure_events_exist(self):
        assert Event.PRESSURE_TRIGGERED.value == "pressure_triggered"
        assert Event.PRESSURE_COOLED.value == "pressure_cooled"

    def test_budget_events_exist(self):
        for name in ["budget_check", "budget_approved", "budget_denied",
                      "budget_exhausted_runtime", "budget_released"]:
            assert Event(name) is not None

    def test_vm_events_exist(self):
        for name in ["provision_requested", "vm_create_accepted",
                      "vm_create_already_exists", "vm_create_failed",
                      "vm_ready", "vm_stopped", "spot_preempted"]:
            assert Event(name) is not None

    def test_health_events_exist(self):
        for name in ["health_probe_ok", "health_probe_degraded",
                      "health_probe_timeout", "health_unreachable_consecutive",
                      "handshake_started", "handshake_succeeded",
                      "handshake_failed", "boot_deadline_exceeded"]:
            assert Event(name) is not None

    def test_control_events_exist(self):
        for name in ["lease_lost", "session_shutdown",
                      "manual_force_local", "manual_force_cloud", "fatal_error"]:
            assert Event(name) is not None


class TestHealthCategory:
    def test_all_categories(self):
        expected = ["healthy", "contract_mismatch", "dependency_degraded",
                     "service_degraded", "unreachable", "timeout", "unknown"]
        for val in expected:
            assert HealthCategory(val) is not None


class TestDisconnectReason:
    def test_all_reasons(self):
        expected = ["timeout", "write_error", "eof", "protocol_error",
                     "lease_lost", "server_shutdown", "client_shutdown"]
        for val in expected:
            assert DisconnectReason(val) is not None


class TestValidation:
    def test_validate_state_accepts_valid(self):
        assert validate_state("active") == State.ACTIVE

    def test_validate_state_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown state"):
            validate_state("running")

    def test_validate_event_accepts_valid(self):
        assert validate_event("vm_ready") == Event.VM_READY

    def test_validate_event_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown event"):
            validate_event("vm_started")

    def test_validate_health_category_accepts_valid(self):
        assert validate_health_category("healthy") == HealthCategory.HEALTHY

    def test_validate_health_category_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown health category"):
            validate_health_category("good")
```

**Step 2: Run test to verify it fails**

Run: `cd .worktrees/phase2b && python3 -m pytest tests/unit/core/test_gcp_lifecycle_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.gcp_lifecycle_schema'`

**Step 3: Write minimal implementation**

```python
# backend/core/gcp_lifecycle_schema.py
"""Canonical schema for GCP lifecycle state machine.

All journal rows, UDS payloads, state machine internals, and tests
use these exact enum values. Reject unknown strings at boundaries.

Design doc: docs/plans/2026-02-25-journal-backed-gcp-lifecycle-design.md
Section 1B: Canonical Schema.
"""
from enum import Enum
from typing import Optional


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


class HealthCategory(str, Enum):
    HEALTHY = "healthy"
    CONTRACT_MISMATCH = "contract_mismatch"
    DEPENDENCY_DEGRADED = "dependency_degraded"
    SERVICE_DEGRADED = "service_degraded"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class DisconnectReason(str, Enum):
    TIMEOUT = "timeout"
    WRITE_ERROR = "write_error"
    EOF = "eof"
    PROTOCOL_ERROR = "protocol_error"
    LEASE_LOST = "lease_lost"
    SERVER_SHUTDOWN = "server_shutdown"
    CLIENT_SHUTDOWN = "client_shutdown"


def validate_state(value: str) -> State:
    try:
        return State(value)
    except ValueError:
        raise ValueError(f"Unknown state: {value!r}. Valid: {[s.value for s in State]}")


def validate_event(value: str) -> Event:
    try:
        return Event(value)
    except ValueError:
        raise ValueError(f"Unknown event: {value!r}. Valid: {[e.value for e in Event]}")


def validate_health_category(value: str) -> HealthCategory:
    try:
        return HealthCategory(value)
    except ValueError:
        raise ValueError(
            f"Unknown health category: {value!r}. "
            f"Valid: {[h.value for h in HealthCategory]}"
        )
```

**Step 4: Run test to verify it passes**

Run: `cd .worktrees/phase2b && python3 -m pytest tests/unit/core/test_gcp_lifecycle_schema.py -v`
Expected: all 18 tests PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_lifecycle_schema.py tests/unit/core/test_gcp_lifecycle_schema.py
git commit -m "feat: add canonical GCP lifecycle schema (State, Event, HealthCategory, DisconnectReason)"
```

---

### Task 1.2: Add event_outbox table + budget tables to journal schema

**Files:**
- Modify: `backend/core/orchestration_journal.py`
- Test: `tests/unit/core/test_journal_outbox_schema.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_journal_outbox_schema.py
"""Tests for journal outbox and budget reservation schema."""
import sqlite3
import pytest
from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease("test_leader")
    yield j
    await j.close()


class TestOutboxSchema:
    @pytest.mark.asyncio
    async def test_event_outbox_table_exists(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_outbox'"
        ).fetchone()
        conn.close()
        assert row is not None, "event_outbox table not created"

    @pytest.mark.asyncio
    async def test_event_outbox_columns(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(event_outbox)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        for col in ["seq", "event_type", "target", "payload", "published", "published_at"]:
            assert col in col_names, f"Missing column: {col}"

    @pytest.mark.asyncio
    async def test_outbox_fk_references_journal(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        fks = conn.execute("PRAGMA foreign_key_list(event_outbox)").fetchall()
        conn.close()
        journal_refs = [fk for fk in fks if fk[2] == "journal"]
        assert len(journal_refs) > 0, "event_outbox has no FK to journal"


class TestComponentStateExtensions:
    @pytest.mark.asyncio
    async def test_component_state_has_start_timestamp(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "start_timestamp" in col_names, "Missing start_timestamp column"

    @pytest.mark.asyncio
    async def test_component_state_has_consecutive_failures(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "consecutive_failures" in col_names, "Missing consecutive_failures column"

    @pytest.mark.asyncio
    async def test_component_state_has_last_probe_category(self, journal, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("PRAGMA table_info(component_state)").fetchall()
        col_names = [r[1] for r in rows]
        conn.close()
        assert "last_probe_category" in col_names, "Missing last_probe_category column"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_journal_outbox_schema.py -v`
Expected: FAIL — `event_outbox` table not found, missing columns

**Step 3: Implement schema additions in `orchestration_journal.py`**

Add to `_create_tables()`:
- `CREATE TABLE event_outbox (...)` per design doc Section 4
- `ALTER TABLE component_state ADD COLUMN start_timestamp REAL`
- `ALTER TABLE component_state ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0`
- `ALTER TABLE component_state ADD COLUMN last_probe_category TEXT`

Use the existing migration pattern: check if table/column exists before creating (idempotent).

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_journal_outbox_schema.py -v`
Expected: all 6 tests PASS

**Step 5: Run full Phase 2A suite to verify no regressions**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py tests/unit/core/test_journal_compaction.py tests/unit/core/test_recovery_protocol.py -v`
Expected: all existing tests PASS

**Step 6: Commit**

```bash
git add backend/core/orchestration_journal.py tests/unit/core/test_journal_outbox_schema.py
git commit -m "feat: add event_outbox table and component_state extensions for hysteresis"
```

---

### Task 1.3: Add transition table + validation to state machine module

**Files:**
- Create: `backend/core/gcp_lifecycle_transitions.py`
- Test: `tests/unit/core/test_gcp_lifecycle_transitions.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_gcp_lifecycle_transitions.py
"""Tests for GCP lifecycle transition table — deterministic validation."""
import pytest
from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_transitions import (
    TRANSITION_TABLE,
    is_valid_transition,
    get_transition,
    ILLEGAL_TRANSITIONS,
)


class TestTransitionTable:
    def test_idle_pressure_triggered_goes_to_triggering(self):
        t = get_transition(State.IDLE, Event.PRESSURE_TRIGGERED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_triggering_budget_approved_goes_to_provisioning(self):
        t = get_transition(State.TRIGGERING, Event.BUDGET_APPROVED)
        assert t is not None
        assert t.next_state == State.PROVISIONING

    def test_triggering_budget_denied_goes_to_cooling_down(self):
        t = get_transition(State.TRIGGERING, Event.BUDGET_DENIED)
        assert t is not None
        assert t.next_state == State.COOLING_DOWN

    def test_booting_health_ok_goes_to_active(self):
        t = get_transition(State.BOOTING, Event.HEALTH_PROBE_OK)
        assert t is not None
        assert t.next_state == State.ACTIVE

    def test_active_preempted_goes_to_triggering(self):
        t = get_transition(State.ACTIVE, Event.SPOT_PREEMPTED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_active_unreachable_goes_to_triggering(self):
        t = get_transition(State.ACTIVE, Event.HEALTH_UNREACHABLE_CONSECUTIVE)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_stopping_vm_stopped_goes_to_idle(self):
        t = get_transition(State.STOPPING, Event.VM_STOPPED)
        assert t is not None
        assert t.next_state == State.IDLE

    def test_cooldown_pressure_returns_to_triggering(self):
        t = get_transition(State.COOLING_DOWN, Event.PRESSURE_TRIGGERED)
        assert t is not None
        assert t.next_state == State.TRIGGERING

    def test_cooldown_expired_goes_to_stopping(self):
        t = get_transition(State.COOLING_DOWN, Event.COOLDOWN_EXPIRED)
        assert t is not None
        assert t.next_state == State.STOPPING


class TestWildcardTransitions:
    def test_session_shutdown_from_any_state_goes_to_stopping(self):
        for state in [State.IDLE, State.TRIGGERING, State.ACTIVE, State.BOOTING]:
            t = get_transition(state, Event.SESSION_SHUTDOWN)
            assert t is not None, f"No SESSION_SHUTDOWN transition from {state}"
            assert t.next_state == State.STOPPING

    def test_lease_lost_from_any_state_goes_to_idle(self):
        for state in [State.TRIGGERING, State.PROVISIONING, State.ACTIVE]:
            t = get_transition(state, Event.LEASE_LOST)
            assert t is not None, f"No LEASE_LOST transition from {state}"
            assert t.next_state == State.IDLE

    def test_fatal_error_goes_to_cooling_down(self):
        for state in [State.TRIGGERING, State.PROVISIONING, State.BOOTING, State.ACTIVE]:
            t = get_transition(state, Event.FATAL_ERROR)
            assert t is not None, f"No FATAL_ERROR transition from {state}"
            assert t.next_state == State.COOLING_DOWN


class TestIllegalTransitions:
    def test_idle_to_active_rejected(self):
        assert not is_valid_transition(State.IDLE, State.ACTIVE)

    def test_provisioning_to_active_rejected(self):
        assert not is_valid_transition(State.PROVISIONING, State.ACTIVE)

    def test_active_to_idle_rejected(self):
        assert not is_valid_transition(State.ACTIVE, State.IDLE)

    def test_illegal_transitions_documented(self):
        assert len(ILLEGAL_TRANSITIONS) >= 3


class TestTransitionTableCompleteness:
    def test_every_primary_state_has_shutdown(self):
        """Every non-terminal state must handle SESSION_SHUTDOWN."""
        primary = [State.IDLE, State.TRIGGERING, State.PROVISIONING,
                   State.BOOTING, State.ACTIVE, State.COOLING_DOWN, State.STOPPING]
        for state in primary:
            t = get_transition(state, Event.SESSION_SHUTDOWN)
            assert t is not None, f"{state} has no SESSION_SHUTDOWN handler"

    def test_every_primary_state_has_lease_lost(self):
        """Every non-terminal state must handle LEASE_LOST."""
        primary = [State.TRIGGERING, State.PROVISIONING, State.BOOTING,
                   State.ACTIVE, State.COOLING_DOWN, State.STOPPING]
        for state in primary:
            t = get_transition(state, Event.LEASE_LOST)
            assert t is not None, f"{state} has no LEASE_LOST handler"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_transitions.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement transition table**

Create `backend/core/gcp_lifecycle_transitions.py` with:
- `TransitionEntry` dataclass: `(from_state, event, next_state, journal_actions: list[str], has_side_effect: bool)`
- `TRANSITION_TABLE`: dict mapping `(State, Event) -> TransitionEntry` from the design doc Section 2 matrix
- Wildcard entries: `(None, Event.SESSION_SHUTDOWN)` etc. that match any state
- `get_transition(state, event)` → checks exact match first, then wildcard
- `is_valid_transition(from_state, to_state)` → checks against `ILLEGAL_TRANSITIONS`
- `ILLEGAL_TRANSITIONS`: set of `(from_state, to_state)` tuples

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_transitions.py -v`
Expected: all 18 tests PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_lifecycle_transitions.py tests/unit/core/test_gcp_lifecycle_transitions.py
git commit -m "feat: add deterministic GCP lifecycle transition table with illegal transition guards"
```

---

### Wave 1 Gate Tests

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_schema.py tests/unit/core/test_journal_outbox_schema.py tests/unit/core/test_gcp_lifecycle_transitions.py -v`

**Gate criteria:**
- All schema enum tests pass (18)
- All outbox/component_state schema tests pass (6)
- All transition table tests pass (18)
- No Phase 2A regressions: `python3 -m pytest tests/unit/core/ tests/adversarial/ -v`
- Total: ~130 tests, 0 failures

**Rollback:** Delete the 3 new files + revert schema changes. No existing behavior modified.

---

## Wave 2: Journaled State Machine Core (Simulation Mode)

**Purpose:** Build the state machine engine that processes events and journals transitions. No real GCP side effects — all external calls go through a pluggable `SideEffectAdapter` that defaults to no-op in tests.

---

### Task 2.1: State machine engine with journal integration

**Files:**
- Create: `backend/core/gcp_lifecycle_state_machine.py`
- Test: `tests/unit/core/test_gcp_lifecycle_state_machine.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_gcp_lifecycle_state_machine.py
"""Tests for GCP lifecycle state machine engine."""
import asyncio
import os
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_state_machine import (
    GCPLifecycleStateMachine,
    SideEffectAdapter,
    TransitionResult,
)


class NoOpAdapter(SideEffectAdapter):
    """Test adapter that records calls but does nothing."""
    def __init__(self):
        self.calls = []

    async def execute(self, action: str, op_id: str, **kwargs):
        self.calls.append({"action": action, "op_id": op_id, **kwargs})
        return {"status": "simulated"}


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


@pytest.fixture
def adapter():
    return NoOpAdapter()


@pytest.fixture
def sm(journal, adapter):
    return GCPLifecycleStateMachine(journal, adapter, target="invincible_node")


class TestStateMachineInit:
    @pytest.mark.asyncio
    async def test_initial_state_is_idle(self, sm):
        assert sm.state == State.IDLE

    @pytest.mark.asyncio
    async def test_target_stored(self, sm):
        assert sm.target == "invincible_node"


class TestStateMachineTransitions:
    @pytest.mark.asyncio
    async def test_pressure_trigger_idle_to_triggering(self, sm):
        result = await sm.handle_event(Event.PRESSURE_TRIGGERED, payload={"tier": "critical"})
        assert result.success
        assert sm.state == State.TRIGGERING

    @pytest.mark.asyncio
    async def test_budget_approved_triggering_to_provisioning(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        result = await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        assert result.success
        assert sm.state == State.PROVISIONING

    @pytest.mark.asyncio
    async def test_budget_denied_triggering_to_cooling_down(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        result = await sm.handle_event(Event.BUDGET_DENIED)
        assert result.success
        assert sm.state == State.COOLING_DOWN

    @pytest.mark.asyncio
    async def test_vm_created_provisioning_to_booting(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        result = await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        assert result.success
        assert sm.state == State.BOOTING

    @pytest.mark.asyncio
    async def test_health_ok_booting_to_active(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        result = await sm.handle_event(Event.HEALTH_PROBE_OK, payload={"ip": "10.0.0.1"})
        assert result.success
        assert sm.state == State.ACTIVE

    @pytest.mark.asyncio
    async def test_full_lifecycle_round_trip(self, sm):
        """IDLE → TRIGGERING → PROVISIONING → BOOTING → ACTIVE → COOLING_DOWN → STOPPING → IDLE"""
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        await sm.handle_event(Event.HEALTH_PROBE_OK, payload={"ip": "10.0.0.1"})
        assert sm.state == State.ACTIVE
        await sm.handle_event(Event.PRESSURE_COOLED)
        assert sm.state == State.COOLING_DOWN
        await sm.handle_event(Event.COOLDOWN_EXPIRED)
        assert sm.state == State.STOPPING
        await sm.handle_event(Event.VM_STOPPED)
        assert sm.state == State.IDLE


class TestJournalIntegration:
    @pytest.mark.asyncio
    async def test_transition_produces_journal_entry(self, sm, journal):
        await sm.handle_event(Event.PRESSURE_TRIGGERED, payload={"tier": "critical"})
        entries = await journal.replay_from(0)
        lifecycle_entries = [e for e in entries if e["action"] == "gcp_lifecycle"]
        assert len(lifecycle_entries) >= 1
        last = lifecycle_entries[-1]
        assert last["target"] == "invincible_node"

    @pytest.mark.asyncio
    async def test_journal_entry_contains_from_to_state(self, sm, journal):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        entries = await journal.replay_from(0)
        lifecycle_entries = [e for e in entries if e["action"] == "gcp_lifecycle"]
        payload = lifecycle_entries[-1]["payload"]
        assert payload["from_state"] == "idle"
        assert payload["to_state"] == "triggering"
        assert payload["event"] == "pressure_triggered"


class TestIllegalTransitions:
    @pytest.mark.asyncio
    async def test_idle_cannot_receive_health_ok(self, sm):
        result = await sm.handle_event(Event.HEALTH_PROBE_OK)
        assert not result.success
        assert sm.state == State.IDLE

    @pytest.mark.asyncio
    async def test_invalid_event_for_state_returns_error(self, sm):
        result = await sm.handle_event(Event.VM_STOPPED)
        assert not result.success
        assert "no transition" in result.reason.lower() or "invalid" in result.reason.lower()


class TestWildcardTransitions:
    @pytest.mark.asyncio
    async def test_session_shutdown_from_active(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        await sm.handle_event(Event.HEALTH_PROBE_OK)
        assert sm.state == State.ACTIVE
        result = await sm.handle_event(Event.SESSION_SHUTDOWN)
        assert result.success
        assert sm.state == State.STOPPING

    @pytest.mark.asyncio
    async def test_lease_lost_halts_to_idle(self, sm):
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        result = await sm.handle_event(Event.LEASE_LOST)
        assert result.success
        assert sm.state == State.IDLE


class TestStateRecovery:
    @pytest.mark.asyncio
    async def test_recover_state_from_journal_replay(self, sm, journal, tmp_path):
        """After crash, new state machine recovers state from journal."""
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        assert sm.state == State.BOOTING

        # Simulate crash: create new state machine from same journal
        adapter2 = NoOpAdapter()
        sm2 = GCPLifecycleStateMachine(journal, adapter2, target="invincible_node")
        await sm2.recover_from_journal()
        assert sm2.state == State.BOOTING
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_state_machine.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement state machine engine**

Create `backend/core/gcp_lifecycle_state_machine.py` with:
- `SideEffectAdapter` abstract base class with `async execute(action, op_id, **kwargs)`
- `TransitionResult` dataclass: `success: bool, from_state: State, to_state: State, seq: Optional[int], reason: str`
- `GCPLifecycleStateMachine.__init__(journal, adapter, target)` — stores journal ref, adapter, current state
- `handle_event(event, payload=None)` — looks up transition, validates guard (fence check), journals, executes side effects, updates state
- `recover_from_journal()` — replays `gcp_lifecycle` entries for this target, reconstructs last known state
- Every state change: `journal.fenced_write(action="gcp_lifecycle", target=self.target, ...)` BEFORE in-memory update

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_state_machine.py -v`
Expected: all 16 tests PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_lifecycle_state_machine.py tests/unit/core/test_gcp_lifecycle_state_machine.py
git commit -m "feat: add journaled GCP lifecycle state machine with recovery from journal replay"
```

---

### Task 2.2: Probe hysteresis buffer + startup ambiguity detection

**Files:**
- Modify: `backend/core/recovery_protocol.py`
- Test: `tests/unit/core/test_probe_hysteresis.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_probe_hysteresis.py
"""Tests for probe hysteresis and startup ambiguity detection."""
import os
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.gcp_lifecycle_schema import HealthCategory
from backend.core.recovery_protocol import (
    HealthBuffer,
    RecoveryReconciler,
    ProbeResult,
)
from backend.core.lifecycle_engine import (
    ComponentDeclaration,
    ComponentLocality,
    LifecycleEngine,
)


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


class TestHealthBuffer:
    def test_single_failure_below_threshold(self):
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)
        buf.record_failure(HealthCategory.UNREACHABLE)
        assert not buf.should_mark_lost()

    def test_consecutive_failures_trigger_lost(self):
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)
        for _ in range(3):
            buf.record_failure(HealthCategory.UNREACHABLE)
        assert buf.should_mark_lost()

    def test_success_resets_counter(self):
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)
        buf.record_failure(HealthCategory.UNREACHABLE)
        buf.record_failure(HealthCategory.UNREACHABLE)
        buf.record_success()
        buf.record_failure(HealthCategory.UNREACHABLE)
        assert not buf.should_mark_lost()

    def test_degraded_threshold_independent(self):
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)
        for _ in range(4):
            buf.record_failure(HealthCategory.SERVICE_DEGRADED)
        assert not buf.should_mark_degraded()
        buf.record_failure(HealthCategory.SERVICE_DEGRADED)
        assert buf.should_mark_degraded()

    def test_timeout_counts_as_unreachable(self):
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)
        for _ in range(3):
            buf.record_failure(HealthCategory.TIMEOUT)
        assert buf.should_mark_lost()


class TestStartupAmbiguity:
    @pytest.mark.asyncio
    async def test_starting_with_no_timestamp_means_never_launched(self, journal):
        """Component STARTING with null start_timestamp → never launched, not crashed."""
        decls = (ComponentDeclaration(name="comp_x", locality=ComponentLocality.IN_PROCESS),)
        engine = LifecycleEngine(journal, decls)
        engine._statuses["comp_x"] = "STARTING"

        reconciler = RecoveryReconciler(journal, engine)
        probe = ProbeResult(reachable=False, category=HealthCategory.UNREACHABLE)

        # With no start_timestamp, reconciler should START, not FAIL
        actions = await reconciler.reconcile(
            "comp_x", "STARTING", probe,
            start_timestamp=None,
        )
        has_start = any(a.get("to") == "STARTING" or a.get("action") == "start_requested" for a in actions)
        has_failed = any(a.get("to") == "FAILED" for a in actions)
        assert has_start or not has_failed, (
            f"Should START (never launched), not FAIL. Actions: {actions}"
        )

    @pytest.mark.asyncio
    async def test_starting_with_old_timestamp_means_crashed(self, journal):
        """Component STARTING with timestamp > 60s ago → crashed during startup."""
        import time
        decls = (ComponentDeclaration(name="comp_y", locality=ComponentLocality.IN_PROCESS),)
        engine = LifecycleEngine(journal, decls)
        engine._statuses["comp_y"] = "STARTING"

        reconciler = RecoveryReconciler(journal, engine)
        probe = ProbeResult(reachable=False, category=HealthCategory.UNREACHABLE)

        actions = await reconciler.reconcile(
            "comp_y", "STARTING", probe,
            start_timestamp=time.time() - 120,  # 2 minutes ago
        )
        has_failed = any(a.get("to") == "FAILED" for a in actions)
        assert has_failed, f"Should mark FAILED (crashed during startup). Actions: {actions}"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_probe_hysteresis.py -v`
Expected: FAIL — `ImportError: cannot import name 'HealthBuffer'`

**Step 3: Implement**

Add to `backend/core/recovery_protocol.py`:
- `HealthBuffer` class with `k_unreachable`, `k_degraded` thresholds, `record_failure()`, `record_success()`, `should_mark_lost()`, `should_mark_degraded()`
- Modify `RecoveryReconciler.reconcile()` to accept optional `start_timestamp` parameter
- When `projected == "STARTING"` and `start_timestamp is None`: treat as "never launched" → action is restart, not fail
- When `projected == "STARTING"` and `start_timestamp` is old (>60s): treat as "crashed during startup" → mark FAILED

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_probe_hysteresis.py tests/unit/core/test_recovery_protocol.py -v`
Expected: all new + existing tests PASS

**Step 5: Commit**

```bash
git add backend/core/recovery_protocol.py tests/unit/core/test_probe_hysteresis.py
git commit -m "feat: add probe hysteresis buffer and startup ambiguity detection"
```

---

### Wave 2 Gate Tests

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_state_machine.py tests/unit/core/test_probe_hysteresis.py -v`

**Gate criteria:**
- State machine handles full lifecycle round-trip (16 tests)
- State recovery from journal replay works
- Probe hysteresis prevents single-failure route flap (7 tests)
- Startup ambiguity correctly distinguished
- No Phase 2A regressions: `python3 -m pytest tests/unit/core/ tests/adversarial/ -v`

**Rollback:** Revert `gcp_lifecycle_state_machine.py` + `HealthBuffer` additions. Schema changes from Wave 1 are safe to keep.

---

## Wave 3: Budget Reservation Atomicity + Outbox Ordering

**Purpose:** Close the TOCTU budget race and guarantee commit-before-publish event ordering.

---

### Task 3.1: Atomic budget reservation via journal

**Files:**
- Modify: `backend/core/orchestration_journal.py`
- Test: `tests/unit/core/test_budget_reservation.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_budget_reservation.py
"""Tests for atomic budget reservation protocol."""
import asyncio
import os
import pytest
from backend.core.orchestration_journal import OrchestrationJournal


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


class TestBudgetReservation:
    @pytest.mark.asyncio
    async def test_reserve_within_budget(self, journal):
        ok = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        assert ok

    @pytest.mark.asyncio
    async def test_reserve_exceeding_budget(self, journal):
        journal.reserve_budget(0.80, "op_1", daily_budget=1.00)
        ok = journal.reserve_budget(0.30, "op_2", daily_budget=1.00)
        assert not ok

    @pytest.mark.asyncio
    async def test_commit_budget(self, journal):
        journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        journal.commit_budget("op_1", actual_cost=0.25)
        entries = await journal.replay_from(0)
        commits = [e for e in entries if e["action"] == "budget_committed"]
        assert len(commits) == 1

    @pytest.mark.asyncio
    async def test_release_budget_frees_capacity(self, journal):
        journal.reserve_budget(0.80, "op_1", daily_budget=1.00)
        journal.release_budget("op_1")
        ok = journal.reserve_budget(0.80, "op_2", daily_budget=1.00)
        assert ok

    @pytest.mark.asyncio
    async def test_idempotent_reserve(self, journal):
        seq1 = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        seq2 = journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        assert seq1 == seq2  # Same op_id → idempotent

    @pytest.mark.asyncio
    async def test_concurrent_reservations_serialized(self, journal):
        """Two concurrent reserve calls cannot both succeed if total exceeds budget."""
        results = []

        async def reserve(op_id):
            ok = journal.reserve_budget(0.60, op_id, daily_budget=1.00)
            results.append((op_id, ok))

        await asyncio.gather(reserve("op_a"), reserve("op_b"))
        approved = [r for r in results if r[1]]
        assert len(approved) == 1, f"Expected 1 approval, got {len(approved)}: {results}"

    @pytest.mark.asyncio
    async def test_calculate_available_budget(self, journal):
        journal.reserve_budget(0.30, "op_1", daily_budget=1.00)
        journal.commit_budget("op_1", actual_cost=0.25)
        journal.reserve_budget(0.20, "op_2", daily_budget=1.00)
        available = journal.calculate_available_budget(daily_budget=1.00)
        # Committed: $0.25 + reserved (uncommitted): $0.20 = $0.45 used
        assert 0.54 <= available <= 0.56
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_budget_reservation.py -v`
Expected: FAIL — `AttributeError: 'OrchestrationJournal' object has no attribute 'reserve_budget'`

**Step 3: Implement budget reservation methods**

Add to `OrchestrationJournal`:
- `reserve_budget(estimated_cost, op_id, daily_budget) -> bool` — calculates available under write lock, writes `budget_reserved` entry with idempotency key `budget_reserve:{op_id}`
- `commit_budget(op_id, actual_cost)` — writes `budget_committed` with key `budget_commit:{op_id}`
- `release_budget(op_id)` — writes `budget_released` with key `budget_release:{op_id}`
- `calculate_available_budget(daily_budget) -> float` — replays today's budget entries, computes: `daily_budget - sum(committed) - sum(reserved_but_not_committed)`

All operations use `fenced_write` (epoch-fenced, under `_write_lock`), so concurrent reservations are serialized.

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_budget_reservation.py -v`
Expected: all 7 tests PASS

**Step 5: Commit**

```bash
git add backend/core/orchestration_journal.py tests/unit/core/test_budget_reservation.py
git commit -m "feat: add atomic budget reservation protocol to orchestration journal"
```

---

### Task 3.2: Outbox publisher for commit-before-publish ordering

**Files:**
- Modify: `backend/core/orchestration_journal.py` (outbox write method)
- Modify: `backend/core/uds_event_fabric.py` (outbox publisher loop)
- Test: `tests/unit/core/test_outbox_ordering.py`

**Step 1: Write the failing test**

```python
# tests/unit/core/test_outbox_ordering.py
"""Tests for outbox-based event ordering (commit-before-publish)."""
import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.uds_event_fabric import EventFabric


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


class TestOutboxWrite:
    @pytest.mark.asyncio
    async def test_write_to_outbox(self, journal, tmp_path):
        seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                    payload={"event": "pressure_triggered"})
        journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                             payload={"event": "pressure_triggered"})
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT * FROM event_outbox WHERE seq = ?", (seq,)).fetchone()
        conn.close()
        assert row is not None
        assert row[4] == 0  # published = false

    @pytest.mark.asyncio
    async def test_outbox_fk_enforced(self, journal, tmp_path):
        """Cannot write outbox entry for nonexistent journal seq."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO event_outbox (seq, event_type, target, published) VALUES (99999, 'x', 'y', 0)"
            )
        conn.close()


class TestOutboxPublisher:
    @pytest.mark.asyncio
    async def test_unpublished_entries_emitted(self, journal, tmp_path):
        _td = tempfile.mkdtemp(prefix="jt_")
        sock_path = Path(os.path.join(_td, "c.sock"))
        fabric = EventFabric(journal, keepalive_interval_s=5.0, keepalive_timeout_s=30.0)
        await fabric.start(sock_path)

        try:
            # Write journal + outbox
            seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                        payload={"event": "vm_ready"})
            journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                                 payload={"event": "vm_ready"})

            # Run one cycle of outbox publisher
            published = await fabric.publish_outbox_once()
            assert published >= 1

            # Verify marked as published
            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute("SELECT published FROM event_outbox WHERE seq = ?", (seq,)).fetchone()
            conn.close()
            assert row[0] == 1
        finally:
            await fabric.stop()

    @pytest.mark.asyncio
    async def test_already_published_not_re_emitted(self, journal, tmp_path):
        _td = tempfile.mkdtemp(prefix="jt_")
        sock_path = Path(os.path.join(_td, "c.sock"))
        fabric = EventFabric(journal, keepalive_interval_s=5.0, keepalive_timeout_s=30.0)
        await fabric.start(sock_path)

        try:
            seq = journal.fenced_write("gcp_lifecycle", "invincible_node",
                                        payload={"event": "vm_ready"})
            journal.write_outbox(seq, "gcp_lifecycle", "invincible_node",
                                 payload={"event": "vm_ready"})

            await fabric.publish_outbox_once()
            count = await fabric.publish_outbox_once()
            assert count == 0  # Nothing new to publish
        finally:
            await fabric.stop()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_outbox_ordering.py -v`
Expected: FAIL — `AttributeError: 'OrchestrationJournal' object has no attribute 'write_outbox'`

**Step 3: Implement**

- `OrchestrationJournal.write_outbox(seq, event_type, target, payload)` — INSERT into `event_outbox`
- `OrchestrationJournal.get_unpublished_outbox() -> list` — SELECT where `published=0` ORDER BY `seq`
- `OrchestrationJournal.mark_outbox_published(seq)` — UPDATE `published=1, published_at=time.time()`
- `EventFabric.publish_outbox_once() -> int` — reads unpublished, emits each via `self.emit()`, marks published. Returns count.

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_outbox_ordering.py -v`
Expected: all 4 tests PASS

**Step 5: Commit**

```bash
git add backend/core/orchestration_journal.py backend/core/uds_event_fabric.py tests/unit/core/test_outbox_ordering.py
git commit -m "feat: add outbox publisher for commit-before-publish event ordering"
```

---

### Wave 3 Gate Tests

Run: `python3 -m pytest tests/unit/core/test_budget_reservation.py tests/unit/core/test_outbox_ordering.py -v`

**Gate criteria:**
- Budget race closed: concurrent reservations serialized (7 tests)
- Outbox ordering: journal commit before event publish (4 tests)
- No Phase 2A regressions: `python3 -m pytest tests/unit/core/ tests/adversarial/ -v`

**Rollback:** Revert journal methods + fabric outbox publisher. Schema (Wave 1) and state machine (Wave 2) unaffected.

---

## Wave 4: GCP Side-Effect Adapters + Reconcile/Takeover Safety

**Purpose:** Wire the state machine to real GCP operations via the adapter pattern, and implement leader takeover reconciliation for pending side effects.

---

### Task 4.1: GCP side-effect adapter with op_id tracking

**Files:**
- Create: `backend/core/gcp_lifecycle_adapter.py`
- Test: `tests/unit/core/test_gcp_lifecycle_adapter.py`

**Step 1: Write the failing test**

Tests verify the adapter:
- Generates stable `op_id` from `(target, event, epoch)`
- Passes `op_id` to GCPVMManager calls
- Records result back to journal with `op_id`
- Handles GCP API errors gracefully
- Checks lease before journaling result

**Step 2: Implement**

`GCPLifecycleAdapter(SideEffectAdapter)`:
- `__init__(journal, gcp_vm_manager)` — stores refs
- `async execute(action, op_id, **kwargs)` — dispatches to appropriate GCPVMManager method based on `action` string
- `_generate_op_id(target, event, epoch)` — deterministic: `f"{target}:{event}:{epoch}:{uuid4().hex[:8]}"`
- For `"create_vm"`: calls `gcp_vm_manager.ensure_static_vm_ready()`
- For `"stop_vm"`: calls appropriate stop method
- All calls wrapped in lease-check-after-execute pattern from design doc Section 5

**Step 3-5: Run tests, commit**

---

### Task 4.2: Leader takeover reconciliation

**Files:**
- Modify: `backend/core/gcp_lifecycle_state_machine.py`
- Test: `tests/unit/core/test_reconcile_takeover.py`

**Step 1: Write the failing test**

Tests verify:
- New leader finds `result="pending"` entries and reconciles against mock GCP state
- Pending entry with running VM → `committed` (adopt)
- Pending entry with no VM → `abandoned`
- Pending entry with stopped VM → `committed_but_stopped`
- Pending budget reservation without commit → `released`

**Step 2: Implement**

Add `reconcile_on_takeover()` to state machine:
- Replays journal for `gcp_lifecycle` entries with `result="pending"`
- For each, extracts `op_id`, queries adapter for actual state
- Journals reconciliation result
- Recovers state machine position from last committed transition

**Step 3-5: Run tests, commit**

---

### Task 4.3: Invincible node as first-class component

**Files:**
- Modify: `backend/core/orchestration_journal.py` (component_state registration)
- Modify: `backend/core/gcp_lifecycle_state_machine.py` (register on init)
- Test: `tests/unit/core/test_invincible_node_component.py`

**Step 1: Write the failing test**

Tests verify:
- Invincible node registered in `component_state` with `locality="gcp_persistent"`
- State machine transitions update `component_state` status
- `start_timestamp` set when entering BOOTING
- `consecutive_failures` tracked on health probes
- Recovery protocol can probe and reconcile the invincible node component

**Step 2-5: Implement, test, commit**

---

### Wave 4 Gate Tests

Run: `python3 -m pytest tests/unit/core/test_gcp_lifecycle_adapter.py tests/unit/core/test_reconcile_takeover.py tests/unit/core/test_invincible_node_component.py -v`

**Gate criteria:**
- Adapter generates stable op_ids and records results (5+ tests)
- Takeover reconciliation handles all 4 pending states (5+ tests)
- Invincible node fully tracked in component_state (5+ tests)
- No Phase 2A regressions: `python3 -m pytest tests/unit/core/ tests/adversarial/ -v`

**Rollback:** Remove adapter + reconcile methods. State machine (Wave 2) still functions in simulation mode.

---

## Wave 5: Fault-Injection Suite + Rollout Toggles + Observability

**Purpose:** Adversarial tests that exercise the full pressure → budget → provision → route → crash → recover path. Feature flag for gradual rollout. Observability for debugging.

---

### Task 5.1: Fault-injection integration tests (HARD GATE)

**Files:**
- Create: `tests/adversarial/test_gcp_lifecycle_fault_injection.py`

Implement all 15 tests from design doc Section 6 test matrix. Each test uses the fault injector from Phase 2A (`tests/adversarial/fault_injector.py`) to simulate crashes, timeouts, and race conditions.

Key tests:
1. `test_pressure_to_provision_full_path` — happy path with journal verification
2. `test_budget_race_two_concurrent_requests` — concurrent reserve, only one wins
3. `test_lease_loss_during_vm_creation` — orphaned side effect reconciled by new leader
4. `test_preemption_detection_and_recovery` — ACTIVE → TRIGGERING → re-provision
5. `test_outbox_event_ordering` — crash between commit and publish, replay on restart
6. `test_invincible_node_crash_recovery` — new leader adopts running invincible node
7. `test_probe_hysteresis_transient_failure` — single timeout doesn't flap
8. `test_startup_ambiguity_never_launched` — null start_timestamp → start, not fail
9. `test_budget_reservation_crash_recovery` — orphaned reservation released
10. `test_session_shutdown_stops_active_vm` — graceful stop path
11. `test_cooldown_prevents_flapping` — re-trigger during cooldown handled
12. `test_stale_signal_file_ignored` — old epoch file rejected
13. `test_full_lifecycle_idle_to_active_to_idle` — round-trip with journal replay
14. `test_cost_tracker_reconciles_with_journal` — cost totals match
15. `test_event_fabric_never_outruns_journal` — subscriber finds entry for every event

---

### Task 5.2: Feature flag for gradual rollout

**Files:**
- Modify: `backend/core/gcp_lifecycle_state_machine.py`

Add env var `JARVIS_GCP_LIFECYCLE_V2=true|false` (default: `false`):
- When `false`: existing behavior unchanged (six brains)
- When `true`: state machine intercepts all GCP lifecycle decisions
- Logged at startup: `"GCP Lifecycle V2: ENABLED"` or `"GCP Lifecycle V2: DISABLED (legacy path)"`

---

### Task 5.3: Observability — state machine metrics

**Files:**
- Modify: `backend/core/gcp_lifecycle_state_machine.py`

Add structured logging for every transition:
```python
logger.info(
    "gcp_lifecycle_transition",
    extra={
        "from_state": from_state.value,
        "to_state": to_state.value,
        "event": event.value,
        "op_id": op_id,
        "seq": seq,
        "duration_ms": duration_ms,
    },
)
```

---

### Wave 5 Gate Tests (FINAL GATE)

Run: `python3 -m pytest tests/adversarial/test_gcp_lifecycle_fault_injection.py -v`

**Gate criteria:**
- All 15 fault-injection tests PASS
- Full regression: `python3 -m pytest tests/unit/core/ tests/adversarial/ -v` — 0 failures
- Feature flag toggle works in both modes

**Rollback:** Feature flag defaults to `false`. Existing behavior unchanged until flag enabled.

---

## Summary

| Wave | Tasks | New Tests | Files Created | Files Modified |
|------|-------|-----------|---------------|----------------|
| 1: Schema + Migration | 3 | ~42 | 3 new | 1 modified |
| 2: State Machine Core | 2 | ~23 | 2 new | 1 modified |
| 3: Budget + Outbox | 2 | ~11 | 2 new | 2 modified |
| 4: Adapters + Reconcile | 3 | ~15 | 2 new | 2 modified |
| 5: Fault Injection + Rollout | 3 | ~15 | 1 new | 1 modified |
| **Total** | **13** | **~106** | **10 new** | **7 modified** |

Gate tests between waves enforce no-advance-without-passing. Feature flag ensures zero-risk rollout.

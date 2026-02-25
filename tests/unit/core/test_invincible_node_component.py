# tests/unit/core/test_invincible_node_component.py
"""Tests for invincible node registration in the journal's component_state table.

Task 4.3: The GCP lifecycle state machine should maintain a first-class
component_state entry for the invincible node, updating it on every
transition and after recovery.
"""
import os
import time
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_state_machine import (
    GCPLifecycleStateMachine,
    SideEffectAdapter,
)


class NoOpAdapter(SideEffectAdapter):
    """Test adapter that records calls but does nothing."""

    def __init__(self):
        self.calls = []

    async def execute(self, action: str, op_id: str, **kwargs):
        self.calls.append({"action": action, "op_id": op_id, **kwargs})
        return {"status": "simulated"}

    async def query_vm_state(self, op_id: str) -> str:
        return "not_found"


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


async def _drive_to_active(sm):
    """Drive state machine IDLE -> TRIGGERING -> PROVISIONING -> BOOTING -> ACTIVE."""
    await sm.handle_event(Event.PRESSURE_TRIGGERED)
    await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
    await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
    await sm.handle_event(Event.HEALTH_PROBE_OK, payload={"ip": "10.0.0.1"})
    assert sm.state == State.ACTIVE


class TestInvincibleNodeRegisteredOnInit:
    @pytest.mark.asyncio
    async def test_invincible_node_registered_on_init(self, sm, journal):
        """After constructing SM, get_component_state(target) returns a dict
        with status='idle' and instance_id=target."""
        state = journal.get_component_state("invincible_node")
        assert state is not None, "Component should be registered on init"
        assert state["status"] == "idle"
        assert state["instance_id"] == "invincible_node"


class TestComponentStateUpdatedOnTransition:
    @pytest.mark.asyncio
    async def test_component_state_updated_on_transition(self, sm, journal):
        """After PRESSURE_TRIGGERED, component status is 'triggering'."""
        result = await sm.handle_event(Event.PRESSURE_TRIGGERED)
        assert result.success

        state = journal.get_component_state("invincible_node")
        assert state is not None
        assert state["status"] == "triggering"


class TestStartTimestampSetOnBooting:
    @pytest.mark.asyncio
    async def test_start_timestamp_set_on_booting(self, sm, journal):
        """Drive SM to BOOTING state, verify start_timestamp is set (not None, > 0)."""
        before = time.time()
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        assert sm.state == State.BOOTING
        after = time.time()

        state = journal.get_component_state("invincible_node")
        assert state is not None
        assert state["status"] == "booting"
        assert state["start_timestamp"] is not None
        assert state["start_timestamp"] > 0
        assert before <= state["start_timestamp"] <= after


class TestConsecutiveFailuresResetOnHealthOk:
    @pytest.mark.asyncio
    async def test_consecutive_failures_reset_on_health_ok(self, sm, journal):
        """Drive to ACTIVE state via HEALTH_PROBE_OK, verify consecutive_failures=0
        and last_probe_category='healthy'."""
        await _drive_to_active(sm)

        state = journal.get_component_state("invincible_node")
        assert state is not None
        assert state["status"] == "active"
        assert state["consecutive_failures"] == 0
        assert state["last_probe_category"] == "healthy"


class TestRecoveryUpdatesComponentState:
    @pytest.mark.asyncio
    async def test_recovery_updates_component_state(self, sm, journal, adapter):
        """After crash + recovery, component_state reflects recovered state."""
        # Drive to BOOTING
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "op_1"})
        await sm.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-123"})
        assert sm.state == State.BOOTING

        # Simulate crash: new SM starts at IDLE
        adapter2 = NoOpAdapter()
        sm2 = GCPLifecycleStateMachine(journal, adapter2, target="invincible_node")

        # Component state from sm2 init would overwrite to "idle",
        # but recovery should restore to "booting"
        await sm2.recover_from_journal()

        assert sm2.state == State.BOOTING

        state = journal.get_component_state("invincible_node")
        assert state is not None
        assert state["status"] == "booting"

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
        """IDLE -> TRIGGERING -> PROVISIONING -> BOOTING -> ACTIVE -> COOLING_DOWN -> STOPPING -> IDLE"""
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

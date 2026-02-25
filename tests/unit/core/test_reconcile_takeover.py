# tests/unit/core/test_reconcile_takeover.py
"""Tests for leader takeover reconciliation in GCP lifecycle state machine.

When a leader crashes and a new leader takes over, pending journal entries
(result="pending") from side-effect transitions must be reconciled by
querying actual GCP VM state and marking entries accordingly.
"""
import os
import pytest
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_state_machine import (
    GCPLifecycleStateMachine,
    SideEffectAdapter,
)


class MockReconcileAdapter(SideEffectAdapter):
    """Test adapter with configurable query_vm_state responses.

    Allows tests to control what the adapter reports as the actual
    VM state when reconciliation queries GCP.
    """

    def __init__(self, vm_state: str = "not_found"):
        self.calls = []
        self._vm_state = vm_state
        self.query_calls = []

    async def execute(self, action: str, op_id: str, **kwargs):
        self.calls.append({"action": action, "op_id": op_id, **kwargs})
        return {"status": "simulated"}

    async def query_vm_state(self, op_id: str) -> str:
        self.query_calls.append(op_id)
        return self._vm_state


@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


def _make_sm(journal, adapter, target="invincible_node"):
    """Helper to create a state machine for reconciliation tests."""
    return GCPLifecycleStateMachine(journal, adapter, target=target)


async def _drive_to_pending_side_effect(sm, journal):
    """Drive the state machine to produce a pending journal entry with has_side_effect=True.

    Goes IDLE -> TRIGGERING -> PROVISIONING (budget_approved has side effect).
    Returns the seq of the side-effect entry.
    """
    await sm.handle_event(Event.PRESSURE_TRIGGERED)
    result = await sm.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "test_op"})
    assert result.success
    assert sm.state == State.PROVISIONING
    return result.seq


class TestPendingWithRunningVM:
    @pytest.mark.asyncio
    async def test_pending_with_running_vm_committed(self, journal, tmp_path):
        """Pending lifecycle entry + running VM -> entry marked 'committed'."""
        # Phase 1: Drive state machine to create a pending side-effect entry
        adapter1 = MockReconcileAdapter(vm_state="running")
        sm1 = _make_sm(journal, adapter1)
        seq = await _drive_to_pending_side_effect(sm1, journal)

        # Verify the entry is pending (fenced_write defaults to "pending")
        entries = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        side_effect_entries = [
            e for e in entries
            if e.get("payload", {}).get("has_side_effect") is True
        ]
        assert len(side_effect_entries) >= 1
        target_entry = side_effect_entries[-1]
        assert target_entry["result"] == "pending"

        # Phase 2: Simulate leader takeover with new adapter reporting VM running
        adapter2 = MockReconcileAdapter(vm_state="running")
        sm2 = _make_sm(journal, adapter2)
        await sm2.reconcile_on_takeover()

        # Verify the pending entry was marked committed
        entries_after = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        reconciled = [e for e in entries_after if e["seq"] == target_entry["seq"]]
        assert len(reconciled) == 1
        assert reconciled[0]["result"] == "committed"

        # Verify adapter was asked to query VM state
        assert len(adapter2.query_calls) >= 1


class TestPendingWithNoVM:
    @pytest.mark.asyncio
    async def test_pending_with_no_vm_failed(self, journal, tmp_path):
        """Pending lifecycle entry + no VM -> entry marked 'failed'."""
        adapter1 = MockReconcileAdapter(vm_state="not_found")
        sm1 = _make_sm(journal, adapter1)
        seq = await _drive_to_pending_side_effect(sm1, journal)

        entries = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        side_effect_entries = [
            e for e in entries
            if e.get("payload", {}).get("has_side_effect") is True
        ]
        target_entry = side_effect_entries[-1]
        assert target_entry["result"] == "pending"

        # New leader with VM not found
        adapter2 = MockReconcileAdapter(vm_state="not_found")
        sm2 = _make_sm(journal, adapter2)
        await sm2.reconcile_on_takeover()

        entries_after = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        reconciled = [e for e in entries_after if e["seq"] == target_entry["seq"]]
        assert len(reconciled) == 1
        assert reconciled[0]["result"] == "failed"


class TestPendingWithStoppedVM:
    @pytest.mark.asyncio
    async def test_pending_with_stopped_vm_committed(self, journal, tmp_path):
        """Pending lifecycle entry + stopped VM -> entry marked 'committed'."""
        adapter1 = MockReconcileAdapter(vm_state="stopped")
        sm1 = _make_sm(journal, adapter1)
        seq = await _drive_to_pending_side_effect(sm1, journal)

        entries = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        side_effect_entries = [
            e for e in entries
            if e.get("payload", {}).get("has_side_effect") is True
        ]
        target_entry = side_effect_entries[-1]

        adapter2 = MockReconcileAdapter(vm_state="stopped")
        sm2 = _make_sm(journal, adapter2)
        await sm2.reconcile_on_takeover()

        entries_after = await journal.replay_from(0, action_filter=["gcp_lifecycle"])
        reconciled = [e for e in entries_after if e["seq"] == target_entry["seq"]]
        assert len(reconciled) == 1
        assert reconciled[0]["result"] == "committed"


class TestPendingBudgetRelease:
    @pytest.mark.asyncio
    async def test_pending_budget_released(self, journal, tmp_path):
        """Pending budget reservation with no matching commit -> released."""
        adapter1 = MockReconcileAdapter(vm_state="not_found")
        sm1 = _make_sm(journal, adapter1)

        # Create a lifecycle transition to get a valid op_id
        await sm1.handle_event(Event.PRESSURE_TRIGGERED)
        result = await sm1.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "test_op"})
        assert result.success

        # The op_id format from handle_event is: "{target}:{event}:{epoch}:{seq}"
        lifecycle_entries = await journal.replay_from(
            0, action_filter=["gcp_lifecycle"]
        )
        budget_event_entry = [
            e for e in lifecycle_entries
            if e.get("payload", {}).get("event") == "budget_approved"
        ][-1]
        entry_seq = budget_event_entry["seq"]
        entry_epoch = budget_event_entry["epoch"]
        op_id = f"invincible_node:budget_approved:{entry_epoch}:{entry_seq}"

        # Create a pending budget reservation for that op_id
        budget_seq = journal.reserve_budget(
            estimated_cost=0.50,
            op_id=op_id,
            daily_budget=10.0,
        )
        assert budget_seq > 0

        # Verify the budget entry exists and is pending
        all_entries = await journal.replay_from(0, action_filter=["budget_reserved"])
        budget_entries = [
            e for e in all_entries
            if e.get("payload", {}).get("op_id") == op_id
        ]
        assert len(budget_entries) == 1
        assert budget_entries[0]["result"] == "pending"

        # Phase 2: Reconcile on takeover - the budget should be released
        # since VM is not_found (the lifecycle entry will be marked failed)
        adapter2 = MockReconcileAdapter(vm_state="not_found")
        sm2 = _make_sm(journal, adapter2)
        await sm2.reconcile_on_takeover()

        # Verify budget was released (budget_released entry exists for this op_id)
        all_after = await journal.replay_from(0, action_filter=["budget_released"])
        release_entries = [
            e for e in all_after
            if e.get("payload", {}).get("op_id") == op_id
        ]
        assert len(release_entries) >= 1, (
            "Expected budget_released entry for orphaned pending budget reservation"
        )


class TestStateRecoveredAfterReconciliation:
    @pytest.mark.asyncio
    async def test_state_recovered_after_reconciliation(self, journal, tmp_path):
        """State machine state matches last committed transition after reconcile."""
        adapter1 = MockReconcileAdapter(vm_state="running")
        sm1 = _make_sm(journal, adapter1)

        # Drive through: IDLE -> TRIGGERING -> PROVISIONING -> BOOTING
        await sm1.handle_event(Event.PRESSURE_TRIGGERED)
        await sm1.handle_event(Event.BUDGET_APPROVED, payload={"op_id": "test_op"})
        await sm1.handle_event(Event.VM_CREATE_ACCEPTED, payload={"instance_ref": "vm-x"})
        assert sm1.state == State.BOOTING

        # Simulate crash: create new state machine (starts at IDLE)
        adapter2 = MockReconcileAdapter(vm_state="running")
        sm2 = _make_sm(journal, adapter2)
        assert sm2.state == State.IDLE  # Pre-reconcile: default state

        # Reconcile recovers state from journal
        await sm2.reconcile_on_takeover()

        # After reconciliation, state should be BOOTING (last committed transition)
        assert sm2.state == State.BOOTING, (
            f"Expected state BOOTING after reconciliation, got {sm2.state}"
        )

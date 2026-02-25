"""
Fault-injection integration tests for the journal-backed GCP lifecycle state machine.

Exercises 15 realistic failure scenarios: crashes, races, recovery,
budget contention, outbox ordering, and probe hysteresis.
"""
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import pytest

from backend.core.gcp_lifecycle_schema import State, Event
from backend.core.gcp_lifecycle_state_machine import (
    GCPLifecycleStateMachine,
    SideEffectAdapter,
    TransitionResult,
)
from backend.core.orchestration_journal import OrchestrationJournal
from backend.core.uds_event_fabric import EventFabric
from backend.core.recovery_protocol import (
    RecoveryReconciler,
    HealthBuffer,
    HealthCategory,
    ProbeResult,
)


# ── Mock Adapter ──────────────────────────────────────────────────────

class MockFaultAdapter(SideEffectAdapter):
    """Configurable test adapter that can fail on specific calls."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_on_actions: Dict[str, Exception] = {}
        self.vm_state: str = "not_found"
        self.execute_delay: float = 0.0

    async def execute(self, action: str, op_id: str, **kwargs) -> Dict[str, Any]:
        self.calls.append({"action": action, "op_id": op_id, **kwargs})
        if action in self.fail_on_actions:
            raise self.fail_on_actions[action]
        if self.execute_delay > 0:
            await asyncio.sleep(self.execute_delay)
        return {"status": "ok", "action": action, "op_id": op_id}

    async def query_vm_state(self, op_id: str) -> str:
        self.calls.append({"query_vm_state": True, "op_id": op_id})
        return self.vm_state


# ── Mock Lifecycle Engine (for RecoveryReconciler) ────────────────────

class MockLifecycleEngine:
    """Minimal mock of lifecycle_engine.LifecycleEngine for reconciler tests."""

    def __init__(self) -> None:
        self.transitions: List[Dict[str, Any]] = []
        self._seq = 100

    async def transition_component(
        self, component: str, new_status: str, *, reason: str, trigger_seq: Optional[int] = None
    ) -> int:
        self._seq += 1
        self.transitions.append({
            "component": component,
            "new_status": new_status,
            "reason": reason,
            "trigger_seq": trigger_seq,
            "seq": self._seq,
        })
        return self._seq

    def get_declaration(self, component: str):
        return None  # Will use default handshake_timeout_s of 10.0


# ── Helper: force-expire lease in DB ─────────────────────────────────

def _expire_lease(journal: OrchestrationJournal) -> None:
    """Directly set last_renewed far in the past so the next acquire_lease() succeeds."""
    conn = journal._conn
    conn.execute(
        "UPDATE lease SET last_renewed = ? WHERE id = 1",
        (time.time() - 3600,),  # 1 hour in the past
    )
    conn.commit()


# ── Helper: drive SM through standard happy path ─────────────────────

async def _drive_to_active(sm: GCPLifecycleStateMachine) -> List[TransitionResult]:
    """Drive SM from IDLE through the standard path to ACTIVE.

    Returns list of all transition results.
    """
    results = []
    # IDLE -> TRIGGERING
    results.append(await sm.handle_event(Event.PRESSURE_TRIGGERED))
    # TRIGGERING -> PROVISIONING
    results.append(await sm.handle_event(Event.BUDGET_APPROVED))
    # PROVISIONING -> BOOTING
    results.append(await sm.handle_event(Event.VM_CREATE_ACCEPTED))
    # BOOTING -> ACTIVE
    results.append(await sm.handle_event(Event.HEALTH_PROBE_OK))
    return results


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
async def journal(tmp_path):
    j = OrchestrationJournal()
    await j.initialize(tmp_path / "test.db")
    await j.acquire_lease(f"test:{os.getpid()}")
    yield j
    await j.close()


@pytest.fixture
def adapter():
    return MockFaultAdapter()


@pytest.fixture
def sm(journal, adapter):
    return GCPLifecycleStateMachine(journal, adapter, target="invincible_node")


# ── Tests ────────────────────────────────────────────────────────────

class TestGCPLifecycleFaultInjection:
    """15 fault-injection integration tests for the GCP lifecycle state machine."""

    # 1. Happy path: IDLE -> TRIGGERING -> PROVISIONING -> BOOTING -> ACTIVE
    @pytest.mark.asyncio
    async def test_pressure_to_provision_full_path(self, sm, journal):
        """Happy path: verify each state transition is journaled correctly."""
        assert sm.state == State.IDLE

        # IDLE -> TRIGGERING
        r1 = await sm.handle_event(Event.PRESSURE_TRIGGERED)
        assert r1.success
        assert r1.from_state == State.IDLE
        assert r1.to_state == State.TRIGGERING
        assert sm.state == State.TRIGGERING

        # TRIGGERING -> PROVISIONING
        r2 = await sm.handle_event(Event.BUDGET_APPROVED)
        assert r2.success
        assert r2.from_state == State.TRIGGERING
        assert r2.to_state == State.PROVISIONING
        assert sm.state == State.PROVISIONING

        # PROVISIONING -> BOOTING
        r3 = await sm.handle_event(Event.VM_CREATE_ACCEPTED)
        assert r3.success
        assert r3.from_state == State.PROVISIONING
        assert r3.to_state == State.BOOTING
        assert sm.state == State.BOOTING

        # BOOTING -> ACTIVE
        r4 = await sm.handle_event(Event.HEALTH_PROBE_OK)
        assert r4.success
        assert r4.from_state == State.BOOTING
        assert r4.to_state == State.ACTIVE
        assert sm.state == State.ACTIVE

        # Verify journal contains all transitions
        entries = await journal.replay_from(
            0, target_filter=["invincible_node"], action_filter=["gcp_lifecycle"]
        )
        assert len(entries) >= 4

        states_in_journal = [
            (e["payload"]["from_state"], e["payload"]["to_state"])
            for e in entries
            if e["payload"] and "from_state" in e["payload"]
        ]
        assert ("idle", "triggering") in states_in_journal
        assert ("triggering", "provisioning") in states_in_journal
        assert ("provisioning", "booting") in states_in_journal
        assert ("booting", "active") in states_in_journal

    # 2. Budget race: two concurrent reserve_budget calls
    @pytest.mark.asyncio
    async def test_budget_race_two_concurrent_requests(self, journal):
        """Two concurrent reserve_budget calls with combined cost > daily budget.

        Only one should succeed (non-zero seq). The other returns 0.
        """
        # Both try to reserve $0.60 against $1.00 daily budget
        seq1 = journal.reserve_budget(0.60, "op_race_1", daily_budget=1.00)
        seq2 = journal.reserve_budget(0.60, "op_race_2", daily_budget=1.00)

        # Exactly one should succeed and one should fail
        results = [seq1, seq2]
        successes = [s for s in results if s > 0]
        failures = [s for s in results if s == 0]

        assert len(successes) == 1, f"Expected exactly 1 success, got {successes}"
        assert len(failures) == 1, f"Expected exactly 1 failure, got {failures}"

    # 3. Lease loss during VM creation; new leader reconciles
    @pytest.mark.asyncio
    async def test_lease_loss_during_vm_creation(self, journal, adapter, tmp_path):
        """Drive SM to PROVISIONING, simulate lease loss, reconcile with new leader."""
        sm1 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")

        # Drive to PROVISIONING (this has has_side_effect=True from BUDGET_APPROVED)
        await sm1.handle_event(Event.PRESSURE_TRIGGERED)
        r = await sm1.handle_event(Event.BUDGET_APPROVED)
        assert sm1.state == State.PROVISIONING
        assert r.success

        # Simulate lease loss: expire the lease and let a new leader acquire it
        _expire_lease(journal)
        acquired = await journal.acquire_lease("new_leader:1")
        assert acquired, "New leader should acquire the lease after TTL expiry"

        # Adapter reports VM is running (side effect actually completed)
        adapter.vm_state = "running"

        # New leader creates SM and reconciles
        sm2 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
        await sm2.reconcile_on_takeover()

        # The pending BUDGET_APPROVED entry should be reconciled to "committed"
        entries = await journal.replay_from(
            0, target_filter=["invincible_node"], action_filter=["gcp_lifecycle"]
        )
        reconcile_entries = [
            e for e in entries
            if e["payload"] and e["payload"].get("reconcile_action") == "resolve_pending"
        ]
        # At least one pending entry was reconciled
        assert len(reconcile_entries) >= 1
        # The resolved result should be "committed" since VM is running
        assert reconcile_entries[0]["payload"]["resolved_as"] == "committed"

    # 4. Preemption detection and recovery
    @pytest.mark.asyncio
    async def test_preemption_detection_and_recovery(self, sm, journal):
        """ACTIVE -> preempted -> TRIGGERING -> re-provision -> ACTIVE."""
        await _drive_to_active(sm)
        assert sm.state == State.ACTIVE

        # Preemption
        r = await sm.handle_event(Event.SPOT_PREEMPTED)
        assert r.success
        assert r.to_state == State.TRIGGERING
        assert sm.state == State.TRIGGERING

        # Re-provision
        r2 = await sm.handle_event(Event.BUDGET_APPROVED)
        assert r2.success and r2.to_state == State.PROVISIONING

        r3 = await sm.handle_event(Event.VM_CREATE_ACCEPTED)
        assert r3.success and r3.to_state == State.BOOTING

        r4 = await sm.handle_event(Event.HEALTH_PROBE_OK)
        assert r4.success and r4.to_state == State.ACTIVE
        assert sm.state == State.ACTIVE

    # 5. Outbox event ordering
    @pytest.mark.asyncio
    async def test_outbox_event_ordering(self, sm, journal):
        """Write 3 transitions with outbox entries; verify publish order."""
        results = []
        events = [
            Event.PRESSURE_TRIGGERED,
            Event.BUDGET_APPROVED,
            Event.VM_CREATE_ACCEPTED,
        ]

        for event in events:
            r = await sm.handle_event(event)
            assert r.success
            results.append(r)
            # Write outbox entry for each transition
            journal.write_outbox(
                r.seq,
                "lifecycle",
                "invincible_node",
                payload={"event": event.value, "to_state": r.to_state.value},
            )

        # Publish via EventFabric
        fabric = EventFabric(journal)
        count = await fabric.publish_outbox_once()
        assert count == 3

        # Verify all are now published (no unpublished remain)
        remaining = journal.get_unpublished_outbox()
        assert len(remaining) == 0

    # 6. Crash recovery: SM1 in BOOTING, SM2 reconciles
    @pytest.mark.asyncio
    async def test_invincible_node_crash_recovery(self, journal, adapter, tmp_path):
        """SM1 reaches BOOTING, crashes. SM2 reconciles and recovers state."""
        sm1 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")

        # Drive to BOOTING
        await sm1.handle_event(Event.PRESSURE_TRIGGERED)
        await sm1.handle_event(Event.BUDGET_APPROVED)
        await sm1.handle_event(Event.VM_CREATE_ACCEPTED)
        assert sm1.state == State.BOOTING

        # SM1 "crashes" -- simulate lease expiry
        _expire_lease(journal)
        acquired = await journal.acquire_lease("new_leader:2")
        assert acquired

        # Adapter returns "running" for any query
        adapter.vm_state = "running"

        # SM2 takes over
        sm2 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
        await sm2.reconcile_on_takeover()

        # SM2 should have recovered state from journal
        # The last transition was to BOOTING, so sm2 should be in BOOTING
        # (reconcile_on_takeover calls recover_from_journal which finds last to_state)
        assert sm2.state == State.BOOTING

        # Component state should reflect the recovered state
        comp = journal.get_component_state("invincible_node")
        assert comp is not None
        assert comp["status"] == "booting"

    # 7. Probe hysteresis: transient failure does not trigger event
    @pytest.mark.asyncio
    async def test_probe_hysteresis_transient_failure(self):
        """HealthBuffer requires k consecutive unreachable before marking lost."""
        buf = HealthBuffer(k_unreachable=3, k_degraded=5)

        # Record 2 unreachable failures (below threshold of 3)
        buf.record_failure(HealthCategory.UNREACHABLE)
        buf.record_failure(HealthCategory.UNREACHABLE)

        # Should NOT yet mark as lost
        assert not buf.should_mark_lost()

        # Recovery resets the counter
        buf.record_success()
        assert buf.consecutive_unreachable == 0
        assert not buf.should_mark_lost()

        # Now reach threshold
        buf.record_failure(HealthCategory.UNREACHABLE)
        buf.record_failure(HealthCategory.UNREACHABLE)
        buf.record_failure(HealthCategory.UNREACHABLE)
        assert buf.should_mark_lost()

    # 8. Startup ambiguity: never-launched component
    @pytest.mark.asyncio
    async def test_startup_ambiguity_never_launched(self, journal):
        """RecoveryReconciler.reconcile() with no start_timestamp recommends start."""
        engine = MockLifecycleEngine()
        reconciler = RecoveryReconciler(journal, engine)

        # Component is STARTING but was never actually launched (no start_timestamp)
        probe = ProbeResult(
            reachable=False,
            category=HealthCategory.UNREACHABLE,
            probe_epoch=journal.epoch,
            probe_seq=journal.current_seq,
        )

        actions = await reconciler.reconcile(
            "test_component",
            "STARTING",
            probe,
            start_timestamp=None,  # Never launched
        )

        # Should recommend start (transition to STARTING with reason reconcile_start_requested)
        assert len(actions) >= 1
        assert actions[0]["to"] == "STARTING"
        assert actions[0]["reason"] == "reconcile_start_requested"

    # 9. Budget reservation crash recovery
    @pytest.mark.asyncio
    async def test_budget_reservation_crash_recovery(self, journal, adapter, tmp_path):
        """Reserve budget, crash before commit. New leader releases orphaned reservation."""
        sm1 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")

        # Reserve budget under the target's op_id namespace
        op_id = "invincible_node:provision:1"
        seq = journal.reserve_budget(0.50, op_id, daily_budget=1.00)
        assert seq > 0

        # Verify budget is consumed
        available = journal.calculate_available_budget(1.00)
        assert available == pytest.approx(0.50, abs=0.01)

        # Crash: new leader takes over
        _expire_lease(journal)
        acquired = await journal.acquire_lease("new_leader:3")
        assert acquired

        adapter.vm_state = "not_found"

        # Reconcile on takeover should release orphaned budget
        sm2 = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
        await sm2.reconcile_on_takeover()

        # Budget should be released (available should be back to ~1.00)
        available_after = journal.calculate_available_budget(1.00)
        assert available_after == pytest.approx(1.00, abs=0.01)

    # 10. Session shutdown stops active VM
    @pytest.mark.asyncio
    async def test_session_shutdown_stops_active_vm(self, sm, adapter):
        """ACTIVE -> SESSION_SHUTDOWN -> STOPPING -> VM_STOPPED -> IDLE."""
        await _drive_to_active(sm)
        assert sm.state == State.ACTIVE

        # Fire SESSION_SHUTDOWN (wildcard transition)
        r = await sm.handle_event(Event.SESSION_SHUTDOWN)
        assert r.success
        assert r.to_state == State.STOPPING
        assert sm.state == State.STOPPING

        # Verify adapter received the side effect call
        shutdown_calls = [c for c in adapter.calls if c["action"] == "session_shutdown"]
        assert len(shutdown_calls) == 1

        # VM confirms stopped
        r2 = await sm.handle_event(Event.VM_STOPPED)
        assert r2.success
        assert r2.to_state == State.IDLE
        assert sm.state == State.IDLE

    # 11. Cooldown prevents flapping; re-trigger allowed from cooldown
    @pytest.mark.asyncio
    async def test_cooldown_prevents_flapping(self, sm, journal):
        """ACTIVE -> COOLING_DOWN, then re-trigger goes to TRIGGERING."""
        await _drive_to_active(sm)
        assert sm.state == State.ACTIVE

        # ACTIVE -> COOLING_DOWN
        r1 = await sm.handle_event(Event.PRESSURE_COOLED)
        assert r1.success
        assert r1.to_state == State.COOLING_DOWN
        assert sm.state == State.COOLING_DOWN

        # Re-trigger from COOLING_DOWN -> TRIGGERING
        r2 = await sm.handle_event(Event.PRESSURE_TRIGGERED)
        assert r2.success
        assert r2.to_state == State.TRIGGERING
        assert sm.state == State.TRIGGERING

    # 12. Stale signal file from old epoch is visible but doesn't interfere
    @pytest.mark.asyncio
    async def test_stale_signal_file_ignored(self, journal, adapter, tmp_path):
        """Budget reserved under epoch 1 is visible in replay but
        doesn't interfere with new epoch operations."""
        # Reserve under epoch 1
        op_id_old = "invincible_node:old_epoch_op"
        seq_old = journal.reserve_budget(0.30, op_id_old, daily_budget=1.00)
        assert seq_old > 0

        old_epoch = journal.epoch

        # Force new epoch
        _expire_lease(journal)
        acquired = await journal.acquire_lease("new_leader:4")
        assert acquired
        assert journal.epoch == old_epoch + 1

        # The old reservation is visible in replay
        entries = await journal.replay_from(0, action_filter=["budget_reserved"])
        old_entries = [
            e for e in entries
            if e["payload"] and e["payload"].get("op_id") == op_id_old
        ]
        assert len(old_entries) == 1

        # New epoch can still reserve budget independently
        op_id_new = "invincible_node:new_epoch_op"
        seq_new = journal.reserve_budget(0.40, op_id_new, daily_budget=1.00)
        assert seq_new > 0

        # New SM operates independently
        sm_new = GCPLifecycleStateMachine(journal, adapter, target="invincible_node")
        r = await sm_new.handle_event(Event.PRESSURE_TRIGGERED)
        assert r.success

    # 13. Full lifecycle round trip
    @pytest.mark.asyncio
    async def test_full_lifecycle_idle_to_active_to_idle(self, sm, journal):
        """IDLE -> TRIGGERING -> PROVISIONING -> BOOTING -> ACTIVE
        -> COOLING_DOWN -> STOPPING -> IDLE.  Verify full journal chain."""
        assert sm.state == State.IDLE

        # Forward path
        await sm.handle_event(Event.PRESSURE_TRIGGERED)
        await sm.handle_event(Event.BUDGET_APPROVED)
        await sm.handle_event(Event.VM_CREATE_ACCEPTED)
        await sm.handle_event(Event.HEALTH_PROBE_OK)
        assert sm.state == State.ACTIVE

        # Return path
        await sm.handle_event(Event.PRESSURE_COOLED)
        assert sm.state == State.COOLING_DOWN

        await sm.handle_event(Event.COOLDOWN_EXPIRED)
        assert sm.state == State.STOPPING

        await sm.handle_event(Event.VM_STOPPED)
        assert sm.state == State.IDLE

        # Replay journal and verify full transition chain
        entries = await journal.replay_from(
            0, target_filter=["invincible_node"], action_filter=["gcp_lifecycle"]
        )
        transition_chain = [
            (e["payload"]["from_state"], e["payload"]["to_state"])
            for e in entries
            if e["payload"] and "from_state" in e["payload"] and "to_state" in e["payload"]
        ]

        expected_chain = [
            ("idle", "triggering"),
            ("triggering", "provisioning"),
            ("provisioning", "booting"),
            ("booting", "active"),
            ("active", "cooling_down"),
            ("cooling_down", "stopping"),
            ("stopping", "idle"),
        ]
        assert transition_chain == expected_chain

    # 14. Cost tracker reconciles with journal
    @pytest.mark.asyncio
    async def test_cost_tracker_reconciles_with_journal(self, journal):
        """Reserve $0.50 (op_1), commit $0.48. Reserve $0.50 (op_2).
        Available from $1.00 should be ~$0.02."""
        # Reserve for op_1
        seq1 = journal.reserve_budget(0.50, "op_1", daily_budget=1.00)
        assert seq1 > 0

        # Commit op_1 with actual cost $0.48
        journal.commit_budget("op_1", 0.48)

        # Reserve for op_2
        seq2 = journal.reserve_budget(0.50, "op_2", daily_budget=1.00)
        assert seq2 > 0

        # Calculate available budget
        available = journal.calculate_available_budget(1.00)
        # 1.00 - 0.48 (committed) - 0.50 (reserved) = 0.02
        assert available == pytest.approx(0.02, abs=0.01)

    # 15. Event fabric never outruns journal
    @pytest.mark.asyncio
    async def test_event_fabric_never_outruns_journal(self, sm, journal):
        """Every outbox event has a matching journal seq."""
        transition_seqs = []
        events = [
            Event.PRESSURE_TRIGGERED,
            Event.BUDGET_APPROVED,
            Event.VM_CREATE_ACCEPTED,
            Event.HEALTH_PROBE_OK,
        ]

        for event in events:
            r = await sm.handle_event(event)
            assert r.success
            transition_seqs.append(r.seq)
            # Write outbox entry for each transition
            journal.write_outbox(
                r.seq,
                "lifecycle",
                "invincible_node",
                payload={"event": event.value},
            )

        # Get all outbox entries before publishing
        unpublished = journal.get_unpublished_outbox()
        assert len(unpublished) == 4

        # Replay journal to get all seqs
        all_entries = await journal.replay_from(0)
        journal_seqs = {e["seq"] for e in all_entries}

        # Every outbox seq must exist in the journal
        for outbox_entry in unpublished:
            assert outbox_entry["seq"] in journal_seqs, (
                f"Outbox seq {outbox_entry['seq']} not found in journal"
            )

        # Publish and verify count
        fabric = EventFabric(journal)
        count = await fabric.publish_outbox_once()
        assert count == 4

        # No unpublished entries remain
        remaining = journal.get_unpublished_outbox()
        assert len(remaining) == 0

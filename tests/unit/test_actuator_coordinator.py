"""Tests for MemoryActuatorCoordinator.

Covers:
 1. submit() returns a decision_id string on success
 2. submit() rejects stale envelopes (returns None)
 3. submit() rejects quarantined actions (returns None)
 4. drain_pending() returns actions sorted by priority (ascending)
 5. drain_pending() clears the queue (second drain returns empty)
 6. quarantine_after_repeated_failures via report_failure()
 7. non-quarantined action passes (below failure_budget)
 8. report_success() resets failure counter and clears quarantine
 9. advance_epoch() updates tracked epoch/sequence
10. shadow mode flags actions but does not suppress them
11. get_stats() returns correct values
12. multiple actions from different sources sorted correctly
13. quarantine expiry allows resubmission
14. concurrent submit from multiple threads
15. decision_id format validation
"""
from __future__ import annotations

import threading
import time

import pytest

from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    PressureTier,
)
from backend.core.memory_actuator_coordinator import (
    MemoryActuatorCoordinator,
    PendingAction,
)


# ===================================================================
# Helpers
# ===================================================================

def _make_envelope(
    *,
    epoch: int = 1,
    sequence: int = 1,
    snapshot_id: str = "s1",
    policy_version: str = "v1.0",
    pressure_tier: PressureTier = PressureTier.CRITICAL,
    timestamp: float | None = None,
) -> DecisionEnvelope:
    return DecisionEnvelope(
        snapshot_id=snapshot_id,
        epoch=epoch,
        sequence=sequence,
        policy_version=policy_version,
        pressure_tier=pressure_tier,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


# ===================================================================
# Submit tests
# ===================================================================

class TestCoordinatorSubmit:
    @pytest.fixture
    def coordinator(self):
        return MemoryActuatorCoordinator()

    def test_submit_returns_decision_id(self, coordinator):
        env = _make_envelope()
        decision_id = coordinator.submit(
            action=ActuatorAction.DISPLAY_SHED,
            envelope=env,
            source="display_controller",
        )
        assert decision_id is not None
        assert isinstance(decision_id, str)

    def test_decision_id_format(self, coordinator):
        """Decision IDs must start with 'dec-' prefix."""
        env = _make_envelope()
        decision_id = coordinator.submit(
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="cleanup_manager",
        )
        assert decision_id.startswith("dec-")
        # 'dec-' + 12 hex chars = 16 chars total
        assert len(decision_id) == 16

    def test_unique_decision_ids(self, coordinator):
        """Each submit must generate a unique decision_id."""
        env = _make_envelope()
        ids = set()
        for _ in range(100):
            did = coordinator.submit(
                action=ActuatorAction.CLEANUP,
                envelope=env,
                source="test",
            )
            assert did not in ids, f"Duplicate decision_id: {did}"
            ids.add(did)

    def test_stale_envelope_rejected(self, coordinator):
        coordinator.advance_epoch(epoch=2, sequence=10)
        env = _make_envelope(epoch=1, sequence=5)
        decision_id = coordinator.submit(
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="cleanup_manager",
        )
        assert decision_id is None  # Rejected

    def test_stale_same_epoch_old_sequence_rejected(self, coordinator):
        """Same epoch but older sequence should be rejected."""
        coordinator.advance_epoch(epoch=3, sequence=7)
        env = _make_envelope(epoch=3, sequence=5)
        decision_id = coordinator.submit(
            action=ActuatorAction.MODEL_EVICT,
            envelope=env,
            source="model_manager",
        )
        assert decision_id is None

    def test_current_epoch_and_sequence_accepted(self, coordinator):
        """Envelope matching current epoch/sequence should be accepted."""
        coordinator.advance_epoch(epoch=5, sequence=10)
        env = _make_envelope(epoch=5, sequence=10)
        decision_id = coordinator.submit(
            action=ActuatorAction.DISPLAY_SHED,
            envelope=env,
            source="display",
        )
        assert decision_id is not None

    def test_future_epoch_accepted(self, coordinator):
        """Envelope ahead of current epoch should be accepted."""
        coordinator.advance_epoch(epoch=3, sequence=1)
        env = _make_envelope(epoch=5, sequence=1)
        decision_id = coordinator.submit(
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="test",
        )
        assert decision_id is not None

    def test_quarantined_action_rejected_on_submit(self, coordinator):
        """A quarantined action should be rejected when submitted."""
        # Force quarantine
        for _ in range(3):
            coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

        env = _make_envelope()
        decision_id = coordinator.submit(
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="cleanup_manager",
        )
        assert decision_id is None

    def test_quarantined_action_does_not_block_other_actions(self, coordinator):
        """Quarantining one action type should not affect other action types."""
        for _ in range(3):
            coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")

        env = _make_envelope()
        # Different action type should still be accepted
        decision_id = coordinator.submit(
            action=ActuatorAction.DISPLAY_SHED,
            envelope=env,
            source="display",
        )
        assert decision_id is not None


# ===================================================================
# Priority / drain tests
# ===================================================================

class TestCoordinatorPriority:
    def test_least_disruptive_action_wins(self):
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        # Submit aggressive action first
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        # Submit less disruptive action second
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display")

        # Drain should return DISPLAY_SHED first (lower priority number)
        actions = coordinator.drain_pending()
        assert len(actions) == 2
        assert actions[0].action == ActuatorAction.DISPLAY_SHED
        assert actions[1].action == ActuatorAction.CLEANUP

    def test_multiple_actions_from_different_sources_sorted(self):
        """All six action types from different sources should be priority-sorted."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()

        # Submit in reverse priority order (most disruptive first)
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup_mgr")
        coordinator.submit(ActuatorAction.CLOUD_SCALE, env, "cloud_scaler")
        coordinator.submit(ActuatorAction.CLOUD_OFFLOAD, env, "cloud_offloader")
        coordinator.submit(ActuatorAction.MODEL_EVICT, env, "model_mgr")
        coordinator.submit(ActuatorAction.DEFCON_ESCALATE, env, "defcon")
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display")

        actions = coordinator.drain_pending()
        assert len(actions) == 6
        # Should come out in ascending priority order
        expected_order = [
            ActuatorAction.DISPLAY_SHED,
            ActuatorAction.DEFCON_ESCALATE,
            ActuatorAction.MODEL_EVICT,
            ActuatorAction.CLOUD_OFFLOAD,
            ActuatorAction.CLOUD_SCALE,
            ActuatorAction.CLEANUP,
        ]
        actual_order = [a.action for a in actions]
        assert actual_order == expected_order

    def test_drain_clears_queue(self):
        """After drain_pending(), the queue should be empty."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display")

        first_drain = coordinator.drain_pending()
        assert len(first_drain) == 2

        second_drain = coordinator.drain_pending()
        assert len(second_drain) == 0

    def test_drain_empty_queue_returns_empty_list(self):
        """Draining an empty queue returns an empty list."""
        coordinator = MemoryActuatorCoordinator()
        assert coordinator.drain_pending() == []

    def test_pending_action_preserves_source(self):
        """PendingAction should record the source that submitted it."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display_controller")
        actions = coordinator.drain_pending()
        assert actions[0].source == "display_controller"

    def test_pending_action_preserves_envelope(self):
        """PendingAction should carry the original DecisionEnvelope."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope(epoch=7, sequence=42)
        coordinator.submit(ActuatorAction.CLEANUP, env, "test")
        actions = coordinator.drain_pending()
        assert actions[0].envelope.epoch == 7
        assert actions[0].envelope.sequence == 42


# ===================================================================
# Quarantine tests
# ===================================================================

class TestCoordinatorQuarantine:
    def test_quarantine_after_repeated_failures(self):
        coordinator = MemoryActuatorCoordinator(failure_budget=2)
        # Report 2 failures for CLEANUP
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_non_quarantined_action_passes(self):
        coordinator = MemoryActuatorCoordinator(failure_budget=3)
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_quarantine_one_below_budget_not_quarantined(self):
        """failure_budget - 1 failures should not trigger quarantine."""
        coordinator = MemoryActuatorCoordinator(failure_budget=5)
        for _ in range(4):
            coordinator.report_failure(ActuatorAction.MODEL_EVICT, "error")
        assert not coordinator.is_quarantined(ActuatorAction.MODEL_EVICT)

    def test_quarantine_exactly_at_budget(self):
        """Exactly failure_budget failures should trigger quarantine."""
        coordinator = MemoryActuatorCoordinator(failure_budget=5)
        for _ in range(5):
            coordinator.report_failure(ActuatorAction.MODEL_EVICT, "error")
        assert coordinator.is_quarantined(ActuatorAction.MODEL_EVICT)

    def test_quarantine_expiry(self):
        """Quarantine should expire after quarantine_seconds."""
        coordinator = MemoryActuatorCoordinator(
            failure_budget=1,
            quarantine_seconds=0.05,  # 50ms
        )
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

        # Wait for quarantine to expire
        time.sleep(0.1)
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_quarantine_expiry_resets_failure_count(self):
        """After quarantine expires, failure count should be reset."""
        coordinator = MemoryActuatorCoordinator(
            failure_budget=2,
            quarantine_seconds=0.05,
        )
        coordinator.report_failure(ActuatorAction.CLEANUP, "t")
        coordinator.report_failure(ActuatorAction.CLEANUP, "t")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

        # Wait for expiry
        time.sleep(0.1)
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

        # After expiry, one more failure should NOT re-quarantine (count was reset)
        coordinator.report_failure(ActuatorAction.CLEANUP, "t")
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_quarantine_does_not_affect_other_action_types(self):
        """Quarantining one action should not quarantine others."""
        coordinator = MemoryActuatorCoordinator(failure_budget=1)
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)
        assert not coordinator.is_quarantined(ActuatorAction.DISPLAY_SHED)
        assert not coordinator.is_quarantined(ActuatorAction.MODEL_EVICT)


# ===================================================================
# report_success tests
# ===================================================================

class TestCoordinatorReportSuccess:
    def test_report_success_resets_failure_counter(self):
        """report_success should reset the failure count to zero."""
        coordinator = MemoryActuatorCoordinator(failure_budget=3)
        coordinator.report_failure(ActuatorAction.CLEANUP, "err")
        coordinator.report_failure(ActuatorAction.CLEANUP, "err")
        # 2 failures, not yet quarantined
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

        coordinator.report_success(ActuatorAction.CLEANUP)
        # After success, one more failure should not quarantine
        coordinator.report_failure(ActuatorAction.CLEANUP, "err")
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_report_success_clears_quarantine(self):
        """report_success should immediately clear an active quarantine."""
        coordinator = MemoryActuatorCoordinator(failure_budget=1)
        coordinator.report_failure(ActuatorAction.CLEANUP, "err")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

        coordinator.report_success(ActuatorAction.CLEANUP)
        assert not coordinator.is_quarantined(ActuatorAction.CLEANUP)

    def test_report_success_allows_resubmission(self):
        """After report_success clears quarantine, submit should accept the action."""
        coordinator = MemoryActuatorCoordinator(failure_budget=1)
        coordinator.report_failure(ActuatorAction.CLEANUP, "err")
        assert coordinator.is_quarantined(ActuatorAction.CLEANUP)

        coordinator.report_success(ActuatorAction.CLEANUP)
        env = _make_envelope()
        decision_id = coordinator.submit(ActuatorAction.CLEANUP, env, "test")
        assert decision_id is not None


# ===================================================================
# advance_epoch tests
# ===================================================================

class TestCoordinatorAdvanceEpoch:
    def test_advance_epoch_updates_values(self):
        coordinator = MemoryActuatorCoordinator()
        assert coordinator._current_epoch == 0
        assert coordinator._current_sequence == 0

        coordinator.advance_epoch(epoch=5, sequence=42)
        assert coordinator._current_epoch == 5
        assert coordinator._current_sequence == 42

    def test_advance_epoch_makes_old_envelopes_stale(self):
        """After advancing epoch, envelopes from the old epoch should be rejected."""
        coordinator = MemoryActuatorCoordinator()

        # Submit works at epoch 0
        env_old = _make_envelope(epoch=0, sequence=0)
        assert coordinator.submit(ActuatorAction.CLEANUP, env_old, "test") is not None

        # Advance epoch
        coordinator.advance_epoch(epoch=5, sequence=10)

        # Same envelope is now stale
        env_stale = _make_envelope(epoch=3, sequence=1)
        assert coordinator.submit(ActuatorAction.CLEANUP, env_stale, "test") is None

    def test_advance_epoch_accepts_matching_envelopes(self):
        """Envelopes matching the new epoch/sequence should still be accepted."""
        coordinator = MemoryActuatorCoordinator()
        coordinator.advance_epoch(epoch=10, sequence=20)

        env = _make_envelope(epoch=10, sequence=20)
        assert coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "test") is not None


# ===================================================================
# Shadow mode tests
# ===================================================================

class TestCoordinatorShadowMode:
    def test_shadow_mode_flags_actions(self):
        coordinator = MemoryActuatorCoordinator(shadow_mode=True)
        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        actions = coordinator.drain_pending()
        # In shadow mode, actions are returned but flagged
        assert all(a.shadow for a in actions)

    def test_shadow_mode_does_not_suppress_actions(self):
        """Shadow mode should not prevent actions from appearing in drain."""
        coordinator = MemoryActuatorCoordinator(shadow_mode=True)
        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "display")
        actions = coordinator.drain_pending()
        assert len(actions) == 2

    def test_non_shadow_mode_actions_not_flagged(self):
        """Without shadow mode, actions should not have shadow=True."""
        coordinator = MemoryActuatorCoordinator(shadow_mode=False)
        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "cleanup")
        actions = coordinator.drain_pending()
        assert all(not a.shadow for a in actions)


# ===================================================================
# get_stats tests
# ===================================================================

class TestCoordinatorStats:
    def test_initial_stats(self):
        """Fresh coordinator should have all zeroes."""
        coordinator = MemoryActuatorCoordinator()
        stats = coordinator.get_stats()
        assert stats["total_submitted"] == 0
        assert stats["total_rejected_stale"] == 0
        assert stats["total_rejected_quarantined"] == 0
        assert stats["pending_count"] == 0
        assert stats["quarantined_actions"] == []
        assert stats["shadow_mode"] is False

    def test_stats_after_submissions(self):
        """Stats should reflect actual submit/reject counts."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()

        coordinator.submit(ActuatorAction.CLEANUP, env, "c1")
        coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "d1")

        stats = coordinator.get_stats()
        assert stats["total_submitted"] == 2
        assert stats["pending_count"] == 2

    def test_stats_after_stale_rejection(self):
        coordinator = MemoryActuatorCoordinator()
        coordinator.advance_epoch(epoch=5, sequence=10)

        env_stale = _make_envelope(epoch=1, sequence=1)
        coordinator.submit(ActuatorAction.CLEANUP, env_stale, "test")

        stats = coordinator.get_stats()
        assert stats["total_rejected_stale"] == 1
        assert stats["total_submitted"] == 0

    def test_stats_after_quarantine_rejection(self):
        coordinator = MemoryActuatorCoordinator(failure_budget=1)
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")

        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "test")

        stats = coordinator.get_stats()
        assert stats["total_rejected_quarantined"] == 1
        assert stats["total_submitted"] == 0

    def test_stats_quarantined_actions_list(self):
        """Stats should list currently quarantined action values."""
        coordinator = MemoryActuatorCoordinator(failure_budget=1)
        coordinator.report_failure(ActuatorAction.CLEANUP, "timeout")
        coordinator.report_failure(ActuatorAction.MODEL_EVICT, "oom")

        stats = coordinator.get_stats()
        quarantined = stats["quarantined_actions"]
        assert "cleanup" in quarantined
        assert "model_evict" in quarantined

    def test_stats_pending_count_after_drain(self):
        """pending_count should be 0 after drain."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        coordinator.submit(ActuatorAction.CLEANUP, env, "test")
        coordinator.drain_pending()

        stats = coordinator.get_stats()
        assert stats["pending_count"] == 0

    def test_stats_shadow_mode_reflected(self):
        """Shadow mode should be reflected in stats."""
        coordinator = MemoryActuatorCoordinator(shadow_mode=True)
        stats = coordinator.get_stats()
        assert stats["shadow_mode"] is True


# ===================================================================
# Thread-safety tests
# ===================================================================

class TestCoordinatorThreadSafety:
    def test_concurrent_submits(self):
        """Multiple threads submitting concurrently should not lose actions."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        num_threads = 10
        submits_per_thread = 50
        results: list = []
        lock = threading.Lock()

        def submit_many():
            local_results = []
            for _ in range(submits_per_thread):
                did = coordinator.submit(ActuatorAction.CLEANUP, env, "thread")
                if did is not None:
                    local_results.append(did)
            with lock:
                results.extend(local_results)

        threads = [threading.Thread(target=submit_many) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_count = num_threads * submits_per_thread
        assert len(results) == expected_count
        # All decision IDs should be unique
        assert len(set(results)) == expected_count

        # drain should return all of them
        actions = coordinator.drain_pending()
        assert len(actions) == expected_count

    def test_concurrent_submit_and_drain(self):
        """Submit and drain from different threads should not crash or deadlock."""
        coordinator = MemoryActuatorCoordinator()
        env = _make_envelope()
        total_drained: list = []
        lock = threading.Lock()
        stop_event = threading.Event()

        def submitter():
            while not stop_event.is_set():
                coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "sub")

        def drainer():
            while not stop_event.is_set():
                drained = coordinator.drain_pending()
                with lock:
                    total_drained.extend(drained)

        threads = [
            threading.Thread(target=submitter),
            threading.Thread(target=submitter),
            threading.Thread(target=drainer),
        ]
        for t in threads:
            t.start()

        time.sleep(0.1)
        stop_event.set()

        for t in threads:
            t.join(timeout=2.0)

        # Should have drained at least some actions without deadlock
        assert len(total_drained) > 0


# ===================================================================
# PendingAction dataclass tests
# ===================================================================

class TestPendingAction:
    def test_pending_action_fields(self):
        """PendingAction should expose all expected fields."""
        env = _make_envelope()
        pa = PendingAction(
            decision_id="dec-abc123",
            action=ActuatorAction.DISPLAY_SHED,
            envelope=env,
            source="test_source",
            submitted_at=time.monotonic(),
            shadow=False,
        )
        assert pa.decision_id == "dec-abc123"
        assert pa.action == ActuatorAction.DISPLAY_SHED
        assert pa.envelope is env
        assert pa.source == "test_source"
        assert pa.shadow is False

    def test_pending_action_default_shadow(self):
        """shadow field should default to False."""
        env = _make_envelope()
        pa = PendingAction(
            decision_id="dec-xyz",
            action=ActuatorAction.CLEANUP,
            envelope=env,
            source="test",
            submitted_at=time.monotonic(),
        )
        assert pa.shadow is False

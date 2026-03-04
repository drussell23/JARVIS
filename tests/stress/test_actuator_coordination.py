"""Multi-actuator coordination integration tests.

Task 14: Verifies the full MemoryActuatorCoordinator pipeline end-to-end:
  - Multiple actuators submitting through the coordinator
  - Priority ordering of drained actions
  - Staleness rejection on epoch/sequence changes
  - Quarantine after repeated failures
  - Shadow mode behaviour (log but no suppression)

These are integration tests: no mocks, real coordinator instances.
"""
from __future__ import annotations

import time
import threading
from unittest.mock import patch

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
    pressure_tier: PressureTier = PressureTier.CRITICAL,
    snapshot_id: str = "snap-test",
    policy_version: str = "v1.0",
) -> DecisionEnvelope:
    """Create a DecisionEnvelope with sensible defaults."""
    return DecisionEnvelope(
        snapshot_id=snapshot_id,
        epoch=epoch,
        sequence=sequence,
        policy_version=policy_version,
        pressure_tier=pressure_tier,
        timestamp=time.time(),
    )


# ===================================================================
# Required tests (4 from plan spec)
# ===================================================================

class TestPlanSpecTests:
    """The 4 test cases mandated by the Task 14 plan."""

    def test_priority_ordering_under_critical_pressure(self) -> None:
        """Display shed fires before process cleanup under CRITICAL.

        Submit CLEANUP and DISPLAY_SHED in the wrong order (highest
        disruption first), then verify drain returns them sorted by
        ascending priority (least disruptive first).
        """
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)

        env = _make_envelope(epoch=1, sequence=1, pressure_tier=PressureTier.CRITICAL)

        # Submit in reverse priority order: cleanup (5) before display_shed (0)
        id_cleanup = coord.submit(ActuatorAction.CLEANUP, env, source="process_cleanup_mgr")
        id_defcon = coord.submit(ActuatorAction.DEFCON_ESCALATE, env, source="defcon_controller")
        id_display = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="display_pressure_ctrl")
        id_model = coord.submit(ActuatorAction.MODEL_EVICT, env, source="model_manager")
        id_cloud_offload = coord.submit(ActuatorAction.CLOUD_OFFLOAD, env, source="gcp_controller")
        id_cloud_scale = coord.submit(ActuatorAction.CLOUD_SCALE, env, source="cloud_scaler")

        # All should be accepted
        assert all(did is not None for did in [
            id_cleanup, id_defcon, id_display, id_model, id_cloud_offload, id_cloud_scale,
        ])

        drained = coord.drain_pending()
        assert len(drained) == 6

        # Verify ascending priority order
        priorities = [pa.action.priority for pa in drained]
        assert priorities == sorted(priorities), (
            f"Expected ascending priority order, got {priorities}"
        )

        # Verify DISPLAY_SHED (priority 0) is first and CLEANUP (priority 5) is last
        assert drained[0].action == ActuatorAction.DISPLAY_SHED
        assert drained[-1].action == ActuatorAction.CLEANUP

        # Verify the full expected order
        expected_order = [
            ActuatorAction.DISPLAY_SHED,      # 0
            ActuatorAction.DEFCON_ESCALATE,    # 1
            ActuatorAction.MODEL_EVICT,        # 2
            ActuatorAction.CLOUD_OFFLOAD,      # 3
            ActuatorAction.CLOUD_SCALE,        # 4
            ActuatorAction.CLEANUP,            # 5
        ]
        actual_order = [pa.action for pa in drained]
        assert actual_order == expected_order

    def test_stale_decisions_rejected_after_pressure_drops(self) -> None:
        """Decisions from old snapshot rejected when pressure resolves.

        Simulate: coordinator is at epoch=2, seq=5.  An actuator submits
        a decision computed against epoch=2, seq=3 (stale).  Must be
        rejected (submit returns None).
        """
        coord = MemoryActuatorCoordinator()

        # System advanced to epoch 2, sequence 5
        coord.advance_epoch(2, 5)

        # Decision was computed against an older sequence
        stale_env = _make_envelope(epoch=2, sequence=3)
        result = coord.submit(
            ActuatorAction.MODEL_EVICT, stale_env, source="stale_actuator",
        )
        assert result is None, "Stale decision (same epoch, old sequence) must be rejected"

        # Also test stale epoch
        stale_epoch_env = _make_envelope(epoch=1, sequence=99)
        result2 = coord.submit(
            ActuatorAction.CLEANUP, stale_epoch_env, source="stale_actuator2",
        )
        assert result2 is None, "Stale decision (old epoch) must be rejected"

        # Verify nothing is pending
        drained = coord.drain_pending()
        assert len(drained) == 0

        # Verify stats reflect rejections
        stats = coord.get_stats()
        assert stats["total_rejected_stale"] == 2
        assert stats["total_submitted"] == 0

    def test_quarantined_action_skipped(self) -> None:
        """Failed actuator quarantined, others still fire.

        After failure_budget (default 3) consecutive failures on
        MODEL_EVICT, further MODEL_EVICT submissions are rejected,
        but other action types still go through.
        """
        coord = MemoryActuatorCoordinator(failure_budget=3, quarantine_seconds=300.0)
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        # MODEL_EVICT fails 3 times in a row -> should get quarantined
        for i in range(3):
            coord.report_failure(ActuatorAction.MODEL_EVICT, reason=f"failure #{i+1}")

        assert coord.is_quarantined(ActuatorAction.MODEL_EVICT)

        # Submitting MODEL_EVICT should be rejected
        result_quarantined = coord.submit(
            ActuatorAction.MODEL_EVICT, env, source="model_manager",
        )
        assert result_quarantined is None, "Quarantined action must be rejected"

        # Other action types should still be accepted
        result_display = coord.submit(
            ActuatorAction.DISPLAY_SHED, env, source="display_ctrl",
        )
        assert result_display is not None, "Non-quarantined action must be accepted"

        result_cleanup = coord.submit(
            ActuatorAction.CLEANUP, env, source="cleanup_mgr",
        )
        assert result_cleanup is not None, "Non-quarantined action must be accepted"

        # Drain and verify only the non-quarantined actions are present
        drained = coord.drain_pending()
        assert len(drained) == 2
        action_types = {pa.action for pa in drained}
        assert ActuatorAction.MODEL_EVICT not in action_types
        assert ActuatorAction.DISPLAY_SHED in action_types
        assert ActuatorAction.CLEANUP in action_types

        # Verify stats
        stats = coord.get_stats()
        assert stats["total_rejected_quarantined"] == 1
        assert "model_evict" in stats["quarantined_actions"]

    def test_shadow_mode_no_actuation(self) -> None:
        """Shadow mode logs but does not execute actions.

        In shadow mode, actions are accepted and appear in drain with
        shadow=True.  The coordinator does NOT suppress them from drain
        (it only flags them -- the caller decides whether to execute).
        """
        coord = MemoryActuatorCoordinator(shadow_mode=True)
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        # Submit several actions
        id1 = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="display_ctrl")
        id2 = coord.submit(ActuatorAction.CLEANUP, env, source="cleanup_mgr")

        assert id1 is not None
        assert id2 is not None

        drained = coord.drain_pending()
        assert len(drained) == 2

        # All drained actions should be flagged as shadow
        for pa in drained:
            assert pa.shadow is True, (
                f"Expected shadow=True for {pa.action.value}, got shadow={pa.shadow}"
            )

        # Verify coordinator reports shadow mode in stats
        stats = coord.get_stats()
        assert stats["shadow_mode"] is True


# ===================================================================
# Additional integration tests beyond plan spec
# ===================================================================

class TestDrainBehaviour:
    """Tests for drain_pending destructiveness and idempotency."""

    def test_drain_returns_empty_after_second_drain(self) -> None:
        """drain_pending is destructive -- second drain returns empty list."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        coord.submit(ActuatorAction.DISPLAY_SHED, env, source="src_a")
        coord.submit(ActuatorAction.CLEANUP, env, source="src_b")

        first_drain = coord.drain_pending()
        assert len(first_drain) == 2

        second_drain = coord.drain_pending()
        assert len(second_drain) == 0, (
            "Second drain must return empty -- drain is destructive"
        )

    def test_drain_empty_coordinator(self) -> None:
        """Draining a coordinator with no submissions returns empty list."""
        coord = MemoryActuatorCoordinator()
        drained = coord.drain_pending()
        assert drained == []


class TestDeduplication:
    """Test that deduplication is NOT performed."""

    def test_multiple_sources_same_action_type_both_accepted(self) -> None:
        """Multiple sources submitting the same action type are ALL accepted.

        The coordinator does not dedup -- each submit is a distinct pending action.
        """
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        id1 = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="display_ctrl_1")
        id2 = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="display_ctrl_2")
        id3 = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="resource_governor")

        assert id1 is not None
        assert id2 is not None
        assert id3 is not None

        # All three must be different decision IDs
        assert len({id1, id2, id3}) == 3, "Each submission must get a unique decision_id"

        drained = coord.drain_pending()
        assert len(drained) == 3
        assert all(pa.action == ActuatorAction.DISPLAY_SHED for pa in drained)

        # Verify distinct sources preserved
        sources = {pa.source for pa in drained}
        assert sources == {"display_ctrl_1", "display_ctrl_2", "resource_governor"}


class TestEpochStaleness:
    """Tests for epoch/sequence staleness logic."""

    def test_epoch_advance_makes_prior_stale(self) -> None:
        """Epoch change invalidates all prior envelopes.

        Even if the old envelope had a higher sequence number, a lower
        epoch makes it stale.
        """
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(2, 1)

        # Envelope from epoch 1, even with a high sequence
        old_env = _make_envelope(epoch=1, sequence=999)
        result = coord.submit(ActuatorAction.CLEANUP, old_env, source="old_src")
        assert result is None, "Old epoch must be rejected regardless of sequence"

        # Same epoch, same sequence should be accepted
        current_env = _make_envelope(epoch=2, sequence=1)
        result2 = coord.submit(ActuatorAction.CLEANUP, current_env, source="current_src")
        assert result2 is not None, "Current epoch+sequence must be accepted"

    def test_same_epoch_same_sequence_accepted(self) -> None:
        """Envelope with exactly matching epoch and sequence is accepted."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(5, 10)

        env = _make_envelope(epoch=5, sequence=10)
        result = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="src")
        assert result is not None

    def test_future_epoch_accepted(self) -> None:
        """Envelope with a future epoch is accepted (not stale)."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)

        future_env = _make_envelope(epoch=2, sequence=0)
        result = coord.submit(ActuatorAction.DISPLAY_SHED, future_env, source="src")
        assert result is not None

    def test_sequential_epoch_advances_reject_intermediate(self) -> None:
        """Multiple epoch advances progressively invalidate older decisions."""
        coord = MemoryActuatorCoordinator()

        # Start at epoch 1, seq 1
        coord.advance_epoch(1, 1)
        env_e1 = _make_envelope(epoch=1, sequence=1)
        result1 = coord.submit(ActuatorAction.DISPLAY_SHED, env_e1, source="src")
        assert result1 is not None

        # Advance to epoch 2, seq 1
        coord.advance_epoch(2, 1)
        result2 = coord.submit(ActuatorAction.CLEANUP, env_e1, source="src")
        assert result2 is None, "Epoch 1 decision rejected at epoch 2"

        # Advance to epoch 3, seq 5
        coord.advance_epoch(3, 5)
        env_e2 = _make_envelope(epoch=2, sequence=1)
        result3 = coord.submit(ActuatorAction.CLEANUP, env_e2, source="src")
        assert result3 is None, "Epoch 2 decision rejected at epoch 3"


class TestQuarantineBehaviour:
    """Tests for quarantine lifecycle -- expiry, resets, and isolation."""

    def test_quarantine_expires_after_timeout(self) -> None:
        """Quarantine is time-limited; action is accepted after expiry.

        Uses monkeypatching of time.monotonic to simulate time passage
        without actual sleeping.
        """
        coord = MemoryActuatorCoordinator(
            failure_budget=2, quarantine_seconds=60.0,
        )
        coord.advance_epoch(1, 1)

        # Fail twice to trigger quarantine
        coord.report_failure(ActuatorAction.CLOUD_OFFLOAD, reason="fail1")
        coord.report_failure(ActuatorAction.CLOUD_OFFLOAD, reason="fail2")
        assert coord.is_quarantined(ActuatorAction.CLOUD_OFFLOAD)

        # Capture the real monotonic value at quarantine time, then simulate
        # time progressing past the quarantine window.
        base_time = time.monotonic()

        with patch("time.monotonic", return_value=base_time + 61.0):
            # Also need to patch in the coordinator's module since it
            # imports time at module level
            with patch(
                "backend.core.memory_actuator_coordinator.time.monotonic",
                return_value=base_time + 61.0,
            ):
                assert not coord.is_quarantined(ActuatorAction.CLOUD_OFFLOAD), (
                    "Quarantine must expire after quarantine_seconds"
                )

                env = _make_envelope(epoch=1, sequence=1)
                result = coord.submit(
                    ActuatorAction.CLOUD_OFFLOAD, env, source="cloud_ctrl",
                )
                assert result is not None, (
                    "Submission must succeed after quarantine expiry"
                )

    def test_success_resets_failure_counter(self) -> None:
        """Intermittent successes prevent quarantine from being reached.

        Fail twice (budget=3), then succeed, then fail twice more.
        The success resets the counter, so we should NOT be quarantined.
        """
        coord = MemoryActuatorCoordinator(failure_budget=3)
        coord.advance_epoch(1, 1)

        # Two failures
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="fail1")
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="fail2")
        assert not coord.is_quarantined(ActuatorAction.MODEL_EVICT), (
            "Should not be quarantined at 2 failures with budget=3"
        )

        # One success resets counter
        coord.report_success(ActuatorAction.MODEL_EVICT)

        # Two more failures -- still under budget because counter was reset
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="fail3")
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="fail4")
        assert not coord.is_quarantined(ActuatorAction.MODEL_EVICT), (
            "Counter was reset by success, so 2 new failures should not quarantine"
        )

    def test_success_clears_existing_quarantine(self) -> None:
        """report_success lifts an active quarantine immediately."""
        coord = MemoryActuatorCoordinator(failure_budget=2)
        coord.advance_epoch(1, 1)

        coord.report_failure(ActuatorAction.CLEANUP, reason="f1")
        coord.report_failure(ActuatorAction.CLEANUP, reason="f2")
        assert coord.is_quarantined(ActuatorAction.CLEANUP)

        coord.report_success(ActuatorAction.CLEANUP)
        assert not coord.is_quarantined(ActuatorAction.CLEANUP), (
            "report_success must lift quarantine"
        )

        # Verify the action can now be submitted
        env = _make_envelope(epoch=1, sequence=1)
        result = coord.submit(ActuatorAction.CLEANUP, env, source="src")
        assert result is not None

    def test_quarantine_isolated_per_action_type(self) -> None:
        """Quarantining one action type does not affect others."""
        coord = MemoryActuatorCoordinator(failure_budget=2)

        # Quarantine MODEL_EVICT
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="f1")
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="f2")
        assert coord.is_quarantined(ActuatorAction.MODEL_EVICT)

        # All other action types must NOT be quarantined
        for action in ActuatorAction:
            if action != ActuatorAction.MODEL_EVICT:
                assert not coord.is_quarantined(action), (
                    f"{action.value} should not be quarantined"
                )


class TestStats:
    """Tests for coordinator statistics accuracy."""

    def test_stats_reflect_submissions_and_rejections(self) -> None:
        """get_stats accurately counts submissions, stale rejects, quarantine rejects."""
        coord = MemoryActuatorCoordinator(failure_budget=1)
        coord.advance_epoch(2, 5)

        env_current = _make_envelope(epoch=2, sequence=5)
        env_stale = _make_envelope(epoch=1, sequence=1)

        # 2 successful submissions
        coord.submit(ActuatorAction.DISPLAY_SHED, env_current, source="s1")
        coord.submit(ActuatorAction.CLEANUP, env_current, source="s2")

        # 1 stale rejection
        coord.submit(ActuatorAction.MODEL_EVICT, env_stale, source="s3")

        # Quarantine CLOUD_OFFLOAD and try to submit it
        coord.report_failure(ActuatorAction.CLOUD_OFFLOAD, reason="fail")
        coord.submit(ActuatorAction.CLOUD_OFFLOAD, env_current, source="s4")

        stats = coord.get_stats()
        assert stats["total_submitted"] == 2
        assert stats["total_rejected_stale"] == 1
        assert stats["total_rejected_quarantined"] == 1
        assert stats["pending_count"] == 2
        assert "cloud_offload" in stats["quarantined_actions"]

    def test_stats_pending_count_decreases_after_drain(self) -> None:
        """pending_count drops to 0 after drain_pending."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        coord.submit(ActuatorAction.DISPLAY_SHED, env, source="src")
        assert coord.get_stats()["pending_count"] == 1

        coord.drain_pending()
        assert coord.get_stats()["pending_count"] == 0


class TestShadowModeDetails:
    """Additional shadow mode edge cases."""

    def test_shadow_mode_off_actions_not_flagged(self) -> None:
        """When shadow_mode=False, drained actions have shadow=False."""
        coord = MemoryActuatorCoordinator(shadow_mode=False)
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        coord.submit(ActuatorAction.DISPLAY_SHED, env, source="src")
        drained = coord.drain_pending()
        assert len(drained) == 1
        assert drained[0].shadow is False

    def test_shadow_mode_still_rejects_stale(self) -> None:
        """Shadow mode does not bypass staleness checks."""
        coord = MemoryActuatorCoordinator(shadow_mode=True)
        coord.advance_epoch(3, 1)

        stale_env = _make_envelope(epoch=2, sequence=1)
        result = coord.submit(ActuatorAction.CLEANUP, stale_env, source="src")
        assert result is None, "Shadow mode must still reject stale decisions"

    def test_shadow_mode_still_rejects_quarantined(self) -> None:
        """Shadow mode does not bypass quarantine checks."""
        coord = MemoryActuatorCoordinator(shadow_mode=True, failure_budget=1)
        coord.advance_epoch(1, 1)

        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="fail")
        env = _make_envelope(epoch=1, sequence=1)
        result = coord.submit(ActuatorAction.MODEL_EVICT, env, source="src")
        assert result is None, "Shadow mode must still reject quarantined actions"


class TestConcurrency:
    """Thread-safety integration tests."""

    def test_concurrent_submissions_from_multiple_threads(self) -> None:
        """Multiple threads submitting concurrently without data corruption."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        results: list = []
        errors: list = []
        num_threads = 8
        submissions_per_thread = 50

        actions = list(ActuatorAction)

        def submitter(thread_id: int) -> None:
            try:
                for i in range(submissions_per_thread):
                    action = actions[i % len(actions)]
                    decision_id = coord.submit(
                        action, env, source=f"thread-{thread_id}",
                    )
                    if decision_id is not None:
                        results.append(decision_id)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submitter, args=(tid,))
            for tid in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent submissions raised errors: {errors}"

        total_expected = num_threads * submissions_per_thread
        assert len(results) == total_expected, (
            f"Expected {total_expected} accepted submissions, got {len(results)}"
        )

        # All decision IDs must be unique
        assert len(set(results)) == total_expected, "Decision IDs must be unique"

        drained = coord.drain_pending()
        assert len(drained) == total_expected

    def test_concurrent_submit_and_drain(self) -> None:
        """Submitting and draining concurrently does not lose or duplicate actions."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        total_submissions = 200
        all_drained: list = []
        drain_lock = threading.Lock()

        def submitter() -> None:
            for _ in range(total_submissions):
                coord.submit(ActuatorAction.DISPLAY_SHED, env, source="submit_thread")

        def drainer() -> None:
            for _ in range(50):
                batch = coord.drain_pending()
                with drain_lock:
                    all_drained.extend(batch)
                time.sleep(0.001)

        submit_thread = threading.Thread(target=submitter)
        drain_thread = threading.Thread(target=drainer)

        submit_thread.start()
        drain_thread.start()

        submit_thread.join(timeout=10)
        drain_thread.join(timeout=10)

        # Final drain to catch any remaining
        final = coord.drain_pending()
        all_drained.extend(final)

        assert len(all_drained) == total_submissions, (
            f"Expected {total_submissions} total drained, got {len(all_drained)} "
            "(actions lost or duplicated)"
        )


class TestPendingActionFields:
    """Verify PendingAction dataclass fields are populated correctly."""

    def test_pending_action_preserves_source_and_envelope(self) -> None:
        """Submitted PendingAction retains original source and envelope."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(3, 7)

        env = _make_envelope(
            epoch=3, sequence=7,
            pressure_tier=PressureTier.EMERGENCY,
            snapshot_id="snap-emergency-42",
            policy_version="v2.1",
        )

        coord.submit(ActuatorAction.CLOUD_SCALE, env, source="cloud_autoscaler")
        drained = coord.drain_pending()
        assert len(drained) == 1

        pa = drained[0]
        assert pa.action == ActuatorAction.CLOUD_SCALE
        assert pa.source == "cloud_autoscaler"
        assert pa.envelope is env
        assert pa.envelope.snapshot_id == "snap-emergency-42"
        assert pa.envelope.pressure_tier == PressureTier.EMERGENCY
        assert pa.envelope.policy_version == "v2.1"
        assert pa.decision_id.startswith("dec-")

    def test_decision_ids_are_unique_across_submissions(self) -> None:
        """Each submission gets a globally unique decision_id."""
        coord = MemoryActuatorCoordinator()
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        ids = set()
        for _ in range(100):
            decision_id = coord.submit(ActuatorAction.DISPLAY_SHED, env, source="src")
            assert decision_id is not None
            ids.add(decision_id)

        assert len(ids) == 100, "All 100 decision IDs must be unique"


class TestEndToEndPipeline:
    """Full pipeline scenarios combining multiple coordinator features."""

    def test_full_lifecycle_submit_fail_quarantine_expire_resubmit(self) -> None:
        """Complete lifecycle: submit -> execute -> fail -> quarantine -> expire -> resubmit."""
        coord = MemoryActuatorCoordinator(failure_budget=2, quarantine_seconds=10.0)
        coord.advance_epoch(1, 1)
        env = _make_envelope(epoch=1, sequence=1)

        # Phase 1: Initial submission and drain
        id1 = coord.submit(ActuatorAction.MODEL_EVICT, env, source="model_mgr")
        assert id1 is not None
        drained = coord.drain_pending()
        assert len(drained) == 1

        # Phase 2: Report consecutive failures -> quarantine
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="OOM")
        coord.report_failure(ActuatorAction.MODEL_EVICT, reason="OOM again")
        assert coord.is_quarantined(ActuatorAction.MODEL_EVICT)

        # Phase 3: Submissions rejected during quarantine
        id2 = coord.submit(ActuatorAction.MODEL_EVICT, env, source="model_mgr")
        assert id2 is None

        # Phase 4: Simulate quarantine expiry
        base_time = time.monotonic()
        with patch(
            "backend.core.memory_actuator_coordinator.time.monotonic",
            return_value=base_time + 11.0,
        ):
            assert not coord.is_quarantined(ActuatorAction.MODEL_EVICT)

            # Phase 5: Resubmit after quarantine expired
            id3 = coord.submit(ActuatorAction.MODEL_EVICT, env, source="model_mgr")
            assert id3 is not None

            # Phase 6: Report success to reset failure counter
            coord.report_success(ActuatorAction.MODEL_EVICT)

        assert not coord.is_quarantined(ActuatorAction.MODEL_EVICT)

    def test_mixed_stale_quarantined_and_valid_submissions(self) -> None:
        """Coordinator correctly handles a mix of valid, stale, and quarantined submissions."""
        coord = MemoryActuatorCoordinator(failure_budget=2)
        coord.advance_epoch(5, 10)

        current_env = _make_envelope(epoch=5, sequence=10)
        stale_env = _make_envelope(epoch=4, sequence=99)

        # Quarantine CLOUD_SCALE
        coord.report_failure(ActuatorAction.CLOUD_SCALE, reason="timeout")
        coord.report_failure(ActuatorAction.CLOUD_SCALE, reason="timeout again")

        # Valid submission
        r1 = coord.submit(ActuatorAction.DISPLAY_SHED, current_env, source="valid_src")
        assert r1 is not None

        # Stale submission
        r2 = coord.submit(ActuatorAction.CLEANUP, stale_env, source="stale_src")
        assert r2 is None

        # Quarantined submission
        r3 = coord.submit(ActuatorAction.CLOUD_SCALE, current_env, source="quarantine_src")
        assert r3 is None

        # Another valid submission
        r4 = coord.submit(ActuatorAction.DEFCON_ESCALATE, current_env, source="valid_src_2")
        assert r4 is not None

        drained = coord.drain_pending()
        assert len(drained) == 2

        # Verify priority order: DISPLAY_SHED (0) before DEFCON_ESCALATE (1)
        assert drained[0].action == ActuatorAction.DISPLAY_SHED
        assert drained[1].action == ActuatorAction.DEFCON_ESCALATE

        stats = coord.get_stats()
        assert stats["total_submitted"] == 2
        assert stats["total_rejected_stale"] == 1
        assert stats["total_rejected_quarantined"] == 1

"""Tests for CloudCapacityController and related types."""

import pytest
import time


def test_cloud_capacity_action_enum():
    """CloudCapacityAction must have all required values."""
    from backend.core.memory_types import CloudCapacityAction

    expected = {
        "STAY_LOCAL",
        "DEGRADE_LOCAL",
        "OFFLOAD_PARTIAL",
        "SPIN_SPOT",
        "FALLBACK_ONDEMAND",
    }
    actual = {a.name for a in CloudCapacityAction}
    assert actual == expected


def test_cloud_capacity_action_is_str_enum():
    """CloudCapacityAction values should be usable as strings."""
    from backend.core.memory_types import CloudCapacityAction

    assert CloudCapacityAction.STAY_LOCAL.value == "stay_local"
    assert CloudCapacityAction.SPIN_SPOT.value == "spin_spot"


# ===================================================================
# MemoryActuatorCoordinator.drain_pending epoch-fencing tests
# ===================================================================


def _make_envelope(epoch: int, sequence: int):
    """Helper: create a DecisionEnvelope for testing."""
    from backend.core.memory_types import DecisionEnvelope, PressureTier

    return DecisionEnvelope(
        snapshot_id="test-snap",
        epoch=epoch,
        sequence=sequence,
        policy_version="1.0.0",
        pressure_tier=PressureTier.ELEVATED,
        timestamp=time.monotonic(),
    )


def test_drain_pending_rejects_stale_actions():
    """Actions submitted at an old epoch must be filtered out at drain time."""
    from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
    from backend.core.memory_types import ActuatorAction

    coord = MemoryActuatorCoordinator()
    coord.advance_epoch(1, 1)

    # Submit an action at epoch=1, sequence=1
    envelope = _make_envelope(epoch=1, sequence=1)
    decision_id = coord.submit(ActuatorAction.CLEANUP, envelope, source="test")
    assert decision_id is not None  # accepted at submit time

    # Advance epoch — the queued action is now stale
    coord.advance_epoch(2, 1)

    # drain_pending should filter out the stale action
    actions = coord.drain_pending()
    assert len(actions) == 0

    # Verify stats reflect the rejection
    stats = coord.get_stats()
    assert stats["total_rejected_stale"] >= 1


def test_drain_pending_keeps_fresh_actions():
    """Actions whose epoch/sequence match current should survive drain."""
    from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
    from backend.core.memory_types import ActuatorAction

    coord = MemoryActuatorCoordinator()
    coord.advance_epoch(1, 5)

    # Submit an action at epoch=1, sequence=5 (matches current)
    envelope = _make_envelope(epoch=1, sequence=5)
    decision_id = coord.submit(ActuatorAction.CLEANUP, envelope, source="test")
    assert decision_id is not None

    # Do NOT advance epoch — action should remain fresh
    actions = coord.drain_pending()
    assert len(actions) == 1
    assert actions[0].decision_id == decision_id

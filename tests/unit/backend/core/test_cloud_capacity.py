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


# ===================================================================
# CloudCapacityController tests
# ===================================================================

from unittest.mock import MagicMock
import asyncio


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.register_pressure_observer = MagicMock()
    broker.latest_snapshot = MagicMock()
    broker.latest_snapshot.memory_percent = 50.0
    return broker


@pytest.mark.asyncio
async def test_controller_registers_with_broker(mock_broker):
    """Controller must register as a pressure observer on init."""
    from backend.core.cloud_capacity_controller import CloudCapacityController

    controller = CloudCapacityController(broker=mock_broker)
    mock_broker.register_pressure_observer.assert_called_once()


@pytest.mark.asyncio
async def test_controller_stay_local_at_low_pressure(mock_broker):
    """At ABUNDANT/OPTIMAL pressure, action should be STAY_LOCAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(tier=PressureTier.OPTIMAL, queue_depth=2)
    assert action == CloudCapacityAction.STAY_LOCAL


@pytest.mark.asyncio
async def test_controller_degrade_local_at_constrained(mock_broker):
    """At CONSTRAINED with manageable queue, should DEGRADE_LOCAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(tier=PressureTier.CONSTRAINED, queue_depth=3)
    assert action == CloudCapacityAction.DEGRADE_LOCAL


@pytest.mark.asyncio
async def test_controller_spin_spot_at_critical_sustained(mock_broker):
    """At CRITICAL pressure sustained > threshold, should SPIN_SPOT."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    controller._first_critical_at = time.monotonic() - 60
    action = controller.evaluate(tier=PressureTier.CRITICAL, queue_depth=10, latency_violations=5)
    assert action == CloudCapacityAction.SPIN_SPOT


@pytest.mark.asyncio
async def test_controller_spot_create_cooldown(mock_broker):
    """Spot create cooldown must prevent rapid VM creation."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    controller._last_spot_create = time.monotonic()  # Just created
    controller._first_critical_at = time.monotonic() - 60

    action = controller.evaluate(tier=PressureTier.CRITICAL, queue_depth=10, latency_violations=5)
    # Should fall back since cooldown prevents SPIN_SPOT
    assert action in (CloudCapacityAction.OFFLOAD_PARTIAL, CloudCapacityAction.FALLBACK_ONDEMAND)


@pytest.mark.asyncio
async def test_controller_offload_partial_at_constrained_growing_queue(mock_broker):
    """CONSTRAINED with growing queue should OFFLOAD_PARTIAL."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    action = controller.evaluate(tier=PressureTier.CONSTRAINED, queue_depth=15, latency_violations=3)
    assert action == CloudCapacityAction.OFFLOAD_PARTIAL


@pytest.mark.asyncio
async def test_controller_stats_telemetry(mock_broker):
    """Controller stats must track decision counts."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    controller.evaluate(tier=PressureTier.OPTIMAL, queue_depth=0)
    controller.evaluate(tier=PressureTier.OPTIMAL, queue_depth=0)
    controller.evaluate(tier=PressureTier.CONSTRAINED, queue_depth=3)

    stats = controller.get_stats()
    assert stats["total_decisions"] == 3
    assert stats["decisions_by_action"]["stay_local"] == 2
    assert stats["decisions_by_action"]["degrade_local"] == 1


@pytest.mark.asyncio
async def test_controller_spot_unavailable_falls_back(mock_broker):
    """When spot is unavailable, CRITICAL should fall back to on-demand."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import CloudCapacityAction, PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    controller._first_critical_at = time.monotonic() - 60
    controller.mark_spot_unavailable()

    action = controller.evaluate(tier=PressureTier.CRITICAL, queue_depth=10, latency_violations=5)
    assert action == CloudCapacityAction.FALLBACK_ONDEMAND


@pytest.mark.asyncio
async def test_controller_pressure_callback(mock_broker):
    """Broker pressure callback should update internal tier."""
    from backend.core.cloud_capacity_controller import CloudCapacityController
    from backend.core.memory_types import PressureTier

    controller = CloudCapacityController(broker=mock_broker)
    assert controller._current_tier == PressureTier.OPTIMAL

    await controller._on_pressure_change(PressureTier.CRITICAL, None)
    assert controller._current_tier == PressureTier.CRITICAL
    assert controller._first_critical_at is not None

    await controller._on_pressure_change(PressureTier.ELEVATED, None)
    assert controller._first_critical_at is None

"""Tests for broker <-> coordinator wiring."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.memory_budget_broker import MemoryBudgetBroker
from backend.core.memory_actuator_coordinator import MemoryActuatorCoordinator
from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    PressurePolicy,
    PressureTier,
)


def _make_broker(epoch: int = 1) -> MemoryBudgetBroker:
    """Create a broker with a mocked quantizer."""
    quantizer = MagicMock()
    quantizer.set_broker_ref = MagicMock()
    return MemoryBudgetBroker(quantizer, epoch=epoch)


class TestBrokerCoordinatorWire:
    def test_broker_has_coordinator_property(self):
        broker = _make_broker()
        assert broker.coordinator is not None
        assert isinstance(broker.coordinator, MemoryActuatorCoordinator)

    def test_broker_has_sequence_counter(self):
        broker = _make_broker()
        seq1 = broker.current_sequence
        broker._advance_sequence()
        seq2 = broker.current_sequence
        assert seq2 == seq1 + 1

    def test_broker_has_policy_property(self):
        broker = _make_broker()
        assert isinstance(broker.policy, PressurePolicy)

    def test_advance_sequence_syncs_coordinator(self):
        broker = _make_broker(epoch=5)
        broker._advance_sequence()
        # After advancing, coordinator should have epoch=5, sequence=1
        # Verify by submitting an envelope with epoch=5, sequence=1 (should not be stale)
        env = DecisionEnvelope(
            snapshot_id="s1",
            epoch=5,
            sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        result = broker.coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "test")
        assert result is not None  # Should be accepted, not stale

    def test_sequence_starts_at_zero(self):
        broker = _make_broker()
        assert broker.current_sequence == 0

    def test_policy_matches_hardware(self):
        """Policy should be hardware-appropriate (from for_ram_gb)."""
        broker = _make_broker()
        # Version should start with 'v' and include a profile suffix
        assert broker.policy.version.startswith("v")

    @pytest.mark.asyncio
    async def test_notify_observers_advances_sequence(self):
        """notify_pressure_observers must advance sequence before notifying."""
        broker = _make_broker()
        initial_seq = broker.current_sequence
        mock_observer = AsyncMock()
        broker.register_pressure_observer(mock_observer)
        mock_snapshot = MagicMock()
        await broker.notify_pressure_observers(PressureTier.CRITICAL, mock_snapshot)
        assert broker.current_sequence == initial_seq + 1
        mock_observer.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_observers_sequence_before_callback(self):
        """Sequence must be advanced BEFORE observers are called."""
        broker = _make_broker()
        captured_seq: list[int] = []

        async def observer(tier, snapshot):
            captured_seq.append(broker.current_sequence)

        broker.register_pressure_observer(observer)
        await broker.notify_pressure_observers(PressureTier.ELEVATED, MagicMock())
        # The observer should have seen sequence=1 (advanced before callback)
        assert captured_seq == [1]

    def test_advance_sequence_returns_new_value(self):
        broker = _make_broker()
        result = broker._advance_sequence()
        assert result == 1
        result = broker._advance_sequence()
        assert result == 2

    def test_coordinator_is_same_instance_on_repeated_access(self):
        """Property should always return the same coordinator instance."""
        broker = _make_broker()
        c1 = broker.coordinator
        c2 = broker.coordinator
        assert c1 is c2

    def test_stale_envelope_rejected_after_advance(self):
        """An envelope from an old sequence should be rejected after advancing."""
        broker = _make_broker(epoch=1)
        broker._advance_sequence()  # sequence=1
        broker._advance_sequence()  # sequence=2

        # Envelope with sequence=1 is now stale
        env = DecisionEnvelope(
            snapshot_id="s1",
            epoch=1,
            sequence=1,
            policy_version="v1.0",
            pressure_tier=PressureTier.CRITICAL,
            timestamp=time.time(),
        )
        result = broker.coordinator.submit(ActuatorAction.DISPLAY_SHED, env, "test")
        assert result is None  # Should be rejected as stale

    @pytest.mark.asyncio
    async def test_multiple_notify_calls_increment_sequence(self):
        """Each call to notify_pressure_observers should increment sequence."""
        broker = _make_broker()
        mock_snapshot = MagicMock()
        await broker.notify_pressure_observers(PressureTier.ELEVATED, mock_snapshot)
        await broker.notify_pressure_observers(PressureTier.CRITICAL, mock_snapshot)
        await broker.notify_pressure_observers(PressureTier.EMERGENCY, mock_snapshot)
        assert broker.current_sequence == 3

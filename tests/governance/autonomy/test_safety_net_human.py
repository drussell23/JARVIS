"""Tests for L3 human presence signal."""
import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import CommandType
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.safety_net import ProductionSafetyNet


class TestHumanPresence:
    def test_signal_active_emits_command(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=True, activity_type="keyboard")

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.SIGNAL_HUMAN_PRESENCE
        assert cmd.payload["is_active"] is True
        assert cmd.payload["activity_type"] == "keyboard"

    def test_signal_inactive_emits_command(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=False, activity_type="idle")

        assert bus.qsize() == 1
        cmd = bus._heap[0][2]
        assert cmd.payload["is_active"] is False

    def test_idempotent_same_state(self):
        bus = CommandBus(maxsize=100)
        net = ProductionSafetyNet(command_bus=bus)
        net.signal_human_presence(is_active=True, activity_type="keyboard")
        net.signal_human_presence(is_active=True, activity_type="keyboard")

        # Idempotency should dedup (CommandBus dedup via idempotency key)
        assert bus.qsize() == 1

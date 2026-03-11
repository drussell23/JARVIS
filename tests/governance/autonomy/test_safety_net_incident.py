"""Tests for L3 incident auto-detection -> mode switch."""
import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.safety_net import ProductionSafetyNet, SafetyNetConfig


def _rollback(op_id: str, reason: str = "validation_failed") -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": op_id,
            "brain_id": "qwen_coder",
            "rollback_reason": reason,
            "affected_files": ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    )


class TestIncidentAutoTrigger:
    def test_three_rollbacks_triggers_incident(self):
        bus = CommandBus(maxsize=100)
        config = SafetyNetConfig(
            rollback_pattern_threshold=2,
            incident_rollback_threshold=3,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        emitter = EventEmitter()
        net.register_event_handlers(emitter)

        for i in range(3):
            net._on_rollback(_rollback(f"op_{i}"))

        # Should have incident-triggered mode switch
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        assert len(mode_cmds) >= 1
        assert any("incident" in c.payload.get("reason", "").lower() for c in mode_cmds)

    def test_incident_not_triggered_below_threshold(self):
        bus = CommandBus(maxsize=100)
        config = SafetyNetConfig(incident_rollback_threshold=5)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        emitter = EventEmitter()
        net.register_event_handlers(emitter)

        for i in range(2):
            net._on_rollback(_rollback(f"op_{i}"))

        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)
        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        assert len(mode_cmds) == 0

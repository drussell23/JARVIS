"""Integration: health probe failure -> SafetyNet -> mode switch command -> GLS handles."""
import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.safety_net import ProductionSafetyNet, SafetyNetConfig


@pytest.mark.asyncio
async def test_health_failure_escalation_flow():
    """End-to-end: health probe failures -> SafetyNet -> mode switch command."""
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    config = SafetyNetConfig(probe_failure_escalation_threshold=2)
    net = ProductionSafetyNet(command_bus=bus, config=config)
    net.register_event_handlers(emitter)

    # Simulate L1 emitting health probe failures
    for i in range(2):
        await emitter.emit(EventEnvelope(
            source_layer="L1",
            event_type=EventType.HEALTH_PROBE_RESULT,
            payload={"provider": "gcp-jprime", "success": False,
                     "latency_ms": 0, "consecutive_failures": i + 1},
        ))

    # SafetyNet should have enqueued a mode switch command
    assert bus.qsize() >= 1
    cmd = await bus.get()
    assert cmd.command_type == CommandType.REQUEST_MODE_SWITCH
    assert cmd.payload["target_mode"] == "REDUCED_AUTONOMY"


@pytest.mark.asyncio
async def test_rollback_emits_root_cause():
    """End-to-end: rollback event -> SafetyNet -> root cause command."""
    bus = CommandBus(maxsize=100)
    emitter = EventEmitter()
    net = ProductionSafetyNet(command_bus=bus)
    net.register_event_handlers(emitter)

    await emitter.emit(EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": "test-op-1",
            "brain_id": "qwen_coder",
            "rollback_reason": "validation_failed",
            "affected_files": ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    ))

    assert bus.qsize() >= 1
    cmd = await bus.get()
    assert cmd.command_type == CommandType.REPORT_ROLLBACK_CAUSE
    assert cmd.payload["op_id"] == "test-op-1"


@pytest.mark.asyncio
async def test_human_presence_flow():
    """End-to-end: human presence signal -> command on bus."""
    bus = CommandBus(maxsize=100)
    net = ProductionSafetyNet(command_bus=bus)
    net.signal_human_presence(is_active=True, activity_type="keyboard")

    assert bus.qsize() == 1
    cmd = await bus.get()
    assert cmd.command_type == CommandType.SIGNAL_HUMAN_PRESENCE
    assert cmd.payload["is_active"] is True

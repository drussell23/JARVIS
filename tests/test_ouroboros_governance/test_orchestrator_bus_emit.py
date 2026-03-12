"""Tests for orchestrator-level bus emit on saga failure boundaries."""
from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessageBus,
    SagaMessageType,
    SagaMessage,
    MessagePriority,
)


def test_bus_has_saga_failed_type():
    assert SagaMessageType.SAGA_FAILED.value == "saga_failed"


def test_bus_stores_messages():
    bus = SagaMessageBus(max_messages=10)
    bus.send(SagaMessage(
        message_type=SagaMessageType.SAGA_FAILED,
        saga_id="test",
        payload={"schema_version": "1.0", "op_id": "test-op", "reason_code": "verify_failed"},
    ))
    msgs = bus.get_messages(saga_id="test")
    assert len(msgs) == 1
    assert msgs[0].payload["reason_code"] == "verify_failed"

"""Tests for B+ saga message types and schema-pinned payloads."""
from backend.core.ouroboros.governance.autonomy.saga_messages import (
    SagaMessage,
    SagaMessageType,
    MessagePriority,
)


def test_partial_promote_type_exists():
    assert hasattr(SagaMessageType, "SAGA_PARTIAL_PROMOTE")
    assert SagaMessageType.SAGA_PARTIAL_PROMOTE.value == "saga_partial_promote"


def test_target_moved_type_exists():
    assert hasattr(SagaMessageType, "TARGET_MOVED")
    assert SagaMessageType.TARGET_MOVED.value == "target_moved"


def test_ancestry_violation_type_exists():
    assert hasattr(SagaMessageType, "ANCESTRY_VIOLATION")
    assert SagaMessageType.ANCESTRY_VIOLATION.value == "ancestry_violation"


def test_schema_version_in_payload():
    msg = SagaMessage(
        message_type=SagaMessageType.SAGA_CREATED,
        saga_id="test-saga",
        payload={
            "schema_version": "1.0",
            "op_id": "op-001",
            "reason_code": "",
        },
    )
    assert msg.payload["schema_version"] == "1.0"
    assert msg.payload["op_id"] == "op-001"
    d = msg.to_dict()
    assert d["payload"]["schema_version"] == "1.0"

"""Tests for the Ouroboros 5-Phase Communication Protocol."""

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.operation_id import generate_operation_id


# ---------------------------------------------------------------------------
# TestMessageEmission
# ---------------------------------------------------------------------------


class TestMessageEmission:
    """Tests for CommProtocol message emission lifecycle."""

    @pytest.mark.asyncio
    async def test_emit_intent(self):
        """emit_intent produces 1 message with type=INTENT and seq=1."""
        transport = LogTransport()
        proto = CommProtocol(transports=[transport])
        op_id = generate_operation_id()

        await proto.emit_intent(
            op_id=op_id,
            goal="Refactor foo.py",
            target_files=["backend/core/foo.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )

        assert len(transport.messages) == 1
        msg = transport.messages[0]
        assert msg.msg_type is MessageType.INTENT
        assert msg.op_id == op_id
        assert msg.seq == 1
        assert msg.causal_parent_seq is None
        assert msg.payload["goal"] == "Refactor foo.py"
        assert msg.payload["target_files"] == ["backend/core/foo.py"]
        assert msg.payload["risk_tier"] == "SAFE_AUTO"
        assert msg.payload["blast_radius"] == 1

    @pytest.mark.asyncio
    async def test_sequence_numbers_monotonic(self):
        """emit intent+plan+heartbeat yields seqs [1,2,3] with correct causal parents."""
        transport = LogTransport()
        proto = CommProtocol(transports=[transport])
        op_id = generate_operation_id()

        await proto.emit_intent(
            op_id=op_id,
            goal="test",
            target_files=[],
            risk_tier="SAFE_AUTO",
            blast_radius=0,
        )
        await proto.emit_plan(
            op_id=op_id,
            steps=["step1", "step2"],
            rollback_strategy="revert commit",
        )
        await proto.emit_heartbeat(
            op_id=op_id,
            phase="PLANNING",
            progress_pct=50.0,
        )

        assert len(transport.messages) == 3
        seqs = [m.seq for m in transport.messages]
        assert seqs == [1, 2, 3]

        # causal_parent_seq: msg[0] has None, msg[1] points to 1, msg[2] points to 2
        assert transport.messages[0].causal_parent_seq is None
        assert transport.messages[1].causal_parent_seq == 1
        assert transport.messages[2].causal_parent_seq == 2

    @pytest.mark.asyncio
    async def test_all_five_types_emitted(self):
        """Emitting all 5 phases produces the complete MessageType sequence."""
        transport = LogTransport()
        proto = CommProtocol(transports=[transport])
        op_id = generate_operation_id()

        await proto.emit_intent(
            op_id=op_id,
            goal="full lifecycle",
            target_files=["a.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
        await proto.emit_plan(
            op_id=op_id,
            steps=["s1"],
            rollback_strategy="none",
        )
        await proto.emit_heartbeat(
            op_id=op_id,
            phase="EXECUTING",
            progress_pct=75.0,
        )
        await proto.emit_decision(
            op_id=op_id,
            outcome="APPROVED",
            reason_code="SAFE_AUTO",
            diff_summary="+1 line",
        )
        await proto.emit_postmortem(
            op_id=op_id,
            root_cause="none — success",
            failed_phase=None,
            next_safe_action="continue",
        )

        types = [m.msg_type for m in transport.messages]
        assert types == [
            MessageType.INTENT,
            MessageType.PLAN,
            MessageType.HEARTBEAT,
            MessageType.DECISION,
            MessageType.POSTMORTEM,
        ]

    @pytest.mark.asyncio
    async def test_emit_decision_preserves_extra_metadata(self):
        """DECISION payload should retain extra route-aware telemetry fields."""
        transport = LogTransport()
        proto = CommProtocol(transports=[transport])
        op_id = generate_operation_id()

        await proto.emit_decision(
            op_id=op_id,
            outcome="immediate",
            reason_code="urgency_route:test_failure",
            route="immediate",
            route_reason="critical_urgency:test_failure",
            budget_profile="120s fast path",
            details={"route": "immediate", "route_description": "Claude direct"},
        )

        msg = transport.messages[0]
        assert msg.msg_type is MessageType.DECISION
        assert msg.payload["route"] == "immediate"
        assert msg.payload["route_reason"] == "critical_urgency:test_failure"
        assert msg.payload["budget_profile"] == "120s fast path"
        assert msg.payload["details"]["route_description"] == "Claude direct"


# ---------------------------------------------------------------------------
# TestTransportFaultIsolation
# ---------------------------------------------------------------------------


class _FailingTransport:
    """Transport that always raises ConnectionError on send."""

    async def send(self, msg: CommMessage) -> None:
        raise ConnectionError("simulated transport failure")


class TestTransportFaultIsolation:
    """Tests for fault isolation: a broken transport must not block the pipeline."""

    @pytest.mark.asyncio
    async def test_transport_failure_does_not_block(self):
        """FailingTransport + LogTransport: pipeline does not raise, LogTransport still gets the message."""
        failing = _FailingTransport()
        healthy = LogTransport()
        proto = CommProtocol(transports=[failing, healthy])
        op_id = generate_operation_id()

        # Must NOT raise, despite the failing transport
        await proto.emit_intent(
            op_id=op_id,
            goal="fault isolation test",
            target_files=[],
            risk_tier="SAFE_AUTO",
            blast_radius=0,
        )

        # Healthy transport still received the message
        assert len(healthy.messages) == 1
        assert healthy.messages[0].msg_type is MessageType.INTENT
        assert healthy.messages[0].op_id == op_id

# tests/test_ouroboros_governance/test_event_bridge.py
"""Tests for the governance-to-cross-repo event bridge."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.core.ouroboros.governance.event_bridge import (
    EventBridge,
    GovernanceEventMapper,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    MessageType,
)


@pytest.fixture
def mock_event_bus():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def bridge(mock_event_bus):
    return EventBridge(event_bus=mock_event_bus)


class TestGovernanceEventMapper:
    def test_intent_maps_to_improvement_request(self):
        """INTENT message maps to IMPROVEMENT_REQUEST event type."""
        msg = CommMessage(
            op_id="op-test-001",
            msg_type=MessageType.INTENT,
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "fix bug", "target_files": ["foo.py"]},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_REQUEST"
        assert event.payload["op_id"] == "op-test-001"
        assert event.payload["goal"] == "fix bug"

    def test_decision_applied_maps_to_improvement_complete(self):
        """DECISION with outcome=applied maps to IMPROVEMENT_COMPLETE."""
        msg = CommMessage(
            op_id="op-test-002",
            msg_type=MessageType.DECISION,
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied", "reason_code": "safe_auto_passed"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_COMPLETE"

    def test_decision_blocked_maps_to_improvement_failed(self):
        """DECISION with outcome=blocked maps to IMPROVEMENT_FAILED."""
        msg = CommMessage(
            op_id="op-test-003",
            msg_type=MessageType.DECISION,
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "blocked", "reason_code": "touches_supervisor"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_FAILED"

    def test_postmortem_maps_to_improvement_failed(self):
        """POSTMORTEM always maps to IMPROVEMENT_FAILED."""
        msg = CommMessage(
            op_id="op-test-004",
            msg_type=MessageType.POSTMORTEM,
            seq=5,
            causal_parent_seq=4,
            payload={"root_cause": "syntax_error", "failed_phase": "VALIDATE"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is not None
        assert event.type.name == "IMPROVEMENT_FAILED"

    def test_heartbeat_not_bridged(self):
        """HEARTBEAT messages are not bridged (too noisy)."""
        msg = CommMessage(
            op_id="op-test-005",
            msg_type=MessageType.HEARTBEAT,
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate", "progress_pct": 50.0},
        )
        event = GovernanceEventMapper.map(msg)
        assert event is None

    def test_source_repo_is_jarvis(self):
        """All bridged events have source_repo=JARVIS."""
        msg = CommMessage(
            op_id="op-test-006",
            msg_type=MessageType.INTENT,
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "test"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.source_repo.name == "JARVIS"

    def test_op_id_preserved_in_payload(self):
        """op_id is always in the event payload for correlation."""
        msg = CommMessage(
            op_id="op-test-007",
            msg_type=MessageType.DECISION,
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied"},
        )
        event = GovernanceEventMapper.map(msg)
        assert event.payload["op_id"] == "op-test-007"


class TestEventBridge:
    @pytest.mark.asyncio
    async def test_bridge_publishes_mapped_event(self, bridge, mock_event_bus):
        """Bridge publishes mapped events to the event bus."""
        msg = CommMessage(
            op_id="op-test-010",
            msg_type=MessageType.INTENT,
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "fix bug"},
        )
        await bridge.forward(msg)
        mock_event_bus.emit.assert_called_once()
        event = mock_event_bus.emit.call_args[0][0]
        assert event.payload["op_id"] == "op-test-010"

    @pytest.mark.asyncio
    async def test_bridge_skips_unmapped_messages(self, bridge, mock_event_bus):
        """Bridge does not publish for unmapped message types."""
        msg = CommMessage(
            op_id="op-test-011",
            msg_type=MessageType.HEARTBEAT,
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate"},
        )
        await bridge.forward(msg)
        mock_event_bus.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_fault_isolation(self, bridge, mock_event_bus):
        """Event bus failure does not propagate to caller."""
        mock_event_bus.emit.side_effect = RuntimeError("bus down")
        msg = CommMessage(
            op_id="op-test-012",
            msg_type=MessageType.INTENT,
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "fix bug"},
        )
        # Should not raise
        await bridge.forward(msg)

    @pytest.mark.asyncio
    async def test_bridge_as_comm_transport(self, bridge, mock_event_bus):
        """Bridge can be used as a CommProtocol transport callback."""
        msg = CommMessage(
            op_id="op-test-013",
            msg_type=MessageType.DECISION,
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied"},
        )
        await bridge.send(msg)
        mock_event_bus.emit.assert_called_once()

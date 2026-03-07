"""Tests for the TUI transport layer."""

import pytest
from unittest.mock import AsyncMock

from backend.core.ouroboros.governance.tui_transport import (
    TUITransport,
    TUIMessageFormatter,
)
from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    MessageType,
)


@pytest.fixture
def transport():
    return TUITransport()


@pytest.fixture
def mock_callback():
    return AsyncMock()


class TestTUITransport:
    @pytest.mark.asyncio
    async def test_send_stores_message(self, transport):
        """Sent messages are stored in the transport's message queue."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-test-1",
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "test"},
        )
        await transport.send(msg)
        assert len(transport.messages) == 1
        assert transport.messages[0].op_id == "op-test-1"

    @pytest.mark.asyncio
    async def test_callback_invoked_on_send(self, transport, mock_callback):
        """Registered callbacks are invoked when a message is sent."""
        transport.on_message(mock_callback)
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-test-2",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate", "progress_pct": 40.0},
        )
        await transport.send(msg)
        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0]
        assert "op_id" in call_args[0]

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_block(self, transport):
        """A failing callback does not prevent message storage."""
        failing_cb = AsyncMock(side_effect=RuntimeError("TUI crashed"))
        transport.on_message(failing_cb)

        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-test-3",
            seq=4,
            causal_parent_seq=3,
            payload={"outcome": "applied"},
        )
        await transport.send(msg)
        # Message still stored despite callback failure
        assert len(transport.messages) == 1

    @pytest.mark.asyncio
    async def test_message_queue_for_offline_tui(self, transport):
        """Messages queue when no callback is registered (TUI offline)."""
        for i in range(5):
            msg = CommMessage(
                msg_type=MessageType.HEARTBEAT,
                op_id="op-test-q",
                seq=i + 1,
                causal_parent_seq=i if i > 0 else None,
                payload={"phase": "test"},
            )
            await transport.send(msg)
        assert len(transport.messages) == 5

    @pytest.mark.asyncio
    async def test_drain_delivers_queued_messages(self, transport):
        """drain() delivers all queued messages to a newly registered callback."""
        for i in range(3):
            await transport.send(
                CommMessage(
                    msg_type=MessageType.HEARTBEAT,
                    op_id="op-drain",
                    seq=i + 1,
                    causal_parent_seq=i if i > 0 else None,
                    payload={"phase": "test"},
                )
            )

        delivered = []
        async def capture(formatted):
            delivered.append(formatted)

        transport.on_message(capture)
        await transport.drain()
        assert len(delivered) == 3

    @pytest.mark.asyncio
    async def test_drain_clears_pending_queue(self, transport):
        """drain() clears the pending queue after delivery."""
        await transport.send(
            CommMessage(
                msg_type=MessageType.INTENT,
                op_id="op-drain-clear",
                seq=1,
                causal_parent_seq=None,
                payload={"goal": "clear test"},
            )
        )

        cb = AsyncMock()
        transport.on_message(cb)
        await transport.drain()
        # First drain delivers
        assert cb.call_count == 1
        # Second drain has nothing to deliver
        await transport.drain()
        assert cb.call_count == 1

    @pytest.mark.asyncio
    async def test_drain_noop_without_callbacks(self, transport):
        """drain() does nothing if no callbacks are registered."""
        await transport.send(
            CommMessage(
                msg_type=MessageType.INTENT,
                op_id="op-noop",
                seq=1,
                causal_parent_seq=None,
                payload={"goal": "noop"},
            )
        )
        # No callbacks registered — drain should be a no-op
        await transport.drain()
        # Pending messages are still there (not lost)
        assert len(transport._pending_drain) == 1

    @pytest.mark.asyncio
    async def test_multiple_callbacks_all_invoked(self, transport):
        """All registered callbacks receive the formatted message."""
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        transport.on_message(cb1)
        transport.on_message(cb2)

        await transport.send(
            CommMessage(
                msg_type=MessageType.PLAN,
                op_id="op-multi",
                seq=2,
                causal_parent_seq=1,
                payload={"steps": ["a", "b"]},
            )
        )
        cb1.assert_called_once()
        cb2.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_callback_failure_does_not_block_others(self, transport):
        """If one callback fails, the remaining callbacks still get called."""
        failing_cb = AsyncMock(side_effect=RuntimeError("boom"))
        healthy_cb = AsyncMock()
        transport.on_message(failing_cb)
        transport.on_message(healthy_cb)

        await transport.send(
            CommMessage(
                msg_type=MessageType.INTENT,
                op_id="op-isolation",
                seq=1,
                causal_parent_seq=None,
                payload={"goal": "isolation test"},
            )
        )
        failing_cb.assert_called_once()
        healthy_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_pending_when_callback_registered(self, transport):
        """When callbacks are registered, messages are not added to pending drain."""
        cb = AsyncMock()
        transport.on_message(cb)

        await transport.send(
            CommMessage(
                msg_type=MessageType.INTENT,
                op_id="op-no-pending",
                seq=1,
                causal_parent_seq=None,
                payload={"goal": "no pending"},
            )
        )
        assert len(transport._pending_drain) == 0


class TestTUIMessageFormatter:
    def test_format_intent(self):
        """INTENT messages are formatted with goal and risk tier."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-fmt-1",
            seq=1,
            causal_parent_seq=None,
            payload={
                "goal": "Add docstring to utils.py",
                "risk_tier": "SAFE_AUTO",
                "blast_radius": 2,
                "target_files": ["utils.py"],
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "INTENT"
        assert "goal" in formatted
        assert formatted["risk_tier"] == "SAFE_AUTO"

    def test_format_heartbeat(self):
        """HEARTBEAT messages include phase and progress."""
        msg = CommMessage(
            msg_type=MessageType.HEARTBEAT,
            op_id="op-fmt-2",
            seq=3,
            causal_parent_seq=2,
            payload={"phase": "validate", "progress_pct": 65.0},
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "HEARTBEAT"
        assert formatted["progress_pct"] == 65.0

    def test_format_decision(self):
        """DECISION messages include outcome and reason."""
        msg = CommMessage(
            msg_type=MessageType.DECISION,
            op_id="op-fmt-3",
            seq=4,
            causal_parent_seq=3,
            payload={
                "outcome": "applied",
                "reason_code": "safe_auto_passed",
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "DECISION"
        assert formatted["outcome"] == "applied"

    def test_format_postmortem(self):
        """POSTMORTEM messages include root cause and next action."""
        msg = CommMessage(
            msg_type=MessageType.POSTMORTEM,
            op_id="op-fmt-4",
            seq=5,
            causal_parent_seq=4,
            payload={
                "root_cause": "syntax_error",
                "failed_phase": "VALIDATE",
                "next_safe_action": "review_code",
            },
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "POSTMORTEM"
        assert formatted["root_cause"] == "syntax_error"

    def test_format_includes_base_fields(self):
        """Formatted dict always includes type, op_id, seq, causal_parent_seq, timestamp."""
        msg = CommMessage(
            msg_type=MessageType.PLAN,
            op_id="op-base-fields",
            seq=2,
            causal_parent_seq=1,
            payload={"steps": ["step1"]},
            timestamp=1234567890.0,
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["type"] == "PLAN"
        assert formatted["op_id"] == "op-base-fields"
        assert formatted["seq"] == 2
        assert formatted["causal_parent_seq"] == 1
        assert formatted["timestamp"] == 1234567890.0
        assert formatted["steps"] == ["step1"]

    def test_format_payload_fields_merged(self):
        """All payload fields are merged into the base dict."""
        msg = CommMessage(
            msg_type=MessageType.INTENT,
            op_id="op-merge",
            seq=1,
            causal_parent_seq=None,
            payload={"goal": "test", "custom_field": 42, "nested": {"a": 1}},
        )
        formatted = TUIMessageFormatter.format(msg)
        assert formatted["goal"] == "test"
        assert formatted["custom_field"] == 42
        assert formatted["nested"] == {"a": 1}


class TestCommProtocolIntegration:
    @pytest.mark.asyncio
    async def test_tui_transport_works_with_comm_protocol(self):
        """TUITransport integrates cleanly as a CommProtocol transport."""
        tui = TUITransport()
        received = []
        tui.on_message(AsyncMock(side_effect=lambda m: received.append(m)))

        comm = CommProtocol(transports=[tui])
        await comm.emit_intent(
            op_id="op-integration",
            goal="test",
            target_files=["test.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
        assert len(tui.messages) == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_tui_transport_alongside_log_transport(self):
        """TUITransport works alongside LogTransport in CommProtocol."""
        from backend.core.ouroboros.governance.comm_protocol import LogTransport

        log = LogTransport()
        tui = TUITransport()
        cb = AsyncMock()
        tui.on_message(cb)

        comm = CommProtocol(transports=[log, tui])
        await comm.emit_intent(
            op_id="op-dual",
            goal="dual transport test",
            target_files=[],
            risk_tier="SAFE_AUTO",
            blast_radius=0,
        )

        assert len(log.messages) == 1
        assert len(tui.messages) == 1
        cb.assert_called_once()

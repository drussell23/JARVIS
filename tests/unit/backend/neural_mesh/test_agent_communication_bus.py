"""Tests for the AgentCommunicationBus, AgentMessage, and related components.

Covers:
- AgentMessage dataclass (IDs, broadcast, expiry, create_response)
- AgentCommunicationBus lifecycle, pub/sub, request/response, metrics
- Per-agent circuit breaker behavior
- Deadletter queue for undeliverable messages
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend.neural_mesh.communication.agent_communication_bus import (
    AgentCommunicationBus,
    BusMetrics,
    _AgentCircuitBreaker,
)
from backend.neural_mesh.config import CommunicationBusConfig
from backend.neural_mesh.data_models import (
    AgentMessage,
    MessageCallback,
    MessagePriority,
    MessageType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> CommunicationBusConfig:
    """Create a CommunicationBusConfig with small queues for fast tests."""
    defaults = {
        "queue_sizes": {0: 100, 1: 100, 2: 100, 3: 100},
        "message_history_size": 200,
        "handler_timeout_seconds": 2.0,
    }
    defaults.update(overrides)
    return CommunicationBusConfig(**defaults)


def _make_message(
    from_agent: str = "sender",
    to_agent: str = "receiver",
    message_type: MessageType = MessageType.TASK_ASSIGNED,
    priority: MessagePriority = MessagePriority.NORMAL,
    payload: dict | None = None,
    expires_at: datetime | None = None,
) -> AgentMessage:
    return AgentMessage(
        from_agent=from_agent,
        to_agent=to_agent,
        message_type=message_type,
        priority=priority,
        payload=payload or {},
        expires_at=expires_at,
    )


@pytest.fixture
async def bus():
    """Provide a started AgentCommunicationBus; stops it after the test."""
    b = AgentCommunicationBus(config=_make_config())
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


@pytest.fixture
def stopped_bus():
    """Provide an AgentCommunicationBus that has NOT been started."""
    return AgentCommunicationBus(config=_make_config())


# =========================================================================
# TestAgentMessage
# =========================================================================


class TestAgentMessage:
    """Tests for the AgentMessage dataclass."""

    def test_auto_generated_ids(self):
        """message_id and correlation_id are auto-generated UUID strings."""
        msg = AgentMessage(from_agent="a", to_agent="b")
        assert isinstance(msg.message_id, str)
        assert len(msg.message_id) == 36  # UUID4 string length with hyphens
        assert isinstance(msg.correlation_id, str)
        assert len(msg.correlation_id) == 36
        # By default __post_init__ sets correlation_id = message_id
        assert msg.correlation_id == msg.message_id

    @pytest.mark.parametrize("target", ["broadcast", "*", "all"])
    def test_is_broadcast_targets(self, target: str):
        """'broadcast', '*', and 'all' as to_agent produce is_broadcast=True."""
        msg = AgentMessage(from_agent="sender", to_agent=target)
        assert msg.is_broadcast() is True

    def test_is_not_broadcast_specific(self):
        """A specific agent name means is_broadcast=False."""
        msg = AgentMessage(from_agent="sender", to_agent="vision_agent")
        assert msg.is_broadcast() is False

    def test_is_expired_past(self):
        """expires_at in the past makes is_expired() True."""
        past = datetime.now() - timedelta(seconds=10)
        msg = AgentMessage(from_agent="a", to_agent="b", expires_at=past)
        assert msg.is_expired() is True

    def test_is_not_expired_future(self):
        """expires_at in the future keeps is_expired() False."""
        future = datetime.now() + timedelta(hours=1)
        msg = AgentMessage(from_agent="a", to_agent="b", expires_at=future)
        assert msg.is_expired() is False

    def test_create_response(self):
        """create_response produces a message with correct routing and correlation."""
        original = AgentMessage(
            from_agent="requester",
            to_agent="responder",
            message_type=MessageType.REQUEST,
            payload={"q": "ping"},
        )
        response = original.create_response(
            from_agent="responder",
            payload={"a": "pong"},
        )
        assert response.from_agent == "responder"
        assert response.to_agent == "requester"
        assert response.correlation_id == original.correlation_id
        assert response.reply_to == original.message_id
        assert response.message_type == MessageType.RESPONSE
        assert response.payload == {"a": "pong"}


# =========================================================================
# TestAgentCommunicationBus
# =========================================================================


class TestAgentCommunicationBus:
    """Tests for the AgentCommunicationBus core functionality."""

    async def test_start_creates_processor_tasks(self, bus: AgentCommunicationBus):
        """After start(), processor tasks exist (one per priority level + cleanup + deadletter)."""
        # 4 priority processors + 1 cleanup + 1 deadletter = 6 tasks total
        assert len(bus._processor_tasks) == len(MessagePriority)
        assert bus._cleanup_task is not None
        assert bus._deadletter_task is not None
        assert bus._running is True

    async def test_start_idempotent(self, bus: AgentCommunicationBus):
        """Calling start() twice does not crash or duplicate tasks."""
        original_task_count = len(bus._processor_tasks)
        await bus.start()  # second start
        assert len(bus._processor_tasks) == original_task_count
        assert bus._running is True

    async def test_stop_cancels_tasks(self):
        """After stop(), processor tasks are cancelled and cleared."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()
        assert b._running is True

        await b.stop()
        assert b._running is False
        assert len(b._processor_tasks) == 0
        assert b._cleanup_task is None
        assert b._deadletter_task is None

    async def test_publish_when_not_running(self, stopped_bus: AgentCommunicationBus):
        """Publishing before start() raises RuntimeError."""
        msg = _make_message()
        with pytest.raises(RuntimeError, match="not running"):
            await stopped_bus.publish(msg)

    async def test_directed_delivery(self, bus: AgentCommunicationBus):
        """A message directed to a specific agent is delivered to that agent's handler."""
        handler = AsyncMock()
        await bus.subscribe("receiver", MessageType.TASK_ASSIGNED, handler)

        msg = _make_message(from_agent="sender", to_agent="receiver")
        await bus.publish(msg)

        # Allow processor to deliver
        await asyncio.sleep(0.15)

        handler.assert_called_once()
        delivered_msg = handler.call_args[0][0]
        assert delivered_msg.message_id == msg.message_id

    async def test_broadcast_delivery(self, bus: AgentCommunicationBus):
        """A broadcast message reaches all subscribers (subscribed to that type)."""
        handler_a = AsyncMock()
        handler_b = AsyncMock()
        await bus.subscribe("agent_a", MessageType.ANNOUNCEMENT, handler_a)
        await bus.subscribe("agent_b", MessageType.ANNOUNCEMENT, handler_b)

        msg = _make_message(
            from_agent="orchestrator",
            to_agent="broadcast",
            message_type=MessageType.ANNOUNCEMENT,
        )
        await bus.publish(msg)
        await asyncio.sleep(0.15)

        handler_a.assert_called_once()
        handler_b.assert_called_once()

    async def test_request_response(self, bus: AgentCommunicationBus):
        """request() returns the response payload matched by correlation_id."""

        async def responder_handler(message: AgentMessage) -> None:
            """Simulate an agent responding to a request."""
            response = message.create_response(
                from_agent="responder",
                payload={"answer": 42},
            )
            await bus.publish(response)

        await bus.subscribe("responder", MessageType.REQUEST, responder_handler)

        req = _make_message(
            from_agent="requester",
            to_agent="responder",
            message_type=MessageType.REQUEST,
            payload={"question": "meaning"},
        )

        result = await bus.request(req, timeout=2.0)
        assert result == {"answer": 42}

    async def test_request_timeout(self, bus: AgentCommunicationBus):
        """request() with short timeout and no responder raises TimeoutError."""
        # Subscribe a handler that does nothing (no response published)
        await bus.subscribe("silent_agent", MessageType.REQUEST, AsyncMock())

        req = _make_message(
            from_agent="requester",
            to_agent="silent_agent",
            message_type=MessageType.REQUEST,
        )
        with pytest.raises(asyncio.TimeoutError):
            await bus.request(req, timeout=0.2)

    async def test_message_history(self, bus: AgentCommunicationBus):
        """get_message_history returns previously published messages."""
        msg1 = _make_message(from_agent="a", to_agent="b")
        msg2 = _make_message(from_agent="c", to_agent="d")
        await bus.publish(msg1)
        await bus.publish(msg2)

        history = bus.get_message_history()
        ids = [m.message_id for m in history]
        assert msg1.message_id in ids
        assert msg2.message_id in ids

    async def test_expired_message_discarded(self, bus: AgentCommunicationBus):
        """An expired message is not delivered to the subscriber."""
        handler = AsyncMock()
        await bus.subscribe("receiver", MessageType.TASK_ASSIGNED, handler)

        past = datetime.now() - timedelta(seconds=60)
        msg = _make_message(from_agent="sender", to_agent="receiver", expires_at=past)
        await bus.publish(msg)
        await asyncio.sleep(0.15)

        handler.assert_not_called()
        assert bus._metrics.messages_expired >= 1

    async def test_metrics_reported(self, bus: AgentCommunicationBus):
        """get_metrics() returns a BusMetrics with queue_depths populated."""
        metrics = bus.get_metrics()
        assert isinstance(metrics, BusMetrics)
        assert isinstance(metrics.queue_depths, dict)
        # Should have an entry for each priority level
        assert len(metrics.queue_depths) == len(MessagePriority)

    async def test_subscribe_registers_handler(self, bus: AgentCommunicationBus):
        """subscribe() adds the agent to the internal subscription map."""
        handler = AsyncMock()
        await bus.subscribe("my_agent", MessageType.TASK_ASSIGNED, handler)

        assert "my_agent" in bus._subscriptions
        assert MessageType.TASK_ASSIGNED in bus._subscriptions["my_agent"]
        assert handler in bus._subscriptions["my_agent"][MessageType.TASK_ASSIGNED]

    async def test_unsubscribe_removes_handler(self, bus: AgentCommunicationBus):
        """unsubscribe() removes the agent from subscriptions."""
        handler = AsyncMock()
        await bus.subscribe("my_agent", MessageType.TASK_ASSIGNED, handler)
        await bus.unsubscribe("my_agent")

        assert "my_agent" not in bus._subscriptions

    async def test_publish_directed_not_to_others(self, bus: AgentCommunicationBus):
        """A directed message only goes to the target agent, not other subscribers."""
        target_handler = AsyncMock()
        other_handler = AsyncMock()
        await bus.subscribe("target", MessageType.TASK_ASSIGNED, target_handler)
        await bus.subscribe("other", MessageType.TASK_ASSIGNED, other_handler)

        msg = _make_message(from_agent="sender", to_agent="target")
        await bus.publish(msg)
        await asyncio.sleep(0.15)

        target_handler.assert_called_once()
        other_handler.assert_not_called()

    async def test_stop_resolves_pending_futures(self):
        """Pending request futures are cancelled when the bus stops."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()

        # Subscribe a do-nothing handler so publish doesn't deadletter
        await b.subscribe("slow_agent", MessageType.REQUEST, AsyncMock())

        req = _make_message(
            from_agent="requester",
            to_agent="slow_agent",
            message_type=MessageType.REQUEST,
        )

        # Start a request that will never get a response
        request_task = asyncio.create_task(b.request(req, timeout=10.0))
        await asyncio.sleep(0.05)  # let the request register

        # Stop the bus -- should cancel pending futures
        await b.stop()

        # The request task should raise (CancelledError or TimeoutError)
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await request_task

    async def test_multiple_messages_ordered(self, bus: AgentCommunicationBus):
        """Messages are delivered in order within the same priority level."""
        received: list[str] = []

        async def ordered_handler(message: AgentMessage) -> None:
            received.append(message.payload.get("seq", ""))

        await bus.subscribe("receiver", MessageType.TASK_ASSIGNED, ordered_handler)

        for i in range(5):
            msg = _make_message(
                from_agent="sender",
                to_agent="receiver",
                payload={"seq": str(i)},
            )
            await bus.publish(msg)

        await asyncio.sleep(0.3)

        assert received == ["0", "1", "2", "3", "4"]


# =========================================================================
# TestCircuitBreaker
# =========================================================================


class TestCircuitBreaker:
    """Tests for the per-agent circuit breaker mechanism."""

    async def test_opens_after_threshold(self):
        """After N failures, the circuit breaker opens."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()
        try:
            agent_name = "flaky_agent"

            # Record failures up to the default threshold (5)
            for _ in range(5):
                b._record_circuit_failure(agent_name)

            cb = b._circuit_breakers[agent_name]
            assert cb.failures >= cb.threshold
            assert cb.open_until is not None
            assert cb.open_until > time.monotonic()
        finally:
            await b.stop()

    async def test_open_circuit_skips_delivery(self):
        """When a circuit breaker is open, messages are not delivered to the agent."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()
        try:
            handler = AsyncMock()
            await b.subscribe("broken_agent", MessageType.TASK_ASSIGNED, handler)

            # Force the circuit open
            cb = _AgentCircuitBreaker(
                failures=10,
                threshold=5,
                open_until=time.monotonic() + 60.0,  # open for 60s
            )
            b._circuit_breakers["broken_agent"] = cb

            msg = _make_message(from_agent="sender", to_agent="broken_agent")
            await b.publish(msg)
            await asyncio.sleep(0.15)

            # The handler should NOT have been called because circuit is open
            handler.assert_not_called()
        finally:
            await b.stop()

    async def test_half_open_after_recovery(self):
        """After recovery time elapses, the circuit enters half-open and allows a retry."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()
        try:
            handler = AsyncMock()
            await b.subscribe("recovering_agent", MessageType.TASK_ASSIGNED, handler)

            # Set circuit open_until in the past (recovery time elapsed)
            cb = _AgentCircuitBreaker(
                failures=5,
                threshold=5,
                open_until=time.monotonic() - 1.0,  # expired 1s ago
            )
            b._circuit_breakers["recovering_agent"] = cb

            msg = _make_message(from_agent="sender", to_agent="recovering_agent")
            await b.publish(msg)
            await asyncio.sleep(0.15)

            # Handler should be called (half-open allows one attempt)
            handler.assert_called_once()
            # After success, failures should be reset
            assert b._circuit_breakers["recovering_agent"].failures == 0
            assert b._circuit_breakers["recovering_agent"].open_until is None
        finally:
            await b.stop()

    async def test_success_resets_count(self):
        """A successful delivery resets the failure count to zero."""
        b = AgentCommunicationBus(config=_make_config())
        await b.start()
        try:
            handler = AsyncMock()
            await b.subscribe("agent", MessageType.TASK_ASSIGNED, handler)

            # Pre-seed some failures (below threshold so circuit stays closed)
            cb = _AgentCircuitBreaker(failures=3, threshold=5, open_until=None)
            b._circuit_breakers["agent"] = cb

            msg = _make_message(from_agent="sender", to_agent="agent")
            await bus_publish_and_wait(b, msg)

            # After successful delivery, failures should be reset
            assert b._circuit_breakers["agent"].failures == 0
        finally:
            await b.stop()


async def bus_publish_and_wait(bus: AgentCommunicationBus, msg: AgentMessage) -> None:
    """Publish a message and wait briefly for processing."""
    await bus.publish(msg)
    await asyncio.sleep(0.15)


# =========================================================================
# TestDeadletterQueue
# =========================================================================


class TestDeadletterQueue:
    """Tests for the deadletter queue for undeliverable messages."""

    async def test_no_subscriber_deadletter(self, bus: AgentCommunicationBus):
        """A message sent to an unknown agent ends up in the deadletter queue."""
        msg = _make_message(from_agent="sender", to_agent="nonexistent_agent")
        await bus.publish(msg)
        await asyncio.sleep(0.15)

        dead = await bus.drain_deadletter()
        assert len(dead) >= 1
        dead_ids = [m.message_id for m in dead]
        assert msg.message_id in dead_ids

    async def test_drain_deadletter(self, bus: AgentCommunicationBus):
        """drain_deadletter returns accumulated messages and clears the queue."""
        for i in range(3):
            msg = _make_message(
                from_agent="sender",
                to_agent=f"ghost_{i}",
                payload={"idx": i},
            )
            await bus.publish(msg)

        await asyncio.sleep(0.2)

        dead = await bus.drain_deadletter()
        assert len(dead) == 3

        # Second drain should be empty
        dead_again = await bus.drain_deadletter()
        assert len(dead_again) == 0

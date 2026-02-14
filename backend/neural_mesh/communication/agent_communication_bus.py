"""
JARVIS Neural Mesh - Agent Communication Bus

Ultra-fast async message passing between agents with:
- Priority queues (CRITICAL < 1ms, HIGH < 5ms, NORMAL < 10ms, LOW < 50ms)
- Pub/sub pattern for topic-based messaging
- Request/response with correlation IDs
- Message persistence for debugging and recovery
- Broadcast and directed messages
- Backpressure handling
- Comprehensive metrics

Performance Target: 10,000 messages/sec with <5ms p99 latency
Memory Footprint: ~10MB base + 1KB per message in history
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from ..data_models import (
    AgentMessage,
    MessageCallback,
    MessagePriority,
    MessageType,
)
from ..config import CommunicationBusConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class BusMetrics:
    """Metrics for the communication bus."""

    messages_published: int = 0
    messages_delivered: int = 0
    messages_dropped: int = 0
    messages_expired: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    queue_depths: Dict[int, int] = field(default_factory=dict)
    active_subscriptions: int = 0
    pending_responses: int = 0

    def average_latency_ms(self) -> float:
        """Calculate average message delivery latency."""
        if self.messages_delivered == 0:
            return 0.0
        return self.total_latency_ms / self.messages_delivered


@dataclass
class _AgentCircuitBreaker:
    """Per-agent circuit breaker for callback failure isolation."""
    failures: int = 0
    threshold: int = 5
    open_until: Optional[float] = None  # time.monotonic() timestamp
    recovery_seconds: float = 30.0


class AgentCommunicationBus:
    """
    Ultra-fast async message routing between agents.

    Features:
    - Priority-based message queues
    - Pub/sub for topic-based messaging
    - Request/response pattern with timeouts
    - Message history for debugging
    - Automatic cleanup of expired messages
    - Backpressure handling

    Usage:
        bus = AgentCommunicationBus()
        await bus.start()

        # Subscribe to messages
        await bus.subscribe("my_agent", MessageType.TASK_ASSIGNED, handle_task)

        # Publish a message
        await bus.publish(AgentMessage(
            from_agent="orchestrator",
            to_agent="my_agent",
            message_type=MessageType.TASK_ASSIGNED,
            payload={"task": "analyze_screen"},
        ))

        # Request/response
        response = await bus.request(
            AgentMessage(
                from_agent="my_agent",
                to_agent="vision_agent",
                message_type=MessageType.REQUEST,
                payload={"action": "capture"},
            ),
            timeout=5.0,
        )
    """

    def __init__(self, config: Optional[CommunicationBusConfig] = None) -> None:
        """Initialize the communication bus.

        Args:
            config: Bus configuration. Uses global config if not provided.
        """
        self.config = config or get_config().communication_bus

        # Priority queues (one per priority level)
        self._queues: Dict[MessagePriority, asyncio.Queue[AgentMessage]] = {}
        for priority in MessagePriority:
            max_size = self.config.queue_sizes.get(priority.value, 10000)
            self._queues[priority] = asyncio.Queue(maxsize=max_size)

        # Subscriptions: {agent_name: {message_type: [callbacks]}}
        self._subscriptions: Dict[str, Dict[MessageType, List[MessageCallback]]] = (
            defaultdict(lambda: defaultdict(list))
        )

        # Broadcast subscriptions: {message_type: [callbacks]}
        self._broadcast_subscriptions: Dict[MessageType, List[MessageCallback]] = (
            defaultdict(list)
        )

        # Pending request/response futures: {correlation_id: Future}
        self._pending_responses: Dict[str, asyncio.Future[Dict[str, Any]]] = {}

        # Message history for debugging
        self._message_history: deque[AgentMessage] = deque(
            maxlen=self.config.message_history_size
        )

        # Metrics
        self._metrics = BusMetrics()

        # Processing state
        self._running = False
        self._processor_tasks: List[asyncio.Task[None]] = []
        self._cleanup_task: Optional[asyncio.Task[None]] = None

        # Locks for thread safety
        self._subscription_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()

        # v238.0: Deadletter queue for undelivered messages
        self._deadletter: asyncio.Queue[AgentMessage] = asyncio.Queue(maxsize=1000)
        self._deadletter_count: int = 0
        self._deadletter_task: Optional[asyncio.Task[None]] = None

        # v238.0: Per-agent circuit breakers
        self._circuit_breakers: Dict[str, _AgentCircuitBreaker] = {}

        logger.info("AgentCommunicationBus initialized")

    async def start(self) -> None:
        """Start the message bus processors."""
        if self._running:
            logger.warning("Communication bus already running")
            return

        self._running = True

        # Start a processor for each priority level
        for priority in MessagePriority:
            task = asyncio.create_task(
                self._process_queue(priority),
                name=f"bus_processor_{priority.name}",
            )
            self._processor_tasks.append(task)

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(
            self._cleanup_expired(),
            name="bus_cleanup",
        )

        # v238.0: Start deadletter consumer
        self._deadletter_task = asyncio.create_task(
            self._periodic_deadletter_drain(),
            name="bus_deadletter_drain",
        )

        logger.info("AgentCommunicationBus started with %d processors", len(self._processor_tasks))

    async def stop(self) -> None:
        """Stop the message bus gracefully."""
        if (
            not self._running
            and not self._processor_tasks
            and self._cleanup_task is None
            and self._deadletter_task is None
        ):
            return

        self._running = False

        # Cancel all processor tasks
        for task in self._processor_tasks:
            task.cancel()

        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._deadletter_task:
            self._deadletter_task.cancel()

        # Wait for tasks to complete
        all_tasks = self._processor_tasks + [
            t for t in (self._cleanup_task, self._deadletter_task) if t
        ]
        if all_tasks:
            done, pending = await asyncio.wait(all_tasks, timeout=5.0)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        self._processor_tasks.clear()
        self._cleanup_task = None
        self._deadletter_task = None

        # Cancel pending responses
        async with self._response_lock:
            for future in self._pending_responses.values():
                if not future.done():
                    future.cancel()
            self._pending_responses.clear()

        logger.info("AgentCommunicationBus stopped")

    async def publish(
        self,
        message: AgentMessage,
        block: bool = False,
    ) -> str:
        """
        Publish a message to the bus.

        Args:
            message: The message to publish
            block: If True, wait for queue space. If False, raise if full.

        Returns:
            The message ID

        Raises:
            asyncio.QueueFull: If queue is full and block=False
        """
        if not self._running:
            raise RuntimeError("Communication bus is not running")

        # Add to history
        self._message_history.append(message)
        self._metrics.messages_published += 1

        queue = self._queues[message.priority]

        try:
            if block:
                await queue.put(message)
            else:
                queue.put_nowait(message)

            logger.debug(
                "Published message %s from %s to %s (type=%s, priority=%s)",
                message.message_id[:8],
                message.from_agent,
                message.to_agent,
                message.message_type.value,
                message.priority.name,
            )

        except asyncio.QueueFull:
            # v238.0: Backpressure retry — yield briefly to let consumers drain
            await asyncio.sleep(0.01)
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._metrics.messages_dropped += 1
                logger.warning(
                    "Queue full for priority %s after retry, dropping message %s",
                    message.priority.name,
                    message.message_id[:8],
                )
                # Route to deadletter instead of silent loss
                try:
                    self._deadletter.put_nowait(message)
                    self._deadletter_count += 1
                except asyncio.QueueFull:
                    pass  # Deadletter full — truly drop
                raise

        return message.message_id

    async def subscribe(
        self,
        agent_name: str,
        message_type: MessageType,
        callback: MessageCallback,
    ) -> None:
        """
        Subscribe an agent to a message type.

        Args:
            agent_name: Name of the subscribing agent
            message_type: Type of message to subscribe to
            callback: Async callback function to handle messages
        """
        async with self._subscription_lock:
            self._subscriptions[agent_name][message_type].append(callback)
            self._metrics.active_subscriptions += 1

        logger.debug(
            "Agent %s subscribed to %s",
            agent_name,
            message_type.value,
        )

    async def subscribe_broadcast(
        self,
        message_type: MessageType,
        callback: MessageCallback,
    ) -> None:
        """
        Subscribe to broadcast messages of a specific type.

        Args:
            message_type: Type of broadcast message to subscribe to
            callback: Async callback function to handle messages
        """
        async with self._subscription_lock:
            self._broadcast_subscriptions[message_type].append(callback)
            self._metrics.active_subscriptions += 1

        logger.debug("Broadcast subscription added for %s", message_type.value)

    async def unsubscribe(
        self,
        agent_name: str,
        message_type: Optional[MessageType] = None,
    ) -> None:
        """
        Unsubscribe an agent from messages.

        Args:
            agent_name: Name of the agent to unsubscribe
            message_type: Specific type to unsubscribe from, or None for all
        """
        async with self._subscription_lock:
            if agent_name in self._subscriptions:
                if message_type:
                    if message_type in self._subscriptions[agent_name]:
                        count = len(self._subscriptions[agent_name][message_type])
                        del self._subscriptions[agent_name][message_type]
                        self._metrics.active_subscriptions -= count
                else:
                    count = sum(
                        len(callbacks)
                        for callbacks in self._subscriptions[agent_name].values()
                    )
                    del self._subscriptions[agent_name]
                    self._metrics.active_subscriptions -= count

        logger.debug(
            "Agent %s unsubscribed from %s",
            agent_name,
            message_type.value if message_type else "all",
        )

    async def request(
        self,
        message: AgentMessage,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Send a request and wait for a response.

        Args:
            message: Request message (must have correlation_id set)
            timeout: Maximum time to wait for response

        Returns:
            Response payload

        Raises:
            asyncio.TimeoutError: If no response within timeout
            RuntimeError: If bus is not running
        """
        if not self._running:
            raise RuntimeError("Communication bus is not running")

        # Ensure correlation ID is set
        if not message.correlation_id:
            message.correlation_id = message.message_id

        # Create future for response
        future: asyncio.Future[Dict[str, Any]] = asyncio.Future()

        async with self._response_lock:
            self._pending_responses[message.correlation_id] = future
            self._metrics.pending_responses = len(self._pending_responses)

        try:
            # Publish the request
            await self.publish(message)

            # Wait for response
            response = await asyncio.wait_for(future, timeout=timeout)
            return response

        except asyncio.TimeoutError:
            logger.warning(
                "Request %s timed out after %.1fs",
                message.message_id[:8],
                timeout,
            )
            raise

        finally:
            # Cleanup
            async with self._response_lock:
                self._pending_responses.pop(message.correlation_id, None)
                self._metrics.pending_responses = len(self._pending_responses)

    async def respond(
        self,
        original_message: AgentMessage,
        payload: Dict[str, Any],
        from_agent: str,
    ) -> str:
        """
        Send a response to a request message.

        Args:
            original_message: The request message being responded to
            payload: Response data
            from_agent: Name of the responding agent

        Returns:
            Response message ID
        """
        response = original_message.create_response(
            from_agent=from_agent,
            payload=payload,
        )
        return await self.publish(response)

    async def broadcast(
        self,
        from_agent: str,
        message_type: MessageType,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> str:
        """
        Broadcast a message to all subscribed agents.

        Args:
            from_agent: Name of the sending agent
            message_type: Type of message
            payload: Message data
            priority: Message priority

        Returns:
            Message ID
        """
        message = AgentMessage(
            from_agent=from_agent,
            to_agent="broadcast",
            message_type=message_type,
            payload=payload,
            priority=priority,
        )
        return await self.publish(message)

    def get_metrics(self) -> BusMetrics:
        """Get current bus metrics."""
        # Update queue depths
        self._metrics.queue_depths = {
            priority.value: queue.qsize()
            for priority, queue in self._queues.items()
        }
        return self._metrics

    def get_message_history(
        self,
        limit: int = 100,
        agent_filter: Optional[str] = None,
        type_filter: Optional[MessageType] = None,
    ) -> List[AgentMessage]:
        """
        Get recent message history.

        Args:
            limit: Maximum number of messages to return
            agent_filter: Filter to specific agent (from or to)
            type_filter: Filter to specific message type

        Returns:
            List of messages
        """
        messages = list(self._message_history)

        if agent_filter:
            messages = [
                m
                for m in messages
                if m.from_agent == agent_filter or m.to_agent == agent_filter
            ]

        if type_filter:
            messages = [m for m in messages if m.message_type == type_filter]

        return messages[-limit:]

    async def _process_queue(self, priority: MessagePriority) -> None:
        """
        Process messages from a priority queue with optimized latency.

        v2.7 CRITICAL FIX: Reduced timeout from 1.0s to priority-based values:
        - CRITICAL: 1ms timeout
        - HIGH: 5ms timeout
        - NORMAL: 10ms timeout
        - LOW: 50ms timeout

        The previous 1.0s timeout was causing 700ms+ workflow latency.
        """
        queue = self._queues[priority]
        target_latency = self.config.latency_targets_ms.get(priority.value, 10.0)

        while self._running:
            try:
                # Get message from queue; shutdown is driven by task cancellation.
                # Avoid asyncio.wait_for(queue.get()) because it creates ephemeral
                # internal Queue.get tasks that can survive late shutdown windows.
                message = await queue.get()

                start_time = time.perf_counter()

                # Check if expired
                if message.is_expired():
                    self._metrics.messages_expired += 1
                    logger.debug(
                        "Message %s expired, discarding",
                        message.message_id[:8],
                    )
                    continue

                # Deliver message
                await self._deliver_message(message)

                # Record latency
                latency_ms = (time.perf_counter() - start_time) * 1000
                self._metrics.total_latency_ms += latency_ms
                self._metrics.max_latency_ms = max(
                    self._metrics.max_latency_ms, latency_ms
                )

                if latency_ms > target_latency:
                    # v251.1: Only WARNING for CRITICAL priority.
                    # Other priorities log at DEBUG to avoid spam —
                    # health checks and routine messages commonly exceed
                    # targets when handlers do real work.
                    _log = logger.warning if priority == MessagePriority.CRITICAL else logger.debug
                    _log(
                        "Message %s exceeded latency target (%.2fms > %.2fms)",
                        message.message_id[:8],
                        latency_ms,
                        target_latency,
                    )

            except asyncio.CancelledError:
                break
            except GeneratorExit:
                # Event loop teardown path: exit quietly.
                break
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    # Late interpreter shutdown can surface from queue internals.
                    break
                logger.exception("Runtime error processing message: %s", e)
                break
            except Exception as e:
                logger.exception("Error processing message: %s", e)

    async def _deliver_message(self, message: AgentMessage) -> None:
        """Deliver a message to subscribers."""
        delivered = False

        # Check if this is a response to a pending request
        if message.reply_to and message.correlation_id:
            async with self._response_lock:
                future = self._pending_responses.get(message.correlation_id)
                if future and not future.done():
                    future.set_result(message.payload)
                    delivered = True
                    self._metrics.messages_delivered += 1
                    return

        # Get callbacks for the target agent
        callbacks: List[MessageCallback] = []

        async with self._subscription_lock:
            if message.is_broadcast():
                # Broadcast message - send to all broadcast subscribers
                if message.message_type in self._broadcast_subscriptions:
                    callbacks.extend(
                        self._broadcast_subscriptions[message.message_type]
                    )

                # Also send to all agents subscribed to this message type
                for agent_subs in self._subscriptions.values():
                    if message.message_type in agent_subs:
                        callbacks.extend(agent_subs[message.message_type])

            else:
                # Directed message - send to specific agent
                if message.to_agent in self._subscriptions:
                    agent_subs = self._subscriptions[message.to_agent]
                    if message.message_type in agent_subs:
                        callbacks.extend(agent_subs[message.message_type])

        # Execute callbacks
        if callbacks:
            tasks = []
            for callback in callbacks:
                try:
                    # Check if it's a coroutine function before calling
                    if asyncio.iscoroutinefunction(callback):
                        # Create task for async callbacks
                        tasks.append(
                            asyncio.create_task(
                                self._execute_callback(callback, message)
                            )
                        )
                    else:
                        # Execute sync callback directly
                        callback(message)
                except Exception as e:
                    logger.exception("Error in message callback: %s", e)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            delivered = True
            self._metrics.messages_delivered += 1

        if not delivered:
            # v238.0: Route to deadletter instead of silent drop
            try:
                self._deadletter.put_nowait(message)
                self._deadletter_count += 1
            except asyncio.QueueFull:
                pass  # Deadletter full — truly drop
            logger.debug(
                "No subscribers for message %s (to=%s, type=%s) → deadletter",
                message.message_id[:8],
                message.to_agent,
                message.message_type.value,
            )

    async def _execute_callback(
        self,
        callback: MessageCallback,
        message: AgentMessage,
    ) -> None:
        """Execute a callback with timeout, error handling, and circuit breaker."""
        agent_name = message.to_agent or "broadcast"

        # v238.0: Check circuit breaker before executing
        cb = self._circuit_breakers.get(agent_name)
        if cb and cb.open_until:
            if time.monotonic() < cb.open_until:
                return  # Circuit open — skip delivery
            # Half-open — try again
            cb.open_until = None
            cb.failures = 0

        try:
            result = callback(message)
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(
                    result,
                    timeout=self.config.handler_timeout_seconds,
                )
            # Success — reset failures
            if cb:
                cb.failures = 0
        except asyncio.TimeoutError:
            self._record_circuit_failure(agent_name)
            logger.warning(
                "Callback timed out for message %s (agent=%s)",
                message.message_id[:8],
                agent_name,
            )
        except Exception as e:
            self._record_circuit_failure(agent_name)
            logger.exception(
                "Callback error for message %s (agent=%s): %s",
                message.message_id[:8],
                agent_name,
                e,
            )

    def _record_circuit_failure(self, agent_name: str) -> None:
        """Record a callback failure and open circuit breaker if threshold reached."""
        cb = self._circuit_breakers.get(agent_name)
        if not cb:
            cb = _AgentCircuitBreaker()
            self._circuit_breakers[agent_name] = cb
        cb.failures += 1
        if cb.failures >= cb.threshold:
            cb.open_until = time.monotonic() + cb.recovery_seconds
            logger.warning(
                "Circuit breaker OPEN for %s after %d failures (recovery in %ds)",
                agent_name,
                cb.failures,
                int(cb.recovery_seconds),
            )

    async def _cleanup_expired(self) -> None:
        """Periodically clean up expired messages and responses."""
        while self._running:
            try:
                await asyncio.sleep(60.0)  # Run every minute

                # Clean up expired pending responses
                async with self._response_lock:
                    expired = [
                        cid
                        for cid, future in self._pending_responses.items()
                        if future.done()
                    ]
                    for cid in expired:
                        del self._pending_responses[cid]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in cleanup task: %s", e)

    async def drain_deadletter(self, limit: int = 100) -> List[AgentMessage]:
        """Drain up to `limit` messages from the deadletter queue.

        Returns:
            List of undelivered messages for inspection or retry.
        """
        messages: List[AgentMessage] = []
        while len(messages) < limit and not self._deadletter.empty():
            try:
                messages.append(self._deadletter.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def _periodic_deadletter_drain(self) -> None:
        """Periodically drain and log deadletter messages (runs every 60s)."""
        while self._running:
            try:
                await asyncio.sleep(60)
                if self._deadletter.empty():
                    continue

                messages = await self.drain_deadletter(limit=50)
                if messages:
                    # Group by message type for concise logging
                    type_counts: Dict[str, int] = {}
                    for msg in messages:
                        key = msg.message_type.value if hasattr(msg.message_type, 'value') else str(msg.message_type)
                        type_counts[key] = type_counts.get(key, 0) + 1

                    logger.info(
                        "Deadletter drain: %d messages (%s), total lifetime: %d",
                        len(messages),
                        ", ".join(f"{k}={v}" for k, v in type_counts.items()),
                        self._deadletter_count,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Deadletter drain error: %s", e)

    async def persist_history(self, path: Optional[str] = None) -> None:
        """
        Persist message history to disk.

        Args:
            path: Path to save history. Uses config path if not provided.
        """
        if not path:
            path = self.config.persistence_path

        if not path:
            logger.warning("No persistence path configured")
            return

        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            filepath = Path(path) / f"messages_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            history = [m.to_dict() for m in self._message_history]
            with open(filepath, "w") as f:
                json.dump(history, f, indent=2, default=str)

            logger.info("Persisted %d messages to %s", len(history), filepath)

        except Exception as e:
            logger.exception("Error persisting message history: %s", e)

    async def load_history(self, path: str) -> int:
        """
        Load message history from disk.

        Args:
            path: Path to the history file

        Returns:
            Number of messages loaded
        """
        try:
            with open(path, "r") as f:
                data = json.load(f)

            for item in data:
                message = AgentMessage.from_dict(item)
                self._message_history.append(message)

            logger.info("Loaded %d messages from %s", len(data), path)
            return len(data)

        except Exception as e:
            logger.exception("Error loading message history: %s", e)
            return 0

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"AgentCommunicationBus("
            f"running={self._running}, "
            f"subscriptions={self._metrics.active_subscriptions}, "
            f"published={self._metrics.messages_published}, "
            f"delivered={self._metrics.messages_delivered}"
            f")"
        )

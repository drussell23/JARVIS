"""
JARVIS Neural Mesh - Base Neural Mesh Agent

Unified agent interface with automatic Neural Mesh integration.
Provides:
- Automatic registration with the registry
- Automatic heartbeat management
- Message routing and handling
- Knowledge graph access
- Lifecycle management

Adoption Target: < 50 lines of code to migrate existing agent
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
)

from ..data_models import (
    AgentInfo,
    AgentMessage,
    AgentStatus,
    KnowledgeEntry,
    KnowledgeType,
    MessagePriority,
    MessageType,
)
from ..communication.agent_communication_bus import AgentCommunicationBus
from ..registry.agent_registry import AgentRegistry
from ..knowledge.shared_knowledge_graph import SharedKnowledgeGraph
from ..config import BaseAgentConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class AgentMetrics:
    """Metrics for an individual agent."""

    tasks_received: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    knowledge_added: int = 0
    knowledge_queried: int = 0
    total_task_time_ms: float = 0.0
    errors: int = 0


class BaseNeuralMeshAgent(ABC):
    """
    Base class for all Neural Mesh agents.

    Provides automatic integration with:
    - Agent Registry (registration, heartbeats)
    - Communication Bus (message routing)
    - Knowledge Graph (collective memory)

    To create a Neural Mesh agent, inherit from this class and implement:
    - on_initialize(): Agent-specific setup
    - execute_task(): Handle assigned tasks

    Example:
        class VisionAgent(BaseNeuralMeshAgent):
            def __init__(self):
                super().__init__(
                    agent_name="vision_agent",
                    agent_type="vision",
                    capabilities={"screen_capture", "error_detection"},
                )
                self.analyzer = ClaudeVisionAnalyzer()

            async def on_initialize(self):
                await self.analyzer.load_models()
                await self.subscribe(
                    MessageType.CUSTOM,
                    self._handle_vision_request,
                )

            async def execute_task(self, payload):
                action = payload.get('action')
                if action == 'capture':
                    return await self.capture_screen(payload)
    """

    def __init__(
        self,
        agent_name: str,
        agent_type: str,
        capabilities: Set[str],
        backend: str = "local",
        version: str = "1.0.0",
        dependencies: Optional[Set[str]] = None,
        config: Optional[BaseAgentConfig] = None,
        description: Optional[str] = None,
        **kwargs,  # Accept additional kwargs for forward compatibility
    ) -> None:
        """Initialize the base agent.

        Args:
            agent_name: Unique name for this agent
            agent_type: Category of agent (vision, voice, context, etc.)
            capabilities: Set of capabilities this agent provides
            backend: Where this agent runs (local, cloud, hybrid)
            version: Agent version
            dependencies: Other agents this agent depends on
            config: Agent configuration
            description: Human-readable description of agent's purpose
            **kwargs: Additional keyword arguments for forward compatibility
        """
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.capabilities = capabilities
        self.backend = backend
        self.version = version
        self.dependencies = dependencies or set()
        self.config = config or get_config().base_agent
        self.description = description or f"{agent_name} ({agent_type})"

        # Log any unexpected kwargs for debugging (but don't fail)
        if kwargs:
            logger.debug(f"[{agent_name}] Ignoring unexpected kwargs: {list(kwargs.keys())}")

        # Neural Mesh components (lazy loaded)
        self.message_bus: Optional[AgentCommunicationBus] = None
        self.registry: Optional[AgentRegistry] = None
        self.knowledge_graph: Optional[SharedKnowledgeGraph] = None

        # State
        self._initialized = False
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._message_handler_task: Optional[asyncio.Task[None]] = None

        # v6.0: Lazy message queue initialization to prevent
        # "There is no current event loop in thread" errors when instantiated
        # from thread pool executors
        self._message_queue: Optional[asyncio.Queue[AgentMessage]] = None

        # Metrics
        self._metrics = AgentMetrics()

        # Task load tracking
        self._current_load = 0.0
        self._task_queue_size = 0

        logger.info(
            "Created agent: %s (type=%s, capabilities=%s, description=%s)",
            agent_name,
            agent_type,
            ", ".join(capabilities),
            self.description[:80] if self.description else "N/A",
        )

    def _get_message_queue(self) -> asyncio.Queue[AgentMessage]:
        """
        v6.0: Lazy message queue getter - creates queue on first async access.

        This prevents "There is no current event loop in thread" errors when
        the agent is instantiated from a thread pool executor.
        """
        if self._message_queue is None:
            self._message_queue = asyncio.Queue(
                maxsize=self.config.message_queue_size
            )
        return self._message_queue

    async def initialize(
        self,
        message_bus: Optional[AgentCommunicationBus] = None,
        registry: Optional[AgentRegistry] = None,
        knowledge_graph: Optional[SharedKnowledgeGraph] = None,
        **kwargs  # Accept any additional kwargs for flexibility
    ) -> None:
        """
        Initialize the agent - supports both standalone and Neural Mesh modes.

        **Dual-Mode Initialization:**

        1. **Standalone Mode** (no parameters):
           - Agent works independently
           - No message bus, no registry
           - Perfect for simple use cases
           - Example: `await agent.initialize()`

        2. **Neural Mesh Mode** (with parameters):
           - Full Neural Mesh integration
           - Message routing, discovery, knowledge sharing
           - Example: `await agent.initialize(message_bus, registry, knowledge_graph)`

        Args:
            message_bus: Communication bus for messaging (optional)
            registry: Agent registry for discovery (optional)
            knowledge_graph: Knowledge graph for collective memory (optional)
            **kwargs: Additional parameters for agent-specific initialization

        This design enables:
        - ✅ Gradual migration to Neural Mesh
        - ✅ Backward compatibility with standalone agents
        - ✅ Graceful degradation when infrastructure unavailable
        - ✅ Zero breaking changes
        """
        if self._initialized:
            logger.debug("Agent %s already initialized", self.agent_name)
            return

        # Detect mode
        mesh_mode = message_bus is not None and registry is not None
        standalone_mode = not mesh_mode

        if standalone_mode:
            logger.info(
                "Agent %s initializing in STANDALONE mode (no Neural Mesh)",
                self.agent_name
            )
        else:
            logger.info(
                "Agent %s initializing in NEURAL MESH mode",
                self.agent_name
            )

        # Set Neural Mesh components (may be None in standalone mode)
        self.message_bus = message_bus
        self.registry = registry
        self.knowledge_graph = knowledge_graph

        # Only register if in Neural Mesh mode
        if mesh_mode:
            try:
                # Register with registry
                await self.registry.register(
                    agent_name=self.agent_name,
                    agent_type=self.agent_type,
                    capabilities=self.capabilities,
                    backend=self.backend,
                    version=self.version,
                    dependencies=self.dependencies,
                    metadata={"config": self.config.__dict__},
                )

                # Subscribe to task assignments
                await self.message_bus.subscribe(
                    self.agent_name,
                    MessageType.TASK_ASSIGNED,
                    self._handle_task_message,
                )

                # Subscribe to health checks
                await self.message_bus.subscribe(
                    self.agent_name,
                    MessageType.AGENT_HEALTH_CHECK,
                    self._handle_health_check,
                )

                logger.info("Agent %s registered with Neural Mesh", self.agent_name)

            except Exception as e:
                logger.warning(
                    "Agent %s Neural Mesh registration failed (degrading to standalone): %s",
                    self.agent_name,
                    e
                )
                # Graceful degradation - continue in standalone mode
                self.message_bus = None
                self.registry = None

        # Call agent-specific initialization (pass kwargs for flexibility)
        try:
            await self.on_initialize(**kwargs)
        except TypeError:
            # Fallback for agents that don't accept kwargs
            await self.on_initialize()

        self._initialized = True

        mode_str = "Neural Mesh" if mesh_mode else "standalone"
        logger.info("Agent %s initialized (%s mode)", self.agent_name, mode_str)

    async def start(self) -> None:
        """Start the agent."""
        if not self._initialized:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

        if self._running:
            logger.warning("Agent %s already running", self.agent_name)
            return

        self._running = True

        # v95.0: Start heartbeat task with automatic recovery
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop_with_recovery(),
            name=f"{self.agent_name}_heartbeat",
        )

        # v95.0: Add done callback for automatic restart on unexpected termination
        def _heartbeat_done_callback(task: asyncio.Task) -> None:
            """Restart heartbeat task if it dies unexpectedly."""
            if not self._running:
                return  # Expected termination

            exc = task.exception() if not task.cancelled() else None
            if exc:
                logger.error(
                    f"Agent {self.agent_name} heartbeat task died unexpectedly: {exc}"
                )
                # Restart heartbeat task
                if self._running:
                    logger.info(f"Agent {self.agent_name} restarting heartbeat task")
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop_with_recovery(),
                        name=f"{self.agent_name}_heartbeat_recovery",
                    )
                    self._heartbeat_task.add_done_callback(_heartbeat_done_callback)

        self._heartbeat_task.add_done_callback(_heartbeat_done_callback)

        # Start message handler task
        self._message_handler_task = asyncio.create_task(
            self._message_handler_loop(),
            name=f"{self.agent_name}_handler",
        )

        # Call agent-specific start
        await self.on_start()

        logger.info("Agent %s started", self.agent_name)

    async def stop(self) -> None:
        """Stop the agent gracefully."""
        if not self._running:
            return

        self._running = False

        # Update status
        if self.registry:
            await self.registry.set_status(
                self.agent_name,
                AgentStatus.SHUTTING_DOWN,
            )

        # Cancel tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._message_handler_task:
            self._message_handler_task.cancel()
            try:
                await self._message_handler_task
            except asyncio.CancelledError:
                pass

        # Call agent-specific stop
        await self.on_stop()

        # Unregister
        if self.registry:
            await self.registry.unregister(self.agent_name)

        logger.info("Agent %s stopped", self.agent_name)

    # =========================================================================
    # Abstract methods - must be implemented by subclasses
    # =========================================================================

    @abstractmethod
    async def on_initialize(self, **kwargs) -> None:
        """
        Agent-specific initialization.

        Override this to perform setup like:
        - Loading models
        - Establishing connections
        - Subscribing to message types

        Args:
            **kwargs: Optional parameters for flexible initialization
                      (agents can accept custom parameters)
        """
        pass

    @abstractmethod
    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute an assigned task.

        Override this to handle tasks from the orchestrator.

        Args:
            payload: Task data including:
                - task_id: Unique task identifier
                - action: What capability to use
                - input: Task input data
                - context: Additional context

        Returns:
            Task result (any JSON-serializable value)

        Raises:
            Exception: If task execution fails
        """
        pass

    # =========================================================================
    # Optional lifecycle hooks
    # =========================================================================

    async def on_start(self) -> None:
        """Called when agent starts. Override for custom behavior."""
        pass

    async def on_stop(self) -> None:
        """Called when agent stops. Override for cleanup."""
        pass

    # =========================================================================
    # Messaging convenience methods
    # =========================================================================

    async def publish(
        self,
        to_agent: str,
        message_type: MessageType,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> str:
        """
        Send a message to another agent.

        Args:
            to_agent: Target agent name (or "broadcast" for all)
            message_type: Type of message
            payload: Message data
            priority: Message priority

        Returns:
            Message ID
        """
        if not self.message_bus:
            raise RuntimeError("Agent not connected to message bus")

        message = AgentMessage(
            from_agent=self.agent_name,
            to_agent=to_agent,
            message_type=message_type,
            payload=payload,
            priority=priority,
        )

        msg_id = await self.message_bus.publish(message)
        self._metrics.messages_sent += 1

        return msg_id

    async def request(
        self,
        to_agent: str,
        payload: Dict[str, Any],
        timeout: float = 10.0,
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> Dict[str, Any]:
        """
        Send a request and wait for response.

        Args:
            to_agent: Target agent name
            payload: Request data
            timeout: Maximum wait time
            priority: Message priority

        Returns:
            Response payload
        """
        if not self.message_bus:
            raise RuntimeError("Agent not connected to message bus")

        message = AgentMessage(
            from_agent=self.agent_name,
            to_agent=to_agent,
            message_type=MessageType.REQUEST,
            payload=payload,
            priority=priority,
        )

        response = await self.message_bus.request(message, timeout=timeout)
        self._metrics.messages_sent += 1
        self._metrics.messages_received += 1

        return response

    async def broadcast(
        self,
        message_type: MessageType,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> str:
        """
        Broadcast a message to all agents.

        Args:
            message_type: Type of message
            payload: Message data
            priority: Message priority

        Returns:
            Message ID
        """
        if not self.message_bus:
            raise RuntimeError("Agent not connected to message bus")

        return await self.message_bus.broadcast(
            from_agent=self.agent_name,
            message_type=message_type,
            payload=payload,
            priority=priority,
        )

    async def subscribe(
        self,
        message_type: MessageType,
        callback: Callable[[AgentMessage], Any],
    ) -> None:
        """
        Subscribe to a message type.

        Args:
            message_type: Type to subscribe to
            callback: Handler function
        """
        if not self.message_bus:
            raise RuntimeError("Agent not connected to message bus")

        await self.message_bus.subscribe(
            self.agent_name,
            message_type,
            callback,
        )

    # =========================================================================
    # Knowledge graph convenience methods
    # =========================================================================

    async def add_knowledge(
        self,
        knowledge_type: KnowledgeType,
        data: Dict[str, Any],
        tags: Optional[Set[str]] = None,
        ttl_seconds: Optional[float] = None,
        confidence: float = 1.0,
    ) -> Optional[KnowledgeEntry]:
        """
        Add knowledge to the collective memory.

        Args:
            knowledge_type: Type of knowledge
            data: Knowledge data
            tags: Searchable tags
            ttl_seconds: Time to live
            confidence: Confidence score

        Returns:
            Created knowledge entry or None if graph unavailable
        """
        if not self.knowledge_graph:
            logger.warning("Knowledge graph not available")
            return None

        entry = await self.knowledge_graph.add_knowledge(
            knowledge_type=knowledge_type,
            agent_name=self.agent_name,
            data=data,
            tags=tags,
            ttl_seconds=ttl_seconds,
            confidence=confidence,
        )

        self._metrics.knowledge_added += 1
        return entry

    async def query_knowledge(
        self,
        query: str,
        knowledge_types: Optional[List[KnowledgeType]] = None,
        limit: int = 10,
        min_confidence: float = 0.0,
    ) -> List[KnowledgeEntry]:
        """
        Query the collective memory.

        Args:
            query: Search query
            knowledge_types: Filter to specific types
            limit: Maximum results
            min_confidence: Minimum confidence

        Returns:
            List of matching knowledge entries
        """
        if not self.knowledge_graph:
            logger.warning("Knowledge graph not available")
            return []

        results = await self.knowledge_graph.query(
            query=query,
            knowledge_types=knowledge_types,
            limit=limit,
            min_confidence=min_confidence,
        )

        self._metrics.knowledge_queried += 1
        return results

    # =========================================================================
    # Internal methods
    # =========================================================================

    async def _heartbeat_loop_with_recovery(self) -> None:
        """
        v95.0: Enterprise-grade heartbeat loop with automatic recovery.

        Wraps the main heartbeat loop with recovery logic to ensure
        heartbeats always continue even after unexpected errors.
        """
        recovery_count = 0
        max_recovery_attempts = 10
        recovery_backoff_base = 5.0

        while self._running and recovery_count < max_recovery_attempts:
            try:
                await self._heartbeat_loop()
                # Normal exit (agent stopped)
                break
            except asyncio.CancelledError:
                # Expected cancellation
                logger.debug("Agent %s heartbeat loop cancelled (recovery wrapper)", self.agent_name)
                break
            except Exception as e:
                recovery_count += 1
                if recovery_count < max_recovery_attempts:
                    backoff = min(60.0, recovery_backoff_base * (2 ** (recovery_count - 1)))
                    logger.error(
                        f"Agent {self.agent_name} heartbeat loop crashed "
                        f"(recovery {recovery_count}/{max_recovery_attempts}), "
                        f"restarting in {backoff:.1f}s: {e}"
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        f"Agent {self.agent_name} heartbeat loop exhausted recovery attempts"
                    )

    async def _heartbeat_loop(self) -> None:
        """
        v95.0: Enterprise-grade fire-and-forget heartbeat system.

        Enhanced Features:
        - Fire-and-forget pattern: schedules heartbeats without waiting
        - Non-blocking: never blocks the event loop
        - Circuit breaker: backs off on persistent failures
        - Self-healing: automatic re-registration on connection loss
        - Adaptive intervals: adjusts based on system health
        - Timeout protection: short timeouts prevent blocking
        """
        consecutive_failures = 0
        max_backoff = 60.0  # Maximum backoff in seconds
        base_interval = getattr(self.config, 'heartbeat_interval_seconds', 10.0)
        total_heartbeats_sent = 0
        heartbeat_timeout = 2.0  # Short timeout for non-blocking operation

        # v95.0: Send IMMEDIATE first heartbeat using fire-and-forget
        try:
            if self.registry:
                logger.debug("Agent %s sending initial heartbeat (fire-and-forget)", self.agent_name)
                # Fire-and-forget: schedule but don't wait
                asyncio.create_task(
                    self._send_heartbeat_safe(timeout=heartbeat_timeout),
                    name=f"{self.agent_name}_initial_hb"
                )
                total_heartbeats_sent += 1
        except Exception as first_hb_err:
            logger.debug("Agent %s initial heartbeat scheduling failed: %s", self.agent_name, first_hb_err)

        while self._running:
            try:
                # v95.0: Defensive check - ensure registry reference is still valid
                if not self.registry:
                    logger.debug("Agent %s has no registry reference, skipping heartbeat", self.agent_name)
                    await asyncio.sleep(base_interval)
                    continue

                # v95.0: Fire-and-forget heartbeat - schedule and don't wait
                # This prevents blocking the event loop if registry is slow
                heartbeat_task = asyncio.create_task(
                    self._send_heartbeat_safe(timeout=heartbeat_timeout),
                    name=f"{self.agent_name}_hb_{total_heartbeats_sent}"
                )

                # Optionally track result for statistics (non-blocking)
                def _track_heartbeat_result(task: asyncio.Task) -> None:
                    nonlocal consecutive_failures, total_heartbeats_sent
                    try:
                        if task.cancelled():
                            return
                        success = task.result()
                        if success:
                            consecutive_failures = 0
                            total_heartbeats_sent += 1
                        else:
                            consecutive_failures += 1
                    except Exception:
                        consecutive_failures += 1

                heartbeat_task.add_done_callback(_track_heartbeat_result)

                # Log successful heartbeats periodically at debug level
                if total_heartbeats_sent > 0 and total_heartbeats_sent % 100 == 0:
                    logger.debug(
                        "Agent %s heartbeat #%d (load=%.2f, queue=%d)",
                        self.agent_name,
                        total_heartbeats_sent,
                        self._current_load,
                        self._task_queue_size,
                    )

                # v95.0: Self-healing re-registration on persistent failures
                if consecutive_failures >= 5:
                    logger.warning(
                        "Agent %s heartbeat failed %d times - attempting re-registration",
                        self.agent_name,
                        consecutive_failures,
                    )
                    # Fire-and-forget re-registration
                    asyncio.create_task(
                        self._attempt_reregistration_safe(),
                        name=f"{self.agent_name}_rereg"
                    )
                    consecutive_failures = 0  # Reset to prevent spam

                # v95.0: Adaptive sleep interval based on failure count
                if consecutive_failures > 0:
                    # Exponential backoff with jitter
                    jitter = (hash(time.time()) % 100) / 100.0  # 0-1 random jitter
                    backoff = min(max_backoff, base_interval * (1.5 ** min(consecutive_failures, 6)))
                    backoff *= (0.8 + 0.4 * jitter)  # ±20% jitter
                    await asyncio.sleep(backoff)
                else:
                    await asyncio.sleep(base_interval)

            except asyncio.CancelledError:
                logger.debug("Agent %s heartbeat loop cancelled", self.agent_name)
                raise  # Re-raise to exit cleanly
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                    logger.warning(
                        "Agent %s heartbeat error (failure %d): %s",
                        self.agent_name,
                        consecutive_failures,
                        str(e),
                    )
                # Backoff on errors
                backoff = min(max_backoff, base_interval * (2 ** min(consecutive_failures, 5)))
                await asyncio.sleep(backoff)

        # v95.0: Log when heartbeat loop exits
        logger.debug(
            "Agent %s heartbeat loop exited (sent %d total heartbeats)",
            self.agent_name,
            total_heartbeats_sent,
        )

    async def _send_heartbeat_safe(self, timeout: float = 2.0) -> bool:
        """
        v95.0: Send a single heartbeat with timeout protection.

        This method is designed to be used in fire-and-forget mode.
        It will never block for more than `timeout` seconds.

        Args:
            timeout: Maximum time to wait for heartbeat completion

        Returns:
            True if heartbeat succeeded, False otherwise
        """
        try:
            return await asyncio.wait_for(
                self.registry.heartbeat(
                    self.agent_name,
                    load=self._current_load,
                    task_queue_size=self._task_queue_size,
                ),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.debug("Agent %s heartbeat timed out (non-fatal)", self.agent_name)
            return False
        except asyncio.CancelledError:
            return False
        except Exception as e:
            logger.debug("Agent %s heartbeat error (non-fatal): %s", self.agent_name, e)
            return False

    async def _attempt_reregistration_safe(self, timeout: float = 10.0) -> bool:
        """
        v95.0: Attempt to re-register with the registry safely.

        Used when heartbeats persistently fail, suggesting the agent
        may have been deregistered or the registry was reset.

        Args:
            timeout: Maximum time to wait for re-registration

        Returns:
            True if re-registration succeeded, False otherwise
        """
        try:
            await asyncio.wait_for(
                self.registry.register(
                    agent_name=self.agent_name,
                    agent_type=self.agent_type,
                    capabilities=self.capabilities,
                    backend=self.backend,
                    version=self.version,
                    dependencies=self.dependencies,
                    metadata={"config": getattr(self.config, '__dict__', {})},
                ),
                timeout=timeout
            )
            logger.info("Agent %s re-registered successfully", self.agent_name)
            return True
        except asyncio.TimeoutError:
            logger.debug("Agent %s re-registration timed out", self.agent_name)
            return False
        except asyncio.CancelledError:
            return False
        except Exception as e:
            logger.debug("Agent %s re-registration failed: %s", self.agent_name, e)
            return False

    async def _message_handler_loop(self) -> None:
        """
        Process incoming messages with optimized low-latency handling.

        v2.7 CRITICAL FIX: Reduced timeout from 1.0s to 0.01s (10ms) to meet
        latency targets:
        - CRITICAL: <1ms
        - HIGH: <5ms
        - NORMAL: <10ms

        The previous 1.0s timeout was a MAJOR bottleneck causing 700ms+ workflow latency.
        """
        # v2.7: Ultra-low latency queue polling (10ms timeout)
        queue_timeout = float(os.getenv("NEURAL_MESH_QUEUE_TIMEOUT", "0.01"))

        while self._running:
            try:
                message = await asyncio.wait_for(
                    self._get_message_queue().get(),  # v6.0: Use lazy getter
                    timeout=queue_timeout,  # v2.7: 10ms instead of 1000ms
                )
                await self._process_message(message)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Message handler error: %s", e)
                self._metrics.errors += 1

    async def _handle_task_message(self, message: AgentMessage) -> None:
        """Handle task assignment messages."""
        self._metrics.messages_received += 1
        self._task_queue_size += 1

        try:
            # Execute task
            self._metrics.tasks_received += 1
            start_time = asyncio.get_event_loop().time()

            result = await self.execute_task(message.payload)

            # Calculate metrics
            task_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            self._metrics.total_task_time_ms += task_time_ms
            self._metrics.tasks_completed += 1

            # Send response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={"result": result, "success": True},
                    from_agent=self.agent_name,
                )

        except Exception as e:
            logger.exception("Task execution error: %s", e)
            self._metrics.tasks_failed += 1
            self._metrics.errors += 1

            # Send error response
            if self.message_bus:
                await self.message_bus.respond(
                    message,
                    payload={"error": str(e), "success": False},
                    from_agent=self.agent_name,
                )

        finally:
            self._task_queue_size -= 1
            # Update load
            self._current_load = min(1.0, self._task_queue_size / 10.0)

    async def _handle_health_check(self, message: AgentMessage) -> None:
        """Handle health check messages."""
        if self.message_bus:
            await self.message_bus.respond(
                message,
                payload={
                    "status": "healthy" if self._running else "stopped",
                    "metrics": {
                        "tasks_received": self._metrics.tasks_received,
                        "tasks_completed": self._metrics.tasks_completed,
                        "tasks_failed": self._metrics.tasks_failed,
                        "current_load": self._current_load,
                    },
                },
                from_agent=self.agent_name,
            )

    async def _process_message(self, message: AgentMessage) -> None:
        """Process a queued message."""
        # This is for custom message processing if needed
        logger.debug(
            "Processing message %s from %s",
            message.message_id[:8],
            message.from_agent,
        )

    def get_metrics(self) -> AgentMetrics:
        """Get agent metrics."""
        return self._metrics

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"{self.__class__.__name__}("
            f"name={self.agent_name}, "
            f"type={self.agent_type}, "
            f"running={self._running}"
            f")"
        )

"""
JARVIS Neural Mesh - Neural Mesh Coordinator

The central coordinator that initializes and manages all Neural Mesh components.
Provides a single entry point for the entire system with:
- Component lifecycle management
- Unified initialization
- Health monitoring
- Graceful shutdown
- Integration with existing JARVIS systems

Usage:
    coordinator = NeuralMeshCoordinator()
    await coordinator.initialize()
    await coordinator.start()

    # Use components
    await coordinator.bus.publish(...)
    await coordinator.knowledge.query(...)
    agents = await coordinator.registry.find_by_capability(...)

    # Shutdown
    await coordinator.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
)

from .data_models import (
    AgentInfo,
    HealthStatus,
    MessageType,
)
from .communication.agent_communication_bus import AgentCommunicationBus
from .knowledge.shared_knowledge_graph import SharedKnowledgeGraph
from .registry.agent_registry import AgentRegistry
from .orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
from .base.base_neural_mesh_agent import BaseNeuralMeshAgent
from .config import NeuralMeshConfig, get_config, set_config

logger = logging.getLogger(__name__)


@dataclass
class SystemMetrics:
    """Aggregate metrics for the Neural Mesh system."""

    uptime_seconds: float = 0.0
    registered_agents: int = 0
    online_agents: int = 0
    messages_published: int = 0
    messages_delivered: int = 0
    knowledge_entries: int = 0
    workflows_completed: int = 0
    system_health: HealthStatus = HealthStatus.UNKNOWN


class NeuralMeshCoordinator:
    """
    Central coordinator for the JARVIS Neural Mesh system.

    This is the main entry point for Neural Mesh. It:
    - Creates and initializes all components
    - Manages component lifecycles
    - Provides unified access to all features
    - Monitors system health
    - Integrates with existing JARVIS systems

    Example:
        # Create coordinator
        coordinator = NeuralMeshCoordinator()

        # Initialize (loads config, creates components)
        await coordinator.initialize()

        # Start all components
        await coordinator.start()

        # Register an agent
        class MyAgent(BaseNeuralMeshAgent):
            ...

        my_agent = MyAgent()
        await coordinator.register_agent(my_agent)

        # Use the system
        await coordinator.bus.publish(...)
        results = await coordinator.knowledge.query("errors")
        agents = await coordinator.registry.find_by_capability("vision")

        # Execute a workflow
        from neural_mesh import WorkflowTask, ExecutionStrategy
        tasks = [WorkflowTask(...), WorkflowTask(...)]
        result = await coordinator.orchestrator.execute_workflow(
            name="Debug workflow",
            tasks=tasks,
            strategy=ExecutionStrategy.HYBRID,
        )

        # Shutdown
        await coordinator.stop()
    """

    def __init__(self, config: Optional[NeuralMeshConfig] = None) -> None:
        """Initialize the coordinator.

        Args:
            config: Neural Mesh configuration. Uses default if not provided.
        """
        self.config = config or get_config()

        # Set as global config
        set_config(self.config)

        # Core components (created during initialize)
        self._bus: Optional[AgentCommunicationBus] = None
        self._registry: Optional[AgentRegistry] = None
        self._knowledge: Optional[SharedKnowledgeGraph] = None
        self._orchestrator: Optional[MultiAgentOrchestrator] = None

        # Registered agents
        self._agents: Dict[str, BaseNeuralMeshAgent] = {}
        # v251.4: Slot reservation — names currently being registered (prevents
        # concurrent register_agent() calls from double-initializing the same agent)
        self._registering: set = set()

        # State
        self._initialized = False
        self._running = False
        self._started_at: Optional[datetime] = None

        # Health monitoring
        self._health_task: Optional[asyncio.Task[None]] = None
        self._system_health = HealthStatus.UNKNOWN

        logger.info("NeuralMeshCoordinator created")

    @property
    def bus(self) -> AgentCommunicationBus:
        """Get the communication bus."""
        if not self._bus:
            raise RuntimeError("Coordinator not initialized")
        return self._bus

    @property
    def registry(self) -> AgentRegistry:
        """Get the agent registry."""
        if not self._registry:
            raise RuntimeError("Coordinator not initialized")
        return self._registry

    @property
    def knowledge(self) -> SharedKnowledgeGraph:
        """Get the knowledge graph."""
        if not self._knowledge:
            raise RuntimeError("Coordinator not initialized")
        return self._knowledge

    @property
    def orchestrator(self) -> MultiAgentOrchestrator:
        """Get the multi-agent orchestrator."""
        if not self._orchestrator:
            raise RuntimeError("Coordinator not initialized")
        return self._orchestrator

    async def initialize(self) -> None:
        """
        Initialize all Neural Mesh components.

        This creates all components and prepares them for use,
        but does not start background tasks.
        """
        if self._initialized:
            logger.warning("Coordinator already initialized")
            return

        logger.info("Initializing Neural Mesh system...")

        # Ensure directories exist
        self.config.ensure_directories()

        # Create components
        self._bus = AgentCommunicationBus(self.config.communication_bus)
        self._registry = AgentRegistry(self.config.agent_registry)

        # v112.0: Integrate AgentRegistry with ProxyReadinessGate for dependency tracking
        # This ensures CloudSQL-dependent agents aren't marked offline when CloudSQL is down
        await self._setup_proxy_readiness_integration()

        self._knowledge = SharedKnowledgeGraph(self.config.knowledge_graph)

        # Initialize knowledge graph (it has its own async init)
        await self._knowledge.initialize()

        # Create orchestrator (depends on bus, registry, knowledge)
        self._orchestrator = MultiAgentOrchestrator(
            communication_bus=self._bus,
            agent_registry=self._registry,
            knowledge_graph=self._knowledge,
            config=self.config.orchestrator,
        )

        self._initialized = True
        logger.info("Neural Mesh system initialized")

    async def _setup_proxy_readiness_integration(self) -> None:
        """
        Set up integration between AgentRegistry and ProxyReadinessGate.

        v112.0: This ensures that CloudSQL-dependent agents won't be marked
        offline when CloudSQL itself is unavailable. The ProxyReadinessGate
        notifies the AgentRegistry whenever CloudSQL state changes.

        This is a best-effort integration - if the gate is not available,
        CloudSQL dependency tracking is disabled but the system continues.
        """
        if self._registry is None:
            logger.debug(
                "[NeuralMeshCoordinator v112.0] Registry not created yet, "
                "skipping proxy integration"
            )
            return

        try:
            # Import lazily to avoid circular imports
            # v115.0: Use get_readiness_gate() to get the singleton instance
            # CRITICAL: Must use singleton, not ProxyReadinessGate() which creates a new instance
            try:
                from intelligence.cloud_sql_connection_manager import get_readiness_gate
            except ImportError:
                from backend.intelligence.cloud_sql_connection_manager import get_readiness_gate

            # v115.0: Get the SINGLETON gate instance (not a new one!)
            gate = get_readiness_gate()

            # Set up the integration
            gate.setup_agent_registry_integration(self._registry)

            logger.info(
                "[NeuralMeshCoordinator v112.0] ProxyReadinessGate integration "
                "established for AgentRegistry dependency tracking"
            )
        except ImportError as e:
            logger.debug(
                "[NeuralMeshCoordinator v112.0] ProxyReadinessGate not available "
                "(import error: %s) - CloudSQL dependency tracking disabled", e
            )
        except Exception as e:
            logger.warning(
                "[NeuralMeshCoordinator v112.0] Failed to set up ProxyReadinessGate "
                "integration: %s - CloudSQL dependency tracking disabled", e
            )

        # Also set service_registry dependency to ready (since Neural Mesh is starting)
        # This is set here because if Neural Mesh is initializing, the service registry
        # must be available (JARVIS body is running)
        try:
            self._registry.set_dependency_ready("service_registry", True)
            logger.debug(
                "[NeuralMeshCoordinator v112.0] service_registry dependency marked as ready"
            )
        except Exception as e:
            logger.debug(
                "[NeuralMeshCoordinator v112.0] Failed to mark service_registry ready: %s", e
            )

    async def start(self) -> None:
        """
        Start all Neural Mesh components.

        This starts background tasks like message processing,
        health monitoring, and cleanup.
        """
        if not self._initialized:
            raise RuntimeError("Coordinator not initialized. Call initialize() first.")

        if self._running:
            # v93.14: Changed from WARNING to DEBUG - this is expected idempotent behavior
            logger.debug("Coordinator already running - skipping duplicate start")
            return

        logger.info("Starting Neural Mesh system...")

        # Start components (guaranteed non-None after initialize())
        assert self._bus is not None, "Bus not initialized"
        assert self._registry is not None, "Registry not initialized"
        assert self._orchestrator is not None, "Orchestrator not initialized"
        await self._bus.start()
        await self._registry.start()
        await self._orchestrator.start()

        # Start all registered agents
        for agent in self._agents.values():
            if not agent._running:
                await agent.start()

        # Start health monitoring
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="neural_mesh_health_monitor",
        )

        self._running = True
        self._started_at = datetime.now()
        self._system_health = HealthStatus.HEALTHY

        logger.info("Neural Mesh system started")

    async def stop(self) -> None:
        """
        Stop all Neural Mesh components gracefully.
        """
        if (
            not self._running
            and not self._initialized
            and self._health_task is None
        ):
            return

        logger.info("Stopping Neural Mesh system...")

        self._running = False
        self._system_health = HealthStatus.UNKNOWN
        agent_stop_timeout = float(os.environ.get("NEURAL_MESH_AGENT_STOP_TIMEOUT", "5.0"))
        component_stop_timeout = float(os.environ.get("NEURAL_MESH_COMPONENT_STOP_TIMEOUT", "8.0"))

        # Cancel health monitor
        if self._health_task:
            health_task = self._health_task
            self._health_task = None
            task_loop_closed = False
            try:
                task_loop_closed = health_task.get_loop().is_closed()
            except Exception:
                task_loop_closed = False

            if task_loop_closed:
                logger.debug(
                    "Skipping Neural Mesh health task cancellation: task loop already closed"
                )
            elif not health_task.done():
                try:
                    health_task.cancel()
                    await health_task
                except asyncio.CancelledError:
                    pass
                except RuntimeError as e:
                    if "Event loop is closed" in str(e):
                        logger.debug(
                            "Skipping Neural Mesh health task await: event loop closed"
                        )
                    else:
                        raise

        async def _stop_agent(agent: BaseNeuralMeshAgent) -> None:
            try:
                await asyncio.wait_for(agent.stop(), timeout=agent_stop_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Agent stop timed out for %s after %.1fs (forcing task cancel)",
                    agent.agent_name,
                    agent_stop_timeout,
                )
                for task_attr in ("_heartbeat_task", "_message_handler_task"):
                    task = getattr(agent, task_attr, None)
                    if task and not task.done():
                        try:
                            task.cancel()
                        except RuntimeError as e:
                            if "Event loop is closed" in str(e):
                                logger.debug(
                                    "Skipping %s task cancel for %s: event loop closed",
                                    task_attr,
                                    agent.agent_name,
                                )
                            else:
                                raise
            except Exception as e:
                logger.exception("Error stopping agent %s: %s", agent.agent_name, e)

        # Stop all agents in parallel to keep shutdown bounded.
        if self._agents:
            await asyncio.gather(
                *(_stop_agent(agent) for agent in self._agents.values()),
                return_exceptions=True,
            )

        async def _stop_component(name: str, component: Any, stop_method: str) -> None:
            if component is None or not hasattr(component, stop_method):
                return
            try:
                await asyncio.wait_for(
                    getattr(component, stop_method)(),
                    timeout=component_stop_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "%s stop timed out after %.1fs",
                    name,
                    component_stop_timeout,
                )
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    logger.debug(
                        "Skipping %s stop: event loop already closed during shutdown",
                        name,
                    )
                else:
                    logger.exception("Error stopping %s: %s", name, e)
            except Exception as e:
                logger.exception("Error stopping %s: %s", name, e)

        # Stop components in reverse order
        await _stop_component("orchestrator", self._orchestrator, "stop")
        await _stop_component("bus", self._bus, "stop")
        await _stop_component("registry", self._registry, "stop")

        if self._knowledge:
            try:
                await asyncio.wait_for(
                    self._knowledge.close(),
                    timeout=max(component_stop_timeout, 10.0),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "knowledge graph stop timed out after %.1fs",
                    max(component_stop_timeout, 10.0),
                )
            except Exception as e:
                logger.exception("Error stopping knowledge graph: %s", e)

        self._started_at = None
        self._initialized = False
        logger.info("Neural Mesh system stopped")

    async def register_agent(self, agent: BaseNeuralMeshAgent) -> None:
        """
        Register and initialize an agent with the Neural Mesh.

        Uses slot-reservation pattern to prevent concurrent callers from
        double-initializing the same agent name across await points.

        Args:
            agent: The agent to register
        """
        if not self._initialized:
            raise RuntimeError("Coordinator not initialized")

        name = agent.agent_name

        if name in self._agents:
            # v250.2: Downgraded from WARNING to DEBUG — this is expected
            # when multiple init paths (bridge, initializer, task runner)
            # converge on the same singleton coordinator. The early return
            # is correct behavior; the log was just noise.
            logger.debug("Agent %s already registered (idempotent skip)", name)
            return

        # v251.4: Slot reservation — check if another coroutine is already
        # registering this name. Between the check above and self._agents[name]
        # below, there are await points (initialize, start) where another
        # coroutine could pass the same check. Reserve the slot BEFORE any await.
        if name in self._registering:
            logger.debug("Agent %s registration already in progress (skip)", name)
            return

        self._registering.add(name)

        try:
            # Initialize agent with components
            await agent.initialize(
                message_bus=self._bus,
                registry=self._registry,
                knowledge_graph=self._knowledge,
            )

            # Start if system is running
            if self._running:
                await agent.start()

            self._agents[name] = agent

            # v250.2: Downgraded from INFO to DEBUG — the caller (bridge or
            # initializer) logs its own INFO line. Having both coordinator AND
            # caller log "Registered agent" at INFO doubles the noise.
            logger.debug("Coordinator registered: %s", name)
        except Exception:
            # Rollback — allow future registration attempts for this name
            logger.debug("Agent %s registration failed, releasing slot", name)
            raise
        finally:
            self._registering.discard(name)

    async def unregister_agent(self, agent_name: str) -> bool:
        """
        Unregister and stop an agent.

        Args:
            agent_name: Name of the agent to unregister

        Returns:
            True if agent was unregistered, False if not found
        """
        if agent_name not in self._agents:
            return False

        agent = self._agents.pop(agent_name)
        await agent.stop()

        logger.info("Unregistered agent: %s", agent_name)
        return True

    def get_agent(self, agent_name: str) -> Optional[BaseNeuralMeshAgent]:
        """Get a registered agent by name."""
        return self._agents.get(agent_name)

    def get_all_agents(self) -> List[BaseNeuralMeshAgent]:
        """Get all registered agents."""
        return list(self._agents.values())

    def get_metrics(self) -> SystemMetrics:
        """Get aggregate system metrics."""
        metrics = SystemMetrics(
            system_health=self._system_health,
        )

        if self._started_at:
            metrics.uptime_seconds = (datetime.now() - self._started_at).total_seconds()

        if self._registry:
            registry_metrics = self._registry.get_metrics()
            metrics.registered_agents = registry_metrics.total_registered
            metrics.online_agents = registry_metrics.currently_online

        if self._bus:
            bus_metrics = self._bus.get_metrics()
            metrics.messages_published = bus_metrics.messages_published
            metrics.messages_delivered = bus_metrics.messages_delivered

        if self._knowledge:
            knowledge_metrics = self._knowledge.get_metrics()
            metrics.knowledge_entries = knowledge_metrics.total_entries

        if self._orchestrator:
            orchestrator_metrics = self._orchestrator.get_metrics()
            metrics.workflows_completed = orchestrator_metrics.workflows_completed

        return metrics

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform a comprehensive health check.

        Returns:
            Health status of all components
        """
        uptime_seconds = 0.0
        if self._started_at:
            uptime_seconds = (datetime.now() - self._started_at).total_seconds()

        health = {
            "healthy": self._system_health == HealthStatus.HEALTHY and self._running,
            "status": self._system_health.value,
            "uptime_seconds": uptime_seconds,
            "uptime_ms": uptime_seconds * 1000,
            "components": {},
            "agents": {},
        }

        # Check bus
        if self._bus:
            bus_metrics = self._bus.get_metrics()
            health["components"]["communication_bus"] = {
                "status": "healthy" if self._running else "stopped",
                "messages_published": bus_metrics.messages_published,
                "messages_delivered": bus_metrics.messages_delivered,
                "queue_depths": bus_metrics.queue_depths,
            }

        # Check registry
        if self._registry:
            registry_metrics = self._registry.get_metrics()
            health["components"]["agent_registry"] = {
                "status": "healthy" if self._running else "stopped",
                "registered": registry_metrics.total_registered,
                "online": registry_metrics.currently_online,
                "offline": registry_metrics.currently_offline,
            }

        # Check knowledge
        if self._knowledge:
            knowledge_metrics = self._knowledge.get_metrics()
            health["components"]["knowledge_graph"] = {
                "status": "healthy" if self._knowledge._initialized else "stopped",
                "entries": knowledge_metrics.total_entries,
                "relationships": knowledge_metrics.total_relationships,
                "cache_hit_rate": knowledge_metrics.cache_hit_rate(),
            }

        # Check orchestrator
        if self._orchestrator:
            orchestrator_metrics = self._orchestrator.get_metrics()
            health["components"]["orchestrator"] = {
                "status": "healthy" if self._running else "stopped",
                "workflows_completed": orchestrator_metrics.workflows_completed,
                "workflows_failed": orchestrator_metrics.workflows_failed,
                "active_workflows": len(self._orchestrator.get_active_workflows()),
            }

        # Check agents
        for agent in self._agents.values():
            agent_metrics = agent.get_metrics()
            health["agents"][agent.agent_name] = {
                "status": "running" if agent._running else "stopped",
                "type": agent.agent_type,
                "tasks_completed": agent_metrics.tasks_completed,
                "tasks_failed": agent_metrics.tasks_failed,
                "errors": agent_metrics.errors,
            }

        return health

    async def _health_monitor_loop(self) -> None:
        """Monitor system health periodically."""
        while self._running:
            try:
                await asyncio.sleep(30.0)  # Check every 30 seconds

                # Simple health assessment
                if self._registry:
                    registry_metrics = self._registry.get_metrics()
                    if registry_metrics.currently_online == 0:
                        self._system_health = HealthStatus.DEGRADED
                    elif registry_metrics.currently_offline > registry_metrics.currently_online:
                        self._system_health = HealthStatus.DEGRADED
                    else:
                        self._system_health = HealthStatus.HEALTHY

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Health monitor error: %s", e)
                self._system_health = HealthStatus.UNHEALTHY

    # ========================================================================
    # CROSS-SYSTEM INTEGRATION METHODS (v10.3)
    # ========================================================================

    async def register_node(
        self,
        node_name: str,
        node_type: str = "system",
        capabilities: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Register an external system node with the Neural Mesh.

        This allows MAS, CAI, Reactor-Core and other systems to integrate
        with the Neural Mesh for cross-system communication.

        Args:
            node_name: Unique identifier for the node
            node_type: Type of node (system, agent, service, coordinator)
            capabilities: List of capabilities this node provides
            metadata: Additional metadata about the node

        Returns:
            True if registration successful
        """
        if not self._initialized:
            logger.warning(f"Cannot register node '{node_name}' - coordinator not initialized")
            return False

        try:
            # Map node_type to agent_type string
            # AgentInfo uses strings for both agent_type and capabilities
            type_mapping = {
                "coordinator": "coordinator",
                "system": "system",
                "agent": "specialized",
                "service": "service",
                "external": "external",
            }
            agent_type_str = type_mapping.get(node_type, node_type)

            # Use provided capabilities or default to analysis
            node_capabilities = set(capabilities or ["analysis", "messaging"])

            # Register with the registry using individual parameters
            # The AgentRegistry.register() method expects individual parameters, not an AgentInfo object
            assert self._registry is not None, "Registry not initialized"
            await self._registry.register(
                agent_name=node_name,
                agent_type=agent_type_str,
                capabilities=node_capabilities,
                backend="external",  # Mark as external system
                version="1.0.0",
                dependencies=None,
                metadata=metadata or {},
            )

            logger.info(f"[NEURAL-MESH] Registered external node: {node_name} (type={agent_type_str})")
            return True

        except Exception as e:
            logger.error(f"[NEURAL-MESH] Failed to register node '{node_name}': {e}")
            return False

    async def subscribe(
        self,
        topic: str,
        callback: Callable,
        subscriber_id: Optional[str] = None,
    ) -> bool:
        """
        Subscribe to messages on a topic.

        This allows external systems to receive messages from the Neural Mesh.

        v18.0: Enhanced with robust error handling and dynamic type resolution.

        Args:
            topic: Topic to subscribe to (e.g., "safety_events", "training_events")
            callback: Async callback function to call when message received
            subscriber_id: Optional unique ID for the subscriber

        Returns:
            True if subscription successful
        """
        if not self._initialized:
            logger.warning(f"Cannot subscribe to '{topic}' - coordinator not initialized")
            return False

        try:
            from .data_models import MessageType

            # v18.0: Dynamic topic-to-MessageType mapping with fallback safety
            # This resolves enum types safely to prevent AttributeError crashes
            def _safe_get_message_type(type_name: str) -> MessageType:
                """Safely get MessageType by name with fallback to BROADCAST."""
                try:
                    return MessageType[type_name]
                except KeyError:
                    logger.warning(
                        f"[NEURAL-MESH] MessageType.{type_name} not found, "
                        f"falling back to BROADCAST"
                    )
                    return MessageType.BROADCAST

            # Map topic to message type name (strings for safety)
            topic_type_names = {
                "safety_events": "BROADCAST",
                "training_events": "BROADCAST",
                "agent_events": "NOTIFICATION",
                "system_events": "BROADCAST",
                "context_sync": "BROADCAST",
                "state_sync": "BROADCAST",
                "model_updates": "BROADCAST",
                "error_events": "NOTIFICATION",
                "health_events": "NOTIFICATION",
            }

            # Get the type name for this topic (default to BROADCAST)
            type_name = topic_type_names.get(topic, "BROADCAST")
            message_type = _safe_get_message_type(type_name)

            # Subscribe via the bus
            # Bus.subscribe expects: agent_name, message_type, callback
            assert self._bus is not None, "Bus not initialized"
            agent_name = subscriber_id or f"external_{topic}"
            await self._bus.subscribe(
                agent_name=agent_name,
                message_type=message_type,
                callback=callback,
            )

            logger.info(
                f"[NEURAL-MESH] Subscribed to topic: {topic} "
                f"(type: {message_type.value}, subscriber: {agent_name})"
            )
            return True

        except Exception as e:
            logger.error(
                f"[NEURAL-MESH] Failed to subscribe to '{topic}': {type(e).__name__}: {e}",
                exc_info=True
            )
            return False

    async def publish_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        target_nodes: Optional[List[str]] = None,
    ) -> bool:
        """
        Publish an event to the Neural Mesh.

        Args:
            event_type: Type of event (e.g., "safety_audit", "training_complete")
            payload: Event payload data
            target_nodes: Optional list of specific nodes to send to

        Returns:
            True if publish successful
        """
        if not self._initialized:
            logger.warning(f"Cannot publish event '{event_type}' - coordinator not initialized")
            return False

        try:
            from .data_models import AgentMessage, MessageType
            import uuid

            assert self._bus is not None, "Bus not initialized"
            message = AgentMessage(
                message_id=str(uuid.uuid4())[:8],
                from_agent="neural_mesh_coordinator",
                to_agent="broadcast",
                message_type=MessageType.BROADCAST,
                payload={"event_type": event_type, **payload},
                metadata={"target_nodes": target_nodes} if target_nodes else {},
            )

            await self._bus.publish(message)
            logger.debug(f"[NEURAL-MESH] Published event: {event_type}")
            return True

        except Exception as e:
            logger.error(f"[NEURAL-MESH] Failed to publish event '{event_type}': {e}")
            return False

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"NeuralMeshCoordinator("
            f"initialized={self._initialized}, "
            f"running={self._running}, "
            f"agents={len(self._agents)}, "
            f"health={self._system_health.value}"
            f")"
        )


# Singleton instance for global access
_coordinator: Optional[NeuralMeshCoordinator] = None


async def get_neural_mesh() -> NeuralMeshCoordinator:
    """
    Get the global Neural Mesh coordinator.

    Creates and initializes the coordinator if not already done.

    Returns:
        The global NeuralMeshCoordinator instance
    """
    global _coordinator

    if _coordinator is None:
        _coordinator = NeuralMeshCoordinator()
    if not _coordinator._initialized:
        await _coordinator.initialize()

    return _coordinator


async def start_neural_mesh() -> NeuralMeshCoordinator:
    """
    Get and start the global Neural Mesh coordinator.

    Returns:
        The started NeuralMeshCoordinator instance
    """
    coordinator = await get_neural_mesh()
    if not coordinator._running:
        await coordinator.start()
    return coordinator


async def stop_neural_mesh() -> None:
    """Stop the global Neural Mesh coordinator."""
    global _coordinator

    if _coordinator is not None:
        await _coordinator.stop()
        _coordinator = None

    # Also stop optional monitoring singletons if they were started.
    try:
        from .monitoring import (
            shutdown_metrics_collector,
            shutdown_health_monitor,
            shutdown_trace_manager,
        )
        await shutdown_trace_manager()
        await shutdown_health_monitor()
        await shutdown_metrics_collector()
    except Exception:
        pass

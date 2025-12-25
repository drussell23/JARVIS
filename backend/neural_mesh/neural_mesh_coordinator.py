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

    async def start(self) -> None:
        """
        Start all Neural Mesh components.

        This starts background tasks like message processing,
        health monitoring, and cleanup.
        """
        if not self._initialized:
            raise RuntimeError("Coordinator not initialized. Call initialize() first.")

        if self._running:
            logger.warning("Coordinator already running")
            return

        logger.info("Starting Neural Mesh system...")

        # Start components
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
        if not self._running:
            return

        logger.info("Stopping Neural Mesh system...")

        self._running = False
        self._system_health = HealthStatus.UNKNOWN

        # Cancel health monitor
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop all agents
        for agent in self._agents.values():
            try:
                await agent.stop()
            except Exception as e:
                logger.exception("Error stopping agent %s: %s", agent.agent_name, e)

        # Stop components in reverse order
        if self._orchestrator:
            await self._orchestrator.stop()

        if self._bus:
            await self._bus.stop()

        if self._registry:
            await self._registry.stop()

        if self._knowledge:
            await self._knowledge.close()

        logger.info("Neural Mesh system stopped")

    async def register_agent(self, agent: BaseNeuralMeshAgent) -> None:
        """
        Register and initialize an agent with the Neural Mesh.

        Args:
            agent: The agent to register
        """
        if not self._initialized:
            raise RuntimeError("Coordinator not initialized")

        if agent.agent_name in self._agents:
            logger.warning("Agent %s already registered", agent.agent_name)
            return

        # Initialize agent with components
        await agent.initialize(
            message_bus=self._bus,
            registry=self._registry,
            knowledge_graph=self._knowledge,
        )

        # Start if system is running
        if self._running:
            await agent.start()

        self._agents[agent.agent_name] = agent

        logger.info("Registered agent: %s", agent.agent_name)

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
            node_type: Type of node (system, agent, service)
            capabilities: List of capabilities this node provides
            metadata: Additional metadata about the node

        Returns:
            True if registration successful
        """
        if not self._initialized:
            logger.warning(f"Cannot register node '{node_name}' - coordinator not initialized")
            return False

        try:
            # Create an agent info for the external node
            from .data_models import AgentInfo, AgentType, AgentCapability

            # Map capabilities to AgentCapability enum
            agent_capabilities = []
            for cap in (capabilities or []):
                try:
                    agent_capabilities.append(AgentCapability(cap))
                except ValueError:
                    # Unknown capability, add as metadata
                    pass

            agent_info = AgentInfo(
                agent_id=node_name,
                agent_name=node_name,
                agent_type=AgentType.COORDINATOR if node_type == "coordinator" else AgentType.SPECIALIZED,
                capabilities=agent_capabilities or [AgentCapability.ANALYSIS],
                version="1.0.0",
                metadata=metadata or {},
            )

            # Register with the registry
            await self._registry.register(agent_info)

            logger.info(f"[NEURAL-MESH] Registered external node: {node_name} (type={node_type})")
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

            # Map topic to message type
            topic_map = {
                "safety_events": MessageType.BROADCAST,
                "training_events": MessageType.BROADCAST,
                "agent_events": MessageType.NOTIFICATION,
                "system_events": MessageType.BROADCAST,
            }

            message_type = topic_map.get(topic, MessageType.BROADCAST)

            # Subscribe via the bus
            await self._bus.subscribe(
                subscriber_id=subscriber_id or f"external_{topic}",
                message_types=[message_type],
                callback=callback,
            )

            logger.info(f"[NEURAL-MESH] Subscribed to topic: {topic}")
            return True

        except Exception as e:
            logger.error(f"[NEURAL-MESH] Failed to subscribe to '{topic}': {e}")
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
            from .data_models import Message, MessageType
            import uuid

            message = Message(
                message_id=str(uuid.uuid4())[:8],
                sender_id="neural_mesh_coordinator",
                message_type=MessageType.BROADCAST,
                topic=event_type,
                payload=payload,
                recipients=target_nodes,
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

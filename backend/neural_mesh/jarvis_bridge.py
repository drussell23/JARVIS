"""
JARVIS Neural Mesh Bridge

Central integration module that connects all JARVIS systems to the Neural Mesh.
This is the main entry point for enabling multi-agent collaboration across the
entire JARVIS ecosystem.

The bridge provides:
- Auto-discovery of existing JARVIS agents
- Automatic adapter creation and registration
- System-wide event coordination
- Health monitoring across all agents
- Graceful startup and shutdown

Usage:
    from neural_mesh.jarvis_bridge import JARVISNeuralMeshBridge

    # Create and start the bridge
    bridge = JARVISNeuralMeshBridge()
    await bridge.initialize()
    await bridge.start()

    # Access any agent
    uae_adapter = bridge.get_agent("intelligence_uae")
    result = await uae_adapter.execute_task({
        "action": "analyze_workspace",
        "input": {"space_id": 3}
    })

    # Or use the coordinator directly
    coordinator = bridge.coordinator
    result = await coordinator.orchestrator.execute_workflow(...)

    # Graceful shutdown
    await bridge.stop()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
    Union,
)

from .neural_mesh_coordinator import NeuralMeshCoordinator, get_neural_mesh
from .base.base_neural_mesh_agent import BaseNeuralMeshAgent
from .data_models import (
    AgentInfo,
    AgentStatus,
    KnowledgeType,
    MessagePriority,
    MessageType,
)
from .adapters.legacy_agent_adapter import LegacyAgentAdapter, adapt_agent
from .adapters.intelligence_adapter import (
    IntelligenceEngineAdapter,
    IntelligenceEngineType,
    create_uae_adapter,
    create_sai_adapter,
    create_cai_adapter,  # v6.2: Contextual Awareness Intelligence
    create_cot_adapter,
    create_rge_adapter,
    create_pie_adapter,
)
from .adapters.autonomy_adapter import (
    AutonomyEngineAdapter,
    AutonomyComponentType,
    create_autonomous_agent_adapter,
    create_reasoning_adapter,
    create_tool_orchestrator_adapter,
    create_memory_adapter,
)
from .adapters.voice_adapter import (
    VoiceSystemAdapter,
    VoiceComponentType,
    create_voice_memory_adapter,
    create_speaker_verification_adapter,
    create_voice_unlock_adapter,
)
from .adapters.vision_adapter import (
    VisionCognitiveAdapter,
    VisionComponentType,
    create_vision_cognitive_adapter,
    create_yabai_adapter,
)

logger = logging.getLogger(__name__)


class SystemCategory(str, Enum):
    """Categories of JARVIS systems."""
    INTELLIGENCE = "intelligence"
    AUTONOMY = "autonomy"
    VOICE = "voice"
    VISION = "vision"  # v10.2: Vision Cognitive Loop
    DISPLAY = "display"
    CORE = "core"
    TOOLS = "tools"


@dataclass
class AgentDiscoveryConfig:
    """Configuration for agent auto-discovery."""
    # Categories to discover
    enabled_categories: Set[SystemCategory] = field(
        default_factory=lambda: {
            SystemCategory.INTELLIGENCE,
            SystemCategory.AUTONOMY,
            SystemCategory.VOICE,
            SystemCategory.VISION,  # v10.2: Vision Cognitive Loop
        }
    )

    # Specific agents to skip
    skip_agents: Set[str] = field(default_factory=set)

    # Custom agent factories
    custom_factories: Dict[str, Callable] = field(default_factory=dict)

    # Auto-initialize agents
    auto_initialize: bool = True

    # Parallel initialization
    parallel_init: bool = True
    max_parallel: int = 10

    # Retry configuration
    retry_on_failure: bool = True
    max_retries: int = 3
    retry_delay: float = 1.0


@dataclass
class BridgeMetrics:
    """Metrics for the Neural Mesh Bridge."""
    started_at: Optional[datetime] = None
    agents_registered: int = 0
    agents_failed: int = 0
    messages_routed: int = 0
    knowledge_entries: int = 0
    workflows_completed: int = 0
    errors: int = 0


class JARVISNeuralMeshBridge:
    """
    Central bridge connecting all JARVIS systems to Neural Mesh.

    This class provides a unified interface for:
    - Discovering and adapting existing JARVIS agents
    - Starting the Neural Mesh ecosystem
    - Coordinating multi-agent workflows
    - Monitoring system health

    Example - Full ecosystem startup:
        bridge = JARVISNeuralMeshBridge()
        await bridge.initialize()
        await bridge.start()

        # All agents are now connected and communicating
        # Execute cross-system workflows
        result = await bridge.execute_cross_system_task(
            "Analyze workspace and suggest improvements",
            systems=["intelligence", "autonomy"]
        )

    Example - Custom agent registration:
        bridge = JARVISNeuralMeshBridge()

        # Add custom agent before starting
        my_agent = MyCustomAgent()
        bridge.add_custom_agent("my_agent", my_agent)

        await bridge.initialize()
        await bridge.start()
    """

    def __init__(
        self,
        config: Optional[AgentDiscoveryConfig] = None,
        coordinator: Optional[NeuralMeshCoordinator] = None,
    ) -> None:
        """Initialize the bridge.

        Args:
            config: Discovery configuration
            coordinator: Existing coordinator (creates new if None)
        """
        self._config = config or AgentDiscoveryConfig()
        self._coordinator = coordinator
        self._initialized = False
        self._running = False

        # Agent tracking
        self._adapters: Dict[str, BaseNeuralMeshAgent] = {}
        self._failed_agents: Dict[str, str] = {}  # name -> error
        self._custom_agents: Dict[str, Any] = {}

        # System state
        self._metrics = BridgeMetrics()
        self._startup_tasks: List[asyncio.Task] = []

        # Event callbacks
        self._callbacks: Dict[str, List[Callable]] = {
            "agent_registered": [],
            "agent_failed": [],
            "system_ready": [],
            "system_error": [],
        }

    async def initialize(self) -> bool:
        """Initialize the bridge and Neural Mesh coordinator.

        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        logger.info("Initializing JARVIS Neural Mesh Bridge...")

        try:
            # Get or create coordinator
            if self._coordinator is None:
                self._coordinator = await get_neural_mesh()

            # Initialize coordinator
            await self._coordinator.initialize()

            self._initialized = True
            logger.info("JARVIS Neural Mesh Bridge initialized")
            return True

        except Exception as e:
            logger.error("Failed to initialize bridge: %s", e)
            self._metrics.errors += 1
            return False

    async def start(self) -> bool:
        """Start the bridge and all discovered agents.

        Returns:
            True if startup successful
        """
        if not self._initialized:
            if not await self.initialize():
                return False

        if self._running:
            return True

        logger.info("Starting JARVIS Neural Mesh Bridge...")
        self._metrics.started_at = datetime.utcnow()

        try:
            # Start coordinator
            await self._coordinator.start()

            # Discover and register agents
            await self._discover_and_register_agents()

            # Register custom agents
            for name, agent in self._custom_agents.items():
                await self._register_custom_agent(name, agent)

            self._running = True

            # Trigger callbacks
            await self._trigger_callback("system_ready", {
                "agents": len(self._adapters),
                "failed": len(self._failed_agents),
            })

            logger.info(
                "JARVIS Neural Mesh Bridge started: %d agents registered, %d failed",
                len(self._adapters),
                len(self._failed_agents),
            )
            return True

        except Exception as e:
            logger.error("Failed to start bridge: %s", e)
            self._metrics.errors += 1
            await self._trigger_callback("system_error", {"error": str(e)})
            return False

    async def stop(self) -> None:
        """Stop the bridge and all agents gracefully."""
        if not self._running:
            return

        logger.info("Stopping JARVIS Neural Mesh Bridge...")

        try:
            # Cancel any pending startup tasks
            for task in self._startup_tasks:
                if not task.done():
                    task.cancel()

            # Stop coordinator (will stop all agents)
            if self._coordinator:
                await self._coordinator.stop()

            self._running = False
            logger.info("JARVIS Neural Mesh Bridge stopped")

        except Exception as e:
            logger.error("Error stopping bridge: %s", e)

    async def _discover_and_register_agents(self) -> None:
        """Discover and register all available agents."""
        logger.info("Discovering JARVIS agents...")

        discovery_tasks = []

        # Intelligence agents
        if SystemCategory.INTELLIGENCE in self._config.enabled_categories:
            discovery_tasks.extend([
                self._discover_intelligence_agents(),
            ])

        # Autonomy agents
        if SystemCategory.AUTONOMY in self._config.enabled_categories:
            discovery_tasks.extend([
                self._discover_autonomy_agents(),
            ])

        # Voice agents
        if SystemCategory.VOICE in self._config.enabled_categories:
            discovery_tasks.extend([
                self._discover_voice_agents(),
            ])

        # Vision agents (v10.2)
        if SystemCategory.VISION in self._config.enabled_categories:
            discovery_tasks.extend([
                self._discover_vision_agents(),
            ])

        # Run discovery in parallel
        if discovery_tasks:
            await asyncio.gather(*discovery_tasks, return_exceptions=True)

        logger.info(
            "Agent discovery complete: %d agents found",
            len(self._adapters),
        )

    async def _discover_intelligence_agents(self) -> None:
        """Discover and register intelligence agents.

        v6.2 Grand Unification: Now includes CAI (Contextual Awareness Intelligence)
        for deep contextual understanding, emotional intelligence, and adaptive behavior.
        """
        intelligence_agents = [
            ("intelligence_uae", create_uae_adapter, IntelligenceEngineType.UAE),
            ("intelligence_sai", create_sai_adapter, IntelligenceEngineType.SAI),
            ("intelligence_cai", create_cai_adapter, IntelligenceEngineType.CAI),  # v6.2: CAI
            ("intelligence_cot", create_cot_adapter, IntelligenceEngineType.COT),
            ("intelligence_rge", create_rge_adapter, IntelligenceEngineType.RGE),
            ("intelligence_pie", create_pie_adapter, IntelligenceEngineType.PIE),
        ]

        for name, factory, engine_type in intelligence_agents:
            if name in self._config.skip_agents:
                continue

            await self._try_register_agent(
                name,
                factory,
                {"agent_name": name},
                f"intelligence.{engine_type.value}",
            )

    async def _discover_autonomy_agents(self) -> None:
        """Discover and register autonomy agents."""
        autonomy_agents = [
            ("autonomy_agent", create_autonomous_agent_adapter),
            ("autonomy_reasoning", create_reasoning_adapter),
            ("autonomy_tools", create_tool_orchestrator_adapter),
            ("autonomy_memory", create_memory_adapter),
        ]

        for name, factory in autonomy_agents:
            if name in self._config.skip_agents:
                continue

            await self._try_register_agent(
                name,
                factory,
                {"agent_name": name},
                "autonomy",
            )

    async def _discover_voice_agents(self) -> None:
        """Discover and register voice agents."""
        voice_agents = [
            ("voice_memory", create_voice_memory_adapter),
            ("voice_verification", create_speaker_verification_adapter),
            ("voice_unlock", create_voice_unlock_adapter),
        ]

        for name, factory in voice_agents:
            if name in self._config.skip_agents:
                continue

            await self._try_register_agent(
                name,
                factory,
                {"agent_name": name},
                "voice",
            )

    async def _discover_vision_agents(self) -> None:
        """Discover and register vision agents (v10.2)."""
        vision_agents = [
            ("vision_cognitive_loop", create_vision_cognitive_adapter),
            ("vision_yabai_multispace", create_yabai_adapter),
        ]

        for name, factory in vision_agents:
            if name in self._config.skip_agents:
                continue

            await self._try_register_agent(
                name,
                factory,
                {"agent_name": name},
                "vision",
            )

    async def _try_register_agent(
        self,
        name: str,
        factory: Callable,
        factory_kwargs: Dict[str, Any],
        category: str,
    ) -> Optional[BaseNeuralMeshAgent]:
        """Try to create and register an agent.

        Args:
            name: Agent name
            factory: Factory function to create adapter
            factory_kwargs: Arguments for factory
            category: Agent category for logging

        Returns:
            Registered adapter or None on failure
        """
        retries = 0
        while retries <= self._config.max_retries:
            try:
                logger.debug("Creating adapter for %s (%s)...", name, category)

                # Create adapter
                adapter = await factory(**factory_kwargs)

                # Register with coordinator
                await self._coordinator.register_agent(adapter)

                # Track
                self._adapters[name] = adapter
                self._metrics.agents_registered += 1

                await self._trigger_callback("agent_registered", {
                    "name": name,
                    "category": category,
                })

                logger.info("Registered agent: %s", name)
                return adapter

            except ImportError as e:
                # Module not available - skip
                logger.debug(
                    "Skipping %s: module not available (%s)",
                    name,
                    e,
                )
                return None

            except Exception as e:
                retries += 1
                if retries <= self._config.max_retries and self._config.retry_on_failure:
                    logger.warning(
                        "Failed to register %s (attempt %d/%d): %s",
                        name,
                        retries,
                        self._config.max_retries,
                        e,
                    )
                    await asyncio.sleep(self._config.retry_delay)
                else:
                    self._failed_agents[name] = str(e)
                    self._metrics.agents_failed += 1
                    await self._trigger_callback("agent_failed", {
                        "name": name,
                        "error": str(e),
                    })
                    logger.error("Failed to register %s: %s", name, e)
                    return None

        return None

    async def _register_custom_agent(
        self,
        name: str,
        agent: Any,
    ) -> Optional[BaseNeuralMeshAgent]:
        """Register a custom agent.

        Args:
            name: Agent name
            agent: Agent instance (can be adapted or raw)

        Returns:
            Registered agent or None on failure
        """
        try:
            # If already a BaseNeuralMeshAgent, register directly
            if isinstance(agent, BaseNeuralMeshAgent):
                await self._coordinator.register_agent(agent)
                self._adapters[name] = agent
            else:
                # Wrap with legacy adapter
                adapted = adapt_agent(
                    agent,
                    name=name,
                    agent_type="custom",
                    capabilities=set(),
                )
                await self._coordinator.register_agent(adapted)
                self._adapters[name] = adapted

            self._metrics.agents_registered += 1
            logger.info("Registered custom agent: %s", name)
            return self._adapters[name]

        except Exception as e:
            self._failed_agents[name] = str(e)
            self._metrics.agents_failed += 1
            logger.error("Failed to register custom agent %s: %s", name, e)
            return None

    def add_custom_agent(self, name: str, agent: Any) -> None:
        """Add a custom agent to be registered on start.

        Args:
            name: Agent name
            agent: Agent instance
        """
        self._custom_agents[name] = agent

    def on(self, event: str, callback: Callable) -> None:
        """Register an event callback.

        Args:
            event: Event name (agent_registered, agent_failed, system_ready, system_error)
            callback: Callback function
        """
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    async def _trigger_callback(self, event: str, data: Dict[str, Any]) -> None:
        """Trigger event callbacks."""
        if event not in self._callbacks:
            return

        for callback in self._callbacks[event]:
            try:
                result = callback(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("Error in %s callback: %s", event, e)

    # =========================================================================
    # Public API
    # =========================================================================

    def get_agent(self, name: str) -> Optional[BaseNeuralMeshAgent]:
        """Get a registered agent by name.

        Args:
            name: Agent name

        Returns:
            Agent adapter or None
        """
        return self._adapters.get(name)

    def get_agents_by_category(
        self,
        category: str,
    ) -> List[BaseNeuralMeshAgent]:
        """Get all agents in a category.

        Args:
            category: Category prefix (intelligence, autonomy, voice)

        Returns:
            List of agents
        """
        return [
            agent for name, agent in self._adapters.items()
            if name.startswith(category)
        ]

    def get_agents_by_capability(
        self,
        capability: str,
    ) -> List[BaseNeuralMeshAgent]:
        """Get all agents with a specific capability.

        Args:
            capability: Capability name

        Returns:
            List of agents
        """
        return [
            agent for agent in self._adapters.values()
            if capability in agent.capabilities
        ]

    async def execute_cross_system_task(
        self,
        task_description: str,
        systems: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a task that spans multiple systems.

        Args:
            task_description: Natural language task description
            systems: List of system categories to involve
            context: Additional context

        Returns:
            Aggregated results from all systems
        """
        systems = systems or ["intelligence", "autonomy"]
        results = {}

        # Get agents from requested systems
        agents = []
        for system in systems:
            agents.extend(self.get_agents_by_category(system))

        if not agents:
            return {"error": "No agents available for requested systems"}

        # Use orchestrator for multi-agent coordination
        orchestrator = self._coordinator.orchestrator

        # Create workflow tasks
        from .data_models import WorkflowTask
        workflow_tasks = []

        for agent in agents:
            # Find relevant capability
            primary_cap = next(iter(agent.capabilities), "execute")
            workflow_tasks.append(WorkflowTask(
                task_id=f"task_{agent.agent_name}",
                required_capability=primary_cap,
                payload={
                    "action": "process_query" if "intelligence" in agent.agent_name else "run",
                    "input": {
                        "query": task_description,
                        "goal": task_description,
                        "context": context or {},
                    },
                },
                timeout_seconds=30.0,
            ))

        # Execute workflow
        from .data_models import ExecutionStrategy
        workflow_result = await orchestrator.execute_workflow(
            name=f"cross_system_{datetime.utcnow().timestamp()}",
            tasks=workflow_tasks,
            strategy=ExecutionStrategy.PARALLEL,
        )

        return {
            "workflow_id": workflow_result.workflow_id,
            "success": workflow_result.success,
            "results": workflow_result.task_results,
            "duration_ms": workflow_result.duration_ms,
        }

    async def broadcast_event(
        self,
        event_name: str,
        payload: Dict[str, Any],
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> None:
        """Broadcast an event to all agents.

        Args:
            event_name: Event name
            payload: Event payload
            priority: Message priority
        """
        if not self._coordinator or not self._coordinator.bus:
            return

        await self._coordinator.bus.broadcast(
            from_agent="jarvis_bridge",
            message_type=MessageType.CUSTOM,
            payload={
                "event": event_name,
                **payload,
            },
            priority=priority,
        )

    async def health_check(self) -> Dict[str, Any]:
        """Get health status of all systems.

        Returns:
            Health status dictionary
        """
        if not self._coordinator:
            return {"healthy": False, "error": "Coordinator not initialized"}

        # Get coordinator health
        coordinator_health = await self._coordinator.health_check()

        # Get per-agent health
        agent_health = {}
        for name, agent in self._adapters.items():
            try:
                metrics = agent.get_metrics()
                agent_health[name] = {
                    "status": "healthy",
                    "tasks_completed": metrics.tasks_completed,
                    "error_count": metrics.error_count,
                }
            except Exception as e:
                agent_health[name] = {
                    "status": "unhealthy",
                    "error": str(e),
                }

        return {
            "healthy": coordinator_health.get("healthy", False),
            "coordinator": coordinator_health,
            "agents": agent_health,
            "metrics": {
                "agents_registered": self._metrics.agents_registered,
                "agents_failed": self._metrics.agents_failed,
                "errors": self._metrics.errors,
                "uptime_seconds": (
                    (datetime.utcnow() - self._metrics.started_at).total_seconds()
                    if self._metrics.started_at else 0
                ),
            },
            "failed_agents": self._failed_agents,
        }

    def get_metrics(self) -> BridgeMetrics:
        """Get bridge metrics.

        Returns:
            Current metrics
        """
        return self._metrics

    @property
    def coordinator(self) -> Optional[NeuralMeshCoordinator]:
        """Access the Neural Mesh coordinator."""
        return self._coordinator

    @property
    def is_running(self) -> bool:
        """Check if bridge is running."""
        return self._running

    @property
    def registered_agents(self) -> List[str]:
        """Get list of registered agent names."""
        return list(self._adapters.keys())


# =============================================================================
# Global Bridge Instance
# =============================================================================

_global_bridge: Optional[JARVISNeuralMeshBridge] = None


async def get_jarvis_bridge() -> JARVISNeuralMeshBridge:
    """Get or create the global JARVIS Neural Mesh Bridge.

    Returns:
        The global bridge instance
    """
    global _global_bridge

    if _global_bridge is None:
        _global_bridge = JARVISNeuralMeshBridge()

    return _global_bridge


async def start_jarvis_neural_mesh() -> JARVISNeuralMeshBridge:
    """Start the global JARVIS Neural Mesh Bridge.

    Returns:
        The started bridge instance
    """
    bridge = await get_jarvis_bridge()
    await bridge.initialize()
    await bridge.start()
    return bridge


async def stop_jarvis_neural_mesh() -> None:
    """Stop the global JARVIS Neural Mesh Bridge."""
    global _global_bridge

    if _global_bridge:
        await _global_bridge.stop()
        _global_bridge = None


# =============================================================================
# Convenience Functions
# =============================================================================

async def execute_multi_agent_task(
    task: str,
    systems: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute a task using the Neural Mesh.

    Convenience function that starts the bridge if needed.

    Args:
        task: Task description
        systems: Systems to involve

    Returns:
        Task results
    """
    bridge = await get_jarvis_bridge()

    if not bridge.is_running:
        await bridge.initialize()
        await bridge.start()

    return await bridge.execute_cross_system_task(task, systems)


async def get_agent(name: str) -> Optional[BaseNeuralMeshAgent]:
    """Get an agent from the global bridge.

    Args:
        name: Agent name

    Returns:
        Agent or None
    """
    bridge = await get_jarvis_bridge()
    return bridge.get_agent(name)

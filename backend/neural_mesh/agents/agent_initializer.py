"""
JARVIS Neural Mesh - Agent Initializer

Handles automatic registration and initialization of all production agents
with the Neural Mesh coordinator. Provides a single entry point for
bootstrapping the agent ecosystem.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Type

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..neural_mesh_coordinator import NeuralMeshCoordinator

from .memory_agent import MemoryAgent
from .coordinator_agent import CoordinatorAgent
from .health_monitor_agent import HealthMonitorAgent
from .context_tracker_agent import ContextTrackerAgent
from .error_analyzer_agent import ErrorAnalyzerAgent
from .pattern_recognition_agent import PatternRecognitionAgent
from .google_workspace_agent import GoogleWorkspaceAgent
from .spatial_awareness_agent import SpatialAwarenessAgent  # v6.2: Grand Unification
from .predictive_planning_agent import PredictivePlanningAgent  # v6.2: Proactive Parallelism

logger = logging.getLogger(__name__)


# Registry of all available production agents
# v6.2 Grand Unification: Added SpatialAwarenessAgent for 3D OS Awareness
# v6.2 Proactive Parallelism: Added PredictivePlanningAgent ("Psychic Brain")
PRODUCTION_AGENTS: List[Type[BaseNeuralMeshAgent]] = [
    # Core agents (fundamental system operations)
    MemoryAgent,
    CoordinatorAgent,
    HealthMonitorAgent,

    # Intelligence agents (analysis and reasoning)
    ContextTrackerAgent,
    ErrorAnalyzerAgent,
    PatternRecognitionAgent,

    # Proactive Intelligence ("The Psychic Brain")
    PredictivePlanningAgent,  # v6.2: Expands intents into parallel tasks

    # Spatial agents (3D OS Awareness - "The Body")
    SpatialAwarenessAgent,  # v6.2: Proprioception for all agents

    # Admin/Communication agents (Chief of Staff role)
    GoogleWorkspaceAgent,
]


class AgentInitializer:
    """
    Initializer for production agents in the Neural Mesh.

    This class handles:
    - Discovery of available agents
    - Ordered initialization (core agents first)
    - Registration with the coordinator
    - Health verification after startup
    - Graceful shutdown

    Usage:
        coordinator = NeuralMeshCoordinator()
        await coordinator.initialize()
        await coordinator.start()

        initializer = AgentInitializer(coordinator)
        agents = await initializer.initialize_all_agents()

        # Later
        await initializer.shutdown_all_agents()
    """

    def __init__(self, coordinator: NeuralMeshCoordinator) -> None:
        """
        Initialize the agent initializer.

        Args:
            coordinator: The Neural Mesh coordinator to register agents with
        """
        self.coordinator = coordinator
        self._registered_agents: Dict[str, BaseNeuralMeshAgent] = {}
        self._initialization_order: List[str] = []

    @property
    def registered_agents(self) -> Dict[str, BaseNeuralMeshAgent]:
        """Get all registered agents."""
        return self._registered_agents.copy()

    @property
    def agent_count(self) -> int:
        """Get count of registered agents."""
        return len(self._registered_agents)

    async def initialize_all_agents(
        self,
        exclude: Optional[List[str]] = None,
        include_only: Optional[List[str]] = None,
    ) -> Dict[str, BaseNeuralMeshAgent]:
        """
        Initialize and register all production agents.

        Args:
            exclude: Optional list of agent names to skip
            include_only: Optional list of agent names to include (ignores exclude)

        Returns:
            Dictionary of registered agents by name
        """
        exclude = exclude or []

        logger.info(f"Initializing {len(PRODUCTION_AGENTS)} production agents...")

        # Determine which agents to initialize
        agents_to_init = []
        for agent_class in PRODUCTION_AGENTS:
            agent_name = agent_class.__name__

            if include_only:
                if agent_name in include_only or agent_name.lower() in include_only:
                    agents_to_init.append(agent_class)
            elif agent_name not in exclude and agent_name.lower() not in exclude:
                agents_to_init.append(agent_class)

        # Initialize agents in order (core first, then intelligence)
        core_agents = []
        intelligence_agents = []
        other_agents = []

        for agent_class in agents_to_init:
            # Peek at agent type without full instantiation
            try:
                temp = agent_class()
                if temp.agent_type == "core":
                    core_agents.append(agent_class)
                elif temp.agent_type == "intelligence":
                    intelligence_agents.append(agent_class)
                else:
                    other_agents.append(agent_class)
            except Exception:
                other_agents.append(agent_class)

        ordered_agents = core_agents + intelligence_agents + other_agents

        # Initialize each agent
        for agent_class in ordered_agents:
            try:
                agent = await self._initialize_agent(agent_class)
                if agent:
                    self._registered_agents[agent.agent_name] = agent
                    self._initialization_order.append(agent.agent_name)
                    logger.info(f"  Registered: {agent.agent_name} ({agent.agent_type})")
            except Exception as e:
                logger.exception(f"Failed to initialize {agent_class.__name__}: {e}")

        logger.info(
            f"Agent initialization complete: {len(self._registered_agents)} agents registered"
        )

        return self._registered_agents

    async def _initialize_agent(
        self,
        agent_class: Type[BaseNeuralMeshAgent],
    ) -> Optional[BaseNeuralMeshAgent]:
        """
        Initialize and register a single agent.

        Args:
            agent_class: The agent class to instantiate

        Returns:
            The initialized agent, or None if failed
        """
        try:
            # Create agent instance
            agent = agent_class()

            # Register with coordinator (this handles init and start)
            await self.coordinator.register_agent(agent)

            return agent
        except Exception as e:
            logger.exception(f"Error initializing {agent_class.__name__}: {e}")
            return None

    async def initialize_agent(
        self,
        agent_class: Type[BaseNeuralMeshAgent],
    ) -> Optional[BaseNeuralMeshAgent]:
        """
        Initialize a single agent by class.

        Args:
            agent_class: The agent class to instantiate

        Returns:
            The initialized agent, or None if failed
        """
        agent = await self._initialize_agent(agent_class)
        if agent:
            self._registered_agents[agent.agent_name] = agent
            self._initialization_order.append(agent.agent_name)
        return agent

    async def shutdown_all_agents(self) -> None:
        """Shutdown all registered agents in reverse order."""
        logger.info(f"Shutting down {len(self._registered_agents)} agents...")

        # Shutdown in reverse order of initialization
        for agent_name in reversed(self._initialization_order):
            agent = self._registered_agents.get(agent_name)
            if agent:
                try:
                    await self.coordinator.unregister_agent(agent_name)
                    logger.info(f"  Shutdown: {agent_name}")
                except Exception as e:
                    logger.exception(f"Error shutting down {agent_name}: {e}")

        self._registered_agents.clear()
        self._initialization_order.clear()

        logger.info("All agents shutdown complete")

    async def verify_agents(self) -> Dict[str, Dict[str, Any]]:
        """
        Verify all registered agents are healthy.

        Returns:
            Dictionary of agent health statuses
        """
        statuses = {}

        for agent_name, agent in self._registered_agents.items():
            try:
                metrics = agent.get_metrics()
                statuses[agent_name] = {
                    "healthy": agent._running,
                    "agent_type": agent.agent_type,
                    "capabilities": list(agent.capabilities),
                    "tasks_completed": metrics.tasks_completed,
                    "tasks_failed": metrics.tasks_failed,
                    "errors": metrics.errors,
                }
            except Exception as e:
                statuses[agent_name] = {
                    "healthy": False,
                    "error": str(e),
                }

        return statuses

    def get_agent(self, agent_name: str) -> Optional[BaseNeuralMeshAgent]:
        """Get a registered agent by name."""
        return self._registered_agents.get(agent_name)

    def get_agents_by_type(self, agent_type: str) -> List[BaseNeuralMeshAgent]:
        """Get all agents of a specific type."""
        return [
            agent for agent in self._registered_agents.values()
            if agent.agent_type == agent_type
        ]

    def get_agents_by_capability(self, capability: str) -> List[BaseNeuralMeshAgent]:
        """Get all agents with a specific capability."""
        return [
            agent for agent in self._registered_agents.values()
            if capability in agent.capabilities
        ]


# Global initializer instance
_initializer: Optional[AgentInitializer] = None


async def get_agent_initializer(
    coordinator: Optional[NeuralMeshCoordinator] = None,
) -> AgentInitializer:
    """
    Get the global agent initializer.

    Args:
        coordinator: Coordinator to use. Required on first call.

    Returns:
        The global AgentInitializer instance
    """
    global _initializer

    if _initializer is None:
        if coordinator is None:
            raise ValueError("Coordinator required for first initialization")
        _initializer = AgentInitializer(coordinator)

    return _initializer


async def initialize_production_agents(
    coordinator: NeuralMeshCoordinator,
    exclude: Optional[List[str]] = None,
) -> Dict[str, BaseNeuralMeshAgent]:
    """
    Convenience function to initialize all production agents.

    Args:
        coordinator: The Neural Mesh coordinator
        exclude: Optional list of agent names to skip

    Returns:
        Dictionary of registered agents
    """
    initializer = await get_agent_initializer(coordinator)
    return await initializer.initialize_all_agents(exclude=exclude)


async def shutdown_production_agents() -> None:
    """Shutdown all production agents."""
    global _initializer

    if _initializer:
        await _initializer.shutdown_all_agents()
        _initializer = None

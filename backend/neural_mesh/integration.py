"""
JARVIS Neural Mesh - System Integration Module

Provides clean integration points for the Neural Mesh with the existing JARVIS system.

This module handles:
- Lazy initialization of Neural Mesh components
- Integration with existing JARVIS agents
- Startup/shutdown lifecycle management
- Health monitoring integration
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Global singleton instances
_neural_mesh_coordinator = None
_crew_manager = None
_initialized = False


@dataclass
class NeuralMeshConfig:
    """Configuration for Neural Mesh initialization."""
    enable_crew: bool = True
    enable_monitoring: bool = True
    enable_knowledge_graph: bool = True
    enable_communication_bus: bool = True
    lazy_load: bool = True
    verbose: bool = False


async def initialize_neural_mesh(
    config: Optional[NeuralMeshConfig] = None,
    jarvis_instance: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Initialize the Neural Mesh system.

    This is the main entry point for integrating Neural Mesh
    with the existing JARVIS system.

    Args:
        config: Configuration options
        jarvis_instance: Optional JARVIS instance to integrate

    Returns:
        Dict with status and component references
    """
    global _neural_mesh_coordinator, _crew_manager, _initialized

    if _initialized:
        logger.info("Neural Mesh already initialized")
        return {
            "status": "already_initialized",
            "coordinator": _neural_mesh_coordinator,
            "crew_manager": _crew_manager,
        }

    config = config or NeuralMeshConfig()
    start_time = datetime.utcnow()
    components_loaded = []

    try:
        logger.info("🧠 Initializing JARVIS Neural Mesh...")

        # Import Neural Mesh components
        from neural_mesh import NeuralMeshCoordinator, NeuralMeshConfig as CoordinatorConfig

        # Create coordinator
        coordinator_config = CoordinatorConfig(
            name="JARVIS-Neural-Mesh",
            enable_monitoring=config.enable_monitoring,
            enable_knowledge_graph=config.enable_knowledge_graph,
            enable_communication_bus=config.enable_communication_bus,
        )
        _neural_mesh_coordinator = NeuralMeshCoordinator(coordinator_config)
        components_loaded.append("coordinator")

        # Initialize coordinator (creates all components)
        await _neural_mesh_coordinator.initialize()
        logger.info("  ✓ Neural Mesh Coordinator initialized")

        # Start coordinator (starts all component services)
        await _neural_mesh_coordinator.start()
        logger.info("  ✓ Neural Mesh Coordinator started")

        # Initialize Crew system if enabled
        if config.enable_crew:
            crew_manager = await _initialize_crew_system()
            _crew_manager = crew_manager
            components_loaded.append("crew")
            logger.info("  ✓ Crew Multi-Agent System initialized")

        # Register existing JARVIS agents if provided
        if jarvis_instance:
            registered = await _register_jarvis_agents(jarvis_instance)
            logger.info(f"  ✓ Registered {registered} JARVIS agents")

        # Set up monitoring integration
        if config.enable_monitoring:
            await _setup_monitoring_integration()
            components_loaded.append("monitoring")
            logger.info("  ✓ Monitoring integration active")

        elapsed = (datetime.utcnow() - start_time).total_seconds()
        _initialized = True

        logger.info(f"🧠 Neural Mesh initialized in {elapsed:.2f}s")
        logger.info(f"   Components: {', '.join(components_loaded)}")

        return {
            "status": "initialized",
            "coordinator": _neural_mesh_coordinator,
            "crew_manager": _crew_manager,
            "components": components_loaded,
            "elapsed_seconds": elapsed,
        }

    except ImportError as e:
        logger.warning(f"Neural Mesh not available: {e}")
        return {"status": "unavailable", "error": str(e)}
    except Exception as e:
        logger.error(f"Neural Mesh initialization failed: {e}")
        return {"status": "failed", "error": str(e)}


async def _initialize_crew_system():
    """Initialize the Crew multi-agent collaboration system."""
    try:
        from neural_mesh.crew import (
            Crew,
            CrewBuilder,
            ProcessType,
            AgentRole,
            DelegationStrategy,
        )

        # Create the main JARVIS crew with dynamic process
        jarvis_crew = (CrewBuilder()
            .name("JARVIS Intelligence Crew")
            .description("Multi-agent collaboration system for JARVIS")
            .process(ProcessType.DYNAMIC)
            .delegation_strategy(DelegationStrategy.HYBRID)
            .memory_enabled(True)
            # Intelligence agents
            .agent(
                name="Context Analyzer",
                role=AgentRole.SPECIALIST,
                goal="Analyze context and understand user intent",
                capabilities=["context_analysis", "intent_detection", "nlp"],
            )
            .agent(
                name="Task Orchestrator",
                role=AgentRole.LEADER,
                goal="Coordinate task execution across agents",
                capabilities=["orchestration", "planning", "delegation"],
            )
            .agent(
                name="Knowledge Manager",
                role=AgentRole.SPECIALIST,
                goal="Manage and retrieve knowledge",
                capabilities=["knowledge_retrieval", "memory", "learning"],
            )
            .agent(
                name="System Controller",
                role=AgentRole.EXECUTOR,
                goal="Execute system control commands",
                capabilities=["system_control", "automation", "macos"],
            )
            .agent(
                name="Voice Processor",
                role=AgentRole.SPECIALIST,
                goal="Process voice commands and biometrics",
                capabilities=["voice", "biometrics", "authentication"],
            )
            .build())

        return jarvis_crew

    except Exception as e:
        logger.warning(f"Crew system initialization skipped: {e}")
        return None


async def _register_jarvis_agents(jarvis_instance: Any) -> int:
    """Register existing JARVIS agents with the Neural Mesh."""
    registered = 0

    if not _neural_mesh_coordinator:
        return 0

    try:
        # Register intelligence engines if available
        if hasattr(jarvis_instance, 'uae'):
            from neural_mesh.adapters import create_uae_adapter
            adapter = create_uae_adapter(jarvis_instance.uae)
            await _neural_mesh_coordinator.register_agent(adapter)
            registered += 1

        if hasattr(jarvis_instance, 'sai'):
            from neural_mesh.adapters import create_sai_adapter
            adapter = create_sai_adapter(jarvis_instance.sai)
            await _neural_mesh_coordinator.register_agent(adapter)
            registered += 1

        # Register voice components if available
        if hasattr(jarvis_instance, 'voice_unlock'):
            from neural_mesh.adapters import create_voice_unlock_adapter
            adapter = create_voice_unlock_adapter(jarvis_instance.voice_unlock)
            await _neural_mesh_coordinator.register_agent(adapter)
            registered += 1

    except Exception as e:
        logger.warning(f"Agent registration incomplete: {e}")

    return registered


async def _setup_monitoring_integration():
    """Set up monitoring integration with existing JARVIS monitoring."""
    try:
        from neural_mesh.monitoring import get_metrics_collector, get_health_monitor

        # Get monitoring instances
        metrics = await get_metrics_collector()
        health = await get_health_monitor()

        # These will be available system-wide
        logger.debug("Neural Mesh monitoring active")

    except Exception as e:
        logger.warning(f"Monitoring integration skipped: {e}")


async def shutdown_neural_mesh():
    """Gracefully shutdown the Neural Mesh system."""
    global _neural_mesh_coordinator, _crew_manager, _initialized

    if not _initialized:
        return

    logger.info("🧠 Shutting down Neural Mesh...")

    try:
        if _neural_mesh_coordinator:
            await _neural_mesh_coordinator.stop()
            _neural_mesh_coordinator = None

        _crew_manager = None
        _initialized = False

        logger.info("🧠 Neural Mesh shutdown complete")

    except Exception as e:
        logger.error(f"Neural Mesh shutdown error: {e}")


def get_neural_mesh_coordinator():
    """Get the global Neural Mesh coordinator instance."""
    return _neural_mesh_coordinator


def get_crew_manager():
    """Get the global Crew manager instance."""
    return _crew_manager


def is_neural_mesh_initialized() -> bool:
    """Check if Neural Mesh is initialized."""
    return _initialized


async def get_neural_mesh_status() -> Dict[str, Any]:
    """Get current Neural Mesh status."""
    if not _initialized:
        return {"status": "not_initialized"}

    status = {
        "status": "running" if _initialized else "stopped",
        "coordinator": None,
        "crew": None,
    }

    if _neural_mesh_coordinator:
        try:
            coord_status = await _neural_mesh_coordinator.get_status()
            status["coordinator"] = coord_status
        except Exception:
            status["coordinator"] = {"error": "unavailable"}

    if _crew_manager:
        try:
            crew_status = await _crew_manager.get_status()
            status["crew"] = crew_status
        except Exception:
            status["crew"] = {"error": "unavailable"}

    return status


# Convenience function for use in startup scripts
def create_neural_mesh_task(
    config: Optional[NeuralMeshConfig] = None,
    jarvis_instance: Optional[Any] = None,
) -> asyncio.Task:
    """
    Create an asyncio task for Neural Mesh initialization.

    Use this in startup scripts:
        task = create_neural_mesh_task()
        await task
    """
    return asyncio.create_task(
        initialize_neural_mesh(config, jarvis_instance)
    )

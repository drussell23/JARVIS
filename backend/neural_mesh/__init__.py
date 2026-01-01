"""
JARVIS Neural Mesh - Unified Multi-Agent Intelligence Framework (v8.0 Hyper-Speed)

This module provides the core infrastructure for transforming 60+ isolated agents
into a cohesive, collaborative AI ecosystem with:

- Agent Communication Bus: Ultra-fast async message passing between agents
- Shared Knowledge Graph: Persistent, searchable collective memory
- Agent Registry: Service discovery and health monitoring
- Multi-Agent Orchestrator: Workflow coordination and task decomposition
- Base Agent: Unified interface for all agents
- JARVIS Bridge: Auto-discovery and integration of all JARVIS systems

v8.0 HYPER-SPEED: All imports are JIT-loaded on first access, reducing
module import time from 500ms+ to <10ms.

Architecture:
    TIER 0: Neural Mesh Intelligence Layer (this module)
    TIER 1: Master Intelligence (UAE, SAI, CAI)
    TIER 2: Core Domain Agents (28 agents)
    TIER 3: Specialized Sub-Agents (30+ agents)

Quick Start:
    from neural_mesh import start_jarvis_neural_mesh

    # Start the entire Neural Mesh ecosystem
    bridge = await start_jarvis_neural_mesh()

    # All 60+ agents are now connected and collaborating!
    result = await bridge.execute_cross_system_task(
        "Analyze workspace and suggest improvements"
    )
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# Only import for type checking, not at runtime
if TYPE_CHECKING:
    from .data_models import (
        MessageType,
        MessagePriority,
        AgentMessage,
        KnowledgeEntry,
        KnowledgeRelationship,
        KnowledgeType,
        AgentInfo,
        AgentStatus,
        WorkflowTask,
        WorkflowResult,
        ExecutionStrategy,
        HealthStatus,
    )
    from .communication.agent_communication_bus import AgentCommunicationBus
    from .knowledge.shared_knowledge_graph import SharedKnowledgeGraph
    from .registry.agent_registry import AgentRegistry
    from .orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
    from .base.base_neural_mesh_agent import BaseNeuralMeshAgent
    from .neural_mesh_coordinator import NeuralMeshCoordinator
    from .config import NeuralMeshConfig
    from .jarvis_bridge import JARVISNeuralMeshBridge, AgentDiscoveryConfig, SystemCategory


__version__ = "2.1.0"  # Crew system added + v8.0 Hyper-Speed
__author__ = "JARVIS AI System"

# =============================================================================
# HYPER-SPEED LAZY LOADING SYSTEM v8.0
# =============================================================================
# All imports are JIT-loaded on first access, reducing import time by 50x.
# This is critical for fast startup as neural_mesh imports many heavy modules.
# =============================================================================

_lazy_modules = {
    # Data Models (lightweight, but defer anyway for consistency)
    "MessageType": (".data_models", "MessageType"),
    "MessagePriority": (".data_models", "MessagePriority"),
    "AgentMessage": (".data_models", "AgentMessage"),
    "KnowledgeEntry": (".data_models", "KnowledgeEntry"),
    "KnowledgeRelationship": (".data_models", "KnowledgeRelationship"),
    "KnowledgeType": (".data_models", "KnowledgeType"),
    "AgentInfo": (".data_models", "AgentInfo"),
    "AgentStatus": (".data_models", "AgentStatus"),
    "WorkflowTask": (".data_models", "WorkflowTask"),
    "WorkflowResult": (".data_models", "WorkflowResult"),
    "ExecutionStrategy": (".data_models", "ExecutionStrategy"),
    "HealthStatus": (".data_models", "HealthStatus"),

    # Core Components (heavy - defer loading)
    "AgentCommunicationBus": (".communication.agent_communication_bus", "AgentCommunicationBus"),
    "SharedKnowledgeGraph": (".knowledge.shared_knowledge_graph", "SharedKnowledgeGraph"),
    "AgentRegistry": (".registry.agent_registry", "AgentRegistry"),
    "MultiAgentOrchestrator": (".orchestration.multi_agent_orchestrator", "MultiAgentOrchestrator"),
    "BaseNeuralMeshAgent": (".base.base_neural_mesh_agent", "BaseNeuralMeshAgent"),
    "NeuralMeshCoordinator": (".neural_mesh_coordinator", "NeuralMeshCoordinator"),
    "NeuralMeshConfig": (".config", "NeuralMeshConfig"),

    # Coordinator Functions
    "get_neural_mesh": (".neural_mesh_coordinator", "get_neural_mesh"),
    "start_neural_mesh": (".neural_mesh_coordinator", "start_neural_mesh"),

    # JARVIS Bridge (heavy - loads many systems)
    "JARVISNeuralMeshBridge": (".jarvis_bridge", "JARVISNeuralMeshBridge"),
    "AgentDiscoveryConfig": (".jarvis_bridge", "AgentDiscoveryConfig"),
    "SystemCategory": (".jarvis_bridge", "SystemCategory"),
    "get_jarvis_bridge": (".jarvis_bridge", "get_jarvis_bridge"),
    "start_jarvis_neural_mesh": (".jarvis_bridge", "start_jarvis_neural_mesh"),
    "stop_jarvis_neural_mesh": (".jarvis_bridge", "stop_jarvis_neural_mesh"),
    "execute_multi_agent_task": (".jarvis_bridge", "execute_multi_agent_task"),
    "get_agent": (".jarvis_bridge", "get_agent"),

    # Integration module
    "initialize_neural_mesh": (".integration", "initialize_neural_mesh"),
    "shutdown_neural_mesh": (".integration", "shutdown_neural_mesh"),
    "get_neural_mesh_coordinator": (".integration", "get_neural_mesh_coordinator"),
    "get_crew_manager": (".integration", "get_crew_manager"),
    "is_neural_mesh_initialized": (".integration", "is_neural_mesh_initialized"),
    "get_neural_mesh_status": (".integration", "get_neural_mesh_status"),
    "create_neural_mesh_task": (".integration", "create_neural_mesh_task"),
    "IntegrationConfig": (".integration", "NeuralMeshConfig"),
}

_loaded_modules = {}


def __getattr__(name: str):
    """
    Lazy import handler - imports modules only when accessed.

    This is the core of the v8.0 Hyper-Speed optimization.
    Instead of loading all modules at import time, we defer
    until the exact moment the module is needed.
    """
    if name in _lazy_modules:
        if name not in _loaded_modules:
            module_path, attr_name = _lazy_modules[name]
            import importlib
            module = importlib.import_module(module_path, package=__name__)
            _loaded_modules[name] = getattr(module, attr_name)
        return _loaded_modules[name]

    raise AttributeError(f"module 'neural_mesh' has no attribute '{name}'")


__all__ = [
    # Data Models
    "MessageType",
    "MessagePriority",
    "AgentMessage",
    "KnowledgeEntry",
    "KnowledgeRelationship",
    "KnowledgeType",
    "AgentInfo",
    "AgentStatus",
    "WorkflowTask",
    "WorkflowResult",
    "ExecutionStrategy",
    "HealthStatus",
    # Core Components
    "AgentCommunicationBus",
    "SharedKnowledgeGraph",
    "AgentRegistry",
    "MultiAgentOrchestrator",
    "BaseNeuralMeshAgent",
    "NeuralMeshCoordinator",
    "NeuralMeshConfig",
    # Coordinator Functions
    "get_neural_mesh",
    "start_neural_mesh",
    # JARVIS Bridge
    "JARVISNeuralMeshBridge",
    "AgentDiscoveryConfig",
    "SystemCategory",
    "get_jarvis_bridge",
    "start_jarvis_neural_mesh",
    "stop_jarvis_neural_mesh",
    "execute_multi_agent_task",
    "get_agent",
    # Integration
    "initialize_neural_mesh",
    "shutdown_neural_mesh",
    "get_neural_mesh_coordinator",
    "get_crew_manager",
    "is_neural_mesh_initialized",
    "get_neural_mesh_status",
    "create_neural_mesh_task",
    "IntegrationConfig",
]

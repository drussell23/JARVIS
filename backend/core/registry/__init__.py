"""
JARVIS Agent Registry Module
=============================

Provides distributed agent registry with Redis backing for:
- Service discovery and capability-based routing
- Distributed state management
- Pub/sub for real-time updates
- Circuit breaker for fault tolerance

Usage:
    from backend.core.registry import get_agent_registry, AgentType

    registry = await get_agent_registry()

    # Register an agent
    agent = await registry.register(
        name="vision_agent",
        agent_type=AgentType.WORKER,
        capabilities=["image_analysis", "object_detection"],
        host="localhost",
        port=8020,
    )

    # Find agents by capability
    agents = await registry.find_by_capability("image_analysis")

    # Get best agent for routing
    best = await registry.get_best_agent("object_detection")
"""

from .unified_agent_registry import (
    UnifiedAgentRegistry,
    AgentInfo,
    AgentStatus,
    AgentType,
    AgentRegistryEvent,
    CircuitState,
    RegistryMetrics,
    get_agent_registry,
    shutdown_agent_registry,
)

from .neural_mesh_bridge import (
    NeuralMeshBridge,
    AgentTranslator,
    SyncDirection,
    SyncResult,
    BridgeMetrics,
    get_registry_bridge,
    shutdown_registry_bridge,
)

__all__ = [
    # Unified Agent Registry
    "UnifiedAgentRegistry",
    "AgentInfo",
    "AgentStatus",
    "AgentType",
    "AgentRegistryEvent",
    "CircuitState",
    "RegistryMetrics",
    "get_agent_registry",
    "shutdown_agent_registry",
    # Neural Mesh Bridge
    "NeuralMeshBridge",
    "AgentTranslator",
    "SyncDirection",
    "SyncResult",
    "BridgeMetrics",
    "get_registry_bridge",
    "shutdown_registry_bridge",
]

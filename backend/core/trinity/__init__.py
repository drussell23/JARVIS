"""
Trinity Cross-Repo Integration Module
=====================================

Provides advanced coordination between JARVIS, JARVIS Prime, and Reactor Core.

Components:
    - TrinityIntegrationCoordinator: Main orchestration engine
    - EventSequencer: Sequence numbering and gap detection
    - CausalEventDelivery: Causal ordering guarantees
    - HealthMonitor: Component health tracking
    - ModelHotSwapManager: Safe model swapping
    - ExperienceValidator: Experience data validation
    - ReactorCoreBridge: Reactor Core integration
"""

from backend.core.trinity.integration_coordinator import (
    TrinityIntegrationCoordinator,
    EventSequencer,
    CausalEventDelivery,
    HealthMonitor,
    ModelHotSwapManager,
    ExperienceValidator,
    DirectoryLifecycleManager,
    SequencedEvent,
    EventType,
    RepoType,
    ComponentStatus,
    get_trinity_coordinator,
    shutdown_trinity_coordinator,
)

from backend.core.trinity.reactor_bridge import (
    ReactorCoreBridge,
    ReactorCorePublisher,
    ReactorCoreReceiver,
    TrainingPipelineIntegration,
    get_reactor_bridge,
    shutdown_reactor_bridge,
)

__all__ = [
    # Integration Coordinator
    "TrinityIntegrationCoordinator",
    "EventSequencer",
    "CausalEventDelivery",
    "HealthMonitor",
    "ModelHotSwapManager",
    "ExperienceValidator",
    "DirectoryLifecycleManager",
    "SequencedEvent",
    "EventType",
    "RepoType",
    "ComponentStatus",
    "get_trinity_coordinator",
    "shutdown_trinity_coordinator",
    # Reactor Bridge
    "ReactorCoreBridge",
    "ReactorCorePublisher",
    "ReactorCoreReceiver",
    "TrainingPipelineIntegration",
    "get_reactor_bridge",
    "shutdown_reactor_bridge",
]

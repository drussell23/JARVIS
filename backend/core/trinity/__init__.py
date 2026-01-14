"""
Trinity Cross-Repo Integration Module v2.0
==========================================

Provides advanced coordination between JARVIS, JARVIS Prime, and Reactor Core.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    Trinity Orchestration Engine                          │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐        │
    │  │   Consensus     │   │   Health        │   │   Experience    │        │
    │  │   Protocol      │   │   Coordinator   │   │   Pipeline      │        │
    │  │   (Raft-like)   │   │   (Circuit BKR) │   │   (Guaranteed)  │        │
    │  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘        │
    │           │                     │                     │                 │
    │           └─────────────────────┼─────────────────────┘                 │
    │                                 │                                        │
    │                                 ▼                                        │
    │                    ┌────────────────────────┐                            │
    │                    │   Component Registry   │                            │
    │                    │   with Vector Clocks   │                            │
    │                    └───────────┬────────────┘                            │
    │                                │                                         │
    │           ┌────────────────────┼────────────────────┐                    │
    │           ▼                    ▼                    ▼                    │
    │    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐             │
    │    │    JARVIS    │    │  JARVIS-Prime│    │ Reactor-Core │             │
    │    │   (Body)     │    │   (Mind)     │    │  (Nerves)    │             │
    │    └──────────────┘    └──────────────┘    └──────────────┘             │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Components:
    - TrinityOrchestrationEngine: "God Process" for ecosystem orchestration
    - ConsensusProtocol: Raft-inspired leader election
    - ExperiencePipeline: Guaranteed delivery with WAL
    - ModelHotSwapManager: RCU-based hot swapping
    - PredictiveAutoScaler: Holt-Winters forecasting
    - GracefulDegradation: Fallback modes for failures
    - DeadLetterQueue: Failed event recovery
    - TrinityIntegrationCoordinator: Legacy orchestration
    - ReactorCoreBridge: Reactor Core integration
"""

from backend.core.trinity.integration_coordinator import (
    TrinityIntegrationCoordinator,
    EventSequencer,
    CausalEventDelivery,
    HealthMonitor,
    ModelHotSwapManager as LegacyModelHotSwapManager,
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

from backend.core.trinity.orchestration_engine import (
    # Core Engine
    TrinityOrchestrationEngine,
    TrinityConfig,
    get_orchestration_engine,
    start_trinity,
    stop_trinity,
    # Data Structures
    VectorClock,
    ComponentInfo,
    ComponentState,
    ComponentType,
    CircuitBreakerState,
    ExperienceEvent,
    ConsensusRole,
    # Distributed Coordination
    ConsensusProtocol,
    BackpressureController,
    ExperiencePipeline,
    ModelHotSwapManager,
    # Predictive Scaling
    HoltWintersForecaster,
    AnomalyDetector,
    PredictiveAutoScaler,
    # Edge Case Handlers
    RetryStrategy,
    GracefulDegradation,
    DeadLetterQueue,
    ResourceGovernor,
)

__all__ = [
    # === Orchestration Engine (v2.0) ===
    "TrinityOrchestrationEngine",
    "TrinityConfig",
    "get_orchestration_engine",
    "start_trinity",
    "stop_trinity",
    # Data Structures
    "VectorClock",
    "ComponentInfo",
    "ComponentState",
    "ComponentType",
    "CircuitBreakerState",
    "ExperienceEvent",
    "ConsensusRole",
    # Distributed Coordination
    "ConsensusProtocol",
    "BackpressureController",
    "ExperiencePipeline",
    "ModelHotSwapManager",
    # Predictive Scaling
    "HoltWintersForecaster",
    "AnomalyDetector",
    "PredictiveAutoScaler",
    # Edge Case Handlers
    "RetryStrategy",
    "GracefulDegradation",
    "DeadLetterQueue",
    "ResourceGovernor",
    # === Integration Coordinator (Legacy) ===
    "TrinityIntegrationCoordinator",
    "EventSequencer",
    "CausalEventDelivery",
    "HealthMonitor",
    "LegacyModelHotSwapManager",
    "ExperienceValidator",
    "DirectoryLifecycleManager",
    "SequencedEvent",
    "EventType",
    "RepoType",
    "ComponentStatus",
    "get_trinity_coordinator",
    "shutdown_trinity_coordinator",
    # === Reactor Bridge ===
    "ReactorCoreBridge",
    "ReactorCorePublisher",
    "ReactorCoreReceiver",
    "TrainingPipelineIntegration",
    "get_reactor_bridge",
    "shutdown_reactor_bridge",
]

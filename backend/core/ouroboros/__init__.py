"""
Ouroboros Self-Improvement Engine v1.0
======================================

The autonomous code evolution system for JARVIS. Uses local LLM (JARVIS Prime)
to analyze, improve, and evolve its own codebase without human intervention.

Named after the ancient symbol of a serpent eating its own tail - representing
eternal cyclic renewal and self-sustaining evolution.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────────┐
    │                        OUROBOROS SELF-IMPROVEMENT ENGINE                     │
    ├─────────────────────────────────────────────────────────────────────────────┤
    │                                                                              │
    │  ┌────────────────┐     ┌────────────────┐     ┌────────────────┐           │
    │  │   Improvement  │     │    Genetic     │     │   Rollback     │           │
    │  │    Request     │────▶│   Evolution    │────▶│   Protection   │           │
    │  │   (Goal/File)  │     │   (Multi-path) │     │   (Git Snap)   │           │
    │  └────────────────┘     └────────────────┘     └────────────────┘           │
    │           │                     │                     │                     │
    │           ▼                     ▼                     ▼                     │
    │  ┌────────────────┐     ┌────────────────┐     ┌────────────────┐           │
    │  │      AST       │     │    JARVIS      │     │     Test       │           │
    │  │   Analysis     │◀────│    Prime       │────▶│   Validator    │           │
    │  │ (Code Context) │     │   (Local LLM)  │     │   (pytest)     │           │
    │  └────────────────┘     └────────────────┘     └────────────────┘           │
    │           │                     │                     │                     │
    │           ▼                     ▼                     ▼                     │
    │  ┌────────────────┐     ┌────────────────┐     ┌────────────────┐           │
    │  │   Semantic     │     │   Consensus    │     │   Coverage     │           │
    │  │     Diff       │────▶│  Validation    │────▶│   Tracking     │           │
    │  │  (Changes)     │     │ (Multi-Model)  │     │  (Mutation)    │           │
    │  └────────────────┘     └────────────────┘     └────────────────┘           │
    │                                                                              │
    │                              THE RALPH LOOP                                  │
    │  ┌───────────────────────────────────────────────────────────────────────┐  │
    │  │  Improve ──▶ Test ──▶ Pass? ──▶ Commit ──▶ Learn                      │  │
    │  │     ▲          │         │                   │                        │  │
    │  │     │          │         ▼ (No)              │                        │  │
    │  │     └──────────┴─── Retry with Error Log ◀───┘                        │  │
    │  └───────────────────────────────────────────────────────────────────────┘  │
    │                                                                              │
    └─────────────────────────────────────────────────────────────────────────────┘

Components:
    - OuroborosEngine: Main orchestrator for self-improvement cycles
    - GeneticEvolver: Multi-path evolution with selection pressure
    - CodeAnalyzer: AST-based code understanding
    - SemanticDiff: Intelligent change analysis
    - RollbackProtector: Git-based safety snapshots
    - TestValidator: pytest integration with coverage
    - ConsensusValidator: Multi-model change validation
    - LearningMemory: Failed attempt tracking to avoid repetition

Author: Trinity System
Version: 1.0.0
"""

from backend.core.ouroboros.engine import (
    OuroborosEngine,
    ImprovementRequest,
    ImprovementResult,
    EvolutionStrategy,
    get_ouroboros_engine,
    improve_file,
    improve_with_goal,
)

from backend.core.ouroboros.genetic import (
    GeneticEvolver,
    Chromosome,
    Population,
    FitnessFunction,
    SelectionStrategy,
)

from backend.core.ouroboros.analyzer import (
    CodeAnalyzer,
    ASTContext,
    SemanticDiff,
    ChangeImpact,
)

from backend.core.ouroboros.validator import (
    TestValidator,
    CoverageTracker,
    MutationTester,
    ValidationResult,
)

from backend.core.ouroboros.protector import (
    RollbackProtector,
    Snapshot,
    RestorePoint,
)

from backend.core.ouroboros.integration import (
    EnhancedOuroborosIntegration,
    MultiProviderLLMClient,
    CircuitBreaker,
    SandboxExecutor,
    ReactorCoreExperiencePublisher,
    ProviderStatus,
    CircuitState,
    get_ouroboros_integration,
    shutdown_ouroboros_integration,
)

from backend.core.ouroboros.advanced_orchestrator import (
    AdvancedOuroborosOrchestrator,
    TokenBucketRateLimiter,
    SemanticCache,
    SyntaxValidator,
    GitStateManager,
    FileLockManager,
    ResourceMonitor,
    HealthMonitor,
    ProviderStarter,
    get_advanced_orchestrator,
    shutdown_advanced_orchestrator,
    jarvis_improve,
)

from backend.core.ouroboros.cross_repo import (
    CrossRepoOrchestrator,
    CrossRepoEventBus,
    RepoConnector,
    CrossRepoEvent,
    RepoType,
    EventType,
    get_cross_repo_orchestrator,
    shutdown_cross_repo,
)

from backend.core.ouroboros.brain_orchestrator import (
    BrainOrchestrator,
    BrainConfig,
    ProviderManager,
    LoadBalancer,
    ProviderType,
    ProviderState,
    LoadBalancerStrategy,
    ProviderInfo,
    HealthCheckResult,
    get_brain_orchestrator,
    ignite_brains,
    shutdown_brains,
)

from backend.core.ouroboros.native_integration import (
    NativeSelfImprovement,
    NativeConfig,
    SecurityValidator,
    SecurityError,
    ThreadSafeMetrics,
    AtomicCounter,
    ImprovedCircuitBreaker,
    ProgressBroadcaster,
    ImprovementProgress,
    ImprovementPhase,
    ImprovementRequest as NativeImprovementRequest,
    ImprovementResult as NativeImprovementResult,
    get_native_self_improvement,
    initialize_native_self_improvement,
    shutdown_native_self_improvement,
    execute_self_improvement,
)

from backend.core.ouroboros.ui_integration import (
    OuroborosUIController,
    OuroborosMenuSection,
    UIActivityState,
    get_ouroboros_ui_controller,
    connect_ouroboros_ui,
    disconnect_ouroboros_ui,
)

from backend.core.ouroboros.neural_mesh import (
    NeuralMesh,
    MeshConfig,
    MeshMessage,
    MeshConnection,
    NodeType,
    NodeStatus,
    ConnectionType,
    MessageType,
    MessagePriority,
    get_neural_mesh,
    initialize_neural_mesh,
    shutdown_neural_mesh,
)

from backend.core.ouroboros.trinity_integration import (
    TrinityIntegration,
    TrinityModelClient,
    TrinityExperiencePublisher,
    TrinityHealthMonitor,
    TrinityConfig,
    ComponentHealth,
    HealthStatus,
    get_trinity_integration,
    initialize_trinity_integration,
    shutdown_trinity_integration,
)

__all__ = [
    # Core Engine
    "OuroborosEngine",
    "ImprovementRequest",
    "ImprovementResult",
    "EvolutionStrategy",
    "get_ouroboros_engine",
    "improve_file",
    "improve_with_goal",
    # Genetic Evolution
    "GeneticEvolver",
    "Chromosome",
    "Population",
    "FitnessFunction",
    "SelectionStrategy",
    # Code Analysis
    "CodeAnalyzer",
    "ASTContext",
    "SemanticDiff",
    "ChangeImpact",
    # Validation
    "TestValidator",
    "CoverageTracker",
    "MutationTester",
    "ValidationResult",
    # Protection
    "RollbackProtector",
    "Snapshot",
    "RestorePoint",
    # Integration Layer
    "EnhancedOuroborosIntegration",
    "MultiProviderLLMClient",
    "CircuitBreaker",
    "SandboxExecutor",
    "ReactorCoreExperiencePublisher",
    "ProviderStatus",
    "CircuitState",
    "get_ouroboros_integration",
    "shutdown_ouroboros_integration",
    # Advanced Orchestrator
    "AdvancedOuroborosOrchestrator",
    "TokenBucketRateLimiter",
    "SemanticCache",
    "SyntaxValidator",
    "GitStateManager",
    "FileLockManager",
    "ResourceMonitor",
    "HealthMonitor",
    "ProviderStarter",
    "get_advanced_orchestrator",
    "shutdown_advanced_orchestrator",
    "jarvis_improve",
    # Cross-Repo Integration
    "CrossRepoOrchestrator",
    "CrossRepoEventBus",
    "RepoConnector",
    "CrossRepoEvent",
    "RepoType",
    "EventType",
    "get_cross_repo_orchestrator",
    "shutdown_cross_repo",
    # Brain Orchestrator
    "BrainOrchestrator",
    "BrainConfig",
    "ProviderManager",
    "LoadBalancer",
    "ProviderType",
    "ProviderState",
    "LoadBalancerStrategy",
    "ProviderInfo",
    "HealthCheckResult",
    "get_brain_orchestrator",
    "ignite_brains",
    "shutdown_brains",
    # Native Self-Improvement (Motor Function)
    "NativeSelfImprovement",
    "NativeConfig",
    "SecurityValidator",
    "SecurityError",
    "ThreadSafeMetrics",
    "AtomicCounter",
    "ImprovedCircuitBreaker",
    "ProgressBroadcaster",
    "ImprovementProgress",
    "ImprovementPhase",
    "NativeImprovementRequest",
    "NativeImprovementResult",
    "get_native_self_improvement",
    "initialize_native_self_improvement",
    "shutdown_native_self_improvement",
    "execute_self_improvement",
    # UI Integration
    "OuroborosUIController",
    "OuroborosMenuSection",
    "UIActivityState",
    "get_ouroboros_ui_controller",
    "connect_ouroboros_ui",
    "disconnect_ouroboros_ui",
    # Neural Mesh (Cross-Repo Connection)
    "NeuralMesh",
    "MeshConfig",
    "MeshMessage",
    "MeshConnection",
    "NodeType",
    "NodeStatus",
    "ConnectionType",
    "MessageType",
    "MessagePriority",
    "get_neural_mesh",
    "initialize_neural_mesh",
    "shutdown_neural_mesh",
    # Trinity Integration (Unified Layer)
    "TrinityIntegration",
    "TrinityModelClient",
    "TrinityExperiencePublisher",
    "TrinityHealthMonitor",
    "TrinityConfig",
    "ComponentHealth",
    "HealthStatus",
    "get_trinity_integration",
    "initialize_trinity_integration",
    "shutdown_trinity_integration",
]

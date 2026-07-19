"""
Ouroboros Self-Improvement Engine v2.0
======================================

The autonomous code evolution system for JARVIS. Uses local LLM (JARVIS Prime)
to analyze, improve, and evolve its own codebase without human intervention.

v2.0 Enhancements:
    - Trinity Integration Layer v2.0 with full ecosystem connectivity
    - Distributed locking for concurrent improvements
    - Coding Council integration for peer code review
    - Automatic rollback mechanism
    - Learning cache to avoid repeated failures
    - Experience deduplication across channels
    - Model hot-swap support
    - Manual review queue for complete failures

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
Version: 2.0.0
"""

# ── Lazy export surface (Supervisor Campaign Step 2, 2026-07-19) ──
# ROOT CAUSE of the 2s thin-client boot: this package init eagerly
# imported engine/integration/aiohttp into EVERY subpackage import
# (cli, battle_test, governance all paid it). PEP 562: submodule
# symbols resolve on first ATTRIBUTE access; `import backend.core.
# ouroboros.battle_test.cockpit_attach` no longer drags the engine.
# Public API unchanged: `from backend.core.ouroboros import
# OuroborosEngine` still works, one lazy hop later.
_LAZY_EXPORTS = {
    'ASTContext': ('backend.core.ouroboros.analyzer', 'ASTContext'),
    'AdvancedOuroborosOrchestrator': ('backend.core.ouroboros.advanced_orchestrator', 'AdvancedOuroborosOrchestrator'),
    'AtomicCounter': ('backend.core.ouroboros.native_integration', 'AtomicCounter'),
    'BrainConfig': ('backend.core.ouroboros.brain_orchestrator', 'BrainConfig'),
    'BrainOrchestrator': ('backend.core.ouroboros.brain_orchestrator', 'BrainOrchestrator'),
    'ChangeImpact': ('backend.core.ouroboros.analyzer', 'ChangeImpact'),
    'Chromosome': ('backend.core.ouroboros.genetic', 'Chromosome'),
    'CircuitBreaker': ('backend.core.ouroboros.integration', 'CircuitBreaker'),
    'CircuitState': ('backend.core.ouroboros.integration', 'CircuitState'),
    'CodeAnalyzer': ('backend.core.ouroboros.analyzer', 'CodeAnalyzer'),
    'CodeReview': ('backend.core.ouroboros.trinity_integration', 'CodeReview'),
    'ComponentHealth': ('backend.core.ouroboros.trinity_integration', 'ComponentHealth'),
    'ConnectionType': ('backend.core.ouroboros.neural_mesh', 'ConnectionType'),
    'CoverageTracker': ('backend.core.ouroboros.validator', 'CoverageTracker'),
    'CrossRepoEvent': ('backend.core.ouroboros.cross_repo', 'CrossRepoEvent'),
    'CrossRepoEventBus': ('backend.core.ouroboros.cross_repo', 'CrossRepoEventBus'),
    'CrossRepoOrchestrator': ('backend.core.ouroboros.cross_repo', 'CrossRepoOrchestrator'),
    'EnhancedOuroborosIntegration': ('backend.core.ouroboros.integration', 'EnhancedOuroborosIntegration'),
    'EventType': ('backend.core.ouroboros.cross_repo', 'EventType'),
    'EvolutionStrategy': ('backend.core.ouroboros.engine', 'EvolutionStrategy'),
    'FileLockManager': ('backend.core.ouroboros.advanced_orchestrator', 'FileLockManager'),
    'FitnessFunction': ('backend.core.ouroboros.genetic', 'FitnessFunction'),
    'GeneticEvolver': ('backend.core.ouroboros.genetic', 'GeneticEvolver'),
    'GitStateManager': ('backend.core.ouroboros.advanced_orchestrator', 'GitStateManager'),
    'HealthCheckResult': ('backend.core.ouroboros.brain_orchestrator', 'HealthCheckResult'),
    'HealthMonitor': ('backend.core.ouroboros.advanced_orchestrator', 'HealthMonitor'),
    'HealthStatus': ('backend.core.ouroboros.trinity_integration', 'HealthStatus'),
    'ImprovedCircuitBreaker': ('backend.core.ouroboros.native_integration', 'ImprovedCircuitBreaker'),
    'ImprovementHistory': ('backend.core.ouroboros.trinity_integration', 'ImprovementHistory'),
    'ImprovementPhase': ('backend.core.ouroboros.native_integration', 'ImprovementPhase'),
    'ImprovementPriority': ('backend.core.ouroboros.trinity_integration', 'ImprovementPriority'),
    'ImprovementProgress': ('backend.core.ouroboros.native_integration', 'ImprovementProgress'),
    'ImprovementRequest': ('backend.core.ouroboros.engine', 'ImprovementRequest'),
    'ImprovementResult': ('backend.core.ouroboros.engine', 'ImprovementResult'),
    'LoadBalancer': ('backend.core.ouroboros.brain_orchestrator', 'LoadBalancer'),
    'LoadBalancerStrategy': ('backend.core.ouroboros.brain_orchestrator', 'LoadBalancerStrategy'),
    'ManualReviewQueue': ('backend.core.ouroboros.trinity_integration', 'ManualReviewQueue'),
    'MeshConfig': ('backend.core.ouroboros.neural_mesh', 'MeshConfig'),
    'MeshConnection': ('backend.core.ouroboros.neural_mesh', 'MeshConnection'),
    'MeshMessage': ('backend.core.ouroboros.neural_mesh', 'MeshMessage'),
    'MessagePriority': ('backend.core.ouroboros.neural_mesh', 'MessagePriority'),
    'MessageType': ('backend.core.ouroboros.neural_mesh', 'MessageType'),
    'MultiProviderLLMClient': ('backend.core.ouroboros.integration', 'MultiProviderLLMClient'),
    'MutationTester': ('backend.core.ouroboros.validator', 'MutationTester'),
    'NativeConfig': ('backend.core.ouroboros.native_integration', 'NativeConfig'),
    'NativeImprovementRequest': ('backend.core.ouroboros.native_integration', 'ImprovementRequest'),
    'NativeImprovementResult': ('backend.core.ouroboros.native_integration', 'ImprovementResult'),
    'NativeSelfImprovement': ('backend.core.ouroboros.native_integration', 'NativeSelfImprovement'),
    'NeuralMesh': ('backend.core.ouroboros.neural_mesh', 'NeuralMesh'),
    'NodeStatus': ('backend.core.ouroboros.neural_mesh', 'NodeStatus'),
    'NodeType': ('backend.core.ouroboros.neural_mesh', 'NodeType'),
    'OuroborosEngine': ('backend.core.ouroboros.engine', 'OuroborosEngine'),
    'OuroborosMenuSection': ('backend.core.ouroboros.ui_integration', 'OuroborosMenuSection'),
    'OuroborosUIController': ('backend.core.ouroboros.ui_integration', 'OuroborosUIController'),
    'Population': ('backend.core.ouroboros.genetic', 'Population'),
    'PrioritizedImprovement': ('backend.core.ouroboros.trinity_integration', 'PrioritizedImprovement'),
    'ProgressBroadcaster': ('backend.core.ouroboros.native_integration', 'ProgressBroadcaster'),
    'ProviderInfo': ('backend.core.ouroboros.brain_orchestrator', 'ProviderInfo'),
    'ProviderManager': ('backend.core.ouroboros.brain_orchestrator', 'ProviderManager'),
    'ProviderStarter': ('backend.core.ouroboros.advanced_orchestrator', 'ProviderStarter'),
    'ProviderState': ('backend.core.ouroboros.brain_orchestrator', 'ProviderState'),
    'ProviderStatus': ('backend.core.ouroboros.integration', 'ProviderStatus'),
    'ProviderType': ('backend.core.ouroboros.brain_orchestrator', 'ProviderType'),
    'ReactorCoreExperiencePublisher': ('backend.core.ouroboros.integration', 'ReactorCoreExperiencePublisher'),
    'RepoConnector': ('backend.core.ouroboros.cross_repo', 'RepoConnector'),
    'RepoType': ('backend.core.ouroboros.cross_repo', 'RepoType'),
    'ResourceMonitor': ('backend.core.ouroboros.advanced_orchestrator', 'ResourceMonitor'),
    'RestorePoint': ('backend.core.ouroboros.protector', 'RestorePoint'),
    'ReviewResult': ('backend.core.ouroboros.trinity_integration', 'ReviewResult'),
    'RollbackProtector': ('backend.core.ouroboros.protector', 'RollbackProtector'),
    'SandboxExecutor': ('backend.core.ouroboros.integration', 'SandboxExecutor'),
    'SecurityError': ('backend.core.ouroboros.native_integration', 'SecurityError'),
    'SecurityValidator': ('backend.core.ouroboros.native_integration', 'SecurityValidator'),
    'SelectionStrategy': ('backend.core.ouroboros.genetic', 'SelectionStrategy'),
    'SemanticCache': ('backend.core.ouroboros.advanced_orchestrator', 'SemanticCache'),
    'SemanticDiff': ('backend.core.ouroboros.analyzer', 'SemanticDiff'),
    'Snapshot': ('backend.core.ouroboros.protector', 'Snapshot'),
    'SyntaxValidator': ('backend.core.ouroboros.advanced_orchestrator', 'SyntaxValidator'),
    'TestValidator': ('backend.core.ouroboros.validator', 'TestValidator'),
    'ThreadSafeMetrics': ('backend.core.ouroboros.native_integration', 'ThreadSafeMetrics'),
    'TokenBucketRateLimiter': ('backend.core.ouroboros.advanced_orchestrator', 'TokenBucketRateLimiter'),
    'TrinityCodeReviewer': ('backend.core.ouroboros.trinity_integration', 'TrinityCodeReviewer'),
    'TrinityConfig': ('backend.core.ouroboros.trinity_integration', 'TrinityConfig'),
    'TrinityCoordinator': ('backend.core.ouroboros.trinity_integration', 'TrinityCoordinator'),
    'TrinityExperiencePublisher': ('backend.core.ouroboros.trinity_integration', 'TrinityExperiencePublisher'),
    'TrinityHealthMonitor': ('backend.core.ouroboros.trinity_integration', 'TrinityHealthMonitor'),
    'TrinityIntegration': ('backend.core.ouroboros.trinity_integration', 'TrinityIntegration'),
    'TrinityLearningCache': ('backend.core.ouroboros.trinity_integration', 'TrinityLearningCache'),
    'TrinityLockManager': ('backend.core.ouroboros.trinity_integration', 'TrinityLockManager'),
    'TrinityModelClient': ('backend.core.ouroboros.trinity_integration', 'TrinityModelClient'),
    'TrinityRollbackManager': ('backend.core.ouroboros.trinity_integration', 'TrinityRollbackManager'),
    'UIActivityState': ('backend.core.ouroboros.ui_integration', 'UIActivityState'),
    'ValidationResult': ('backend.core.ouroboros.validator', 'ValidationResult'),
    'connect_ouroboros_ui': ('backend.core.ouroboros.ui_integration', 'connect_ouroboros_ui'),
    'disconnect_ouroboros_ui': ('backend.core.ouroboros.ui_integration', 'disconnect_ouroboros_ui'),
    'execute_self_improvement': ('backend.core.ouroboros.native_integration', 'execute_self_improvement'),
    'get_advanced_orchestrator': ('backend.core.ouroboros.advanced_orchestrator', 'get_advanced_orchestrator'),
    'get_brain_orchestrator': ('backend.core.ouroboros.brain_orchestrator', 'get_brain_orchestrator'),
    'get_cross_repo_orchestrator': ('backend.core.ouroboros.cross_repo', 'get_cross_repo_orchestrator'),
    'get_native_self_improvement': ('backend.core.ouroboros.native_integration', 'get_native_self_improvement'),
    'get_neural_mesh': ('backend.core.ouroboros.neural_mesh', 'get_neural_mesh'),
    'get_ouroboros_engine': ('backend.core.ouroboros.engine', 'get_ouroboros_engine'),
    'get_ouroboros_integration': ('backend.core.ouroboros.integration', 'get_ouroboros_integration'),
    'get_ouroboros_ui_controller': ('backend.core.ouroboros.ui_integration', 'get_ouroboros_ui_controller'),
    'get_trinity_integration': ('backend.core.ouroboros.trinity_integration', 'get_trinity_integration'),
    'ignite_brains': ('backend.core.ouroboros.brain_orchestrator', 'ignite_brains'),
    'improve_file': ('backend.core.ouroboros.engine', 'improve_file'),
    'improve_with_goal': ('backend.core.ouroboros.engine', 'improve_with_goal'),
    'initialize_native_self_improvement': ('backend.core.ouroboros.native_integration', 'initialize_native_self_improvement'),
    'initialize_neural_mesh': ('backend.core.ouroboros.neural_mesh', 'initialize_neural_mesh'),
    'initialize_trinity_integration': ('backend.core.ouroboros.trinity_integration', 'initialize_trinity_integration'),
    'jarvis_improve': ('backend.core.ouroboros.advanced_orchestrator', 'jarvis_improve'),
    'shutdown_advanced_orchestrator': ('backend.core.ouroboros.advanced_orchestrator', 'shutdown_advanced_orchestrator'),
    'shutdown_brains': ('backend.core.ouroboros.brain_orchestrator', 'shutdown_brains'),
    'shutdown_cross_repo': ('backend.core.ouroboros.cross_repo', 'shutdown_cross_repo'),
    'shutdown_native_self_improvement': ('backend.core.ouroboros.native_integration', 'shutdown_native_self_improvement'),
    'shutdown_neural_mesh': ('backend.core.ouroboros.neural_mesh', 'shutdown_neural_mesh'),
    'shutdown_ouroboros_integration': ('backend.core.ouroboros.integration', 'shutdown_ouroboros_integration'),
    'shutdown_trinity_integration': ('backend.core.ouroboros.trinity_integration', 'shutdown_trinity_integration'),
}


def __getattr__(name):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib
    value = getattr(importlib.import_module(entry[0]), entry[1])
    globals()[name] = value
    return value


def __dir__():
    return sorted(list(globals()) + list(_LAZY_EXPORTS))

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
    # Trinity Integration v2.0 (Unified Layer)
    "TrinityIntegration",
    "TrinityModelClient",
    "TrinityExperiencePublisher",
    "TrinityHealthMonitor",
    "TrinityConfig",
    # v2.0 Components
    "TrinityLockManager",
    "TrinityCodeReviewer",
    "TrinityRollbackManager",
    "TrinityLearningCache",
    "TrinityCoordinator",
    "ManualReviewQueue",
    # Enums and Data Classes
    "ImprovementPriority",
    "ComponentHealth",
    "ReviewResult",
    "HealthStatus",
    "CodeReview",
    "ImprovementHistory",
    "PrioritizedImprovement",
    # Functions
    "get_trinity_integration",
    "initialize_trinity_integration",
    "shutdown_trinity_integration",
]

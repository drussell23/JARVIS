"""
Unified Model Management System v1.0
=====================================

Enterprise-grade model management for the JARVIS Trinity ecosystem.
Handles all aspects of model lifecycle across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 7 critical model management patterns:
1. Model Version Registry - Centralized version tracking with metadata
2. Model A/B Testing - Statistical multi-variant testing framework
3. Model Rollback - Safe rollback with state preservation
4. Model Validation Pipeline - Automated pre-deployment validation
5. Model Performance Tracking - Historical tracking with anomaly detection
6. Model Metadata Management - Structured queryable metadata database
7. Model Lifecycle Management - Policy-driven lifecycle automation

Author: Trinity Model System
Version: 1.0.0
"""

from backend.core.model_management.unified_engine import (
    # Configuration
    ModelManagementConfig,
    # Enums
    ModelType,
    ModelStatus,
    DeploymentStrategy,
    RollbackReason,
    ValidationLevel,
    LifecycleAction,
    MetricType,
    AlertSeverity,
    # Data Structures
    SemanticVersion,
    ModelMetadata,
    MetricSample,
    MetricWindow,
    PerformanceMetrics,
    ValidationResult,
    ABTestVariant,
    ABTest,
    RollbackEvent,
    LifecyclePolicy,
    Alert,
    # Components
    ModelVersionRegistry,
    ABTestingFramework,
    ModelRollbackManager,
    ModelValidationPipeline,
    ModelPerformanceTracker,
    ModelLifecycleManager,
    # Main Engine
    UnifiedModelManagementEngine,
    # Global Functions
    get_model_management_engine,
    initialize_model_management,
    shutdown_model_management,
)

from backend.core.model_management.cross_repo_bridge import (
    # Configuration
    CrossRepoModelConfig,
    # Enums
    ModelSyncDirection,
    ModelEventType,
    SyncStatus,
    # Data Structures
    ModelEvent,
    SyncOperation,
    RepoModelInventory,
    TrainingRequest,
    # Event Bus
    ModelEventBus,
    # Bridge
    CrossRepoModelBridge,
    # Global Functions
    get_cross_repo_model_bridge,
    initialize_cross_repo_models,
    shutdown_cross_repo_models,
)

from backend.core.model_management.supervisor_integration import (
    # Enums
    ModelSystemState,
    ModelComponentState,
    # Configuration
    ModelManagementSupervisorConfig,
    # Data Structures
    ModelComponentHealth,
    ModelManagementInitResult,
    ModelManagementHealthReport,
    # Coordinator
    ModelManagementSupervisorCoordinator,
    # Global Functions
    get_model_management_supervisor,
    initialize_model_management_supervisor,
    shutdown_model_management_supervisor,
    get_model_management_status,
    get_model_management_health,
    # Context Manager
    ModelManagementContext,
)

__all__ = [
    # =========================================================================
    # UNIFIED ENGINE
    # =========================================================================
    # Configuration
    "ModelManagementConfig",
    # Enums
    "ModelType",
    "ModelStatus",
    "DeploymentStrategy",
    "RollbackReason",
    "ValidationLevel",
    "LifecycleAction",
    "MetricType",
    "AlertSeverity",
    # Data Structures
    "SemanticVersion",
    "ModelMetadata",
    "MetricSample",
    "MetricWindow",
    "PerformanceMetrics",
    "ValidationResult",
    "ABTestVariant",
    "ABTest",
    "RollbackEvent",
    "LifecyclePolicy",
    "Alert",
    # Components
    "ModelVersionRegistry",
    "ABTestingFramework",
    "ModelRollbackManager",
    "ModelValidationPipeline",
    "ModelPerformanceTracker",
    "ModelLifecycleManager",
    # Main Engine
    "UnifiedModelManagementEngine",
    # Global Functions
    "get_model_management_engine",
    "initialize_model_management",
    "shutdown_model_management",
    # =========================================================================
    # CROSS-REPO BRIDGE
    # =========================================================================
    # Configuration
    "CrossRepoModelConfig",
    # Enums
    "ModelSyncDirection",
    "ModelEventType",
    "SyncStatus",
    # Data Structures
    "ModelEvent",
    "SyncOperation",
    "RepoModelInventory",
    "TrainingRequest",
    # Event Bus
    "ModelEventBus",
    # Bridge
    "CrossRepoModelBridge",
    # Global Functions
    "get_cross_repo_model_bridge",
    "initialize_cross_repo_models",
    "shutdown_cross_repo_models",
    # =========================================================================
    # SUPERVISOR INTEGRATION
    # =========================================================================
    # Enums
    "ModelSystemState",
    "ModelComponentState",
    # Configuration
    "ModelManagementSupervisorConfig",
    # Data Structures
    "ModelComponentHealth",
    "ModelManagementInitResult",
    "ModelManagementHealthReport",
    # Coordinator
    "ModelManagementSupervisorCoordinator",
    # Global Functions
    "get_model_management_supervisor",
    "initialize_model_management_supervisor",
    "shutdown_model_management_supervisor",
    "get_model_management_status",
    "get_model_management_health",
    # Context Manager
    "ModelManagementContext",
]

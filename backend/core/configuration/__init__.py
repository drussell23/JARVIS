"""
Unified Configuration System v1.0
==================================

Enterprise-grade configuration management for the JARVIS Trinity ecosystem.
Handles all aspects of configuration across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 4 critical configuration patterns:
1. Configuration Synchronization - Centralized config with cross-repo sync
2. Configuration Validation - JSON Schema validation with type checking
3. Configuration Versioning - Git-like version history with rollback
4. Dynamic Configuration Updates - Hot-reload without restart

Author: Trinity Configuration System
Version: 1.0.0
"""

from backend.core.configuration.unified_engine import (
    # Configuration
    ConfigurationEngineConfig,
    # Enums
    ConfigEnvironment,
    ConfigSource,
    ConfigFormat,
    ValidationSeverity,
    ChangeType,
    SyncStatus,
    # Data Structures
    ConfigValue,
    ConfigVersion,
    ValidationIssue,
    ValidationResult,
    ConfigSchema,
    ConfigChangeEvent,
    # Components
    SchemaValidator,
    ConfigVersionManager,
    HotReloadManager,
    ConfigurationStore,
    # Engine
    UnifiedConfigurationEngine,
    # Global Functions
    get_configuration_engine,
    initialize_configuration,
    shutdown_configuration,
)

from backend.core.configuration.cross_repo_bridge import (
    # Configuration
    CrossRepoConfigConfig,
    # Enums
    ConfigEventType,
    ConflictResolutionStrategy,
    RepoConfigRole,
    # Data Structures
    ConfigEvent,
    RepoConfigState,
    ConfigConflict,
    SyncResult,
    # Components
    ConfigEventBus,
    ConfigConflictResolver,
    # Bridge
    CrossRepoConfigBridge,
    # Global Functions
    get_cross_repo_config_bridge,
    initialize_cross_repo_config,
    shutdown_cross_repo_config,
)

from backend.core.configuration.supervisor_integration import (
    # Enums
    ConfigSystemState,
    ConfigComponentState,
    # Configuration
    ConfigSupervisorConfig,
    # Data Structures
    ConfigComponentHealth,
    ConfigInitResult,
    ConfigHealthReport,
    # Coordinator
    ConfigSupervisorCoordinator,
    # Global Functions
    get_config_supervisor,
    initialize_config_supervisor,
    shutdown_config_supervisor,
    get_config_status,
    get_config_health,
    # Context Manager
    ConfigurationContext,
)

__all__ = [
    # =========================================================================
    # UNIFIED ENGINE
    # =========================================================================
    # Configuration
    "ConfigurationEngineConfig",
    # Enums
    "ConfigEnvironment",
    "ConfigSource",
    "ConfigFormat",
    "ValidationSeverity",
    "ChangeType",
    "SyncStatus",
    # Data Structures
    "ConfigValue",
    "ConfigVersion",
    "ValidationIssue",
    "ValidationResult",
    "ConfigSchema",
    "ConfigChangeEvent",
    # Components
    "SchemaValidator",
    "ConfigVersionManager",
    "HotReloadManager",
    "ConfigurationStore",
    # Engine
    "UnifiedConfigurationEngine",
    # Global Functions
    "get_configuration_engine",
    "initialize_configuration",
    "shutdown_configuration",
    # =========================================================================
    # CROSS-REPO BRIDGE
    # =========================================================================
    # Configuration
    "CrossRepoConfigConfig",
    # Enums
    "ConfigEventType",
    "ConflictResolutionStrategy",
    "RepoConfigRole",
    # Data Structures
    "ConfigEvent",
    "RepoConfigState",
    "ConfigConflict",
    "SyncResult",
    # Components
    "ConfigEventBus",
    "ConfigConflictResolver",
    # Bridge
    "CrossRepoConfigBridge",
    # Global Functions
    "get_cross_repo_config_bridge",
    "initialize_cross_repo_config",
    "shutdown_cross_repo_config",
    # =========================================================================
    # SUPERVISOR INTEGRATION
    # =========================================================================
    # Enums
    "ConfigSystemState",
    "ConfigComponentState",
    # Configuration
    "ConfigSupervisorConfig",
    # Data Structures
    "ConfigComponentHealth",
    "ConfigInitResult",
    "ConfigHealthReport",
    # Coordinator
    "ConfigSupervisorCoordinator",
    # Global Functions
    "get_config_supervisor",
    "initialize_config_supervisor",
    "shutdown_config_supervisor",
    "get_config_status",
    "get_config_health",
    # Context Manager
    "ConfigurationContext",
]

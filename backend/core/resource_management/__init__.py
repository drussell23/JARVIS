"""
Unified Resource Management System v1.0
========================================

Enterprise-grade resource management for the JARVIS Trinity ecosystem.
Handles all aspects of resource lifecycle across JARVIS (Body), JARVIS Prime (Mind),
and Reactor Core (Learning).

Implements 6 critical resource management patterns:
1. Unified Resource Coordinator - Centralized resource management
2. Port Pool Management - Port allocation with reservation
3. Memory Budget Allocation - Per-repo memory budgets with enforcement
4. CPU Affinity Management - CPU core assignment for optimal performance
5. Disk Space Management - Monitoring and automated cleanup
6. Network Bandwidth Management - Bandwidth limits and monitoring

Author: Trinity Resource System
Version: 1.0.0
"""

from backend.core.resource_management.unified_engine import (
    # Configuration
    ResourceManagementConfig,
    # Enums
    ResourceType,
    ResourceState,
    ComponentType,
    AlertLevel,
    CleanupStrategy,
    BandwidthPolicy,
    # Data Structures
    ResourceAllocation,
    PortReservation,
    MemoryBudget,
    CPUAllocation,
    DiskUsageSnapshot,
    NetworkUsageSnapshot,
    ResourceAlert,
    ResourceMetrics,
    # Managers
    PortPoolManager,
    MemoryBudgetManager,
    CPUAffinityManager,
    DiskSpaceManager,
    NetworkBandwidthManager,
    # Coordinator
    UnifiedResourceCoordinator,
    # Global Functions
    get_resource_coordinator,
    initialize_resource_management,
    shutdown_resource_management,
)

from backend.core.resource_management.cross_repo_bridge import (
    # Configuration
    CrossRepoResourceConfig,
    # Enums
    ResourceEventType,
    SyncDirection,
    ConflictResolution,
    RepoStatus,
    # Data Structures
    ResourceEvent,
    ResourceInventory,
    AllocationRequest,
    AllocationResponse,
    RepoHealthStatus,
    # Event Bus
    ResourceEventBus,
    # Bridge
    CrossRepoResourceBridge,
    # Global Functions
    get_cross_repo_resource_bridge,
    initialize_cross_repo_resources,
    shutdown_cross_repo_resources,
)

from backend.core.resource_management.supervisor_integration import (
    # Enums
    ResourceSystemState,
    ResourceComponentState,
    # Configuration
    ResourceManagementSupervisorConfig,
    # Data Structures
    ResourceComponentHealth,
    ResourceManagementInitResult,
    ResourceManagementHealthReport,
    # Coordinator
    ResourceManagementSupervisorCoordinator,
    # Global Functions
    get_resource_management_supervisor,
    initialize_resource_management_supervisor,
    shutdown_resource_management_supervisor,
    get_resource_management_status,
    get_resource_management_health,
    # Context Manager
    ResourceManagementContext,
)

__all__ = [
    # =========================================================================
    # UNIFIED ENGINE
    # =========================================================================
    # Configuration
    "ResourceManagementConfig",
    # Enums
    "ResourceType",
    "ResourceState",
    "ComponentType",
    "AlertLevel",
    "CleanupStrategy",
    "BandwidthPolicy",
    # Data Structures
    "ResourceAllocation",
    "PortReservation",
    "MemoryBudget",
    "CPUAllocation",
    "DiskUsageSnapshot",
    "NetworkUsageSnapshot",
    "ResourceAlert",
    "ResourceMetrics",
    # Managers
    "PortPoolManager",
    "MemoryBudgetManager",
    "CPUAffinityManager",
    "DiskSpaceManager",
    "NetworkBandwidthManager",
    # Coordinator
    "UnifiedResourceCoordinator",
    # Global Functions
    "get_resource_coordinator",
    "initialize_resource_management",
    "shutdown_resource_management",
    # =========================================================================
    # CROSS-REPO BRIDGE
    # =========================================================================
    # Configuration
    "CrossRepoResourceConfig",
    # Enums
    "ResourceEventType",
    "SyncDirection",
    "ConflictResolution",
    "RepoStatus",
    # Data Structures
    "ResourceEvent",
    "ResourceInventory",
    "AllocationRequest",
    "AllocationResponse",
    "RepoHealthStatus",
    # Event Bus
    "ResourceEventBus",
    # Bridge
    "CrossRepoResourceBridge",
    # Global Functions
    "get_cross_repo_resource_bridge",
    "initialize_cross_repo_resources",
    "shutdown_cross_repo_resources",
    # =========================================================================
    # SUPERVISOR INTEGRATION
    # =========================================================================
    # Enums
    "ResourceSystemState",
    "ResourceComponentState",
    # Configuration
    "ResourceManagementSupervisorConfig",
    # Data Structures
    "ResourceComponentHealth",
    "ResourceManagementInitResult",
    "ResourceManagementHealthReport",
    # Coordinator
    "ResourceManagementSupervisorCoordinator",
    # Global Functions
    "get_resource_management_supervisor",
    "initialize_resource_management_supervisor",
    "shutdown_resource_management_supervisor",
    "get_resource_management_status",
    "get_resource_management_health",
    # Context Manager
    "ResourceManagementContext",
]

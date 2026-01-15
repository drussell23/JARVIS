"""
Resource Management Supervisor Integration v1.0
================================================

Provides integration between the Resource Management system and the
JARVIS Supervisor for centralized startup/shutdown management.

Features:
- Single-command initialization
- Health monitoring integration
- Graceful shutdown handling
- Status reporting
- Cross-repo coordination

Author: Trinity Resource System
Version: 1.0.0
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ResourceSystemState(Enum):
    """Overall state of the resource management system."""
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    DEGRADED = auto()
    SHUTTING_DOWN = auto()
    SHUTDOWN = auto()
    ERROR = auto()


class ResourceComponentState(Enum):
    """State of individual resource components."""
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    DEGRADED = auto()
    STOPPING = auto()
    ERROR = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ResourceManagementSupervisorConfig:
    """Configuration for resource management supervisor integration."""

    # Enable/disable components
    coordinator_enabled: bool = os.getenv("RESOURCE_COORDINATOR_ENABLED", "true").lower() == "true"
    cross_repo_enabled: bool = os.getenv("RESOURCE_CROSS_REPO_ENABLED", "true").lower() == "true"

    # Timeouts
    startup_timeout: float = float(os.getenv("RESOURCE_STARTUP_TIMEOUT", "30.0"))
    shutdown_timeout: float = float(os.getenv("RESOURCE_SHUTDOWN_TIMEOUT", "15.0"))

    # Health check settings
    health_check_interval: float = float(os.getenv("RESOURCE_HEALTH_INTERVAL", "30.0"))
    max_consecutive_failures: int = int(os.getenv("RESOURCE_MAX_FAILURES", "3"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ResourceComponentHealth:
    """Health status of a resource component."""
    name: str
    state: ResourceComponentState = ResourceComponentState.STOPPED
    healthy: bool = False
    last_check: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None
    consecutive_failures: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceManagementInitResult:
    """Result of resource management initialization."""
    success: bool = False
    components_initialized: List[str] = field(default_factory=list)
    components_failed: List[str] = field(default_factory=list)
    initialization_time_ms: float = 0.0
    error_message: Optional[str] = None


@dataclass
class ResourceManagementHealthReport:
    """Health report for the resource management system."""
    overall_state: ResourceSystemState = ResourceSystemState.UNINITIALIZED
    overall_healthy: bool = False
    components: Dict[str, ResourceComponentHealth] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    uptime_seconds: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# SUPERVISOR COORDINATOR
# =============================================================================


class ResourceManagementSupervisorCoordinator:
    """
    Coordinates resource management with the JARVIS supervisor.

    Responsibilities:
    - Initialize all resource management components
    - Monitor component health
    - Handle graceful shutdown
    - Provide status reporting
    """

    def __init__(self, config: Optional[ResourceManagementSupervisorConfig] = None):
        self.config = config or ResourceManagementSupervisorConfig()
        self.logger = logging.getLogger("ResourceManagementSupervisor")

        # State
        self._state = ResourceSystemState.UNINITIALIZED
        self._start_time: Optional[datetime] = None
        self._components: Dict[str, ResourceComponentHealth] = {}
        self._coordinator = None
        self._bridge = None

        # Tasks
        self._health_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> ResourceSystemState:
        """Get current system state."""
        return self._state

    async def initialize(self) -> ResourceManagementInitResult:
        """Initialize all resource management components."""
        start_time = time.time()
        result = ResourceManagementInitResult()

        self._state = ResourceSystemState.INITIALIZING
        self.logger.info("Initializing Resource Management System...")

        try:
            # Initialize Unified Resource Coordinator
            if self.config.coordinator_enabled:
                await self._init_coordinator()
                result.components_initialized.append("UnifiedResourceCoordinator")

            # Initialize Cross-Repo Bridge
            if self.config.cross_repo_enabled:
                await self._init_cross_repo_bridge()
                result.components_initialized.append("CrossRepoResourceBridge")

            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor_loop())
            result.components_initialized.append("HealthMonitor")

            self._start_time = datetime.utcnow()
            self._state = ResourceSystemState.RUNNING
            result.success = True
            result.initialization_time_ms = (time.time() - start_time) * 1000

            self.logger.info(
                f"Resource Management initialized in {result.initialization_time_ms:.1f}ms "
                f"({len(result.components_initialized)} components)"
            )

        except Exception as e:
            self._state = ResourceSystemState.ERROR
            result.success = False
            result.error_message = str(e)
            self.logger.error(f"Resource Management initialization failed: {e}")

        return result

    async def shutdown(self):
        """Shutdown all resource management components."""
        if self._state in [ResourceSystemState.SHUTDOWN, ResourceSystemState.SHUTTING_DOWN]:
            return

        self._state = ResourceSystemState.SHUTTING_DOWN
        self.logger.info("Shutting down Resource Management System...")

        try:
            # Stop health monitoring
            if self._health_task:
                self._health_task.cancel()
                try:
                    await asyncio.wait_for(self._health_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Shutdown cross-repo bridge
            if self._bridge is not None:
                try:
                    from backend.core.resource_management.cross_repo_bridge import (
                        shutdown_cross_repo_resources,
                    )
                    await shutdown_cross_repo_resources()
                    self.logger.info("Cross-Repo Resource Bridge shutdown")
                except Exception as e:
                    self.logger.error(f"Bridge shutdown error: {e}")

            # Shutdown coordinator
            if self._coordinator is not None:
                try:
                    from backend.core.resource_management.unified_engine import (
                        shutdown_resource_management,
                    )
                    await shutdown_resource_management()
                    self.logger.info("Unified Resource Coordinator shutdown")
                except Exception as e:
                    self.logger.error(f"Coordinator shutdown error: {e}")

            self._state = ResourceSystemState.SHUTDOWN
            self.logger.info("Resource Management System shutdown complete")

        except Exception as e:
            self._state = ResourceSystemState.ERROR
            self.logger.error(f"Shutdown error: {e}")

    async def get_status(self) -> Dict[str, Any]:
        """Get current system status."""
        async with self._lock:
            uptime = 0.0
            if self._start_time:
                uptime = (datetime.utcnow() - self._start_time).total_seconds()

            status = {
                "state": self._state.name,
                "uptime_seconds": uptime,
                "components": {},
            }

            for name, health in self._components.items():
                status["components"][name] = {
                    "state": health.state.name,
                    "healthy": health.healthy,
                    "consecutive_failures": health.consecutive_failures,
                    "last_check": health.last_check.isoformat(),
                }

            # Add coordinator status if available
            if self._coordinator is not None:
                try:
                    coordinator_status = await self._coordinator.get_status()
                    status["coordinator"] = coordinator_status
                except Exception as e:
                    status["coordinator_error"] = str(e)

            return status

    async def get_health(self) -> ResourceManagementHealthReport:
        """Get comprehensive health report."""
        async with self._lock:
            uptime = 0.0
            if self._start_time:
                uptime = (datetime.utcnow() - self._start_time).total_seconds()

            # Determine overall health
            all_healthy = all(c.healthy for c in self._components.values())
            any_running = any(
                c.state == ResourceComponentState.RUNNING
                for c in self._components.values()
            )

            if all_healthy and any_running:
                overall_healthy = True
            elif any_running:
                overall_healthy = False  # Some components unhealthy
            else:
                overall_healthy = False

            return ResourceManagementHealthReport(
                overall_state=self._state,
                overall_healthy=overall_healthy,
                components=self._components.copy(),
                uptime_seconds=uptime,
            )

    # =========================================================================
    # Component Initialization
    # =========================================================================

    async def _init_coordinator(self):
        """Initialize the Unified Resource Coordinator."""
        self._components["UnifiedResourceCoordinator"] = ResourceComponentHealth(
            name="UnifiedResourceCoordinator",
            state=ResourceComponentState.STARTING,
        )

        try:
            from backend.core.resource_management.unified_engine import (
                initialize_resource_management,
                get_resource_coordinator,
            )

            # Initialize and start
            await initialize_resource_management()
            self._coordinator = await get_resource_coordinator()

            self._components["UnifiedResourceCoordinator"].state = ResourceComponentState.RUNNING
            self._components["UnifiedResourceCoordinator"].healthy = True
            self.logger.info("Unified Resource Coordinator initialized")

        except Exception as e:
            self._components["UnifiedResourceCoordinator"].state = ResourceComponentState.ERROR
            self._components["UnifiedResourceCoordinator"].error_message = str(e)
            self.logger.error(f"Coordinator initialization failed: {e}")
            raise

    async def _init_cross_repo_bridge(self):
        """Initialize the Cross-Repo Resource Bridge."""
        self._components["CrossRepoResourceBridge"] = ResourceComponentHealth(
            name="CrossRepoResourceBridge",
            state=ResourceComponentState.STARTING,
        )

        try:
            from backend.core.resource_management.cross_repo_bridge import (
                initialize_cross_repo_resources,
                get_cross_repo_resource_bridge,
            )

            # Initialize and start
            await initialize_cross_repo_resources()
            self._bridge = await get_cross_repo_resource_bridge()

            self._components["CrossRepoResourceBridge"].state = ResourceComponentState.RUNNING
            self._components["CrossRepoResourceBridge"].healthy = True
            self.logger.info("Cross-Repo Resource Bridge initialized")

        except Exception as e:
            self._components["CrossRepoResourceBridge"].state = ResourceComponentState.ERROR
            self._components["CrossRepoResourceBridge"].error_message = str(e)
            self.logger.error(f"Bridge initialization failed: {e}")
            raise

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_monitor_loop(self):
        """Periodic health monitoring loop."""
        while True:
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._check_all_health()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")

    async def _check_all_health(self):
        """Check health of all components."""
        async with self._lock:
            # Check coordinator
            if "UnifiedResourceCoordinator" in self._components:
                await self._check_coordinator_health()

            # Check bridge
            if "CrossRepoResourceBridge" in self._components:
                await self._check_bridge_health()

            # Update overall state based on component health
            await self._update_overall_state()

    async def _check_coordinator_health(self):
        """Check coordinator health."""
        component = self._components["UnifiedResourceCoordinator"]
        component.last_check = datetime.utcnow()

        try:
            if self._coordinator is not None:
                status = await self._coordinator.get_status()
                if status.get("running", False):
                    component.healthy = True
                    component.state = ResourceComponentState.RUNNING
                    component.consecutive_failures = 0
                    component.metrics = status
                else:
                    component.healthy = False
                    component.state = ResourceComponentState.DEGRADED
            else:
                component.healthy = False
                component.state = ResourceComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = ResourceComponentState.ERROR

    async def _check_bridge_health(self):
        """Check bridge health."""
        component = self._components["CrossRepoResourceBridge"]
        component.last_check = datetime.utcnow()

        try:
            if self._bridge is not None:
                health = await self._bridge.get_health()
                component.healthy = True
                component.state = ResourceComponentState.RUNNING
                component.consecutive_failures = 0
                component.metrics = {"repo_health": {k.value: v.status.name for k, v in health.items()}}
            else:
                component.healthy = False
                component.state = ResourceComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = ResourceComponentState.ERROR

    async def _update_overall_state(self):
        """Update overall system state based on component health."""
        if self._state in [ResourceSystemState.SHUTDOWN, ResourceSystemState.SHUTTING_DOWN]:
            return

        all_healthy = all(c.healthy for c in self._components.values())
        any_error = any(
            c.state == ResourceComponentState.ERROR
            for c in self._components.values()
        )

        if any_error:
            self._state = ResourceSystemState.ERROR
        elif all_healthy:
            self._state = ResourceSystemState.RUNNING
        else:
            self._state = ResourceSystemState.DEGRADED


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_supervisor: Optional[ResourceManagementSupervisorCoordinator] = None
_supervisor_lock = asyncio.Lock()


async def get_resource_management_supervisor() -> ResourceManagementSupervisorCoordinator:
    """Get or create the global supervisor instance."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is None:
            _supervisor = ResourceManagementSupervisorCoordinator()
        return _supervisor


async def initialize_resource_management_supervisor() -> ResourceManagementInitResult:
    """Initialize the resource management supervisor."""
    supervisor = await get_resource_management_supervisor()
    return await supervisor.initialize()


async def shutdown_resource_management_supervisor():
    """Shutdown the resource management supervisor."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is not None:
            await _supervisor.shutdown()
            _supervisor = None
            logger.info("Resource Management Supervisor shutdown")


async def get_resource_management_status() -> Dict[str, Any]:
    """Get resource management system status."""
    supervisor = await get_resource_management_supervisor()
    return await supervisor.get_status()


async def get_resource_management_health() -> ResourceManagementHealthReport:
    """Get resource management system health."""
    supervisor = await get_resource_management_supervisor()
    return await supervisor.get_health()


# =============================================================================
# CONTEXT MANAGER
# =============================================================================


class ResourceManagementContext:
    """
    Async context manager for resource management lifecycle.

    Usage:
        async with ResourceManagementContext() as ctx:
            # Resource management is active
            status = await ctx.get_status()
    """

    def __init__(self, config: Optional[ResourceManagementSupervisorConfig] = None):
        self.config = config
        self._supervisor: Optional[ResourceManagementSupervisorCoordinator] = None

    async def __aenter__(self) -> ResourceManagementSupervisorCoordinator:
        """Enter the context, initializing resource management."""
        self._supervisor = ResourceManagementSupervisorCoordinator(self.config)
        result = await self._supervisor.initialize()
        if not result.success:
            raise RuntimeError(f"Resource management initialization failed: {result.error_message}")
        return self._supervisor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the context, shutting down resource management."""
        if self._supervisor:
            await self._supervisor.shutdown()
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
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

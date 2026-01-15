"""
Security Supervisor Integration v1.0
=====================================

Provides integration between the Security System and the
JARVIS Supervisor for centralized startup/shutdown management.

Features:
- Single-command initialization
- Security health monitoring
- Graceful shutdown handling
- Status reporting
- Cross-repo security coordination

Author: Trinity Security System
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


class SecuritySystemState(Enum):
    """Overall state of the security system."""
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    DEGRADED = auto()
    SHUTTING_DOWN = auto()
    SHUTDOWN = auto()
    ERROR = auto()


class SecurityComponentState(Enum):
    """State of individual security components."""
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
class SecuritySupervisorConfig:
    """Configuration for security supervisor integration."""

    # Enable/disable components
    security_engine_enabled: bool = os.getenv("SECURITY_ENGINE_ENABLED", "true").lower() == "true"
    cross_repo_security_enabled: bool = os.getenv("CROSS_REPO_SECURITY_ENABLED", "true").lower() == "true"
    audit_enabled: bool = os.getenv("SECURITY_AUDIT_ENABLED", "true").lower() == "true"

    # Timeouts
    startup_timeout: float = float(os.getenv("SECURITY_STARTUP_TIMEOUT", "30.0"))
    shutdown_timeout: float = float(os.getenv("SECURITY_SHUTDOWN_TIMEOUT", "15.0"))

    # Health check settings
    health_check_interval: float = float(os.getenv("SECURITY_HEALTH_INTERVAL", "30.0"))
    max_consecutive_failures: int = int(os.getenv("SECURITY_MAX_FAILURES", "3"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class SecurityComponentHealth:
    """Health status of a security component."""
    name: str
    state: SecurityComponentState = SecurityComponentState.STOPPED
    healthy: bool = False
    last_check: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None
    consecutive_failures: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SecurityInitResult:
    """Result of security system initialization."""
    success: bool = False
    components_initialized: List[str] = field(default_factory=list)
    components_failed: List[str] = field(default_factory=list)
    initialization_time_ms: float = 0.0
    error_message: Optional[str] = None


@dataclass
class SecurityHealthReport:
    """Health report for the security system."""
    overall_state: SecuritySystemState = SecuritySystemState.UNINITIALIZED
    overall_healthy: bool = False
    components: Dict[str, SecurityComponentHealth] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    uptime_seconds: float = 0.0
    security_metrics: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# SUPERVISOR COORDINATOR
# =============================================================================


class SecuritySupervisorCoordinator:
    """
    Coordinates security with the JARVIS supervisor.

    Responsibilities:
    - Initialize all security components
    - Monitor security health
    - Handle graceful shutdown
    - Provide security status reporting
    """

    def __init__(self, config: Optional[SecuritySupervisorConfig] = None):
        self.config = config or SecuritySupervisorConfig()
        self.logger = logging.getLogger("SecuritySupervisor")

        # State
        self._state = SecuritySystemState.UNINITIALIZED
        self._start_time: Optional[datetime] = None
        self._components: Dict[str, SecurityComponentHealth] = {}
        self._security_engine = None
        self._security_bridge = None

        # Tasks
        self._health_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> SecuritySystemState:
        """Get current system state."""
        return self._state

    async def initialize(self) -> SecurityInitResult:
        """Initialize all security components."""
        start_time = time.time()
        result = SecurityInitResult()

        self._state = SecuritySystemState.INITIALIZING
        self.logger.info("Initializing Security System...")

        try:
            # Initialize Security Engine
            if self.config.security_engine_enabled:
                await self._init_security_engine()
                result.components_initialized.append("UnifiedSecurityEngine")

            # Initialize Cross-Repo Security Bridge
            if self.config.cross_repo_security_enabled:
                await self._init_security_bridge()
                result.components_initialized.append("CrossRepoSecurityBridge")

            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor_loop())
            result.components_initialized.append("SecurityHealthMonitor")

            self._start_time = datetime.utcnow()
            self._state = SecuritySystemState.RUNNING
            result.success = True
            result.initialization_time_ms = (time.time() - start_time) * 1000

            self.logger.info(
                f"Security System initialized in {result.initialization_time_ms:.1f}ms "
                f"({len(result.components_initialized)} components)"
            )

        except Exception as e:
            self._state = SecuritySystemState.ERROR
            result.success = False
            result.error_message = str(e)
            self.logger.error(f"Security System initialization failed: {e}")

        return result

    async def shutdown(self):
        """Shutdown all security components."""
        if self._state in [SecuritySystemState.SHUTDOWN, SecuritySystemState.SHUTTING_DOWN]:
            return

        self._state = SecuritySystemState.SHUTTING_DOWN
        self.logger.info("Shutting down Security System...")

        try:
            # Stop health monitoring
            if self._health_task:
                self._health_task.cancel()
                try:
                    await asyncio.wait_for(self._health_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Shutdown cross-repo security bridge
            if self._security_bridge is not None:
                try:
                    from backend.core.security.cross_repo_bridge import (
                        shutdown_cross_repo_security,
                    )
                    await shutdown_cross_repo_security()
                    self.logger.info("Cross-Repo Security Bridge shutdown")
                except Exception as e:
                    self.logger.error(f"Security bridge shutdown error: {e}")

            # Shutdown security engine
            if self._security_engine is not None:
                try:
                    from backend.core.security.unified_engine import shutdown_security
                    await shutdown_security()
                    self.logger.info("Security Engine shutdown")
                except Exception as e:
                    self.logger.error(f"Security engine shutdown error: {e}")

            self._state = SecuritySystemState.SHUTDOWN
            self.logger.info("Security System shutdown complete")

        except Exception as e:
            self._state = SecuritySystemState.ERROR
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

            # Add security engine status if available
            if self._security_engine is not None:
                try:
                    engine_status = await self._security_engine.get_status()
                    status["engine"] = engine_status
                except Exception as e:
                    status["engine_error"] = str(e)

            # Add bridge status if available
            if self._security_bridge is not None:
                try:
                    bridge_status = await self._security_bridge.get_status()
                    status["bridge"] = bridge_status
                except Exception as e:
                    status["bridge_error"] = str(e)

            return status

    async def get_health(self) -> SecurityHealthReport:
        """Get comprehensive health report."""
        async with self._lock:
            uptime = 0.0
            if self._start_time:
                uptime = (datetime.utcnow() - self._start_time).total_seconds()

            # Determine overall health
            all_healthy = all(c.healthy for c in self._components.values())
            any_running = any(
                c.state == SecurityComponentState.RUNNING
                for c in self._components.values()
            )

            if all_healthy and any_running:
                overall_healthy = True
            elif any_running:
                overall_healthy = False
            else:
                overall_healthy = False

            # Gather security metrics
            security_metrics = {}
            if self._security_engine:
                try:
                    status = await self._security_engine.get_status()
                    security_metrics.update(status)
                except Exception:
                    pass

            return SecurityHealthReport(
                overall_state=self._state,
                overall_healthy=overall_healthy,
                components=self._components.copy(),
                uptime_seconds=uptime,
                security_metrics=security_metrics,
            )

    # =========================================================================
    # Component Initialization
    # =========================================================================

    async def _init_security_engine(self):
        """Initialize the Security Engine."""
        self._components["UnifiedSecurityEngine"] = SecurityComponentHealth(
            name="UnifiedSecurityEngine",
            state=SecurityComponentState.STARTING,
        )

        try:
            from backend.core.security.unified_engine import (
                initialize_security,
                get_security_engine,
            )

            # Initialize and start
            await initialize_security()
            self._security_engine = await get_security_engine()

            self._components["UnifiedSecurityEngine"].state = SecurityComponentState.RUNNING
            self._components["UnifiedSecurityEngine"].healthy = True
            self.logger.info("Security Engine initialized")

        except Exception as e:
            self._components["UnifiedSecurityEngine"].state = SecurityComponentState.ERROR
            self._components["UnifiedSecurityEngine"].error_message = str(e)
            self.logger.error(f"Security engine initialization failed: {e}")
            raise

    async def _init_security_bridge(self):
        """Initialize the Cross-Repo Security Bridge."""
        self._components["CrossRepoSecurityBridge"] = SecurityComponentHealth(
            name="CrossRepoSecurityBridge",
            state=SecurityComponentState.STARTING,
        )

        try:
            from backend.core.security.cross_repo_bridge import (
                initialize_cross_repo_security,
                get_cross_repo_security_bridge,
            )

            # Initialize and start
            await initialize_cross_repo_security()
            self._security_bridge = await get_cross_repo_security_bridge()

            self._components["CrossRepoSecurityBridge"].state = SecurityComponentState.RUNNING
            self._components["CrossRepoSecurityBridge"].healthy = True
            self.logger.info("Cross-Repo Security Bridge initialized")

        except Exception as e:
            self._components["CrossRepoSecurityBridge"].state = SecurityComponentState.ERROR
            self._components["CrossRepoSecurityBridge"].error_message = str(e)
            self.logger.error(f"Security bridge initialization failed: {e}")
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
            # Check security engine
            if "UnifiedSecurityEngine" in self._components:
                await self._check_engine_health()

            # Check bridge
            if "CrossRepoSecurityBridge" in self._components:
                await self._check_bridge_health()

            # Update overall state
            await self._update_overall_state()

    async def _check_engine_health(self):
        """Check security engine health."""
        component = self._components["UnifiedSecurityEngine"]
        component.last_check = datetime.utcnow()

        try:
            if self._security_engine is not None:
                status = await self._security_engine.get_status()
                if status.get("running", False):
                    component.healthy = True
                    component.state = SecurityComponentState.RUNNING
                    component.consecutive_failures = 0
                    component.metrics = status
                else:
                    component.healthy = False
                    component.state = SecurityComponentState.DEGRADED
            else:
                component.healthy = False
                component.state = SecurityComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = SecurityComponentState.ERROR

    async def _check_bridge_health(self):
        """Check security bridge health."""
        component = self._components["CrossRepoSecurityBridge"]
        component.last_check = datetime.utcnow()

        try:
            if self._security_bridge is not None:
                status = await self._security_bridge.get_status()
                if status.get("running", False):
                    component.healthy = True
                    component.state = SecurityComponentState.RUNNING
                    component.consecutive_failures = 0
                    component.metrics = status
                else:
                    component.healthy = False
                    component.state = SecurityComponentState.DEGRADED
            else:
                component.healthy = False
                component.state = SecurityComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = SecurityComponentState.ERROR

    async def _update_overall_state(self):
        """Update overall system state based on component health."""
        if self._state in [SecuritySystemState.SHUTDOWN, SecuritySystemState.SHUTTING_DOWN]:
            return

        all_healthy = all(c.healthy for c in self._components.values())
        any_error = any(
            c.state == SecurityComponentState.ERROR
            for c in self._components.values()
        )

        if any_error:
            self._state = SecuritySystemState.ERROR
        elif all_healthy:
            self._state = SecuritySystemState.RUNNING
        else:
            self._state = SecuritySystemState.DEGRADED


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_supervisor: Optional[SecuritySupervisorCoordinator] = None
_supervisor_lock = asyncio.Lock()


async def get_security_supervisor() -> SecuritySupervisorCoordinator:
    """Get or create the global supervisor instance."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is None:
            _supervisor = SecuritySupervisorCoordinator()
        return _supervisor


async def initialize_security_supervisor() -> SecurityInitResult:
    """Initialize the security supervisor."""
    supervisor = await get_security_supervisor()
    return await supervisor.initialize()


async def shutdown_security_supervisor():
    """Shutdown the security supervisor."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is not None:
            await _supervisor.shutdown()
            _supervisor = None
            logger.info("Security Supervisor shutdown")


async def get_security_status() -> Dict[str, Any]:
    """Get security system status."""
    supervisor = await get_security_supervisor()
    return await supervisor.get_status()


async def get_security_health() -> SecurityHealthReport:
    """Get security system health."""
    supervisor = await get_security_supervisor()
    return await supervisor.get_health()


# =============================================================================
# CONTEXT MANAGER
# =============================================================================


class SecurityContext:
    """
    Async context manager for security lifecycle.

    Usage:
        async with SecurityContext() as ctx:
            # Security is active
            status = await ctx.get_status()
    """

    def __init__(self, config: Optional[SecuritySupervisorConfig] = None):
        self.config = config
        self._supervisor: Optional[SecuritySupervisorCoordinator] = None

    async def __aenter__(self) -> SecuritySupervisorCoordinator:
        """Enter the context, initializing security."""
        self._supervisor = SecuritySupervisorCoordinator(self.config)
        result = await self._supervisor.initialize()
        if not result.success:
            raise RuntimeError(f"Security initialization failed: {result.error_message}")
        return self._supervisor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the context, shutting down security."""
        if self._supervisor:
            await self._supervisor.shutdown()
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Enums
    "SecuritySystemState",
    "SecurityComponentState",
    # Configuration
    "SecuritySupervisorConfig",
    # Data Structures
    "SecurityComponentHealth",
    "SecurityInitResult",
    "SecurityHealthReport",
    # Coordinator
    "SecuritySupervisorCoordinator",
    # Global Functions
    "get_security_supervisor",
    "initialize_security_supervisor",
    "shutdown_security_supervisor",
    "get_security_status",
    "get_security_health",
    # Context Manager
    "SecurityContext",
]

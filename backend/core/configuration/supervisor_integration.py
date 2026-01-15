"""
Configuration Supervisor Integration v1.0
==========================================

Provides integration between the Configuration System and the
JARVIS Supervisor for centralized startup/shutdown management.

Features:
- Single-command initialization
- Configuration health monitoring
- Graceful shutdown handling
- Status reporting
- Cross-repo coordination

Author: Trinity Configuration System
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


class ConfigSystemState(Enum):
    """Overall state of the configuration system."""
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    DEGRADED = auto()
    SHUTTING_DOWN = auto()
    SHUTDOWN = auto()
    ERROR = auto()


class ConfigComponentState(Enum):
    """State of individual configuration components."""
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
class ConfigSupervisorConfig:
    """Configuration for configuration supervisor integration."""

    # Enable/disable components
    config_engine_enabled: bool = os.getenv("CONFIG_ENGINE_ENABLED", "true").lower() == "true"
    cross_repo_config_enabled: bool = os.getenv("CROSS_REPO_CONFIG_ENABLED", "true").lower() == "true"
    hot_reload_enabled: bool = os.getenv("CONFIG_HOT_RELOAD", "true").lower() == "true"

    # Timeouts
    startup_timeout: float = float(os.getenv("CONFIG_STARTUP_TIMEOUT", "30.0"))
    shutdown_timeout: float = float(os.getenv("CONFIG_SHUTDOWN_TIMEOUT", "15.0"))

    # Health check settings
    health_check_interval: float = float(os.getenv("CONFIG_HEALTH_INTERVAL", "30.0"))
    max_consecutive_failures: int = int(os.getenv("CONFIG_MAX_FAILURES", "3"))

    # Initial config files to load
    initial_config_files: str = os.getenv("CONFIG_INITIAL_FILES", "")


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ConfigComponentHealth:
    """Health status of a configuration component."""
    name: str
    state: ConfigComponentState = ConfigComponentState.STOPPED
    healthy: bool = False
    last_check: datetime = field(default_factory=datetime.utcnow)
    error_message: Optional[str] = None
    consecutive_failures: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfigInitResult:
    """Result of configuration system initialization."""
    success: bool = False
    components_initialized: List[str] = field(default_factory=list)
    components_failed: List[str] = field(default_factory=list)
    initialization_time_ms: float = 0.0
    error_message: Optional[str] = None
    config_files_loaded: int = 0


@dataclass
class ConfigHealthReport:
    """Health report for the configuration system."""
    overall_state: ConfigSystemState = ConfigSystemState.UNINITIALIZED
    overall_healthy: bool = False
    components: Dict[str, ConfigComponentHealth] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    uptime_seconds: float = 0.0
    config_metrics: Dict[str, Any] = field(default_factory=dict)
    health_score: float = 0.0

    @property
    def is_healthy(self) -> bool:
        """Alias for overall_healthy for consistency."""
        return self.overall_healthy


# =============================================================================
# SUPERVISOR COORDINATOR
# =============================================================================


class ConfigSupervisorCoordinator:
    """
    Coordinates configuration with the JARVIS supervisor.

    Responsibilities:
    - Initialize all configuration components
    - Monitor configuration health
    - Handle graceful shutdown
    - Provide configuration status reporting
    """

    def __init__(self, config: Optional[ConfigSupervisorConfig] = None):
        self.config = config or ConfigSupervisorConfig()
        self.logger = logging.getLogger("ConfigSupervisor")

        # State
        self._state = ConfigSystemState.UNINITIALIZED
        self._start_time: Optional[datetime] = None
        self._components: Dict[str, ConfigComponentHealth] = {}
        self._config_engine = None
        self._config_bridge = None

        # Tasks
        self._health_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> ConfigSystemState:
        """Get current system state."""
        return self._state

    async def initialize(self) -> ConfigInitResult:
        """Initialize all configuration components."""
        start_time = time.time()
        result = ConfigInitResult()

        self._state = ConfigSystemState.INITIALIZING
        self.logger.info("Initializing Configuration System...")

        try:
            # Initialize Configuration Engine
            if self.config.config_engine_enabled:
                await self._init_config_engine()
                result.components_initialized.append("UnifiedConfigurationEngine")

            # Initialize Cross-Repo Config Bridge
            if self.config.cross_repo_config_enabled:
                await self._init_config_bridge()
                result.components_initialized.append("CrossRepoConfigBridge")

            # Load initial config files
            if self.config.initial_config_files:
                loaded = await self._load_initial_configs()
                result.config_files_loaded = loaded

            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor_loop())
            result.components_initialized.append("ConfigHealthMonitor")

            self._start_time = datetime.utcnow()
            self._state = ConfigSystemState.RUNNING
            result.success = True
            result.initialization_time_ms = (time.time() - start_time) * 1000

            self.logger.info(
                f"Configuration System initialized in {result.initialization_time_ms:.1f}ms "
                f"({len(result.components_initialized)} components, "
                f"{result.config_files_loaded} config files)"
            )

        except Exception as e:
            self._state = ConfigSystemState.ERROR
            result.success = False
            result.error_message = str(e)
            self.logger.error(f"Configuration System initialization failed: {e}")

        return result

    async def shutdown(self):
        """Shutdown all configuration components."""
        if self._state in [ConfigSystemState.SHUTDOWN, ConfigSystemState.SHUTTING_DOWN]:
            return

        self._state = ConfigSystemState.SHUTTING_DOWN
        self.logger.info("Shutting down Configuration System...")

        try:
            # Stop health monitoring
            if self._health_task:
                self._health_task.cancel()
                try:
                    await asyncio.wait_for(self._health_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            # Shutdown cross-repo config bridge
            if self._config_bridge is not None:
                try:
                    from backend.core.configuration.cross_repo_bridge import (
                        shutdown_cross_repo_config,
                    )
                    await shutdown_cross_repo_config()
                    self.logger.info("Cross-Repo Config Bridge shutdown")
                except Exception as e:
                    self.logger.error(f"Config bridge shutdown error: {e}")

            # Shutdown configuration engine
            if self._config_engine is not None:
                try:
                    from backend.core.configuration.unified_engine import (
                        shutdown_configuration,
                    )
                    await shutdown_configuration()
                    self.logger.info("Configuration Engine shutdown")
                except Exception as e:
                    self.logger.error(f"Config engine shutdown error: {e}")

            self._state = ConfigSystemState.SHUTDOWN
            self.logger.info("Configuration System shutdown complete")

        except Exception as e:
            self._state = ConfigSystemState.ERROR
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

            # Add config engine status if available
            if self._config_engine is not None:
                try:
                    engine_status = await self._config_engine.get_status()
                    status["engine"] = engine_status
                except Exception as e:
                    status["engine_error"] = str(e)

            # Add bridge status if available
            if self._config_bridge is not None:
                try:
                    bridge_status = await self._config_bridge.get_status()
                    status["bridge"] = bridge_status
                except Exception as e:
                    status["bridge_error"] = str(e)

            return status

    async def get_health(self) -> ConfigHealthReport:
        """Get comprehensive health report."""
        async with self._lock:
            uptime = 0.0
            if self._start_time:
                uptime = (datetime.utcnow() - self._start_time).total_seconds()

            # Determine overall health
            all_healthy = all(c.healthy for c in self._components.values())
            any_running = any(
                c.state == ConfigComponentState.RUNNING
                for c in self._components.values()
            )

            overall_healthy = all_healthy and any_running

            # Gather config metrics
            config_metrics = {}
            if self._config_engine:
                try:
                    status = await self._config_engine.get_status()
                    config_metrics.update(status)
                except Exception:
                    pass

            # Calculate health score (0.0 to 1.0)
            if self._components:
                healthy_count = sum(1 for c in self._components.values() if c.healthy)
                health_score = healthy_count / len(self._components)
            else:
                health_score = 0.0

            return ConfigHealthReport(
                overall_state=self._state,
                overall_healthy=overall_healthy,
                components=self._components.copy(),
                uptime_seconds=uptime,
                config_metrics=config_metrics,
                health_score=health_score,
            )

    # =========================================================================
    # Component Initialization
    # =========================================================================

    async def _init_config_engine(self):
        """Initialize the Configuration Engine."""
        self._components["UnifiedConfigurationEngine"] = ConfigComponentHealth(
            name="UnifiedConfigurationEngine",
            state=ConfigComponentState.STARTING,
        )

        try:
            from backend.core.configuration.unified_engine import (
                initialize_configuration,
                get_configuration_engine,
            )

            # Initialize
            await initialize_configuration()
            self._config_engine = await get_configuration_engine()

            # Load environment variables
            await self._config_engine.load_environment("JARVIS_")

            self._components["UnifiedConfigurationEngine"].state = ConfigComponentState.RUNNING
            self._components["UnifiedConfigurationEngine"].healthy = True
            self.logger.info("Configuration Engine initialized")

        except Exception as e:
            self._components["UnifiedConfigurationEngine"].state = ConfigComponentState.ERROR
            self._components["UnifiedConfigurationEngine"].error_message = str(e)
            self.logger.error(f"Config engine initialization failed: {e}")
            raise

    async def _init_config_bridge(self):
        """Initialize the Cross-Repo Config Bridge."""
        self._components["CrossRepoConfigBridge"] = ConfigComponentHealth(
            name="CrossRepoConfigBridge",
            state=ConfigComponentState.STARTING,
        )

        try:
            from backend.core.configuration.cross_repo_bridge import (
                initialize_cross_repo_config,
                get_cross_repo_config_bridge,
            )

            # Initialize and start
            await initialize_cross_repo_config()
            self._config_bridge = await get_cross_repo_config_bridge()

            self._components["CrossRepoConfigBridge"].state = ConfigComponentState.RUNNING
            self._components["CrossRepoConfigBridge"].healthy = True
            self.logger.info("Cross-Repo Config Bridge initialized")

        except Exception as e:
            self._components["CrossRepoConfigBridge"].state = ConfigComponentState.ERROR
            self._components["CrossRepoConfigBridge"].error_message = str(e)
            self.logger.error(f"Config bridge initialization failed: {e}")
            raise

    async def _load_initial_configs(self) -> int:
        """Load initial configuration files."""
        if not self._config_engine:
            return 0

        loaded = 0
        files = self.config.initial_config_files.split(",")

        for file_path in files:
            file_path = file_path.strip()
            if file_path:
                try:
                    await self._config_engine.load_file(file_path)
                    loaded += 1
                except Exception as e:
                    self.logger.warning(f"Failed to load config file {file_path}: {e}")

        return loaded

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
            # Check config engine
            if "UnifiedConfigurationEngine" in self._components:
                await self._check_engine_health()

            # Check bridge
            if "CrossRepoConfigBridge" in self._components:
                await self._check_bridge_health()

            # Update overall state
            await self._update_overall_state()

    async def _check_engine_health(self):
        """Check configuration engine health."""
        component = self._components["UnifiedConfigurationEngine"]
        component.last_check = datetime.utcnow()

        try:
            if self._config_engine is not None:
                status = await self._config_engine.get_status()
                if status.get("running", False):
                    component.healthy = True
                    component.state = ConfigComponentState.RUNNING
                    component.consecutive_failures = 0
                    component.metrics = status
                else:
                    component.healthy = False
                    component.state = ConfigComponentState.DEGRADED
            else:
                component.healthy = False
                component.state = ConfigComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = ConfigComponentState.ERROR

    async def _check_bridge_health(self):
        """Check config bridge health."""
        component = self._components["CrossRepoConfigBridge"]
        component.last_check = datetime.utcnow()

        try:
            if self._config_bridge is not None:
                status = await self._config_bridge.get_status()
                if status.get("running", False):
                    component.healthy = True
                    component.state = ConfigComponentState.RUNNING
                    component.consecutive_failures = 0
                    component.metrics = status
                else:
                    component.healthy = False
                    component.state = ConfigComponentState.DEGRADED
            else:
                component.healthy = False
                component.state = ConfigComponentState.STOPPED

        except Exception as e:
            component.consecutive_failures += 1
            component.error_message = str(e)

            if component.consecutive_failures >= self.config.max_consecutive_failures:
                component.healthy = False
                component.state = ConfigComponentState.ERROR

    async def _update_overall_state(self):
        """Update overall system state based on component health."""
        if self._state in [ConfigSystemState.SHUTDOWN, ConfigSystemState.SHUTTING_DOWN]:
            return

        all_healthy = all(c.healthy for c in self._components.values())
        any_error = any(
            c.state == ConfigComponentState.ERROR
            for c in self._components.values()
        )

        if any_error:
            self._state = ConfigSystemState.ERROR
        elif all_healthy:
            self._state = ConfigSystemState.RUNNING
        else:
            self._state = ConfigSystemState.DEGRADED


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_supervisor: Optional[ConfigSupervisorCoordinator] = None
_supervisor_lock = asyncio.Lock()


async def get_config_supervisor() -> ConfigSupervisorCoordinator:
    """Get or create the global supervisor instance."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is None:
            _supervisor = ConfigSupervisorCoordinator()
        return _supervisor


async def initialize_config_supervisor() -> ConfigInitResult:
    """Initialize the configuration supervisor."""
    supervisor = await get_config_supervisor()
    return await supervisor.initialize()


async def shutdown_config_supervisor():
    """Shutdown the configuration supervisor."""
    global _supervisor

    async with _supervisor_lock:
        if _supervisor is not None:
            await _supervisor.shutdown()
            _supervisor = None
            logger.info("Config Supervisor shutdown")


async def get_config_status() -> Dict[str, Any]:
    """Get configuration system status."""
    supervisor = await get_config_supervisor()
    return await supervisor.get_status()


async def get_config_health() -> ConfigHealthReport:
    """Get configuration system health."""
    supervisor = await get_config_supervisor()
    return await supervisor.get_health()


# =============================================================================
# CONTEXT MANAGER
# =============================================================================


class ConfigurationContext:
    """
    Async context manager for configuration lifecycle.

    Usage:
        async with ConfigurationContext() as ctx:
            # Configuration is active
            status = await ctx.get_status()
    """

    def __init__(self, config: Optional[ConfigSupervisorConfig] = None):
        self.config = config
        self._supervisor: Optional[ConfigSupervisorCoordinator] = None

    async def __aenter__(self) -> ConfigSupervisorCoordinator:
        """Enter the context, initializing configuration."""
        self._supervisor = ConfigSupervisorCoordinator(self.config)
        result = await self._supervisor.initialize()
        if not result.success:
            raise RuntimeError(f"Configuration initialization failed: {result.error_message}")
        return self._supervisor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the context, shutting down configuration."""
        if self._supervisor:
            await self._supervisor.shutdown()
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
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

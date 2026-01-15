"""
Model Management Supervisor Integration v1.0
=============================================

Provides seamless integration of the Unified Model Management System with the
JARVIS Trinity Supervisor. Enables single-command initialization and shutdown
of all model management capabilities.

Features:
- Single entry point for all model management initialization
- Component startup ordering with dependency resolution
- Health monitoring and status reporting
- Graceful shutdown
- Integration with existing model systems

Author: Trinity Model System
Version: 1.0.0
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================


class ModelSystemState(Enum):
    """State of the model management system."""
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    RUNNING = auto()
    DEGRADED = auto()
    SHUTTING_DOWN = auto()
    STOPPED = auto()


class ModelComponentState(Enum):
    """State of individual model components."""
    PENDING = auto()
    STARTING = auto()
    RUNNING = auto()
    UNHEALTHY = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class ModelManagementSupervisorConfig:
    """Configuration for model management supervisor integration."""

    # Component toggles
    enable_registry: bool = os.getenv("MODEL_ENABLE_REGISTRY", "true").lower() == "true"
    enable_ab_testing: bool = os.getenv("MODEL_ENABLE_AB_TESTING", "true").lower() == "true"
    enable_rollback: bool = os.getenv("MODEL_ENABLE_ROLLBACK", "true").lower() == "true"
    enable_validation: bool = os.getenv("MODEL_ENABLE_VALIDATION", "true").lower() == "true"
    enable_performance: bool = os.getenv("MODEL_ENABLE_PERFORMANCE", "true").lower() == "true"
    enable_lifecycle: bool = os.getenv("MODEL_ENABLE_LIFECYCLE", "true").lower() == "true"
    enable_cross_repo: bool = os.getenv("MODEL_ENABLE_CROSS_REPO", "true").lower() == "true"

    # Initialization settings
    startup_timeout_seconds: float = float(os.getenv("MODEL_STARTUP_TIMEOUT", "30.0"))
    shutdown_timeout_seconds: float = float(os.getenv("MODEL_SHUTDOWN_TIMEOUT", "30.0"))

    # Health monitoring
    health_check_interval: float = float(os.getenv("MODEL_HEALTH_CHECK_INTERVAL", "30.0"))
    unhealthy_threshold: int = int(os.getenv("MODEL_UNHEALTHY_THRESHOLD", "3"))


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class ModelComponentHealth:
    """Health status of a model component."""
    component_name: str
    state: ModelComponentState = ModelComponentState.PENDING
    last_check: Optional[datetime] = None
    consecutive_failures: int = 0
    error_message: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelManagementInitResult:
    """Result of model management initialization."""
    success: bool
    state: ModelSystemState
    components: Dict[str, ModelComponentHealth]
    errors: List[str]
    duration_seconds: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ModelManagementHealthReport:
    """Health report for the model management system."""
    system_state: ModelSystemState
    components: Dict[str, ModelComponentHealth]
    overall_health_score: float  # 0.0 to 1.0
    issues: List[str]
    metrics: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# SUPERVISOR COORDINATOR
# =============================================================================


class ModelManagementSupervisorCoordinator:
    """Coordinates model management system with the supervisor."""

    def __init__(self, config: Optional[ModelManagementSupervisorConfig] = None):
        self.config = config or ModelManagementSupervisorConfig()

        # State
        self._state = ModelSystemState.UNINITIALIZED
        self._components: Dict[str, ModelComponentHealth] = {}
        self._initialized = False

        # Component instances
        self._model_engine = None
        self._cross_repo_bridge = None

        # Background tasks
        self._health_task: Optional[asyncio.Task] = None

        # Lock for thread safety
        self._lock = asyncio.Lock()

        logger.info("ModelManagementSupervisorCoordinator created")

    @property
    def state(self) -> ModelSystemState:
        """Get current system state."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Check if system is running."""
        return self._state == ModelSystemState.RUNNING

    async def initialize(self) -> ModelManagementInitResult:
        """Initialize all model management components."""
        async with self._lock:
            if self._initialized:
                return ModelManagementInitResult(
                    success=True,
                    state=self._state,
                    components=self._components,
                    errors=[],
                    duration_seconds=0.0
                )

            start_time = datetime.utcnow()
            errors: List[str] = []
            self._state = ModelSystemState.INITIALIZING

            logger.info("Initializing Model Management System...")

            # Initialize components in order
            component_order = [
                ("registry", self._init_registry),
                ("validation", self._init_validation),
                ("performance", self._init_performance),
                ("rollback", self._init_rollback),
                ("ab_testing", self._init_ab_testing),
                ("lifecycle", self._init_lifecycle),
                ("model_engine", self._init_model_engine),
                ("cross_repo", self._init_cross_repo),
            ]

            for component_name, init_fn in component_order:
                # Check if component is enabled
                if not self._is_component_enabled(component_name):
                    logger.info(f"Component {component_name} is disabled, skipping")
                    continue

                # Initialize component
                health = ModelComponentHealth(
                    component_name=component_name,
                    state=ModelComponentState.STARTING
                )
                self._components[component_name] = health

                try:
                    await asyncio.wait_for(
                        init_fn(),
                        timeout=self.config.startup_timeout_seconds
                    )
                    health.state = ModelComponentState.RUNNING
                    health.last_check = datetime.utcnow()
                    logger.info(f"Component {component_name} initialized successfully")

                except asyncio.TimeoutError:
                    error = f"Component {component_name} initialization timed out"
                    errors.append(error)
                    health.state = ModelComponentState.FAILED
                    health.error_message = error
                    logger.error(error)

                except Exception as e:
                    error = f"Component {component_name} initialization failed: {e}"
                    errors.append(error)
                    health.state = ModelComponentState.FAILED
                    health.error_message = str(e)
                    logger.error(error)

            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor())

            # Determine final state
            failed_count = sum(
                1 for h in self._components.values()
                if h.state == ModelComponentState.FAILED
            )

            if failed_count == 0:
                self._state = ModelSystemState.RUNNING
                self._initialized = True
            elif failed_count < len(self._components):
                self._state = ModelSystemState.DEGRADED
                self._initialized = True
            else:
                self._state = ModelSystemState.STOPPED

            duration = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                f"Model Management initialization complete: "
                f"state={self._state.name}, duration={duration:.2f}s, errors={len(errors)}"
            )

            return ModelManagementInitResult(
                success=len(errors) == 0,
                state=self._state,
                components=self._components.copy(),
                errors=errors,
                duration_seconds=duration
            )

    async def shutdown(self):
        """Shutdown all model management components."""
        async with self._lock:
            if not self._initialized:
                return

            self._state = ModelSystemState.SHUTTING_DOWN
            logger.info("Shutting down Model Management System...")

            # Cancel health task
            if self._health_task:
                self._health_task.cancel()
                try:
                    await self._health_task
                except asyncio.CancelledError:
                    pass

            # Shutdown components in reverse order
            for component_name, health in reversed(list(self._components.items())):
                if health.state not in (ModelComponentState.RUNNING, ModelComponentState.UNHEALTHY):
                    continue

                health.state = ModelComponentState.STOPPING

                try:
                    await self._shutdown_component(component_name)
                    health.state = ModelComponentState.STOPPED
                    logger.info(f"Component {component_name} stopped")

                except Exception as e:
                    logger.error(f"Error stopping {component_name}: {e}")
                    health.error_message = str(e)

            # Shutdown main components
            if self._cross_repo_bridge:
                try:
                    from backend.core.model_management.cross_repo_bridge import shutdown_cross_repo_models
                    await shutdown_cross_repo_models()
                except Exception as e:
                    logger.error(f"Error shutting down cross-repo bridge: {e}")

            if self._model_engine:
                try:
                    from backend.core.model_management.unified_engine import shutdown_model_management
                    await shutdown_model_management()
                except Exception as e:
                    logger.error(f"Error shutting down model engine: {e}")

            self._state = ModelSystemState.STOPPED
            self._initialized = False
            logger.info("Model Management System shutdown complete")

    async def get_health(self) -> ModelManagementHealthReport:
        """Get health report for the model management system."""
        issues: List[str] = []
        metrics: Dict[str, Any] = {}

        # Calculate health score
        running_count = 0
        total_count = 0

        for name, health in self._components.items():
            total_count += 1
            if health.state == ModelComponentState.RUNNING:
                running_count += 1
            elif health.state == ModelComponentState.UNHEALTHY:
                issues.append(f"{name} is unhealthy: {health.error_message}")
            elif health.state == ModelComponentState.FAILED:
                issues.append(f"{name} has failed: {health.error_message}")

            if health.metrics:
                metrics[name] = health.metrics

        health_score = running_count / total_count if total_count > 0 else 0.0

        # Get engine metrics if available
        if self._model_engine:
            try:
                engine_status = await self._model_engine.get_status()
                metrics["engine"] = engine_status
            except Exception:
                pass

        # Get cross-repo metrics if available
        if self._cross_repo_bridge:
            try:
                bridge_status = self._cross_repo_bridge.get_status()
                metrics["cross_repo"] = bridge_status
            except Exception:
                pass

        return ModelManagementHealthReport(
            system_state=self._state,
            components=self._components.copy(),
            overall_health_score=health_score,
            issues=issues,
            metrics=metrics
        )

    async def get_status(self) -> Dict[str, Any]:
        """Get status summary."""
        health = await self.get_health()
        return {
            "state": self._state.name,
            "initialized": self._initialized,
            "health_score": health.overall_health_score,
            "components": {
                name: h.state.name
                for name, h in self._components.items()
            },
            "issues": health.issues,
        }

    # -------------------------------------------------------------------------
    # Component Initialization
    # -------------------------------------------------------------------------

    def _is_component_enabled(self, component_name: str) -> bool:
        """Check if a component is enabled."""
        mapping = {
            "registry": self.config.enable_registry,
            "ab_testing": self.config.enable_ab_testing,
            "rollback": self.config.enable_rollback,
            "validation": self.config.enable_validation,
            "performance": self.config.enable_performance,
            "lifecycle": self.config.enable_lifecycle,
            "cross_repo": self.config.enable_cross_repo,
            "model_engine": True,  # Always enabled
        }
        return mapping.get(component_name, True)

    async def _init_registry(self):
        """Initialize registry component."""
        pass  # Part of unified engine

    async def _init_validation(self):
        """Initialize validation component."""
        pass  # Part of unified engine

    async def _init_performance(self):
        """Initialize performance component."""
        pass  # Part of unified engine

    async def _init_rollback(self):
        """Initialize rollback component."""
        pass  # Part of unified engine

    async def _init_ab_testing(self):
        """Initialize A/B testing component."""
        pass  # Part of unified engine

    async def _init_lifecycle(self):
        """Initialize lifecycle component."""
        pass  # Part of unified engine

    async def _init_model_engine(self):
        """Initialize the main model engine."""
        from backend.core.model_management.unified_engine import (
            initialize_model_management,
            get_model_management_engine,
        )

        await initialize_model_management()
        self._model_engine = await get_model_management_engine()

    async def _init_cross_repo(self):
        """Initialize cross-repo model bridge."""
        from backend.core.model_management.cross_repo_bridge import (
            initialize_cross_repo_models,
        )

        self._cross_repo_bridge = await initialize_cross_repo_models()

    async def _shutdown_component(self, component_name: str):
        """Shutdown a specific component."""
        pass  # Components are shut down via the main engine and bridge

    # -------------------------------------------------------------------------
    # Health Monitoring
    # -------------------------------------------------------------------------

    async def _health_monitor(self):
        """Background task to monitor component health."""
        while self._state in (ModelSystemState.RUNNING, ModelSystemState.DEGRADED):
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._check_component_health()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health monitor: {e}")

    async def _check_component_health(self):
        """Check health of all components."""
        for name, health in self._components.items():
            if health.state not in (ModelComponentState.RUNNING, ModelComponentState.UNHEALTHY):
                continue

            try:
                is_healthy = await self._probe_component(name)

                if is_healthy:
                    health.state = ModelComponentState.RUNNING
                    health.consecutive_failures = 0
                    health.error_message = None
                else:
                    health.consecutive_failures += 1
                    if health.consecutive_failures >= self.config.unhealthy_threshold:
                        health.state = ModelComponentState.UNHEALTHY
                        health.error_message = "Consecutive health checks failed"

                health.last_check = datetime.utcnow()

            except Exception as e:
                health.consecutive_failures += 1
                health.error_message = str(e)
                if health.consecutive_failures >= self.config.unhealthy_threshold:
                    health.state = ModelComponentState.UNHEALTHY

        # Update system state
        unhealthy_count = sum(
            1 for h in self._components.values()
            if h.state in (ModelComponentState.UNHEALTHY, ModelComponentState.FAILED)
        )

        if unhealthy_count == 0:
            self._state = ModelSystemState.RUNNING
        elif unhealthy_count < len(self._components):
            self._state = ModelSystemState.DEGRADED

    async def _probe_component(self, component_name: str) -> bool:
        """Probe a component for health."""
        try:
            if component_name == "model_engine" and self._model_engine:
                status = await self._model_engine.get_status()
                return status.get("running", False)

            elif component_name == "cross_repo" and self._cross_repo_bridge:
                status = self._cross_repo_bridge.get_status()
                return status.get("running", False)

            return self._model_engine is not None

        except Exception:
            return False


# =============================================================================
# GLOBAL INSTANCE MANAGEMENT
# =============================================================================

_model_supervisor: Optional[ModelManagementSupervisorCoordinator] = None
_supervisor_lock = asyncio.Lock()


async def get_model_management_supervisor() -> ModelManagementSupervisorCoordinator:
    """Get or create the global model management supervisor."""
    global _model_supervisor

    async with _supervisor_lock:
        if _model_supervisor is None:
            _model_supervisor = ModelManagementSupervisorCoordinator()
        return _model_supervisor


async def initialize_model_management_supervisor(
    config: Optional[ModelManagementSupervisorConfig] = None
) -> ModelManagementInitResult:
    """Initialize the model management supervisor and all components."""
    global _model_supervisor

    async with _supervisor_lock:
        if _model_supervisor is None:
            _model_supervisor = ModelManagementSupervisorCoordinator(config)

        return await _model_supervisor.initialize()


async def shutdown_model_management_supervisor():
    """Shutdown the model management supervisor and all components."""
    global _model_supervisor

    async with _supervisor_lock:
        if _model_supervisor is not None:
            await _model_supervisor.shutdown()
            _model_supervisor = None


async def get_model_management_status() -> Dict[str, Any]:
    """Get model management status."""
    supervisor = await get_model_management_supervisor()
    return await supervisor.get_status()


async def get_model_management_health() -> ModelManagementHealthReport:
    """Get model management health report."""
    supervisor = await get_model_management_supervisor()
    return await supervisor.get_health()


# =============================================================================
# CONTEXT MANAGER
# =============================================================================


class ModelManagementContext:
    """Context manager for model management initialization."""

    def __init__(self, config: Optional[ModelManagementSupervisorConfig] = None):
        self.config = config
        self._result: Optional[ModelManagementInitResult] = None

    async def __aenter__(self) -> ModelManagementSupervisorCoordinator:
        """Enter context and initialize model management."""
        self._result = await initialize_model_management_supervisor(self.config)
        return await get_model_management_supervisor()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit context and shutdown model management."""
        await shutdown_model_management_supervisor()
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
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

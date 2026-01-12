"""
Autonomy Coordinator for JARVIS Autonomous Action System.

This module provides the central coordinator that orchestrates all autonomy
components including action registry, permission system, safety validator,
advanced executor, and learning system.

Key Features:
    - Unified lifecycle management
    - Cross-component event routing
    - Health monitoring with auto-recovery
    - Integrated execution pipeline
    - Statistics aggregation
    - Configuration management

Environment Variables:
    JARVIS_AUTONOMY_ENABLED: Enable autonomy system (default: true)
    JARVIS_AUTONOMY_HEALTH_CHECK_INTERVAL: Health check interval (default: 60)
    JARVIS_AUTONOMY_AUTO_RESTART: Auto-restart failed components (default: true)
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from backend.core.async_safety import LazyAsyncLock
from .action_registry import (
    ActionMetadata,
    ActionRegistry,
    ActionType,
    get_action_registry,
    start_action_registry,
    stop_action_registry,
)
from .permission_system import (
    PermissionContext,
    PermissionDecision,
    PermissionSystem,
    get_permission_system,
    start_permission_system,
    stop_permission_system,
)
from .safety_validator import (
    SafetyCheckResult,
    SafetyValidator,
    get_safety_validator,
    start_safety_validator,
    stop_safety_validator,
)
from .advanced_executor import (
    AdvancedActionExecutor,
    ExecutionContext,
    ExecutionResult,
    get_advanced_executor,
    start_advanced_executor,
    stop_advanced_executor,
)
from .action_learning import (
    ActionLearningSystem,
    ActionOutcome,
    PredictionResult,
    get_action_learning_system,
    start_action_learning_system,
    stop_action_learning_system,
)

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================


class ComponentStatus(Enum):
    """Status of autonomy components."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass
class ComponentHealth:
    """Health status of a component."""

    name: str
    status: ComponentStatus
    last_check: datetime
    error: Optional[str] = None
    restart_count: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "last_check": self.last_check.isoformat(),
            "error": self.error,
            "restart_count": self.restart_count,
            "uptime_seconds": self.uptime_seconds,
        }


@dataclass
class AutonomyCoordinatorConfig:
    """Configuration for the autonomy coordinator."""

    enabled: bool = True
    health_check_interval_seconds: float = 60.0
    auto_restart_failed: bool = True
    max_restart_attempts: int = 3
    restart_cooldown_seconds: float = 30.0

    # Feature flags
    enable_learning: bool = True
    enable_safety_validation: bool = True
    enable_permission_checks: bool = True

    @classmethod
    def from_env(cls) -> "AutonomyCoordinatorConfig":
        """Create configuration from environment variables."""
        return cls(
            enabled=os.getenv("JARVIS_AUTONOMY_ENABLED", "true").lower() == "true",
            health_check_interval_seconds=float(os.getenv(
                "JARVIS_AUTONOMY_HEALTH_CHECK_INTERVAL", "60"
            )),
            auto_restart_failed=os.getenv(
                "JARVIS_AUTONOMY_AUTO_RESTART", "true"
            ).lower() == "true",
            max_restart_attempts=int(os.getenv(
                "JARVIS_AUTONOMY_MAX_RESTART", "3"
            )),
            enable_learning=os.getenv(
                "JARVIS_AUTONOMY_LEARNING", "true"
            ).lower() == "true",
            enable_safety_validation=os.getenv(
                "JARVIS_AUTONOMY_SAFETY", "true"
            ).lower() == "true",
            enable_permission_checks=os.getenv(
                "JARVIS_AUTONOMY_PERMISSIONS", "true"
            ).lower() == "true",
        )


# =============================================================================
# AUTONOMY COORDINATOR
# =============================================================================


class AutonomyCoordinator:
    """
    Central coordinator for the autonomy system.

    This class manages the lifecycle of all autonomy components and provides
    a unified interface for action execution with full safety and learning.
    """

    def __init__(self, config: Optional[AutonomyCoordinatorConfig] = None):
        """Initialize the coordinator."""
        self.config = config or AutonomyCoordinatorConfig.from_env()

        # Component references
        self._registry: Optional[ActionRegistry] = None
        self._permission_system: Optional[PermissionSystem] = None
        self._safety_validator: Optional[SafetyValidator] = None
        self._executor: Optional[AdvancedActionExecutor] = None
        self._learning_system: Optional[ActionLearningSystem] = None

        # Component health tracking
        self._component_health: Dict[str, ComponentHealth] = {}
        self._component_start_times: Dict[str, datetime] = {}

        # Event callbacks
        self._callbacks: Dict[str, List[weakref.ref]] = {
            "execution_complete": [],
            "execution_failed": [],
            "permission_denied": [],
            "safety_blocked": [],
            "component_status_changed": [],
        }

        # State
        self._is_running = False
        self._started_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the autonomy coordinator and all components."""
        if self._is_running:
            logger.warning("AutonomyCoordinator already running")
            return

        if not self.config.enabled:
            logger.info("Autonomy system disabled by configuration")
            return

        logger.info("Starting AutonomyCoordinator...")
        self._started_at = datetime.now()

        try:
            # Start components in dependency order
            await self._start_component("action_registry", self._start_registry)
            await self._start_component("permission_system", self._start_permissions)
            await self._start_component("safety_validator", self._start_safety)
            await self._start_component("executor", self._start_executor)

            if self.config.enable_learning:
                await self._start_component("learning_system", self._start_learning)

            self._is_running = True

            # Start health monitoring
            asyncio.create_task(self._health_monitor_loop())

            logger.info("AutonomyCoordinator started successfully")
            self._emit_event("component_status_changed", {
                "component": "coordinator",
                "status": ComponentStatus.RUNNING
            })

        except Exception as e:
            logger.error(f"Failed to start AutonomyCoordinator: {e}")
            await self._cleanup_failed_start()
            raise

    async def stop(self) -> None:
        """Stop the autonomy coordinator and all components."""
        if not self._is_running:
            return

        logger.info("Stopping AutonomyCoordinator...")
        self._is_running = False

        # Stop components in reverse order
        if self.config.enable_learning:
            await self._stop_component("learning_system", stop_action_learning_system)

        await self._stop_component("executor", stop_advanced_executor)
        await self._stop_component("safety_validator", stop_safety_validator)
        await self._stop_component("permission_system", stop_permission_system)
        await self._stop_component("action_registry", stop_action_registry)

        logger.info("AutonomyCoordinator stopped")

    @property
    def is_running(self) -> bool:
        """Check if coordinator is running."""
        return self._is_running

    @property
    def uptime_seconds(self) -> float:
        """Get coordinator uptime in seconds."""
        if self._started_at:
            return (datetime.now() - self._started_at).total_seconds()
        return 0.0

    # =========================================================================
    # Component Management
    # =========================================================================

    async def _start_component(
        self,
        name: str,
        start_fn: Callable[[], Any]
    ) -> None:
        """Start a component with health tracking."""
        health = ComponentHealth(
            name=name,
            status=ComponentStatus.STARTING,
            last_check=datetime.now()
        )
        self._component_health[name] = health

        try:
            await start_fn()
            self._component_start_times[name] = datetime.now()
            health.status = ComponentStatus.RUNNING
            logger.info(f"Started component: {name}")

        except Exception as e:
            health.status = ComponentStatus.FAILED
            health.error = str(e)
            logger.error(f"Failed to start {name}: {e}")
            raise

    async def _stop_component(
        self,
        name: str,
        stop_fn: Callable[[], Any]
    ) -> None:
        """Stop a component."""
        if name in self._component_health:
            self._component_health[name].status = ComponentStatus.STOPPING

        try:
            await stop_fn()
            if name in self._component_health:
                self._component_health[name].status = ComponentStatus.STOPPED
            logger.info(f"Stopped component: {name}")

        except Exception as e:
            logger.error(f"Error stopping {name}: {e}")

    async def _start_registry(self) -> None:
        """Start action registry."""
        self._registry = await start_action_registry()

    async def _start_permissions(self) -> None:
        """Start permission system."""
        self._permission_system = await start_permission_system()

    async def _start_safety(self) -> None:
        """Start safety validator."""
        self._safety_validator = await start_safety_validator()

    async def _start_executor(self) -> None:
        """Start advanced executor."""
        self._executor = await start_advanced_executor()

    async def _start_learning(self) -> None:
        """Start learning system."""
        self._learning_system = await start_action_learning_system()

    async def _cleanup_failed_start(self) -> None:
        """Cleanup after a failed start."""
        for name, health in self._component_health.items():
            if health.status == ComponentStatus.RUNNING:
                try:
                    if name == "action_registry":
                        await stop_action_registry()
                    elif name == "permission_system":
                        await stop_permission_system()
                    elif name == "safety_validator":
                        await stop_safety_validator()
                    elif name == "executor":
                        await stop_advanced_executor()
                    elif name == "learning_system":
                        await stop_action_learning_system()
                except Exception:
                    pass

    # =========================================================================
    # Action Execution
    # =========================================================================

    async def execute_action(
        self,
        action_type: ActionType,
        params: Dict[str, Any],
        dry_run: bool = False,
        force: bool = False,
        request_source: str = "unknown"
    ) -> ExecutionResult:
        """
        Execute an action through the full autonomy pipeline.

        This method provides a unified interface that:
        1. Validates safety constraints
        2. Checks permissions
        3. Predicts success (if learning enabled)
        4. Executes with retry logic
        5. Records outcome for learning

        Args:
            action_type: Type of action to execute
            params: Action parameters
            dry_run: Run in simulation mode
            force: Force execution (skip confirmations)
            request_source: Source of the request

        Returns:
            ExecutionResult with full outcome details
        """
        if not self._is_running:
            raise RuntimeError("AutonomyCoordinator is not running")

        if not self._executor:
            raise RuntimeError("Executor not initialized")

        # Get prediction if learning is enabled
        prediction: Optional[PredictionResult] = None
        if self._learning_system and self.config.enable_learning:
            prediction = await self._learning_system.predict_success(
                action_type=action_type,
                params=params,
                context=self._build_context()
            )

            # Log prediction
            if prediction.predicted_success_rate < 0.5:
                logger.warning(
                    f"Low success prediction for {action_type.name}: "
                    f"{prediction.predicted_success_rate:.1%}"
                )

        # Execute through the advanced executor
        result = await self._executor.execute(
            action_type=action_type,
            params=params,
            dry_run=dry_run,
            force=force,
            request_source=request_source,
            context_overrides=self._build_context()
        )

        # Record outcome for learning
        if self._learning_system and self.config.enable_learning:
            outcome = ActionOutcome(
                action_type=action_type,
                success=result.success,
                execution_time_ms=result.total_execution_time_ms,
                params=params,
                context=self._build_context(),
                error_type=result.error_type.value if result.error_type else None,
                retry_count=result.attempt_count - 1
            )
            await self._learning_system.record_outcome(outcome)

        # Emit events
        if result.success:
            self._emit_event("execution_complete", {
                "action_type": action_type.name,
                "result": result.to_dict()
            })
        else:
            self._emit_event("execution_failed", {
                "action_type": action_type.name,
                "result": result.to_dict()
            })

            if result.status.value == "permission_denied":
                self._emit_event("permission_denied", {
                    "action_type": action_type.name,
                    "decision": result.permission_decision.to_dict() if result.permission_decision else None
                })

            if result.status.value == "safety_blocked":
                self._emit_event("safety_blocked", {
                    "action_type": action_type.name,
                    "safety_result": result.safety_result.to_dict() if result.safety_result else None
                })

        return result

    def _build_context(self) -> Dict[str, Any]:
        """Build execution context from current state."""
        return {
            "screen_locked": False,  # Would integrate with actual screen state
            "focus_mode_active": False,  # Would integrate with focus tracker
            "meeting_in_progress": False,  # Would integrate with calendar
            "recent_failures": self._get_recent_failure_count(),
        }

    def _get_recent_failure_count(self) -> int:
        """Get count of recent execution failures."""
        if not self._executor:
            return 0

        recent = self._executor.get_recent_executions(limit=10)
        return sum(1 for r in recent if not r.success)

    # =========================================================================
    # Event System
    # =========================================================================

    def add_callback(
        self,
        event: str,
        callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Add a callback for an event."""
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(weakref.ref(callback))

    def remove_callback(
        self,
        event: str,
        callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Remove a callback."""
        if event in self._callbacks:
            self._callbacks[event] = [
                ref for ref in self._callbacks[event]
                if ref() is not None and ref() != callback
            ]

    def _emit_event(self, event: str, data: Dict[str, Any]) -> None:
        """Emit an event to callbacks."""
        if event not in self._callbacks:
            return

        # Clean up dead references and call live ones
        live_refs = []
        for ref in self._callbacks[event]:
            callback = ref()
            if callback is not None:
                try:
                    callback(data)
                except Exception as e:
                    logger.error(f"Callback error for {event}: {e}")
                live_refs.append(ref)

        self._callbacks[event] = live_refs

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_monitor_loop(self) -> None:
        """Background task for health monitoring."""
        while self._is_running:
            try:
                await asyncio.sleep(self.config.health_check_interval_seconds)
                await self._check_component_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def _check_component_health(self) -> None:
        """Check health of all components."""
        now = datetime.now()

        components = [
            ("action_registry", self._registry),
            ("permission_system", self._permission_system),
            ("safety_validator", self._safety_validator),
            ("executor", self._executor),
        ]

        if self.config.enable_learning:
            components.append(("learning_system", self._learning_system))

        for name, component in components:
            if name not in self._component_health:
                continue

            health = self._component_health[name]
            health.last_check = now

            # Update uptime
            if name in self._component_start_times:
                health.uptime_seconds = (now - self._component_start_times[name]).total_seconds()

            # Check if component is healthy
            is_healthy = component is not None and getattr(component, 'is_running', False)

            if is_healthy:
                if health.status != ComponentStatus.RUNNING:
                    health.status = ComponentStatus.RUNNING
                    health.error = None
                    self._emit_event("component_status_changed", {
                        "component": name,
                        "status": ComponentStatus.RUNNING
                    })
            else:
                if health.status == ComponentStatus.RUNNING:
                    health.status = ComponentStatus.FAILED
                    health.error = "Component not running"
                    logger.error(f"Component {name} is not healthy")

                    self._emit_event("component_status_changed", {
                        "component": name,
                        "status": ComponentStatus.FAILED
                    })

                    # Attempt restart if configured
                    if self.config.auto_restart_failed:
                        await self._attempt_restart(name)

    async def _attempt_restart(self, component_name: str) -> None:
        """Attempt to restart a failed component."""
        health = self._component_health.get(component_name)
        if not health:
            return

        if health.restart_count >= self.config.max_restart_attempts:
            logger.error(f"Max restart attempts reached for {component_name}")
            return

        logger.info(f"Attempting restart of {component_name}...")
        health.restart_count += 1

        try:
            if component_name == "action_registry":
                await self._start_registry()
            elif component_name == "permission_system":
                await self._start_permissions()
            elif component_name == "safety_validator":
                await self._start_safety()
            elif component_name == "executor":
                await self._start_executor()
            elif component_name == "learning_system":
                await self._start_learning()

            health.status = ComponentStatus.RUNNING
            health.error = None
            self._component_start_times[component_name] = datetime.now()
            logger.info(f"Successfully restarted {component_name}")

        except Exception as e:
            health.status = ComponentStatus.FAILED
            health.error = str(e)
            logger.error(f"Failed to restart {component_name}: {e}")

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of all components."""
        return {
            "coordinator": {
                "running": self._is_running,
                "uptime_seconds": self.uptime_seconds,
            },
            "components": {
                name: health.to_dict()
                for name, health in self._component_health.items()
            }
        }

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> Dict[str, Any]:
        """Get aggregated statistics from all components."""
        stats = {
            "coordinator": {
                "running": self._is_running,
                "uptime_seconds": self.uptime_seconds,
                "config": {
                    "enabled": self.config.enabled,
                    "learning_enabled": self.config.enable_learning,
                    "safety_enabled": self.config.enable_safety_validation,
                    "permissions_enabled": self.config.enable_permission_checks,
                }
            },
            "health": self.get_health_status(),
        }

        # Add component-specific stats
        if self._registry:
            stats["registry"] = self._registry.get_statistics()

        if self._permission_system:
            stats["permissions"] = self._permission_system.get_statistics()

        if self._safety_validator:
            stats["safety"] = self._safety_validator.get_statistics()

        if self._executor:
            stats["executor"] = self._executor.get_statistics()

        if self._learning_system:
            stats["learning"] = self._learning_system.get_statistics()

        return stats

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_registry(self) -> Optional[ActionRegistry]:
        """Get the action registry."""
        return self._registry

    def get_permission_system(self) -> Optional[PermissionSystem]:
        """Get the permission system."""
        return self._permission_system

    def get_safety_validator(self) -> Optional[SafetyValidator]:
        """Get the safety validator."""
        return self._safety_validator

    def get_executor(self) -> Optional[AdvancedActionExecutor]:
        """Get the advanced executor."""
        return self._executor

    def get_learning_system(self) -> Optional[ActionLearningSystem]:
        """Get the learning system."""
        return self._learning_system

    async def rollback_last_action(self) -> tuple[bool, str]:
        """Rollback the last executed action."""
        if not self._executor:
            return False, "Executor not available"
        return await self._executor.rollback_last()


# =============================================================================
# SINGLETON MANAGEMENT
# =============================================================================


_coordinator_instance: Optional[AutonomyCoordinator] = None
_coordinator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_autonomy_coordinator() -> AutonomyCoordinator:
    """Get the global autonomy coordinator instance."""
    global _coordinator_instance
    if _coordinator_instance is None:
        _coordinator_instance = AutonomyCoordinator()
    return _coordinator_instance


async def start_autonomy_coordinator() -> AutonomyCoordinator:
    """Start the global autonomy coordinator."""
    async with _coordinator_lock:
        coordinator = get_autonomy_coordinator()
        if not coordinator.is_running:
            await coordinator.start()
        return coordinator


async def stop_autonomy_coordinator() -> None:
    """Stop the global autonomy coordinator."""
    async with _coordinator_lock:
        global _coordinator_instance
        if _coordinator_instance and _coordinator_instance.is_running:
            await _coordinator_instance.stop()

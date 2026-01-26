"""
Readiness State Manager - Multi-Phase Initialization Tracking
=============================================================

Fixes the ROOT CAUSE of health check false positives by:
1. Tracking initialization through discrete phases
2. Distinguishing between liveness (process alive) and readiness (fully initialized)
3. Publishing component-level initialization status
4. Supporting startup probes, liveness probes, and readiness probes
5. Broadcasting state changes for monitoring/observability

Architecture:
    +-------------------------------------------------------------------+
    |  ReadinessStateManager                                            |
    |  +-- InitializationPhase (STARTING -> INITIALIZING -> READY)      |
    |  +-- ComponentReadiness (per-component initialization tracking)   |
    |  +-- PhaseTransitionValidator (validate phase transitions)        |
    |  +-- StatePublisher (broadcast state changes via IPC/files)       |
    |  +-- HealthProbeResponder (respond appropriately to probe types)  |
    +-------------------------------------------------------------------+

Key Concepts:
- LIVENESS: Is the process running? (HTTP 200 from /health/live)
- READINESS: Is the service ready to accept traffic? (HTTP 200 from /health/ready)
- STARTUP: Has the service completed startup? (HTTP 200 from /health/startup)

Health Endpoint Status Codes:
- /health/live: 200 (process running) or 503 (process not running)
- /health/ready: 200 (ready) or 503 (not ready)
- /health/startup: 200 (startup complete) or 503 (still starting)

Author: JARVIS Trinity v94.0 - Readiness State Management
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class InitializationPhase(enum.Enum):
    """
    Phases of component initialization.

    State Machine:
        NOT_STARTED -> STARTING -> INITIALIZING -> READY
                                       |             |
                                       v             v
                                   DEGRADED <---> HEALTHY
                                       |
                                       v
                                   SHUTTING_DOWN -> STOPPED

    Key distinctions:
    - STARTING: Process spawned, basic setup
    - INITIALIZING: Loading models, connecting to services
    - READY: All critical components loaded, ready for traffic
    - HEALTHY: Fully operational
    - DEGRADED: Operational but with reduced functionality
    - SHUTTING_DOWN: Graceful shutdown in progress
    - STOPPED: Process terminating
    """
    NOT_STARTED = "not_started"
    STARTING = "starting"
    INITIALIZING = "initializing"
    READY = "ready"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    ERROR = "error"


class ProbeType(enum.Enum):
    """Types of health probes."""
    LIVENESS = "liveness"      # Is the process alive?
    READINESS = "readiness"    # Is the service ready for traffic?
    STARTUP = "startup"        # Has startup completed?


class ComponentCategory(enum.Enum):
    """Categories of components for initialization ordering."""
    CRITICAL = "critical"        # Must succeed for startup
    IMPORTANT = "important"      # Failures cause degradation
    OPTIONAL = "optional"        # Can fail without affecting status


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ComponentReadiness:
    """Readiness state for a single component."""
    name: str
    category: ComponentCategory = ComponentCategory.IMPORTANT
    phase: InitializationPhase = InitializationPhase.NOT_STARTED
    progress_percent: float = 0.0
    started_at: Optional[float] = None
    ready_at: Optional[float] = None
    error: Optional[str] = None
    last_health_check: Optional[float] = None
    consecutive_failures: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        """Check if component is ready for traffic."""
        return self.phase in (
            InitializationPhase.READY,
            InitializationPhase.HEALTHY,
            InitializationPhase.DEGRADED,
        )

    @property
    def is_healthy(self) -> bool:
        """Check if component is fully healthy."""
        return self.phase == InitializationPhase.HEALTHY

    @property
    def is_alive(self) -> bool:
        """Check if component process is alive (liveness)."""
        return self.phase not in (
            InitializationPhase.NOT_STARTED,
            InitializationPhase.STOPPED,
            InitializationPhase.ERROR,
        )

    @property
    def initialization_time_ms(self) -> Optional[float]:
        """Time from started to ready in milliseconds."""
        if self.started_at and self.ready_at:
            return (self.ready_at - self.started_at) * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "category": self.category.value,
            "phase": self.phase.value,
            "progress_percent": round(self.progress_percent, 1),
            "is_ready": self.is_ready,
            "is_healthy": self.is_healthy,
            "is_alive": self.is_alive,
            "initialization_time_ms": self.initialization_time_ms,
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class SystemReadinessState:
    """
    Overall system readiness state.

    Aggregates readiness from all components.
    """
    phase: InitializationPhase = InitializationPhase.NOT_STARTED
    components: Dict[str, ComponentReadiness] = field(default_factory=dict)
    started_at: Optional[float] = None
    ready_at: Optional[float] = None
    last_update: float = field(default_factory=time.time)
    version: str = "1.0.0"

    @property
    def is_live(self) -> bool:
        """Check liveness - is the system process running?"""
        return self.phase not in (
            InitializationPhase.NOT_STARTED,
            InitializationPhase.STOPPED,
        )

    @property
    def is_ready(self) -> bool:
        """Check readiness - is the system ready for traffic?"""
        if self.phase not in (
            InitializationPhase.READY,
            InitializationPhase.HEALTHY,
            InitializationPhase.DEGRADED,
        ):
            return False

        # Check all critical components are ready
        for comp in self.components.values():
            if comp.category == ComponentCategory.CRITICAL and not comp.is_ready:
                return False

        return True

    @property
    def is_startup_complete(self) -> bool:
        """Check if initial startup is complete."""
        return self.ready_at is not None

    @property
    def is_healthy(self) -> bool:
        """Check if system is fully healthy."""
        if self.phase != InitializationPhase.HEALTHY:
            return False

        # All critical and important components must be healthy
        for comp in self.components.values():
            if comp.category in (ComponentCategory.CRITICAL, ComponentCategory.IMPORTANT):
                if not comp.is_healthy:
                    return False

        return True

    @property
    def ready_component_count(self) -> int:
        """Count of ready components."""
        return sum(1 for c in self.components.values() if c.is_ready)

    @property
    def total_component_count(self) -> int:
        """Total component count."""
        return len(self.components)

    @property
    def overall_progress_percent(self) -> float:
        """Overall initialization progress."""
        if not self.components:
            return 0.0
        return sum(c.progress_percent for c in self.components.values()) / len(self.components)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "phase": self.phase.value,
            "is_live": self.is_live,
            "is_ready": self.is_ready,
            "is_startup_complete": self.is_startup_complete,
            "is_healthy": self.is_healthy,
            "ready_components": self.ready_component_count,
            "total_components": self.total_component_count,
            "overall_progress_percent": round(self.overall_progress_percent, 1),
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "last_update": self.last_update,
            "version": self.version,
            "components": {
                name: comp.to_dict()
                for name, comp in self.components.items()
            },
        }


@dataclass
class ProbeResponse:
    """Response for a health probe."""
    probe_type: ProbeType
    success: bool
    status_code: int  # HTTP status code
    phase: InitializationPhase
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for HTTP response."""
        return {
            "status": "ok" if self.success else "not_ready",
            "ready": self.success,
            "phase": self.phase.value,
            "message": self.message,
            **self.details,
        }


# =============================================================================
# Readiness State Manager
# =============================================================================

class ReadinessStateManager:
    """
    Manages initialization and readiness state for JARVIS components.

    This class provides:
    1. Phase transition tracking with validation
    2. Component-level initialization progress
    3. Health probe responses (liveness/readiness/startup)
    4. State broadcasting via files and callbacks
    5. Automatic state aggregation

    Usage:
        manager = ReadinessStateManager(component_name="jarvis-prime")

        # During initialization
        await manager.transition_to(InitializationPhase.STARTING)
        await manager.register_component("llm_model", ComponentCategory.CRITICAL)
        await manager.update_component_progress("llm_model", 50.0)
        await manager.mark_component_ready("llm_model")
        await manager.transition_to(InitializationPhase.READY)

        # For health checks
        response = manager.handle_probe(ProbeType.READINESS)
    """

    # Valid phase transitions
    VALID_TRANSITIONS: Dict[InitializationPhase, Set[InitializationPhase]] = {
        InitializationPhase.NOT_STARTED: {InitializationPhase.STARTING},
        InitializationPhase.STARTING: {InitializationPhase.INITIALIZING, InitializationPhase.ERROR},
        InitializationPhase.INITIALIZING: {
            InitializationPhase.READY,
            InitializationPhase.ERROR,
            InitializationPhase.SHUTTING_DOWN,
        },
        InitializationPhase.READY: {
            InitializationPhase.HEALTHY,
            InitializationPhase.DEGRADED,
            InitializationPhase.SHUTTING_DOWN,
        },
        InitializationPhase.HEALTHY: {
            InitializationPhase.DEGRADED,
            InitializationPhase.SHUTTING_DOWN,
        },
        InitializationPhase.DEGRADED: {
            InitializationPhase.HEALTHY,
            InitializationPhase.SHUTTING_DOWN,
        },
        InitializationPhase.SHUTTING_DOWN: {InitializationPhase.STOPPED},
        InitializationPhase.STOPPED: set(),
        InitializationPhase.ERROR: {InitializationPhase.STARTING, InitializationPhase.STOPPED},
    }

    def __init__(
        self,
        component_name: str,
        state_dir: Optional[Path] = None,
        publish_to_file: bool = True,
        startup_timeout_seconds: float = 300.0,
    ):
        self.component_name = component_name
        self.state_dir = state_dir or Path.home() / ".jarvis" / "trinity" / "readiness"
        self.publish_to_file = publish_to_file
        self.startup_timeout = startup_timeout_seconds

        self.state = SystemReadinessState()
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_phase_change: List[Callable[[InitializationPhase, InitializationPhase], None]] = []
        self._on_ready: List[Callable[[], None]] = []

        # Ensure state directory
        if self.publish_to_file:
            self.state_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"[ReadinessStateManager] Initialized for {component_name}")

    # =========================================================================
    # Phase Transitions
    # =========================================================================

    async def transition_to(
        self,
        new_phase: InitializationPhase,
        reason: Optional[str] = None,
    ) -> bool:
        """
        Transition to a new initialization phase.

        Args:
            new_phase: The target phase
            reason: Optional reason for the transition

        Returns:
            True if transition succeeded, False if invalid
        """
        async with self._lock:
            old_phase = self.state.phase

            # Validate transition
            if new_phase not in self.VALID_TRANSITIONS.get(old_phase, set()):
                logger.warning(
                    f"[ReadinessStateManager] Invalid transition: "
                    f"{old_phase.value} -> {new_phase.value}"
                )
                return False

            # Update state
            self.state.phase = new_phase
            self.state.last_update = time.time()

            # Track timing milestones
            if new_phase == InitializationPhase.STARTING and self.state.started_at is None:
                self.state.started_at = time.time()

            if new_phase == InitializationPhase.READY and self.state.ready_at is None:
                self.state.ready_at = time.time()

            logger.info(
                f"[ReadinessStateManager] {self.component_name} phase transition: "
                f"{old_phase.value} -> {new_phase.value}"
                f"{f' ({reason})' if reason else ''}"
            )

            # Publish state
            await self._publish_state()

            # Fire callbacks
            for callback in self._on_phase_change:
                try:
                    callback(old_phase, new_phase)
                except Exception as e:
                    logger.error(f"[ReadinessStateManager] Callback error: {e}")

            # Fire ready callbacks
            if new_phase in (InitializationPhase.READY, InitializationPhase.HEALTHY):
                for callback in self._on_ready:
                    try:
                        callback()
                    except Exception as e:
                        logger.error(f"[ReadinessStateManager] Ready callback error: {e}")

            return True

    async def start(self) -> None:
        """Mark component as starting."""
        await self.transition_to(InitializationPhase.STARTING, "startup initiated")

    async def mark_initializing(self) -> None:
        """Mark component as initializing."""
        await self.transition_to(InitializationPhase.INITIALIZING, "loading components")

    async def mark_ready(self) -> None:
        """Mark component as ready for traffic."""
        await self.transition_to(InitializationPhase.READY, "initialization complete")

    async def mark_healthy(self) -> None:
        """Mark component as fully healthy."""
        await self.transition_to(InitializationPhase.HEALTHY, "all systems operational")

    async def mark_degraded(self, reason: str) -> None:
        """Mark component as degraded."""
        await self.transition_to(InitializationPhase.DEGRADED, reason)

    async def start_shutdown(self) -> None:
        """Mark component as shutting down."""
        await self.transition_to(InitializationPhase.SHUTTING_DOWN, "graceful shutdown")

    async def mark_stopped(self) -> None:
        """Mark component as stopped."""
        await self.transition_to(InitializationPhase.STOPPED, "shutdown complete")

    async def mark_error(self, error: str) -> None:
        """Mark component as errored."""
        await self.transition_to(InitializationPhase.ERROR, error)

    # =========================================================================
    # Component Management
    # =========================================================================

    async def register_component(
        self,
        name: str,
        category: ComponentCategory = ComponentCategory.IMPORTANT,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a component for tracking."""
        async with self._lock:
            self.state.components[name] = ComponentReadiness(
                name=name,
                category=category,
                phase=InitializationPhase.NOT_STARTED,
                metadata=metadata or {},
            )
            await self._publish_state()

        logger.debug(f"[ReadinessStateManager] Registered component: {name} ({category.value})")

    async def update_component_progress(
        self,
        name: str,
        progress_percent: float,
        phase: Optional[InitializationPhase] = None,
    ) -> None:
        """Update component initialization progress."""
        async with self._lock:
            if name not in self.state.components:
                logger.warning(f"[ReadinessStateManager] Unknown component: {name}")
                return

            comp = self.state.components[name]
            comp.progress_percent = min(max(progress_percent, 0.0), 100.0)

            if phase:
                comp.phase = phase

            # Auto-transition to INITIALIZING on first progress update
            if comp.phase == InitializationPhase.NOT_STARTED and progress_percent > 0:
                comp.phase = InitializationPhase.STARTING
                comp.started_at = time.time()

            await self._publish_state()

    async def mark_component_ready(
        self,
        name: str,
        healthy: bool = True,
    ) -> None:
        """Mark a component as ready."""
        async with self._lock:
            if name not in self.state.components:
                logger.warning(f"[ReadinessStateManager] Unknown component: {name}")
                return

            comp = self.state.components[name]
            comp.phase = InitializationPhase.HEALTHY if healthy else InitializationPhase.READY
            comp.progress_percent = 100.0
            comp.ready_at = time.time()
            comp.consecutive_failures = 0

            logger.info(
                f"[ReadinessStateManager] Component ready: {name} "
                f"(init_time={comp.initialization_time_ms or 0:.0f}ms)"
            )

            # Check if all critical components are ready -> system ready
            await self._check_system_ready()

            await self._publish_state()

    async def mark_component_failed(
        self,
        name: str,
        error: str,
    ) -> None:
        """Mark a component as failed."""
        async with self._lock:
            if name not in self.state.components:
                return

            comp = self.state.components[name]
            comp.phase = InitializationPhase.ERROR
            comp.error = error
            comp.consecutive_failures += 1

            logger.error(f"[ReadinessStateManager] Component failed: {name} - {error}")

            # If critical component failed, mark system degraded/error
            if comp.category == ComponentCategory.CRITICAL:
                self.state.phase = InitializationPhase.ERROR

            await self._publish_state()

    async def _check_system_ready(self) -> None:
        """Check if system should transition to ready."""
        if self.state.phase in (InitializationPhase.READY, InitializationPhase.HEALTHY):
            return

        if self.state.phase != InitializationPhase.INITIALIZING:
            return

        # Check all critical components
        critical_ready = all(
            comp.is_ready for comp in self.state.components.values()
            if comp.category == ComponentCategory.CRITICAL
        )

        if critical_ready:
            # Transition to READY
            self.state.phase = InitializationPhase.READY
            self.state.ready_at = time.time()

            # Check if all important are also ready -> HEALTHY
            important_ready = all(
                comp.is_ready for comp in self.state.components.values()
                if comp.category == ComponentCategory.IMPORTANT
            )

            if important_ready:
                self.state.phase = InitializationPhase.HEALTHY

            logger.info(
                f"[ReadinessStateManager] System ready (phase={self.state.phase.value})"
            )

    # =========================================================================
    # Health Probes
    # =========================================================================

    def handle_probe(self, probe_type: ProbeType) -> ProbeResponse:
        """
        Handle a health probe request.

        Returns appropriate response based on probe type and current state.
        """
        if probe_type == ProbeType.LIVENESS:
            return self._handle_liveness_probe()
        elif probe_type == ProbeType.READINESS:
            return self._handle_readiness_probe()
        elif probe_type == ProbeType.STARTUP:
            return self._handle_startup_probe()
        else:
            return ProbeResponse(
                probe_type=probe_type,
                success=False,
                status_code=400,
                phase=self.state.phase,
                message="Unknown probe type",
            )

    def _handle_liveness_probe(self) -> ProbeResponse:
        """
        Handle liveness probe.

        Returns 200 if process is alive, 503 otherwise.
        Liveness checks should ALWAYS pass unless process is truly dead.
        """
        is_alive = self.state.is_live

        return ProbeResponse(
            probe_type=ProbeType.LIVENESS,
            success=is_alive,
            status_code=200 if is_alive else 503,
            phase=self.state.phase,
            message="Process alive" if is_alive else "Process not running",
            details={
                "uptime_seconds": time.time() - self.state.started_at if self.state.started_at else 0,
            },
        )

    def _handle_readiness_probe(self) -> ProbeResponse:
        """
        Handle readiness probe.

        Returns 200 only when system is ready to accept traffic.
        This is the KEY fix for health check false positives.
        """
        is_ready = self.state.is_ready

        if not is_ready:
            # Provide detailed reason why not ready
            if self.state.phase == InitializationPhase.STARTING:
                message = "Service starting up"
            elif self.state.phase == InitializationPhase.INITIALIZING:
                progress = self.state.overall_progress_percent
                message = f"Initializing ({progress:.0f}% complete)"
            elif self.state.phase == InitializationPhase.ERROR:
                message = "Service in error state"
            elif self.state.phase == InitializationPhase.SHUTTING_DOWN:
                message = "Service shutting down"
            else:
                message = f"Not ready (phase={self.state.phase.value})"
        else:
            message = "Ready to accept traffic"

        return ProbeResponse(
            probe_type=ProbeType.READINESS,
            success=is_ready,
            status_code=200 if is_ready else 503,
            phase=self.state.phase,
            message=message,
            details={
                "ready_components": self.state.ready_component_count,
                "total_components": self.state.total_component_count,
                "progress_percent": round(self.state.overall_progress_percent, 1),
            },
        )

    def _handle_startup_probe(self) -> ProbeResponse:
        """
        Handle startup probe.

        Returns 200 once initial startup is complete.
        Unlike readiness, this doesn't revert to 503 after becoming ready.
        """
        startup_complete = self.state.is_startup_complete

        # Check for startup timeout
        if not startup_complete and self.state.started_at:
            elapsed = time.time() - self.state.started_at
            if elapsed > self.startup_timeout:
                return ProbeResponse(
                    probe_type=ProbeType.STARTUP,
                    success=False,
                    status_code=503,
                    phase=self.state.phase,
                    message=f"Startup timeout ({elapsed:.0f}s > {self.startup_timeout:.0f}s)",
                )

        if startup_complete:
            message = "Startup complete"
        else:
            progress = self.state.overall_progress_percent
            message = f"Starting up ({progress:.0f}% complete)"

        return ProbeResponse(
            probe_type=ProbeType.STARTUP,
            success=startup_complete,
            status_code=200 if startup_complete else 503,
            phase=self.state.phase,
            message=message,
            details={
                "startup_time_ms": (
                    (self.state.ready_at - self.state.started_at) * 1000
                    if self.state.ready_at and self.state.started_at
                    else None
                ),
            },
        )

    # =========================================================================
    # Callbacks
    # =========================================================================

    def on_phase_change(
        self,
        callback: Callable[[InitializationPhase, InitializationPhase], None],
    ) -> None:
        """Register callback for phase changes."""
        self._on_phase_change.append(callback)

    def on_ready(self, callback: Callable[[], None]) -> None:
        """Register callback for when system becomes ready."""
        self._on_ready.append(callback)

    # =========================================================================
    # State Publishing
    # =========================================================================

    async def _publish_state(self) -> None:
        """Publish current state to file for cross-process visibility."""
        if not self.publish_to_file:
            return

        try:
            state_file = self.state_dir / f"{self.component_name}.json"

            # Atomic write
            import tempfile
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.state_dir),
                prefix=f".{self.component_name}.",
                suffix=".tmp",
            )

            state_dict = self.state.to_dict()
            state_dict["component_name"] = self.component_name
            state_dict["timestamp"] = time.time()

            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(state_dict, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, state_file)

        except Exception as e:
            logger.debug(f"[ReadinessStateManager] Failed to publish state: {e}")

    async def read_component_state(
        self,
        component_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Read another component's state from file."""
        try:
            state_file = self.state_dir / f"{component_name}.json"
            if not state_file.exists():
                return None

            with open(state_file) as f:
                return json.load(f)

        except Exception as e:
            logger.debug(f"[ReadinessStateManager] Failed to read {component_name} state: {e}")
            return None

    # =========================================================================
    # Status Accessors
    # =========================================================================

    @property
    def current_phase(self) -> InitializationPhase:
        """Get current initialization phase."""
        return self.state.phase

    @property
    def is_ready(self) -> bool:
        """Check if system is ready for traffic."""
        return self.state.is_ready

    @property
    def is_healthy(self) -> bool:
        """Check if system is fully healthy."""
        return self.state.is_healthy

    def get_status(self) -> Dict[str, Any]:
        """Get complete status dictionary."""
        return self.state.to_dict()


# =============================================================================
# Singleton Access
# =============================================================================

_manager_instances: Dict[str, ReadinessStateManager] = {}


def get_readiness_manager(
    component_name: str,
    **kwargs,
) -> ReadinessStateManager:
    """Get or create a readiness manager for a component."""
    if component_name not in _manager_instances:
        _manager_instances[component_name] = ReadinessStateManager(
            component_name=component_name,
            **kwargs,
        )

    return _manager_instances[component_name]


async def get_readiness_manager_async(
    component_name: str,
    **kwargs,
) -> ReadinessStateManager:
    """Async version of get_readiness_manager."""
    return get_readiness_manager(component_name, **kwargs)


def get_all_readiness_managers() -> Dict[str, ReadinessStateManager]:
    """Get all active readiness managers."""
    return _manager_instances.copy()

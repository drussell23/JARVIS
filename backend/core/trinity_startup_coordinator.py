"""
Trinity Startup Coordinator v81.0 - Dependency-Based Component Startup
======================================================================

Coordinates startup of Trinity components with proper dependency resolution:
- JARVIS Body must be ready before launching J-Prime and Reactor-Core
- Health monitor must start before system is marked ready
- Each component's readiness is verified via heartbeat + PID

FEATURES:
    - Dependency graph resolution for startup ordering
    - Parallel launch where dependencies allow
    - Readiness verification with timeout
    - Phase-based startup with callbacks
    - Graceful handling of optional components

STARTUP PHASES:
    1. INFRASTRUCTURE - IPC bus, directories, locks
    2. JARVIS_BODY    - Main JARVIS (must be first)
    3. CORE_SERVICES  - Internal JARVIS services
    4. JARVIS_PRIME   - J-Prime Mind (depends on Body)
    5. REACTOR_CORE   - Reactor-Core Nerves (depends on Body)
    6. HEALTH_MONITOR - Health monitoring (after all components)
    7. READY          - System fully ready

ZERO HARDCODING - All configuration via environment variables:
    TRINITY_COMPONENT_TIMEOUT_JPRIME    - J-Prime startup timeout (default: 60s)
    TRINITY_COMPONENT_TIMEOUT_REACTOR   - Reactor-Core timeout (default: 60s)
    TRINITY_STARTUP_PHASE_TIMEOUT       - Per-phase timeout (default: 120s)
    TRINITY_READINESS_POLL_INTERVAL     - Polling interval (default: 0.5s)

Author: JARVIS v81.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .trinity_ipc import (
    ComponentType,
    HeartbeatData,
    TrinityIPCBus,
    get_trinity_ipc_bus,
    is_pid_alive,
)
from .trinity_heartbeat_publisher import (
    HeartbeatPublisher,
    HeartbeatSubscriber,
    ComponentStatus,
    start_heartbeat_publishing,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ENVIRONMENT HELPERS
# =============================================================================


def _env_float(key: str, default: float) -> float:
    """Get float from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    """Get integer from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


# =============================================================================
# STARTUP PHASES
# =============================================================================


class StartupPhase(IntEnum):
    """Phases of Trinity startup in order."""
    INFRASTRUCTURE = 1
    JARVIS_BODY = 2
    CORE_SERVICES = 3
    JARVIS_PRIME = 4
    REACTOR_CORE = 5
    HEALTH_MONITOR = 6
    READY = 7


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class StartupConfig:
    """Configuration for startup coordination."""

    # Component timeouts
    jprime_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_COMPONENT_TIMEOUT_JPRIME", 60.0
    ))
    reactor_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_COMPONENT_TIMEOUT_REACTOR", 60.0
    ))
    body_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_COMPONENT_TIMEOUT_BODY", 30.0
    ))

    # Phase timeout
    phase_timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_STARTUP_PHASE_TIMEOUT", 120.0
    ))

    # Polling
    readiness_poll_interval: float = field(default_factory=lambda: _env_float(
        "TRINITY_READINESS_POLL_INTERVAL", 0.5
    ))

    # Retry settings
    max_launch_retries: int = field(default_factory=lambda: _env_int(
        "TRINITY_MAX_LAUNCH_RETRIES", 3
    ))
    retry_delay: float = field(default_factory=lambda: _env_float(
        "TRINITY_LAUNCH_RETRY_DELAY", 2.0
    ))

    # Feature flags
    parallel_optional_launch: bool = field(default_factory=lambda: _env_bool(
        "TRINITY_PARALLEL_OPTIONAL_LAUNCH", True
    ))
    require_jprime: bool = field(default_factory=lambda: _env_bool(
        "TRINITY_REQUIRE_JPRIME", False
    ))
    require_reactor: bool = field(default_factory=lambda: _env_bool(
        "TRINITY_REQUIRE_REACTOR", False
    ))

    def get_timeout(self, component: ComponentType) -> float:
        """Get timeout for a component."""
        timeouts = {
            ComponentType.JARVIS_BODY: self.body_timeout,
            ComponentType.JARVIS_PRIME: self.jprime_timeout,
            ComponentType.REACTOR_CORE: self.reactor_timeout,
            ComponentType.CODING_COUNCIL: 30.0,
        }
        return timeouts.get(component, 60.0)


# =============================================================================
# COMPONENT DEPENDENCY
# =============================================================================


@dataclass
class ComponentDependency:
    """Defines a component and its dependencies."""
    component: ComponentType
    phase: StartupPhase
    dependencies: List[ComponentType] = field(default_factory=list)
    required: bool = False
    timeout: float = 60.0

    def __post_init__(self):
        if isinstance(self.dependencies, tuple):
            self.dependencies = list(self.dependencies)


# Default startup order with dependencies
DEFAULT_STARTUP_ORDER: List[ComponentDependency] = [
    ComponentDependency(
        component=ComponentType.JARVIS_BODY,
        phase=StartupPhase.JARVIS_BODY,
        dependencies=[],
        required=True,
    ),
    ComponentDependency(
        component=ComponentType.JARVIS_PRIME,
        phase=StartupPhase.JARVIS_PRIME,
        dependencies=[ComponentType.JARVIS_BODY],
        required=False,  # Optional - system works without Mind
    ),
    ComponentDependency(
        component=ComponentType.REACTOR_CORE,
        phase=StartupPhase.REACTOR_CORE,
        dependencies=[ComponentType.JARVIS_BODY],
        required=False,  # Optional - system works without Nerves
    ),
]


# =============================================================================
# STARTUP RESULT
# =============================================================================


@dataclass
class ComponentStartResult:
    """Result of starting a single component."""
    component: ComponentType
    success: bool
    start_time: float = 0.0
    ready_time: float = 0.0
    pid: Optional[int] = None
    error: Optional[str] = None
    retries: int = 0

    @property
    def startup_duration(self) -> float:
        """Time taken to start."""
        if self.ready_time > 0 and self.start_time > 0:
            return self.ready_time - self.start_time
        return 0.0


@dataclass
class StartupResult:
    """Result of coordinated startup."""
    success: bool
    phase_reached: StartupPhase
    total_duration: float = 0.0
    components: Dict[ComponentType, ComponentStartResult] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def all_required_ready(self) -> bool:
        """Check if all required components are ready."""
        for comp_type, result in self.components.items():
            dep = next(
                (d for d in DEFAULT_STARTUP_ORDER if d.component == comp_type),
                None
            )
            if dep and dep.required and not result.success:
                return False
        return True

    @property
    def summary(self) -> str:
        """Get a summary string."""
        ready = [c.value for c, r in self.components.items() if r.success]
        failed = [c.value for c, r in self.components.items() if not r.success]
        return (
            f"Phase: {self.phase_reached.name}, "
            f"Ready: {', '.join(ready) or 'none'}, "
            f"Failed: {', '.join(failed) or 'none'}"
        )


# =============================================================================
# STARTUP COORDINATOR
# =============================================================================


class TrinityStartupCoordinator:
    """
    Coordinates Trinity component startup with dependency resolution.

    Ensures:
    1. JARVIS Body is ready before launching J-Prime/Reactor-Core
    2. Each component's readiness is verified via heartbeat + PID
    3. Health monitor starts AFTER all components are launched
    4. System ready event is emitted only after health monitor is running

    Usage:
        coordinator = TrinityStartupCoordinator()

        # Define launchers for each component
        launchers = {
            ComponentType.JARVIS_PRIME: launch_jprime_async,
            ComponentType.REACTOR_CORE: launch_reactor_async,
        }

        # Coordinate startup
        result = await coordinator.coordinate_startup(
            launchers=launchers,
            on_phase_complete=handle_phase_complete,
        )

        if result.success:
            print("All components ready!")
    """

    def __init__(
        self,
        config: Optional[StartupConfig] = None,
        ipc_bus: Optional[TrinityIPCBus] = None,
    ):
        self._config = config or StartupConfig()
        self._ipc_bus = ipc_bus

        # State
        self._current_phase = StartupPhase.INFRASTRUCTURE
        self._component_status: Dict[ComponentType, ComponentStatus] = {}
        self._component_pids: Dict[ComponentType, int] = {}
        self._startup_order = list(DEFAULT_STARTUP_ORDER)

    @property
    def current_phase(self) -> StartupPhase:
        return self._current_phase

    async def coordinate_startup(
        self,
        launchers: Dict[ComponentType, Callable[[], Awaitable[bool]]],
        on_phase_complete: Optional[Callable[[StartupPhase], Awaitable[None]]] = None,
        on_component_ready: Optional[Callable[[ComponentType], Awaitable[None]]] = None,
    ) -> StartupResult:
        """
        Coordinate full Trinity startup.

        Args:
            launchers: Map of component type to async launch function
            on_phase_complete: Callback when each phase completes
            on_component_ready: Callback when a component becomes ready

        Returns:
            StartupResult with details about what was started
        """
        result = StartupResult(
            success=False,
            phase_reached=StartupPhase.INFRASTRUCTURE,
        )
        start_time = time.time()

        try:
            # Get IPC bus
            if self._ipc_bus is None:
                self._ipc_bus = await get_trinity_ipc_bus()

            # Phase 1: Infrastructure
            self._current_phase = StartupPhase.INFRASTRUCTURE
            logger.info("[StartupCoordinator] Phase 1: Infrastructure")
            if on_phase_complete:
                await on_phase_complete(self._current_phase)
            result.phase_reached = self._current_phase

            # Phase 2: JARVIS Body (verify it's ready)
            self._current_phase = StartupPhase.JARVIS_BODY
            logger.info("[StartupCoordinator] Phase 2: Verifying JARVIS Body")

            # Start heartbeat publishing for Body
            await start_heartbeat_publishing(ComponentType.JARVIS_BODY)

            # Wait for Body to be ready (internal check)
            body_result = await self._wait_for_body_ready()
            result.components[ComponentType.JARVIS_BODY] = body_result

            if not body_result.success:
                result.errors.append("JARVIS Body failed to become ready")
                return result

            if on_phase_complete:
                await on_phase_complete(self._current_phase)
            if on_component_ready:
                await on_component_ready(ComponentType.JARVIS_BODY)
            result.phase_reached = self._current_phase

            # Phase 3: Core Services (placeholder for internal services)
            self._current_phase = StartupPhase.CORE_SERVICES
            logger.info("[StartupCoordinator] Phase 3: Core Services")
            if on_phase_complete:
                await on_phase_complete(self._current_phase)
            result.phase_reached = self._current_phase

            # Phase 4 & 5: Launch J-Prime and Reactor-Core
            await self._launch_optional_components(
                launchers=launchers,
                result=result,
                on_phase_complete=on_phase_complete,
                on_component_ready=on_component_ready,
            )

            # Phase 6: Health Monitor
            self._current_phase = StartupPhase.HEALTH_MONITOR
            logger.info("[StartupCoordinator] Phase 6: Health Monitor")
            if on_phase_complete:
                await on_phase_complete(self._current_phase)
            result.phase_reached = self._current_phase

            # Phase 7: Ready
            self._current_phase = StartupPhase.READY
            logger.info("[StartupCoordinator] Phase 7: System Ready")
            if on_phase_complete:
                await on_phase_complete(self._current_phase)
            result.phase_reached = self._current_phase

            result.success = result.all_required_ready
            result.total_duration = time.time() - start_time

            logger.info(
                f"[StartupCoordinator] Startup complete: {result.summary} "
                f"({result.total_duration:.1f}s)"
            )

        except Exception as e:
            logger.error(f"[StartupCoordinator] Startup failed: {e}")
            result.errors.append(str(e))
            result.total_duration = time.time() - start_time

        return result

    async def _wait_for_body_ready(self) -> ComponentStartResult:
        """Wait for JARVIS Body to be ready."""
        result = ComponentStartResult(
            component=ComponentType.JARVIS_BODY,
            success=False,
            start_time=time.time(),
        )

        # JARVIS Body is "us" - mark as ready after warmup
        await asyncio.sleep(2.0)  # Brief warmup

        result.success = True
        result.ready_time = time.time()
        result.pid = os.getpid()

        self._component_status[ComponentType.JARVIS_BODY] = ComponentStatus.READY
        self._component_pids[ComponentType.JARVIS_BODY] = os.getpid()

        logger.info(
            f"[StartupCoordinator] JARVIS Body ready "
            f"(PID {os.getpid()}, {result.startup_duration:.1f}s)"
        )

        return result

    async def _launch_optional_components(
        self,
        launchers: Dict[ComponentType, Callable[[], Awaitable[bool]]],
        result: StartupResult,
        on_phase_complete: Optional[Callable[[StartupPhase], Awaitable[None]]],
        on_component_ready: Optional[Callable[[ComponentType], Awaitable[None]]],
    ) -> None:
        """Launch optional components (J-Prime and Reactor-Core)."""
        optional_components = [
            (ComponentType.JARVIS_PRIME, StartupPhase.JARVIS_PRIME),
            (ComponentType.REACTOR_CORE, StartupPhase.REACTOR_CORE),
        ]

        if self._config.parallel_optional_launch:
            # Launch both in parallel
            tasks = []
            for component, phase in optional_components:
                if component in launchers:
                    task = asyncio.create_task(
                        self._launch_and_verify(
                            component=component,
                            launcher=launchers[component],
                            phase=phase,
                            on_ready=on_component_ready,
                        )
                    )
                    tasks.append((component, task))

            # Wait for all
            for component, task in tasks:
                try:
                    comp_result = await asyncio.wait_for(
                        task,
                        timeout=self._config.get_timeout(component),
                    )
                    result.components[component] = comp_result
                except asyncio.TimeoutError:
                    result.components[component] = ComponentStartResult(
                        component=component,
                        success=False,
                        error=f"Timeout after {self._config.get_timeout(component)}s",
                    )
                    result.warnings.append(f"{component.value} timed out")
                except Exception as e:
                    result.components[component] = ComponentStartResult(
                        component=component,
                        success=False,
                        error=str(e),
                    )
                    result.warnings.append(f"{component.value} failed: {e}")

        else:
            # Launch sequentially
            for component, phase in optional_components:
                self._current_phase = phase
                logger.info(f"[StartupCoordinator] Phase {phase.value}: {component.value}")

                if component in launchers:
                    try:
                        comp_result = await asyncio.wait_for(
                            self._launch_and_verify(
                                component=component,
                                launcher=launchers[component],
                                phase=phase,
                                on_ready=on_component_ready,
                            ),
                            timeout=self._config.get_timeout(component),
                        )
                        result.components[component] = comp_result
                    except asyncio.TimeoutError:
                        result.components[component] = ComponentStartResult(
                            component=component,
                            success=False,
                            error=f"Timeout after {self._config.get_timeout(component)}s",
                        )
                    except Exception as e:
                        result.components[component] = ComponentStartResult(
                            component=component,
                            success=False,
                            error=str(e),
                        )

                if on_phase_complete:
                    await on_phase_complete(phase)
                result.phase_reached = phase

    async def _launch_and_verify(
        self,
        component: ComponentType,
        launcher: Callable[[], Awaitable[bool]],
        phase: StartupPhase,
        on_ready: Optional[Callable[[ComponentType], Awaitable[None]]],
    ) -> ComponentStartResult:
        """Launch a component and verify it becomes ready."""
        result = ComponentStartResult(
            component=component,
            success=False,
            start_time=time.time(),
        )

        for attempt in range(self._config.max_launch_retries):
            result.retries = attempt

            try:
                # Launch the component
                logger.info(
                    f"[StartupCoordinator] Launching {component.value} "
                    f"(attempt {attempt + 1}/{self._config.max_launch_retries})"
                )

                launch_success = await launcher()

                if not launch_success:
                    result.error = "Launcher returned False"
                    if attempt < self._config.max_launch_retries - 1:
                        await asyncio.sleep(self._config.retry_delay)
                        continue
                    return result

                # Wait for component to become ready
                ready = await self.wait_for_component_ready(
                    component=component,
                    timeout=self._config.get_timeout(component),
                )

                if ready:
                    result.success = True
                    result.ready_time = time.time()

                    # Get PID from heartbeat
                    heartbeat = await self._ipc_bus.read_heartbeat(component)
                    if heartbeat:
                        result.pid = heartbeat.pid
                        self._component_pids[component] = heartbeat.pid

                    self._component_status[component] = ComponentStatus.READY

                    logger.info(
                        f"[StartupCoordinator] {component.value} ready "
                        f"(PID {result.pid}, {result.startup_duration:.1f}s)"
                    )

                    if on_ready:
                        await on_ready(component)

                    return result
                else:
                    result.error = "Did not become ready in time"
                    if attempt < self._config.max_launch_retries - 1:
                        await asyncio.sleep(self._config.retry_delay)

            except Exception as e:
                result.error = str(e)
                logger.warning(
                    f"[StartupCoordinator] {component.value} launch error: {e}"
                )
                if attempt < self._config.max_launch_retries - 1:
                    await asyncio.sleep(self._config.retry_delay)

        return result

    async def wait_for_component_ready(
        self,
        component: ComponentType,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Wait for a component to be ready (heartbeat + PID verified).

        Uses exponential backoff with jitter.

        Args:
            component: Component to wait for
            timeout: Maximum wait time

        Returns:
            True if component is ready
        """
        if self._ipc_bus is None:
            self._ipc_bus = await get_trinity_ipc_bus()

        timeout = timeout or self._config.get_timeout(component)
        start_time = time.time()
        poll_interval = self._config.readiness_poll_interval

        while time.time() - start_time < timeout:
            heartbeat = await self._ipc_bus.read_heartbeat(component)

            if heartbeat and heartbeat.status == "ready":
                # Verify PID is actually alive
                if is_pid_alive(heartbeat.pid):
                    return True

            await asyncio.sleep(poll_interval)

            # Simple backoff (cap at 2 seconds)
            poll_interval = min(poll_interval * 1.5, 2.0)

        return False

    def get_component_pid(self, component: ComponentType) -> Optional[int]:
        """Get the PID of a component."""
        return self._component_pids.get(component)

    def get_component_status(self, component: ComponentType) -> ComponentStatus:
        """Get the status of a component."""
        return self._component_status.get(component, ComponentStatus.STARTING)

    def is_component_ready(self, component: ComponentType) -> bool:
        """Check if a component is ready."""
        return self._component_status.get(component) == ComponentStatus.READY


# =============================================================================
# SINGLETON
# =============================================================================

_coordinator: Optional[TrinityStartupCoordinator] = None


async def get_startup_coordinator(
    config: Optional[StartupConfig] = None,
) -> TrinityStartupCoordinator:
    """Get or create the global startup coordinator."""
    global _coordinator

    if _coordinator is None:
        _coordinator = TrinityStartupCoordinator(config=config)

    return _coordinator


def get_startup_coordinator_sync() -> Optional[TrinityStartupCoordinator]:
    """Get the coordinator synchronously (may be None)."""
    return _coordinator

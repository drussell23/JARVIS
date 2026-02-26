# backend/core/enterprise_startup_orchestrator.py
"""
EnterpriseStartupOrchestrator - Main async orchestrator for enterprise-grade startup.

This module ties together all enterprise hardening modules:
- ComponentRegistry: Single source of truth for component definitions
- StartupDAG: Dependency-ordered startup execution
- RecoveryEngine: Intelligent error handling and recovery
- StartupSummary: Progress display and reporting
- StartupContext: Crash history awareness and conservative mode

Key Design Principles:
1. Async-first: All operations use async/await
2. No hardcoding: All config from ComponentRegistry
3. Parallel execution: asyncio.gather for tier-parallel startup
4. Event-driven: Emits events for monitoring and observability
5. Robust recovery: Uses RecoveryEngine for all failures
6. Observable: Real-time progress via StartupSummary

Usage:
    from backend.core.enterprise_startup_orchestrator import (
        create_enterprise_orchestrator,
        EnterpriseStartupOrchestrator,
        StartupEvent,
    )

    # Create orchestrator with defaults
    orchestrator = create_enterprise_orchestrator()

    # Register custom starters for components
    orchestrator.register_component_starter("my-service", my_start_function)

    # Register event handlers
    orchestrator.on_event(StartupEvent.COMPONENT_HEALTHY, my_handler)

    # Execute startup
    result = await orchestrator.orchestrate_startup()

    if result.success:
        print(f"Startup completed in {result.total_time:.2f}s")
    else:
        print(f"Startup failed: {result.overall_status}")

    # Shutdown when done
    await orchestrator.shutdown_all()
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Union

try:
    from backend.core.async_safety import shielded_wait_for
except ImportError:
    shielded_wait_for = None

from backend.core.component_registry import (
    ComponentDefinition,
    ComponentRegistry,
    ComponentStatus,
    Criticality,
)
from backend.core.health_contracts import (
    SystemHealth,
    SystemHealthAggregator,
)
from backend.core.recovery_engine import (
    ErrorClassifier,
    RecoveryAction,
    RecoveryEngine,
    RecoveryPhase,
    RecoveryStrategy,
)
from backend.core.startup_context import StartupContext
from backend.core.startup_dag import StartupDAG
from backend.core.startup_summary import StartupSummary

logger = logging.getLogger("jarvis.enterprise_startup_orchestrator")


def _require_control_plane_authority(action: str) -> None:
    """
    Prevent split-brain lifecycle control when a kernel owner is active.

    Standalone/unit-test mode remains allowed when no kernel lock owner exists.
    """
    try:
        from backend.supervisor.cross_repo_startup_orchestrator import (
            enforce_single_control_plane_authority,
        )
    except Exception:
        return

    if not enforce_single_control_plane_authority(
        action,
        allow_bootstrap_owner=True,
        allow_when_no_kernel_lock=True,
    ):
        raise RuntimeError(
            f"Control-plane authority required for {action}; unified supervisor owns lifecycle"
        )


class StartupEvent(Enum):
    """Events emitted during startup/shutdown lifecycle."""

    STARTUP_BEGUN = "startup_begun"
    TIER_STARTED = "tier_started"
    TIER_COMPLETED = "tier_completed"
    COMPONENT_STARTING = "component_starting"
    COMPONENT_HEALTHY = "component_healthy"
    COMPONENT_FAILED = "component_failed"
    COMPONENT_DISABLED = "component_disabled"
    RECOVERY_ATTEMPTED = "recovery_attempted"
    STARTUP_COMPLETED = "startup_completed"
    SHUTDOWN_BEGUN = "shutdown_begun"
    SHUTDOWN_COMPLETED = "shutdown_completed"


@dataclass
class ComponentResult:
    """Result of starting a single component."""

    name: str
    success: bool
    status: ComponentStatus
    startup_time: float
    error: Optional[Exception] = None
    message: Optional[str] = None


@dataclass
class StartupResult:
    """Comprehensive result of the startup orchestration."""

    success: bool
    total_time: float
    components: Dict[str, ComponentResult]
    healthy_count: int
    failed_count: int
    disabled_count: int
    overall_status: str  # "HEALTHY", "DEGRADED", "FAILED"


# Type alias for event handlers
EventHandler = Callable[[StartupEvent, Dict[str, Any]], Any]


class EnterpriseStartupOrchestrator:
    """
    Enterprise-grade startup orchestrator.

    Ties together all enterprise hardening modules:
    - ComponentRegistry: Single source of truth
    - StartupDAG: Dependency-ordered startup
    - RecoveryEngine: Intelligent error handling
    - StartupSummary: Progress display
    - StartupContext: Crash history awareness

    Attributes:
        registry: The component registry containing all component definitions
        recovery_engine: Engine for handling component failures
        startup_context: Context about previous runs (crash history)
        summary: Startup summary for progress display
    """

    def __init__(
        self,
        registry: ComponentRegistry,
        recovery_engine: Optional[RecoveryEngine] = None,
        startup_context: Optional[StartupContext] = None,
    ):
        """
        Initialize the orchestrator.

        Args:
            registry: ComponentRegistry with component definitions
            recovery_engine: Optional custom RecoveryEngine
            startup_context: Optional StartupContext for crash history
        """
        self.registry = registry
        self.recovery_engine = recovery_engine or RecoveryEngine(
            registry, ErrorClassifier()
        )
        self.startup_context = startup_context
        self.summary = StartupSummary(registry)

        # Internal state
        self._event_handlers: Dict[StartupEvent, List[EventHandler]] = {}
        self._component_starters: Dict[str, Callable[[], Awaitable[bool]]] = {}
        self._shutdown_order: List[str] = []
        self._retry_counts: Dict[str, int] = {}

    def register_component_starter(
        self,
        name: str,
        starter: Callable[[], Awaitable[bool]],
    ) -> None:
        """
        Register a custom start function for a component.

        The starter function should be an async function that returns True
        on successful startup, or raises an exception on failure.

        Args:
            name: Component name to register starter for
            starter: Async function that starts the component
        """
        self._component_starters[name] = starter
        logger.debug(f"Registered custom starter for component: {name}")

    def on_event(
        self,
        event: StartupEvent,
        handler: EventHandler,
    ) -> None:
        """
        Register an event handler for a startup event.

        Handlers can be sync or async functions. They receive the event
        and a data dict with event-specific information.

        Args:
            event: The StartupEvent to listen for
            handler: Callback function (sync or async)
        """
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)
        logger.debug(f"Registered handler for event: {event.value}")

    async def _emit_event(
        self,
        event: StartupEvent,
        **data: Any,
    ) -> None:
        """
        Emit an event to all registered handlers.

        Handler exceptions are logged but don't break event emission.

        Args:
            event: The event to emit
            **data: Event-specific data to pass to handlers
        """
        handlers = self._event_handlers.get(event, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event, data)
                else:
                    handler(event, data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"Event handler error for {event.value}: {e}",
                    exc_info=True,
                )

    async def orchestrate_startup(self) -> StartupResult:
        """
        Main entry point - orchestrate full system startup.

        Execution flow:
        1. Build DAG from registry
        2. Check for conservative mode (crash history)
        3. Start components tier-by-tier (parallel within tier)
        4. Handle failures with RecoveryEngine
        5. Return comprehensive result

        Returns:
            StartupResult with success status, timing, and component results
        """
        _require_control_plane_authority("enterprise_orchestrate_startup")

        start_time = datetime.now(timezone.utc)
        self.summary.start_time = start_time

        logger.info("Beginning enterprise startup orchestration")
        await self._emit_event(StartupEvent.STARTUP_BEGUN)

        # Build DAG from registry
        dag = StartupDAG(self.registry)
        try:
            tiers = dag.build()
        except Exception as e:
            logger.error(f"Failed to build startup DAG: {e}")
            return self._build_result({}, start_time, success=False)

        logger.info(f"Built startup DAG with {len(tiers)} tiers")

        # Determine which components to start (respecting conservative mode)
        components_to_start = self._get_components_to_start(tiers)
        logger.info(f"Starting {len(components_to_start)} components")

        # Start components tier by tier
        results: Dict[str, ComponentResult] = {}

        for tier_index, tier in enumerate(tiers):
            # Filter to only components we should start
            tier_components = [c for c in tier if c in components_to_start]
            if not tier_components:
                continue

            logger.info(f"Starting tier {tier_index}: {tier_components}")
            await self._emit_event(
                StartupEvent.TIER_STARTED,
                tier=tier_index,
                components=tier_components,
            )

            # Start all components in tier in parallel
            tier_results = await self._start_tier(tier_components)
            results.update(tier_results)

            # Check for required component failures - abort if found
            for name, result in tier_results.items():
                if not result.success:
                    try:
                        defn = self.registry.get(name)
                        if defn.effective_criticality == Criticality.REQUIRED:
                            logger.error(
                                f"Required component {name} failed - aborting startup"
                            )
                            await self._emit_event(
                                StartupEvent.STARTUP_COMPLETED,
                                success=False,
                            )
                            self.summary.end_time = datetime.now(timezone.utc)
                            return self._build_result(results, start_time, success=False)
                    except KeyError:
                        pass  # Component not in registry (shouldn't happen)

            await self._emit_event(
                StartupEvent.TIER_COMPLETED,
                tier=tier_index,
            )

        # Build and return result
        self.summary.end_time = datetime.now(timezone.utc)
        result = self._build_result(results, start_time)

        logger.info(
            f"Startup completed: success={result.success}, "
            f"status={result.overall_status}, "
            f"healthy={result.healthy_count}, "
            f"failed={result.failed_count}, "
            f"time={result.total_time:.2f}s"
        )

        await self._emit_event(
            StartupEvent.STARTUP_COMPLETED,
            success=result.success,
        )

        return result

    async def _start_tier(
        self,
        components: List[str],
    ) -> Dict[str, ComponentResult]:
        """
        Start all components in a tier in parallel.

        Uses asyncio.gather for parallel execution with exception handling.

        Args:
            components: List of component names to start

        Returns:
            Dict mapping component name to ComponentResult
        """
        tasks = [self._start_component(name) for name in components]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        tier_results: Dict[str, ComponentResult] = {}
        for name, result in zip(components, results):
            if isinstance(result, Exception):
                # Unexpected exception during startup
                logger.error(f"Unexpected error starting {name}: {result}")
                tier_results[name] = ComponentResult(
                    name=name,
                    success=False,
                    status=ComponentStatus.FAILED,
                    startup_time=0.0,
                    error=result,
                    message=str(result),
                )
                self.registry.mark_status(name, ComponentStatus.FAILED, str(result))
            else:
                tier_results[name] = result

        return tier_results

    async def _start_component(self, name: str) -> ComponentResult:
        """
        Start a single component with recovery handling.

        Handles:
        - Disabled components (via environment variable)
        - Custom starters
        - Timeouts
        - Failures with recovery engine

        Args:
            name: Component name to start

        Returns:
            ComponentResult with startup outcome
        """
        start = datetime.now(timezone.utc)

        await self._emit_event(StartupEvent.COMPONENT_STARTING, component=name)

        try:
            defn = self.registry.get(name)
        except KeyError:
            logger.error(f"Component not found in registry: {name}")
            return ComponentResult(
                name=name,
                success=False,
                status=ComponentStatus.FAILED,
                startup_time=0.0,
                error=KeyError(f"Component not found: {name}"),
            )

        # Check if disabled by environment variable
        if defn.is_disabled_by_env():
            self.registry.mark_status(
                name,
                ComponentStatus.DISABLED,
                "Disabled by environment variable",
            )
            await self._emit_event(StartupEvent.COMPONENT_DISABLED, component=name)

            logger.info(f"Component {name} disabled by environment variable")
            return ComponentResult(
                name=name,
                success=True,  # Disabled is not a failure
                status=ComponentStatus.DISABLED,
                startup_time=0.0,
                message="Disabled by environment variable",
            )

        # Mark as starting
        self.registry.mark_status(name, ComponentStatus.STARTING)

        try:
            # Use custom starter if registered, otherwise mark healthy
            if name in self._component_starters:
                if shielded_wait_for:
                    success = await shielded_wait_for(
                        self._component_starters[name](),
                        timeout=defn.startup_timeout,
                        name=f"enterprise_start_{name}",
                    )
                else:
                    success = await asyncio.wait_for(
                        self._component_starters[name](),
                        timeout=defn.startup_timeout,
                    )
            else:
                # No custom starter - default to success (component is in-process)
                success = True

            if success:
                self.registry.mark_status(name, ComponentStatus.HEALTHY)
                self._shutdown_order.append(name)
                await self._emit_event(StartupEvent.COMPONENT_HEALTHY, component=name)

                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                logger.info(f"Component {name} healthy in {elapsed:.2f}s")

                return ComponentResult(
                    name=name,
                    success=True,
                    status=ComponentStatus.HEALTHY,
                    startup_time=elapsed,
                )
            else:
                raise RuntimeError(f"Component {name} startup returned False")

        except asyncio.TimeoutError:
            error = TimeoutError(f"Startup timeout ({defn.startup_timeout}s)")
            return await self._handle_failure(name, error, start)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            return await self._handle_failure(name, e, start)

    async def _handle_failure(
        self,
        name: str,
        error: Exception,
        start_time: datetime,
    ) -> ComponentResult:
        """
        Handle component failure using RecoveryEngine.

        Determines recovery strategy and takes appropriate action:
        - FULL_RESTART: Retry with delay
        - FALLBACK_MODE: Use fallback and mark degraded
        - DISABLE_AND_CONTINUE: Mark failed and continue
        - ESCALATE_TO_USER: Mark failed, requires manual intervention

        Args:
            name: Failed component name
            error: The exception that was raised
            start_time: When the component started

        Returns:
            ComponentResult with failure outcome
        """
        logger.warning(f"Component {name} failed: {error}")

        # Get recovery action from engine
        action = await self.recovery_engine.handle_failure(
            name, error, RecoveryPhase.STARTUP
        )

        await self._emit_event(
            StartupEvent.RECOVERY_ATTEMPTED,
            component=name,
            error=str(error),
            strategy=action.strategy.value,
        )

        try:
            defn = self.registry.get(name)
        except KeyError:
            defn = None

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

        if action.strategy == RecoveryStrategy.FULL_RESTART:
            # Retry with delay
            logger.info(
                f"Retrying {name} after {action.delay:.2f}s delay"
            )
            await asyncio.sleep(action.delay)
            return await self._start_component(name)

        elif action.strategy == RecoveryStrategy.FALLBACK_MODE:
            # Use fallback - mark as degraded
            self.registry.mark_status(name, ComponentStatus.DEGRADED, str(error))
            self._shutdown_order.append(name)  # Still needs shutdown

            await self._emit_event(
                StartupEvent.COMPONENT_FAILED,
                component=name,
                error=str(error),
            )

            fallback_msg = f"Using fallback: {action.fallback_targets}"
            logger.info(f"Component {name} in fallback mode: {fallback_msg}")

            return ComponentResult(
                name=name,
                success=True,  # Fallback means we continue
                status=ComponentStatus.DEGRADED,
                startup_time=elapsed,
                error=error,
                message=fallback_msg,
            )

        elif action.strategy == RecoveryStrategy.DISABLE_AND_CONTINUE:
            # Disable and continue
            self.registry.mark_status(name, ComponentStatus.FAILED, str(error))

            await self._emit_event(
                StartupEvent.COMPONENT_FAILED,
                component=name,
                error=str(error),
            )

            # Success depends on criticality
            is_required = (
                defn and defn.effective_criticality == Criticality.REQUIRED
            )

            return ComponentResult(
                name=name,
                success=not is_required,
                status=ComponentStatus.FAILED,
                startup_time=elapsed,
                error=error,
            )

        else:  # ESCALATE_TO_USER
            # Mark failed, requires manual intervention
            self.registry.mark_status(name, ComponentStatus.FAILED, str(error))

            await self._emit_event(
                StartupEvent.COMPONENT_FAILED,
                component=name,
                error=str(error),
            )

            logger.error(
                f"Component {name} requires manual intervention: {error}"
            )

            return ComponentResult(
                name=name,
                success=False,
                status=ComponentStatus.FAILED,
                startup_time=elapsed,
                error=error,
                message="Requires manual intervention",
            )

    def _get_components_to_start(
        self,
        tiers: List[List[str]],
    ) -> Set[str]:
        """
        Get components to start, respecting conservative mode.

        In conservative mode (multiple recent crashes), low-priority
        optional components are skipped to improve stability.

        Args:
            tiers: List of tiers from StartupDAG

        Returns:
            Set of component names to start
        """
        all_components = {c for tier in tiers for c in tier}

        # Check if we need conservative startup
        if self.startup_context and self.startup_context.needs_conservative_startup:
            logger.warning(
                "Conservative startup mode enabled due to recent crashes"
            )

            to_skip: Set[str] = set()
            for name in all_components:
                try:
                    defn = self.registry.get(name)
                    # Skip low-priority optional components
                    if defn.effective_criticality == Criticality.OPTIONAL:
                        if defn.conservative_skip_priority < 50:
                            to_skip.add(name)
                            logger.info(
                                f"Skipping {name} in conservative mode "
                                f"(priority={defn.conservative_skip_priority})"
                            )
                except KeyError:
                    pass

            return all_components - to_skip

        return all_components

    def _build_result(
        self,
        results: Dict[str, ComponentResult],
        start_time: datetime,
        success: Optional[bool] = None,
    ) -> StartupResult:
        """
        Build comprehensive startup result.

        Computes success, counts, and overall status from component results.

        Args:
            results: Dict of component results
            start_time: When startup began
            success: Override success flag (None = compute from results)

        Returns:
            StartupResult with all metrics
        """
        healthy = sum(
            1 for r in results.values()
            if r.status == ComponentStatus.HEALTHY
        )
        failed = sum(
            1 for r in results.values()
            if r.status == ComponentStatus.FAILED
        )
        disabled = sum(
            1 for r in results.values()
            if r.status == ComponentStatus.DISABLED
        )
        degraded = sum(
            1 for r in results.values()
            if r.status == ComponentStatus.DEGRADED
        )

        # Compute success if not overridden
        if success is None:
            if failed == 0:
                success = True
            else:
                # Check if any required components failed
                success = all(
                    self._get_criticality(r.name) != Criticality.REQUIRED
                    for r in results.values()
                    if r.status == ComponentStatus.FAILED
                )

        # Determine overall status
        if failed > 0:
            overall = "FAILED" if not success else "DEGRADED"
        elif degraded > 0:
            overall = "DEGRADED"
        else:
            overall = "HEALTHY"

        total_time = (datetime.now(timezone.utc) - start_time).total_seconds()

        return StartupResult(
            success=success,
            total_time=total_time,
            components=results,
            healthy_count=healthy,
            failed_count=failed,
            disabled_count=disabled,
            overall_status=overall,
        )

    def _get_criticality(self, name: str) -> Optional[Criticality]:
        """Get the effective criticality of a component."""
        try:
            defn = self.registry.get(name)
            return defn.effective_criticality
        except KeyError:
            return None

    async def shutdown_all(self, reverse_order: bool = True) -> None:
        """
        Shutdown all components in reverse startup order.

        Emits SHUTDOWN_BEGUN and SHUTDOWN_COMPLETED events.

        Args:
            reverse_order: If True, shutdown in reverse startup order
        """
        _require_control_plane_authority("enterprise_shutdown_all")

        logger.info("Beginning shutdown sequence")
        await self._emit_event(StartupEvent.SHUTDOWN_BEGUN)

        order = (
            list(reversed(self._shutdown_order))
            if reverse_order
            else self._shutdown_order
        )

        for name in order:
            try:
                logger.info(f"Shutting down component: {name}")
                self.registry.mark_status(name, ComponentStatus.PENDING)
            except Exception as e:
                logger.warning(f"Error shutting down {name}: {e}")

        logger.info("Shutdown sequence completed")
        await self._emit_event(StartupEvent.SHUTDOWN_COMPLETED)

    async def health_check_all(self) -> SystemHealth:
        """
        Perform health check on all components.

        Uses SystemHealthAggregator to collect health from all
        registered components in parallel.

        Returns:
            SystemHealth with aggregated health information
        """
        aggregator = SystemHealthAggregator(self.registry)
        return await aggregator.collect_all()


def create_enterprise_orchestrator(
    register_defaults: bool = True,
    startup_context: Optional[StartupContext] = None,
) -> EnterpriseStartupOrchestrator:
    """
    Factory to create fully configured orchestrator.

    Creates a new ComponentRegistry and optionally registers
    default components from default_components module.

    Args:
        register_defaults: If True, register JARVIS default components
        startup_context: Optional StartupContext for crash history

    Returns:
        Configured EnterpriseStartupOrchestrator
    """
    from backend.core.default_components import register_default_components

    registry = ComponentRegistry()

    if register_defaults:
        register_default_components(registry)

    if startup_context is None:
        try:
            startup_context = StartupContext.load()
        except Exception as e:
            logger.warning(f"Failed to load startup context: {e}")
            startup_context = None

    return EnterpriseStartupOrchestrator(
        registry=registry,
        startup_context=startup_context,
    )

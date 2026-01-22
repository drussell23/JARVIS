"""
Supervisor Lifecycle Integration v1.0 - Connect Lifecycle Orchestrator to Supervisor
=====================================================================================

This module integrates the LifecycleEventOrchestrator with the existing
JARVISSupervisor system, enabling:

1. Automatic lifecycle event publishing during startup/shutdown
2. Dependency-aware service startup ordering
3. Resilient health checking with proper backoff

Usage in JARVISSupervisor:
    from backend.core.supervisor_lifecycle_integration import (
        SupervisorLifecycleIntegration,
    )

    # In supervisor __init__:
    self._lifecycle_integration = SupervisorLifecycleIntegration(self)

    # In supervisor startup:
    await self._lifecycle_integration.initialize()

    # Before starting each component:
    success = await self._lifecycle_integration.start_component_with_lifecycle(
        "jarvis-body",
        start_func=self._start_jarvis_body,
    )

Author: JARVIS Trinity v95.0 - Supervisor Lifecycle Integration
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Use try/except for import to handle circular import issues during static analysis
try:
    from backend.core.lifecycle_event_orchestrator import (
        ComponentDefinition,
        ComponentState,
        HealthCheckStrategy,
        LifecycleEventOrchestrator,
        get_lifecycle_orchestrator,
        publish_lifecycle_event,
    )
except ImportError:
    # Fallback for static analysis tools
    from lifecycle_event_orchestrator import (  # type: ignore
        ComponentDefinition,
        ComponentState,
        HealthCheckStrategy,
        LifecycleEventOrchestrator,
        get_lifecycle_orchestrator,
        publish_lifecycle_event,
    )

logger = logging.getLogger(__name__)


# =============================================================================
# Trinity Component Definitions
# =============================================================================

# Default component definitions for the Trinity architecture
TRINITY_COMPONENTS: List[ComponentDefinition] = [
    # Core Infrastructure (no dependencies)
    ComponentDefinition(
        name="redis",
        dependencies=[],
        health_strategy=HealthCheckStrategy.PROCESS,
        health_endpoint=None,
        critical=False,  # Can run without Redis in degraded mode
        startup_timeout_ms=30000,
    ),

    # Service Registry (depends on nothing)
    ComponentDefinition(
        name="service-registry",
        dependencies=[],
        health_strategy=HealthCheckStrategy.FILE,
        health_endpoint=None,
        critical=True,
        startup_timeout_ms=10000,
    ),

    # Event Bus (depends on service registry)
    ComponentDefinition(
        name="event-bus",
        dependencies=["service-registry"],
        health_strategy=HealthCheckStrategy.FILE,
        health_endpoint=None,
        critical=True,
        startup_timeout_ms=10000,
    ),

    # JARVIS-Prime (Mind) - Local LLM
    ComponentDefinition(
        name="jarvis-prime",
        dependencies=["service-registry"],
        health_strategy=HealthCheckStrategy.HTTP,
        health_endpoint=f"http://localhost:{os.getenv('PRIME_PORT', '8004')}/health/ready",
        critical=False,  # Can fall back to cloud AI
        startup_timeout_ms=120000,  # 2 minutes - model loading takes time
    ),

    # JARVIS-Body (Main Backend)
    ComponentDefinition(
        name="jarvis-body",
        dependencies=["service-registry", "event-bus"],
        health_strategy=HealthCheckStrategy.HTTP,
        health_endpoint=f"http://localhost:{os.getenv('BACKEND_PORT', '8010')}/health/ready",
        critical=True,
        startup_timeout_ms=60000,
    ),

    # Reactor-Core (Nerves) - Training Pipeline
    ComponentDefinition(
        name="reactor-core",
        dependencies=["service-registry", "event-bus", "jarvis-prime"],
        health_strategy=HealthCheckStrategy.FILE,
        health_endpoint=None,
        critical=False,  # Training is optional
        startup_timeout_ms=60000,
    ),

    # WebSocket Server
    ComponentDefinition(
        name="websocket-server",
        dependencies=["jarvis-body"],
        health_strategy=HealthCheckStrategy.HTTP,
        health_endpoint=f"http://localhost:{os.getenv('WS_PORT', '8011')}/health",
        critical=False,
        startup_timeout_ms=30000,
    ),

    # Frontend
    ComponentDefinition(
        name="frontend",
        dependencies=["jarvis-body", "websocket-server"],
        health_strategy=HealthCheckStrategy.HTTP,
        health_endpoint=f"http://localhost:{os.getenv('FRONTEND_PORT', '3000')}",
        critical=False,  # Can run headless
        startup_timeout_ms=60000,
    ),
]


@dataclass
class IntegrationConfig:
    """Configuration for supervisor lifecycle integration."""
    auto_register_trinity_components: bool = True
    publish_heartbeats: bool = True
    heartbeat_interval_ms: int = 30000
    enable_dependency_validation: bool = True
    enable_graceful_degradation: bool = True
    startup_parallelism: int = 2  # How many non-dependent components to start in parallel


@dataclass
class StartupResult:
    """Result of a component startup attempt."""
    component_name: str
    success: bool
    duration_ms: float
    error: Optional[str] = None
    was_dependency_failure: bool = False


class SupervisorLifecycleIntegration:
    """
    Integrates LifecycleEventOrchestrator with JARVISSupervisor.

    Provides:
    - Automatic lifecycle event publishing
    - Dependency-aware startup sequencing
    - Resilient health monitoring
    - Graceful degradation support
    """

    def __init__(
        self,
        supervisor: Any,  # JARVISSupervisor instance
        config: Optional[IntegrationConfig] = None,
    ):
        self._supervisor = supervisor
        self._config = config or IntegrationConfig()
        self._orchestrator: Optional[LifecycleEventOrchestrator] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        self._startup_results: List[StartupResult] = []
        self._custom_components: Dict[str, ComponentDefinition] = {}

    async def initialize(self) -> None:
        """Initialize the lifecycle integration."""
        if self._running:
            return

        self._running = True

        # Get orchestrator
        self._orchestrator = await get_lifecycle_orchestrator()

        # Register Trinity components if configured
        if self._config.auto_register_trinity_components and self._orchestrator:
            for component in TRINITY_COMPONENTS:
                self._orchestrator.register_component(component)

        # Register any custom components
        if self._orchestrator:
            for component in self._custom_components.values():
                self._orchestrator.register_component(component)

        # Start heartbeat publishing if enabled
        if self._config.publish_heartbeats:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Publish system starting event
        await publish_lifecycle_event(
            event_type="lifecycle.system.starting",
            component="supervisor",
            state=ComponentState.STARTING,
            payload={
                "registered_components": len(TRINITY_COMPONENTS) + len(self._custom_components),
                "startup_order": self._orchestrator.get_startup_order() if self._orchestrator else [],
            },
        )

        logger.info("[SupervisorLifecycle] Integration initialized")

    async def shutdown(self) -> None:
        """Shutdown the lifecycle integration."""
        if not self._running:
            return

        self._running = False

        # Cancel heartbeat task
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Publish system shutdown event
        await publish_lifecycle_event(
            event_type="lifecycle.system.shutdown",
            component="supervisor",
            state=ComponentState.SHUTTING_DOWN,
        )

        logger.info("[SupervisorLifecycle] Integration shut down")

    def register_component(
        self,
        name: str,
        dependencies: Optional[List[str]] = None,
        health_endpoint: Optional[str] = None,
        health_check_func: Optional[Callable[[], Awaitable[bool]]] = None,
        critical: bool = True,
        startup_timeout_ms: int = 60000,
    ) -> None:
        """
        Register a custom component for lifecycle management.

        Args:
            name: Component name
            dependencies: List of component names this depends on
            health_endpoint: HTTP endpoint for health checks
            health_check_func: Custom async health check function
            critical: Whether system can run without this component
            startup_timeout_ms: Maximum startup time
        """
        if health_check_func:
            strategy = HealthCheckStrategy.CUSTOM
        elif health_endpoint:
            strategy = HealthCheckStrategy.HTTP
        else:
            strategy = HealthCheckStrategy.FILE

        component = ComponentDefinition(
            name=name,
            dependencies=dependencies or [],
            health_strategy=strategy,
            health_endpoint=health_endpoint,
            health_check_func=health_check_func,
            critical=critical,
            startup_timeout_ms=startup_timeout_ms,
        )

        self._custom_components[name] = component

        if self._orchestrator:
            self._orchestrator.register_component(component)

    async def start_component_with_lifecycle(
        self,
        component_name: str,
        start_func: Callable[[], Awaitable[bool]],
    ) -> StartupResult:
        """
        Start a component with full lifecycle management.

        This wraps the component startup with:
        - Dependency validation
        - Lifecycle event publishing
        - Health check verification

        Args:
            component_name: Name of component to start
            start_func: Async function that starts the component

        Returns:
            StartupResult with success status and details
        """
        start_time = datetime.now()

        if not self._orchestrator:
            return StartupResult(
                component_name=component_name,
                success=False,
                duration_ms=0,
                error="Orchestrator not initialized",
            )

        try:
            success = await self._orchestrator.start_component(
                component_name=component_name,
                start_func=start_func,
            )

            duration_ms = (datetime.now() - start_time).total_seconds() * 1000

            result = StartupResult(
                component_name=component_name,
                success=success,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (datetime.now() - start_time).total_seconds() * 1000
            result = StartupResult(
                component_name=component_name,
                success=False,
                duration_ms=duration_ms,
                error=str(e),
            )

        self._startup_results.append(result)
        return result

    async def start_all_components(
        self,
        component_starters: Dict[str, Callable[[], Awaitable[bool]]],
    ) -> Dict[str, StartupResult]:
        """
        Start all components in dependency order.

        Args:
            component_starters: Dict mapping component names to start functions

        Returns:
            Dict mapping component names to startup results
        """
        results: Dict[str, StartupResult] = {}

        if not self._orchestrator:
            logger.error("[SupervisorLifecycle] Orchestrator not initialized")
            return results

        startup_order = self._orchestrator.get_startup_order()

        # Group components by dependency level for parallel startup
        levels = self._calculate_dependency_levels(startup_order)

        for level_components in levels:
            # Filter to only components we have starters for
            startable = [c for c in level_components if c in component_starters]

            if not startable:
                continue

            # Start components at this level in parallel (with limit)
            tasks = []
            for component_name in startable:
                if len(tasks) >= self._config.startup_parallelism:
                    # Wait for some to complete before starting more
                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    tasks = list(pending)

                    # Process completed
                    for task in done:
                        try:
                            result = await task
                            results[result.component_name] = result
                        except Exception as e:
                            logger.error(f"Startup task error: {e}")

                # Create new task
                task = asyncio.create_task(
                    self.start_component_with_lifecycle(
                        component_name,
                        component_starters[component_name],
                    )
                )
                tasks.append(task)

            # Wait for remaining tasks at this level
            if tasks:
                completed = await asyncio.gather(*tasks, return_exceptions=True)
                for item in completed:
                    if isinstance(item, StartupResult):
                        results[item.component_name] = item

            # Check if critical component failed
            critical_failed = False
            for component_name in startable:
                if component_name in results and not results[component_name].success:
                    # Check if critical
                    component = next(
                        (c for c in TRINITY_COMPONENTS if c.name == component_name),
                        None,
                    ) or self._custom_components.get(component_name)

                    if component and component.critical:
                        critical_failed = True
                        logger.error(
                            f"[SupervisorLifecycle] Critical component {component_name} "
                            f"failed: {results[component_name].error}"
                        )

            if critical_failed and not self._config.enable_graceful_degradation:
                # Stop startup if critical component failed and degradation disabled
                logger.error("[SupervisorLifecycle] Stopping startup due to critical failure")
                break

        # Publish system ready or degraded event
        all_success = all(r.success for r in results.values())
        critical_success = all(
            r.success for name, r in results.items()
            if any(c.name == name and c.critical for c in TRINITY_COMPONENTS)
            or (name in self._custom_components and self._custom_components[name].critical)
        )

        if all_success:
            await publish_lifecycle_event(
                event_type="lifecycle.system.ready",
                component="supervisor",
                state=ComponentState.READY,
                payload={"startup_results": {n: r.success for n, r in results.items()}},
            )
        elif critical_success:
            await publish_lifecycle_event(
                event_type="lifecycle.system.degraded",
                component="supervisor",
                state=ComponentState.DEGRADED,
                payload={
                    "startup_results": {n: r.success for n, r in results.items()},
                    "failed_components": [n for n, r in results.items() if not r.success],
                },
            )
        else:
            await publish_lifecycle_event(
                event_type="lifecycle.system.failed",
                component="supervisor",
                state=ComponentState.FAILED,
                payload={
                    "startup_results": {n: r.success for n, r in results.items()},
                    "failed_critical": [
                        n for n, r in results.items()
                        if not r.success and any(
                            c.name == n and c.critical for c in TRINITY_COMPONENTS
                        )
                    ],
                },
            )

        return results

    def _calculate_dependency_levels(
        self,
        startup_order: List[str],
    ) -> List[List[str]]:
        """
        Group components by dependency level for parallel startup.

        Components at the same level have no dependencies on each other
        and can be started in parallel.
        """
        if not self._orchestrator:
            return [[c] for c in startup_order]

        levels: List[List[str]] = []
        assigned: set = set()
        components = {
            c.name: c for c in TRINITY_COMPONENTS
        }
        components.update({
            name: comp for name, comp in self._custom_components.items()
        })

        remaining = set(startup_order)

        while remaining:
            # Find components whose dependencies are all assigned
            current_level = []

            for name in remaining:
                component = components.get(name)
                if not component:
                    # Unknown component - add to current level
                    current_level.append(name)
                    continue

                deps_satisfied = all(
                    dep in assigned or dep not in remaining
                    for dep in component.dependencies
                )

                if deps_satisfied:
                    current_level.append(name)

            if not current_level:
                # No progress - circular dependency or error
                # Add remaining as final level
                current_level = list(remaining)

            levels.append(current_level)
            assigned.update(current_level)
            remaining -= set(current_level)

        return levels

    async def _heartbeat_loop(self) -> None:
        """Background loop to publish heartbeat events."""
        interval = self._config.heartbeat_interval_ms / 1000

        while self._running:
            try:
                await publish_lifecycle_event(
                    event_type="lifecycle.supervisor.heartbeat",
                    component="supervisor",
                    state=ComponentState.READY,
                    payload={
                        "timestamp": datetime.now().isoformat(),
                        "uptime_seconds": self._get_supervisor_uptime(),
                        "system_status": self.get_system_status(),
                    },
                )

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[SupervisorLifecycle] Heartbeat error: {e}")
                await asyncio.sleep(5.0)

    def _get_supervisor_uptime(self) -> float:
        """Get supervisor uptime in seconds."""
        try:
            if hasattr(self._supervisor, 'stats') and self._supervisor.stats:
                return (
                    datetime.now() - self._supervisor.stats.supervisor_start_time
                ).total_seconds()
        except Exception:
            pass
        return 0.0

    def get_system_status(self) -> Dict[str, Any]:
        """Get current system status."""
        if not self._orchestrator:
            return {"status": "not_initialized"}

        return self._orchestrator.get_system_status()

    def get_startup_results(self) -> List[StartupResult]:
        """Get all startup results."""
        return self._startup_results.copy()

    def get_startup_summary(self) -> Dict[str, Any]:
        """Get a summary of startup results."""
        if not self._startup_results:
            return {"status": "no_startups"}

        total = len(self._startup_results)
        successful = sum(1 for r in self._startup_results if r.success)
        total_duration = sum(r.duration_ms for r in self._startup_results)

        return {
            "total_components": total,
            "successful": successful,
            "failed": total - successful,
            "success_rate": successful / total if total > 0 else 0,
            "total_duration_ms": total_duration,
            "failed_components": [
                {"name": r.component_name, "error": r.error}
                for r in self._startup_results
                if not r.success
            ],
        }


# =============================================================================
# Convenience Functions
# =============================================================================

async def create_supervisor_integration(
    supervisor: Any,
) -> SupervisorLifecycleIntegration:
    """
    Create and initialize a supervisor lifecycle integration.

    Usage:
        integration = await create_supervisor_integration(supervisor)
        # Now use integration.start_component_with_lifecycle() for each component
    """
    integration = SupervisorLifecycleIntegration(supervisor)
    await integration.initialize()
    return integration

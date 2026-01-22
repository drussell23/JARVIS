"""
Lifecycle Event Orchestrator v1.0 - Unified Lifecycle Management
================================================================

Fixes Issues 14, 15, 16 by providing:
1. Event Bus Integration - Publish lifecycle events (starting, ready, failed, shutdown)
2. Resilient Health Checking - Retry/backoff before declaring unhealthy
3. Dependency Validation - Ensure dependencies are ready before starting dependents

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                   LIFECYCLE EVENT ORCHESTRATOR                      │
    ├─────────────────────────────────────────────────────────────────────┤
    │                                                                     │
    │  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐   │
    │  │ Event Publisher │   │ Health Checker  │   │ Dependency      │   │
    │  │                 │   │ (with backoff)  │   │ Validator       │   │
    │  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘   │
    │           │                     │                     │            │
    │           └─────────────────────┼─────────────────────┘            │
    │                                 │                                  │
    │                    ┌────────────▼────────────┐                     │
    │                    │    Trinity Event Bus    │                     │
    │                    │    (Cross-Repo IPC)     │                     │
    │                    └────────────┬────────────┘                     │
    │                                 │                                  │
    │    ┌────────────────────────────┼────────────────────────────┐     │
    │    │                            │                            │     │
    │    ▼                            ▼                            ▼     │
    │  JARVIS                     Prime                      Reactor     │
    │  (Body)                     (Mind)                    (Nerves)     │
    └─────────────────────────────────────────────────────────────────────┘

Event Types Published:
- lifecycle.component.starting  - Component is beginning initialization
- lifecycle.component.ready     - Component is fully initialized and ready
- lifecycle.component.failed    - Component failed to initialize
- lifecycle.component.shutdown  - Component is shutting down
- lifecycle.component.recovered - Component recovered from unhealthy state
- lifecycle.system.ready        - All critical components are ready
- lifecycle.system.degraded     - Some components unhealthy (graceful degradation)
- lifecycle.system.failed       - Critical failure, cannot continue

Author: JARVIS Trinity v95.0 - Lifecycle Event Orchestration
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
# Note: ABC and abstractmethod kept for potential future extension
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (100% Environment-Driven)
# =============================================================================

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class LifecycleConfig:
    """Configuration for lifecycle event orchestrator."""

    # Retry/Backoff settings
    INITIAL_RETRY_DELAY_MS = _env_int("LIFECYCLE_INITIAL_RETRY_DELAY_MS", 500)
    MAX_RETRY_DELAY_MS = _env_int("LIFECYCLE_MAX_RETRY_DELAY_MS", 30000)
    BACKOFF_MULTIPLIER = _env_float("LIFECYCLE_BACKOFF_MULTIPLIER", 2.0)
    MAX_RETRIES_BEFORE_UNHEALTHY = _env_int("LIFECYCLE_MAX_RETRIES", 5)
    JITTER_FACTOR = _env_float("LIFECYCLE_JITTER_FACTOR", 0.1)

    # Health check settings
    HEALTH_CHECK_INTERVAL_MS = _env_int("LIFECYCLE_HEALTH_CHECK_INTERVAL_MS", 5000)
    HEALTH_CHECK_TIMEOUT_MS = _env_int("LIFECYCLE_HEALTH_CHECK_TIMEOUT_MS", 10000)
    CONSECUTIVE_HEALTHY_FOR_RECOVERY = _env_int("LIFECYCLE_CONSECUTIVE_HEALTHY", 3)

    # Dependency validation settings
    DEPENDENCY_WAIT_TIMEOUT_MS = _env_int("LIFECYCLE_DEPENDENCY_TIMEOUT_MS", 60000)
    DEPENDENCY_POLL_INTERVAL_MS = _env_int("LIFECYCLE_DEPENDENCY_POLL_MS", 500)

    # Event publishing settings
    PUBLISH_TIMEOUT_MS = _env_int("LIFECYCLE_PUBLISH_TIMEOUT_MS", 5000)
    ENABLE_EVENT_PERSISTENCE = _env_bool("LIFECYCLE_PERSIST_EVENTS", True)


# =============================================================================
# Enums and Data Types
# =============================================================================

class ComponentState(Enum):
    """Component lifecycle states."""
    NOT_STARTED = "not_started"
    STARTING = "starting"
    WAITING_DEPENDENCIES = "waiting_dependencies"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"
    RECOVERED = "recovered"  # Transitioned from unhealthy back to healthy
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


class HealthCheckStrategy(Enum):
    """Strategy for health checking."""
    HTTP = "http"           # HTTP endpoint check
    PROCESS = "process"     # Process existence check
    FILE = "file"           # File/heartbeat check
    CUSTOM = "custom"       # Custom callback


class EventPriority(Enum):
    """Event priority for publishing."""
    CRITICAL = 0   # Must be delivered immediately
    HIGH = 1       # Important but can tolerate slight delay
    NORMAL = 2     # Standard priority
    LOW = 3        # Background/informational


@dataclass
class ComponentDefinition:
    """Definition of a component with its dependencies and health check strategy."""
    name: str
    dependencies: List[str] = field(default_factory=list)
    health_strategy: HealthCheckStrategy = HealthCheckStrategy.HTTP
    health_endpoint: Optional[str] = None
    health_check_func: Optional[Callable[[], Awaitable[bool]]] = None
    critical: bool = True  # If False, system can run in degraded mode without it
    startup_timeout_ms: int = 60000

    def __hash__(self):
        return hash(self.name)


@dataclass
class ComponentStatus:
    """Current status of a component."""
    name: str
    state: ComponentState = ComponentState.NOT_STARTED
    health_score: float = 0.0  # 0.0 to 1.0
    last_check: Optional[datetime] = None
    last_healthy: Optional[datetime] = None
    last_error: Optional[str] = None
    retry_count: int = 0
    consecutive_healthy: int = 0
    consecutive_unhealthy: int = 0
    started_at: Optional[datetime] = None
    ready_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.state == ComponentState.READY

    @property
    def is_healthy(self) -> bool:
        return self.state in (ComponentState.READY, ComponentState.DEGRADED)

    @property
    def startup_duration_ms(self) -> Optional[float]:
        if self.started_at and self.ready_at:
            return (self.ready_at - self.started_at).total_seconds() * 1000
        return None


@dataclass
class LifecycleEvent:
    """A lifecycle event to be published."""
    event_type: str
    component: str
    state: ComponentState
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    correlation_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "component": self.component,
            "state": self.state.value,
            "priority": self.priority.value,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "correlation_id": self.correlation_id,
        }


@dataclass
class BackoffState:
    """State for exponential backoff calculation."""
    current_delay_ms: float = 500.0
    retry_count: int = 0
    last_attempt: Optional[datetime] = None

    def next_delay(self) -> float:
        """Calculate next delay with exponential backoff and jitter."""
        import random

        base_delay = min(
            self.current_delay_ms * LifecycleConfig.BACKOFF_MULTIPLIER,
            LifecycleConfig.MAX_RETRY_DELAY_MS
        )

        # Add jitter to prevent thundering herd
        jitter = base_delay * LifecycleConfig.JITTER_FACTOR * random.random()
        delay = base_delay + jitter

        self.current_delay_ms = delay
        self.retry_count += 1
        self.last_attempt = datetime.now()

        return delay

    def reset(self) -> None:
        """Reset backoff state."""
        self.current_delay_ms = LifecycleConfig.INITIAL_RETRY_DELAY_MS
        self.retry_count = 0


# =============================================================================
# Event Publisher (Issue 14 Fix)
# =============================================================================

class LifecycleEventPublisher:
    """
    Publishes lifecycle events to the Trinity Event Bus.

    This ensures all components across repositories are informed of
    lifecycle state changes, enabling coordinated startup/shutdown.
    """

    def __init__(self):
        self._event_bus = None
        self._event_queue: asyncio.Queue[LifecycleEvent] = asyncio.Queue()
        self._publisher_task: Optional[asyncio.Task] = None
        self._running = False
        self._callbacks: List[Callable[[LifecycleEvent], Awaitable[None]]] = []
        self._published_count = 0
        self._failed_count = 0

    async def start(self) -> None:
        """Start the event publisher."""
        if self._running:
            return

        self._running = True

        # Get event bus (lazy import to avoid circular deps)
        try:
            from backend.core.trinity_event_bus import get_trinity_event_bus, RepoType
            self._event_bus = await get_trinity_event_bus(RepoType.JARVIS)
            logger.info("[LifecyclePublisher] Connected to Trinity Event Bus")
        except Exception as e:
            logger.warning(f"[LifecyclePublisher] Event bus not available: {e}")

        # Start background publisher
        self._publisher_task = asyncio.create_task(self._publisher_loop())

    async def stop(self) -> None:
        """Stop the event publisher."""
        self._running = False

        if self._publisher_task:
            self._publisher_task.cancel()
            try:
                await self._publisher_task
            except asyncio.CancelledError:
                pass

        # Drain remaining events
        while not self._event_queue.empty():
            try:
                event = self._event_queue.get_nowait()
                await self._publish_event(event)
            except Exception:
                pass

    async def publish(
        self,
        event_type: str,
        component: str,
        state: ComponentState,
        priority: EventPriority = EventPriority.NORMAL,
        payload: Optional[Dict[str, Any]] = None,
        correlation_id: str = "",
    ) -> None:
        """
        Queue a lifecycle event for publishing.

        Args:
            event_type: Type of event (e.g., "lifecycle.component.ready")
            component: Component name
            state: Current component state
            priority: Event priority
            payload: Additional event data
            correlation_id: Correlation ID for tracing
        """
        event = LifecycleEvent(
            event_type=event_type,
            component=component,
            state=state,
            priority=priority,
            payload=payload or {},
            correlation_id=correlation_id,
        )

        await self._event_queue.put(event)

        # Also notify local callbacks
        for callback in self._callbacks:
            try:
                await callback(event)
            except Exception as e:
                logger.debug(f"[LifecyclePublisher] Callback error: {e}")

    async def _publisher_loop(self) -> None:
        """Background loop to publish events."""
        while self._running:
            try:
                # Wait for event with timeout
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )

                await self._publish_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LifecyclePublisher] Publisher loop error: {e}")
                await asyncio.sleep(1.0)

    async def _publish_event(self, event: LifecycleEvent) -> bool:
        """Publish a single event to the bus."""
        if not self._event_bus:
            return False

        try:
            from backend.core.trinity_event_bus import (
                TrinityEvent,
                RepoType,
                EventPriority as BusPriority,
            )

            # Map our priority to bus priority
            priority_map = {
                EventPriority.CRITICAL: BusPriority.CRITICAL,
                EventPriority.HIGH: BusPriority.HIGH,
                EventPriority.NORMAL: BusPriority.NORMAL,
                EventPriority.LOW: BusPriority.LOW,
            }

            trinity_event = TrinityEvent(
                topic=event.event_type,
                source=RepoType.JARVIS,
                target=RepoType.BROADCAST,
                priority=priority_map.get(event.priority, BusPriority.NORMAL),
                payload=event.to_dict(),
                correlation_id=event.correlation_id,
            )

            await asyncio.wait_for(
                self._event_bus.publish(
                    trinity_event,
                    persist=LifecycleConfig.ENABLE_EVENT_PERSISTENCE,
                ),
                timeout=LifecycleConfig.PUBLISH_TIMEOUT_MS / 1000,
            )

            self._published_count += 1
            logger.debug(
                f"[LifecyclePublisher] Published {event.event_type} "
                f"for {event.component} (state={event.state.value})"
            )
            return True

        except asyncio.TimeoutError:
            self._failed_count += 1
            logger.warning(f"[LifecyclePublisher] Publish timeout for {event.event_type}")
            return False
        except Exception as e:
            self._failed_count += 1
            logger.warning(f"[LifecyclePublisher] Publish error: {e}")
            return False

    def on_event(self, callback: Callable[[LifecycleEvent], Awaitable[None]]) -> None:
        """Register a local event callback."""
        self._callbacks.append(callback)

    def get_stats(self) -> Dict[str, Any]:
        """Get publisher statistics."""
        return {
            "published_count": self._published_count,
            "failed_count": self._failed_count,
            "queue_size": self._event_queue.qsize(),
            "running": self._running,
            "connected": self._event_bus is not None,
        }


# =============================================================================
# Resilient Health Checker (Issue 15 Fix)
# =============================================================================

class ResilientHealthChecker:
    """
    Health checker with intelligent retry and backoff.

    Prevents aggressive recovery by:
    1. Applying exponential backoff between retries
    2. Requiring multiple consecutive failures before declaring unhealthy
    3. Supporting half-open circuit breaker for recovery testing
    4. Distinguishing transient failures from persistent ones
    """

    def __init__(self):
        self._backoff_states: Dict[str, BackoffState] = {}
        self._component_statuses: Dict[str, ComponentStatus] = {}
        self._aiohttp_session = None
        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable[[str, ComponentStatus], Awaitable[None]]] = []

    async def start(self) -> None:
        """Start the health checker."""
        if self._running:
            return

        self._running = True

        # Create aiohttp session
        try:
            import aiohttp
            self._aiohttp_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=LifecycleConfig.HEALTH_CHECK_TIMEOUT_MS / 1000
                )
            )
        except ImportError:
            logger.warning("[HealthChecker] aiohttp not available")

        logger.info("[HealthChecker] Started")

    async def stop(self) -> None:
        """Stop the health checker."""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        if self._aiohttp_session:
            await self._aiohttp_session.close()

    async def check_component_health(
        self,
        component: ComponentDefinition,
    ) -> Tuple[bool, ComponentStatus]:
        """
        Check health of a component with retry and backoff.

        Returns:
            Tuple of (is_healthy, status)
        """
        # Get or create status
        if component.name not in self._component_statuses:
            self._component_statuses[component.name] = ComponentStatus(name=component.name)
        status = self._component_statuses[component.name]

        # Get or create backoff state
        if component.name not in self._backoff_states:
            self._backoff_states[component.name] = BackoffState(
                current_delay_ms=LifecycleConfig.INITIAL_RETRY_DELAY_MS
            )
        backoff = self._backoff_states[component.name]

        # Perform health check based on strategy
        is_healthy = False
        error: Optional[str] = None

        try:
            if component.health_strategy == HealthCheckStrategy.HTTP:
                is_healthy, error = await self._check_http(component)
            elif component.health_strategy == HealthCheckStrategy.PROCESS:
                is_healthy, error = await self._check_process(component)
            elif component.health_strategy == HealthCheckStrategy.FILE:
                is_healthy, error = await self._check_file(component)
            elif component.health_strategy == HealthCheckStrategy.CUSTOM:
                is_healthy, error = await self._check_custom(component)
        except Exception as e:
            error = str(e)

        status.last_check = datetime.now()

        if is_healthy:
            # Healthy - update counters
            status.consecutive_healthy += 1
            status.consecutive_unhealthy = 0
            status.last_healthy = datetime.now()
            status.last_error = None
            backoff.reset()

            # Determine state based on consecutive healthy checks
            if status.state == ComponentState.RECOVERING:
                if status.consecutive_healthy >= LifecycleConfig.CONSECUTIVE_HEALTHY_FOR_RECOVERY:
                    status.state = ComponentState.READY
                    status.health_score = 1.0
                    logger.info(f"[HealthChecker] {component.name} recovered to READY")
            elif status.state in (ComponentState.UNHEALTHY, ComponentState.DEGRADED):
                status.state = ComponentState.RECOVERING
                logger.info(f"[HealthChecker] {component.name} entering RECOVERING state")
            else:
                status.state = ComponentState.READY
                status.health_score = 1.0

        else:
            # Unhealthy - apply backoff logic
            status.consecutive_unhealthy += 1
            status.consecutive_healthy = 0
            status.last_error = error
            status.retry_count += 1

            # Calculate health score decay
            max_retries = LifecycleConfig.MAX_RETRIES_BEFORE_UNHEALTHY
            status.health_score = max(0.0, 1.0 - (status.consecutive_unhealthy / max_retries))

            # Only declare unhealthy after multiple consecutive failures
            if status.consecutive_unhealthy >= max_retries:
                old_state = status.state
                status.state = ComponentState.UNHEALTHY

                if old_state != ComponentState.UNHEALTHY:
                    logger.warning(
                        f"[HealthChecker] {component.name} is UNHEALTHY after "
                        f"{status.consecutive_unhealthy} consecutive failures. "
                        f"Last error: {error}"
                    )
            elif status.consecutive_unhealthy >= max_retries // 2:
                status.state = ComponentState.DEGRADED
                logger.info(
                    f"[HealthChecker] {component.name} is DEGRADED "
                    f"({status.consecutive_unhealthy} failures)"
                )

            # Calculate backoff delay for next check
            next_delay = backoff.next_delay()
            status.metadata["next_check_delay_ms"] = next_delay

        # Notify callbacks
        for callback in self._callbacks:
            try:
                await callback(component.name, status)
            except Exception as e:
                logger.debug(f"[HealthChecker] Callback error: {e}")

        return is_healthy, status

    async def _check_http(
        self,
        component: ComponentDefinition,
    ) -> Tuple[bool, Optional[str]]:
        """Check HTTP endpoint health."""
        if not component.health_endpoint:
            return False, "No health endpoint configured"

        if not self._aiohttp_session:
            return False, "HTTP client not available"

        try:
            async with self._aiohttp_session.get(component.health_endpoint) as response:
                if response.status == 200:
                    return True, None
                elif response.status == 503:
                    # 503 = Not ready yet (expected during initialization)
                    return False, f"Not ready (503)"
                else:
                    return False, f"HTTP {response.status}"
        except asyncio.TimeoutError:
            return False, "Timeout"
        except Exception as e:
            return False, str(e)

    async def _check_process(
        self,
        component: ComponentDefinition,
    ) -> Tuple[bool, Optional[str]]:
        """Check if process is running."""
        try:
            import psutil

            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline', []) or []
                    if any(component.name.lower() in str(arg).lower() for arg in cmdline):
                        return True, None
                except Exception:
                    continue

            return False, "Process not found"
        except ImportError:
            return False, "psutil not available"
        except Exception as e:
            return False, str(e)

    async def _check_file(
        self,
        component: ComponentDefinition,
    ) -> Tuple[bool, Optional[str]]:
        """Check heartbeat file."""
        import json
        from pathlib import Path

        trinity_dir = os.getenv("TRINITY_DIR", os.path.expanduser("~/.jarvis/trinity"))
        heartbeat_file = Path(trinity_dir) / "components" / f"{component.name}.json"

        if not heartbeat_file.exists():
            return False, "Heartbeat file not found"

        try:
            with open(heartbeat_file) as f:
                data = json.load(f)

            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            if age < 30:  # Heartbeat within 30 seconds
                return True, None
            else:
                return False, f"Stale heartbeat ({age:.1f}s old)"
        except Exception as e:
            return False, str(e)

    async def _check_custom(
        self,
        component: ComponentDefinition,
    ) -> Tuple[bool, Optional[str]]:
        """Check using custom function."""
        if not component.health_check_func:
            return False, "No custom health check function"

        try:
            result = await component.health_check_func()
            return result, None if result else "Custom check failed"
        except Exception as e:
            return False, str(e)

    def on_status_change(
        self,
        callback: Callable[[str, ComponentStatus], Awaitable[None]],
    ) -> None:
        """Register status change callback."""
        self._callbacks.append(callback)

    def get_component_status(self, name: str) -> Optional[ComponentStatus]:
        """Get status for a specific component."""
        return self._component_statuses.get(name)

    def get_all_statuses(self) -> Dict[str, ComponentStatus]:
        """Get all component statuses."""
        return self._component_statuses.copy()


# =============================================================================
# Dependency Validator (Issue 16 Fix)
# =============================================================================

class DependencyValidator:
    """
    Validates component dependencies before startup.

    Ensures:
    1. Dependencies are started before dependents
    2. Dependencies are actually ready (not just started)
    3. Circular dependencies are detected and reported
    4. Proper startup ordering is calculated
    """

    def __init__(self, health_checker: ResilientHealthChecker):
        self._health_checker = health_checker
        self._components: Dict[str, ComponentDefinition] = {}
        self._startup_order: List[str] = []
        self._ready_components: Set[str] = set()

    def register_component(self, component: ComponentDefinition) -> None:
        """Register a component definition."""
        self._components[component.name] = component
        self._startup_order = []  # Invalidate cached order

    def get_startup_order(self) -> List[str]:
        """
        Calculate topological sort of components based on dependencies.

        Returns:
            List of component names in startup order

        Raises:
            ValueError: If circular dependency detected
        """
        if self._startup_order:
            return self._startup_order

        # Build dependency graph
        in_degree: Dict[str, int] = {name: 0 for name in self._components}
        dependents: Dict[str, List[str]] = {name: [] for name in self._components}

        for name, component in self._components.items():
            for dep in component.dependencies:
                if dep in self._components:
                    in_degree[name] += 1
                    dependents[dep].append(name)

        # Kahn's algorithm for topological sort
        queue: List[str] = [name for name, degree in in_degree.items() if degree == 0]
        result: List[str] = []

        while queue:
            # Sort by criticality (critical components first)
            queue.sort(key=lambda n: (0 if self._components[n].critical else 1, n))
            current = queue.pop(0)
            result.append(current)

            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._components):
            # Circular dependency detected
            remaining = [n for n in self._components if n not in result]
            raise ValueError(f"Circular dependency detected involving: {remaining}")

        self._startup_order = result
        return result

    async def wait_for_dependencies(
        self,
        component: ComponentDefinition,
        timeout_ms: Optional[int] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Wait for all dependencies of a component to be ready.

        Args:
            component: Component to check dependencies for
            timeout_ms: Maximum time to wait (default from config)

        Returns:
            Tuple of (all_ready, list_of_not_ready_deps)
        """
        if not component.dependencies:
            return True, []

        timeout = (timeout_ms or LifecycleConfig.DEPENDENCY_WAIT_TIMEOUT_MS) / 1000
        poll_interval = LifecycleConfig.DEPENDENCY_POLL_INTERVAL_MS / 1000

        start_time = time.time()
        not_ready: List[str] = []

        while time.time() - start_time < timeout:
            not_ready = []
            all_ready = True

            for dep_name in component.dependencies:
                # Check if dependency is registered
                if dep_name not in self._components:
                    logger.warning(
                        f"[DependencyValidator] {component.name} depends on "
                        f"unknown component: {dep_name}"
                    )
                    continue

                dep_component = self._components[dep_name]

                # Check if already marked ready
                if dep_name in self._ready_components:
                    continue

                # Check actual health
                _, status = await self._health_checker.check_component_health(
                    dep_component
                )

                if status.is_ready:
                    self._ready_components.add(dep_name)
                else:
                    not_ready.append(dep_name)
                    all_ready = False

            if all_ready:
                logger.info(
                    f"[DependencyValidator] All dependencies ready for {component.name}"
                )
                return True, []

            # Wait before next poll
            await asyncio.sleep(poll_interval)

        # Timeout reached
        logger.warning(
            f"[DependencyValidator] Timeout waiting for dependencies of {component.name}: "
            f"{not_ready}"
        )
        return False, not_ready

    def mark_ready(self, component_name: str) -> None:
        """Mark a component as ready."""
        self._ready_components.add(component_name)

    def mark_not_ready(self, component_name: str) -> None:
        """Mark a component as not ready."""
        self._ready_components.discard(component_name)

    def get_ready_components(self) -> Set[str]:
        """Get set of ready component names."""
        return self._ready_components.copy()

    def get_dependency_graph(self) -> Dict[str, List[str]]:
        """Get the dependency graph for visualization."""
        return {
            name: comp.dependencies
            for name, comp in self._components.items()
        }


# =============================================================================
# Lifecycle Event Orchestrator (Main Class)
# =============================================================================

class LifecycleEventOrchestrator:
    """
    Main orchestrator that combines event publishing, health checking,
    and dependency validation into a unified lifecycle management system.
    """

    def __init__(self):
        self._publisher = LifecycleEventPublisher()
        self._health_checker = ResilientHealthChecker()
        self._dependency_validator = DependencyValidator(self._health_checker)

        self._running = False
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitored_components: Dict[str, ComponentDefinition] = {}

    async def start(self) -> None:
        """Start the lifecycle orchestrator."""
        if self._running:
            return

        self._running = True

        # Start sub-components
        await self._publisher.start()
        await self._health_checker.start()

        # Register status change callback to publish events
        self._health_checker.on_status_change(self._on_component_status_change)

        logger.info("[LifecycleOrchestrator] Started")

    async def stop(self) -> None:
        """Stop the lifecycle orchestrator."""
        self._running = False

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        await self._publisher.stop()
        await self._health_checker.stop()

        logger.info("[LifecycleOrchestrator] Stopped")

    def register_component(self, component: ComponentDefinition) -> None:
        """Register a component for lifecycle management."""
        self._monitored_components[component.name] = component
        self._dependency_validator.register_component(component)
        logger.debug(f"[LifecycleOrchestrator] Registered component: {component.name}")

    async def start_component(
        self,
        component_name: str,
        start_func: Callable[[], Awaitable[bool]],
    ) -> bool:
        """
        Start a component with proper lifecycle management.

        This method:
        1. Waits for all dependencies to be ready
        2. Publishes starting event
        3. Calls the start function
        4. Validates the component is actually ready
        5. Publishes ready/failed event

        Args:
            component_name: Name of component to start
            start_func: Async function that starts the component

        Returns:
            True if component started successfully
        """
        if component_name not in self._monitored_components:
            logger.error(f"[LifecycleOrchestrator] Unknown component: {component_name}")
            return False

        component = self._monitored_components[component_name]

        # Publish starting event
        await self._publisher.publish(
            event_type="lifecycle.component.starting",
            component=component_name,
            state=ComponentState.STARTING,
            priority=EventPriority.HIGH,
            payload={"dependencies": component.dependencies},
        )

        # Wait for dependencies
        deps_ready, not_ready = await self._dependency_validator.wait_for_dependencies(
            component
        )

        if not deps_ready:
            await self._publisher.publish(
                event_type="lifecycle.component.failed",
                component=component_name,
                state=ComponentState.FAILED,
                priority=EventPriority.CRITICAL,
                payload={
                    "reason": "dependency_timeout",
                    "not_ready_dependencies": not_ready,
                },
            )
            return False

        # Publish waiting -> initializing transition
        await self._publisher.publish(
            event_type="lifecycle.component.initializing",
            component=component_name,
            state=ComponentState.INITIALIZING,
            priority=EventPriority.NORMAL,
        )

        # Call start function
        try:
            success = await asyncio.wait_for(
                start_func(),
                timeout=component.startup_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            await self._publisher.publish(
                event_type="lifecycle.component.failed",
                component=component_name,
                state=ComponentState.FAILED,
                priority=EventPriority.CRITICAL,
                payload={"reason": "startup_timeout"},
            )
            return False
        except Exception as e:
            await self._publisher.publish(
                event_type="lifecycle.component.failed",
                component=component_name,
                state=ComponentState.FAILED,
                priority=EventPriority.CRITICAL,
                payload={"reason": "startup_error", "error": str(e)},
            )
            return False

        if not success:
            await self._publisher.publish(
                event_type="lifecycle.component.failed",
                component=component_name,
                state=ComponentState.FAILED,
                priority=EventPriority.CRITICAL,
                payload={"reason": "startup_returned_false"},
            )
            return False

        # Validate component is actually ready via health check
        is_healthy, status = await self._health_checker.check_component_health(component)

        if is_healthy:
            self._dependency_validator.mark_ready(component_name)

            await self._publisher.publish(
                event_type="lifecycle.component.ready",
                component=component_name,
                state=ComponentState.READY,
                priority=EventPriority.HIGH,
                payload={
                    "startup_duration_ms": status.startup_duration_ms,
                    "health_score": status.health_score,
                },
            )

            logger.info(f"[LifecycleOrchestrator] Component {component_name} is READY")
            return True
        else:
            await self._publisher.publish(
                event_type="lifecycle.component.failed",
                component=component_name,
                state=ComponentState.FAILED,
                priority=EventPriority.CRITICAL,
                payload={
                    "reason": "health_check_failed",
                    "error": status.last_error,
                },
            )
            return False

    async def shutdown_component(self, component_name: str) -> None:
        """Gracefully shutdown a component."""
        await self._publisher.publish(
            event_type="lifecycle.component.shutdown",
            component=component_name,
            state=ComponentState.SHUTTING_DOWN,
            priority=EventPriority.HIGH,
        )

        self._dependency_validator.mark_not_ready(component_name)

    async def start_monitoring(self) -> None:
        """Start continuous health monitoring for all registered components."""
        if self._monitoring_task:
            return

        self._monitoring_task = asyncio.create_task(self._monitoring_loop())

    async def _monitoring_loop(self) -> None:
        """Background loop for continuous health monitoring."""
        interval = LifecycleConfig.HEALTH_CHECK_INTERVAL_MS / 1000

        while self._running:
            try:
                for name, component in self._monitored_components.items():
                    if name in self._dependency_validator.get_ready_components():
                        await self._health_checker.check_component_health(component)

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LifecycleOrchestrator] Monitoring error: {e}")
                await asyncio.sleep(5.0)

    async def _on_component_status_change(
        self,
        component_name: str,
        status: ComponentStatus,
    ) -> None:
        """Handle component status changes and publish appropriate events."""
        # Determine event type based on state transition
        event_type = None
        priority = EventPriority.NORMAL

        if status.state == ComponentState.RECOVERED:
            event_type = "lifecycle.component.recovered"
            priority = EventPriority.HIGH
        elif status.state == ComponentState.UNHEALTHY:
            event_type = "lifecycle.component.unhealthy"
            priority = EventPriority.CRITICAL
            self._dependency_validator.mark_not_ready(component_name)
        elif status.state == ComponentState.DEGRADED:
            event_type = "lifecycle.component.degraded"
            priority = EventPriority.HIGH

        if event_type:
            await self._publisher.publish(
                event_type=event_type,
                component=component_name,
                state=status.state,
                priority=priority,
                payload={
                    "health_score": status.health_score,
                    "consecutive_unhealthy": status.consecutive_unhealthy,
                    "last_error": status.last_error,
                },
            )

    def get_startup_order(self) -> List[str]:
        """Get the calculated startup order."""
        return self._dependency_validator.get_startup_order()

    def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status."""
        statuses = self._health_checker.get_all_statuses()
        ready_count = len(self._dependency_validator.get_ready_components())
        total_count = len(self._monitored_components)

        # Calculate overall health
        health_scores = [s.health_score for s in statuses.values() if s.health_score > 0]
        avg_health = sum(health_scores) / len(health_scores) if health_scores else 0.0

        # Determine system state
        critical_unhealthy = any(
            s.state == ComponentState.UNHEALTHY
            and self._monitored_components.get(s.name, ComponentDefinition("")).critical
            for s in statuses.values()
        )

        if critical_unhealthy:
            system_state = "failed"
        elif ready_count == total_count:
            system_state = "ready"
        elif ready_count > 0:
            system_state = "degraded"
        else:
            system_state = "starting"

        return {
            "state": system_state,
            "ready_components": ready_count,
            "total_components": total_count,
            "average_health_score": avg_health,
            "components": {
                name: {
                    "state": status.state.value,
                    "health_score": status.health_score,
                    "last_error": status.last_error,
                }
                for name, status in statuses.items()
            },
            "publisher_stats": self._publisher.get_stats(),
        }


# =============================================================================
# Global Instance
# =============================================================================

_orchestrator: Optional[LifecycleEventOrchestrator] = None


async def get_lifecycle_orchestrator() -> LifecycleEventOrchestrator:
    """Get or create the global lifecycle orchestrator."""
    global _orchestrator

    if _orchestrator is None:
        _orchestrator = LifecycleEventOrchestrator()
        await _orchestrator.start()

    return _orchestrator


async def shutdown_lifecycle_orchestrator() -> None:
    """Shutdown the global lifecycle orchestrator."""
    global _orchestrator

    if _orchestrator:
        await _orchestrator.stop()
        _orchestrator = None


# =============================================================================
# Convenience Functions
# =============================================================================

async def publish_lifecycle_event(
    event_type: str,
    component: str,
    state: ComponentState,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Convenience function to publish a lifecycle event."""
    orchestrator = await get_lifecycle_orchestrator()
    await orchestrator._publisher.publish(
        event_type=event_type,
        component=component,
        state=state,
        payload=payload,
    )


async def register_component_for_monitoring(
    name: str,
    dependencies: Optional[List[str]] = None,
    health_endpoint: Optional[str] = None,
    critical: bool = True,
) -> None:
    """Convenience function to register a component for monitoring."""
    orchestrator = await get_lifecycle_orchestrator()

    component = ComponentDefinition(
        name=name,
        dependencies=dependencies or [],
        health_strategy=HealthCheckStrategy.HTTP if health_endpoint else HealthCheckStrategy.FILE,
        health_endpoint=health_endpoint,
        critical=critical,
    )

    orchestrator.register_component(component)

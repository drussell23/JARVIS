"""
Trinity Heartbeat Publisher v81.0 - Symmetric Heartbeat System
=============================================================

Provides symmetric heartbeat publishing for all Trinity components:
- JARVIS Body (must publish, not just read)
- JARVIS Prime (publishes via subprocess)
- Reactor Core (publishes via subprocess)

FEATURES:
    - Background heartbeat publishing with configurable interval
    - Automatic status detection based on dependencies
    - Graceful shutdown with "stopping" heartbeat
    - Metrics collection for monitoring
    - PID and host tracking for process validation

ZERO HARDCODING - All configuration via environment variables:
    TRINITY_HEARTBEAT_INTERVAL      - Publishing interval (default: 5.0s)
    TRINITY_HEARTBEAT_WARMUP        - Warmup period before "ready" (default: 3.0s)
    TRINITY_HEARTBEAT_TIMEOUT       - Max age for valid heartbeat (default: 15.0s)

Author: JARVIS v81.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from .trinity_ipc import (
    ComponentType,
    HeartbeatData,
    TrinityIPCBus,
    TrinityIPCConfig,
    get_trinity_ipc_bus,
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


def _env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class HeartbeatConfig:
    """Configuration for heartbeat publishing."""

    # Timing
    interval: float = field(default_factory=lambda: _env_float(
        "TRINITY_HEARTBEAT_INTERVAL", 5.0
    ))
    warmup_seconds: float = field(default_factory=lambda: _env_float(
        "TRINITY_HEARTBEAT_WARMUP", 3.0
    ))
    timeout: float = field(default_factory=lambda: _env_float(
        "TRINITY_HEARTBEAT_TIMEOUT", 15.0
    ))

    # Feature flags
    include_metrics: bool = field(default_factory=lambda: _env_bool(
        "TRINITY_HEARTBEAT_INCLUDE_METRICS", True
    ))
    auto_detect_status: bool = field(default_factory=lambda: _env_bool(
        "TRINITY_HEARTBEAT_AUTO_STATUS", True
    ))


# =============================================================================
# COMPONENT STATUS
# =============================================================================


class ComponentStatus(str, Enum):
    """Status values for heartbeat publishing."""
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"


# =============================================================================
# METRICS COLLECTOR
# =============================================================================


class HeartbeatMetricsCollector:
    """
    Collects metrics for heartbeat publishing.

    Metrics include:
    - Memory usage
    - CPU usage (if available)
    - Request counts (if registered)
    - Custom metrics
    """

    def __init__(self):
        self._custom_metrics: Dict[str, Any] = {}
        self._counters: Dict[str, int] = {}
        self._start_time = time.time()

    def set_metric(self, key: str, value: Any) -> None:
        """Set a custom metric."""
        self._custom_metrics[key] = value

    def increment_counter(self, key: str, amount: int = 1) -> None:
        """Increment a counter metric."""
        self._counters[key] = self._counters.get(key, 0) + amount

    def get_counter(self, key: str) -> int:
        """Get counter value."""
        return self._counters.get(key, 0)

    def collect(self) -> Dict[str, Any]:
        """Collect all metrics."""
        metrics = {
            "uptime_seconds": time.time() - self._start_time,
            **self._custom_metrics,
            "counters": self._counters.copy(),
        }

        # Try to add system metrics
        try:
            import resource
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            metrics["memory_mb"] = rusage.ru_maxrss / 1024  # KB to MB on Linux
            if platform.system() == "Darwin":
                metrics["memory_mb"] = rusage.ru_maxrss / (1024 * 1024)  # bytes to MB on macOS
        except Exception:
            pass

        return metrics


# =============================================================================
# HEARTBEAT PUBLISHER
# =============================================================================


class HeartbeatPublisher:
    """
    Publishes heartbeats for a Trinity component.

    Must be started by JARVIS Body, J-Prime, AND Reactor-Core to enable
    symmetric health monitoring.

    Usage:
        publisher = HeartbeatPublisher(
            component_type=ComponentType.JARVIS_BODY,
        )

        # Start publishing
        await publisher.start()

        # Set status manually if needed
        publisher.set_status(ComponentStatus.READY)

        # Add custom metrics
        publisher.metrics.set_metric("active_connections", 42)

        # Stop publishing (sends final 'stopping' heartbeat)
        await publisher.stop()
    """

    def __init__(
        self,
        component_type: ComponentType,
        config: Optional[HeartbeatConfig] = None,
        ipc_bus: Optional[TrinityIPCBus] = None,
    ):
        self._component_type = component_type
        self._config = config or HeartbeatConfig()
        self._ipc_bus = ipc_bus

        # State
        self._status = ComponentStatus.STARTING
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._start_time = time.time()
        self._heartbeat_count = 0

        # Dependencies
        self._dependencies: Set[ComponentType] = set()
        self._dependencies_ready: Dict[str, bool] = {}

        # Metrics
        self.metrics = HeartbeatMetricsCollector()

        # Status callbacks
        self._status_callbacks: List[Callable[[ComponentStatus], None]] = []

    @property
    def component_type(self) -> ComponentType:
        return self._component_type

    @property
    def status(self) -> ComponentStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    @property
    def heartbeat_count(self) -> int:
        return self._heartbeat_count

    def set_status(self, status: ComponentStatus) -> None:
        """Set component status manually."""
        old_status = self._status
        self._status = status

        if old_status != status:
            logger.info(
                f"[HeartbeatPublisher] {self._component_type.value} "
                f"status: {old_status.value} -> {status.value}"
            )

            # Notify callbacks
            for callback in self._status_callbacks:
                try:
                    callback(status)
                except Exception as e:
                    logger.error(f"[HeartbeatPublisher] Callback error: {e}")

    def add_dependency(self, component: ComponentType) -> None:
        """Add a dependency that must be ready for this component to be ready."""
        self._dependencies.add(component)

    def remove_dependency(self, component: ComponentType) -> None:
        """Remove a dependency."""
        self._dependencies.discard(component)

    def set_dependency_ready(self, component: ComponentType, ready: bool) -> None:
        """Set dependency ready state."""
        self._dependencies_ready[component.value] = ready

    def register_status_callback(
        self,
        callback: Callable[[ComponentStatus], None]
    ) -> None:
        """Register callback for status changes."""
        self._status_callbacks.append(callback)

    async def start(self) -> None:
        """Start publishing heartbeats."""
        if self._running:
            logger.warning(
                f"[HeartbeatPublisher] {self._component_type.value} already running"
            )
            return

        # Get IPC bus if not provided
        if self._ipc_bus is None:
            self._ipc_bus = await get_trinity_ipc_bus()

        self._running = True
        self._start_time = time.time()
        self._task = asyncio.create_task(self._heartbeat_loop())

        logger.info(
            f"[HeartbeatPublisher] Started for {self._component_type.value} "
            f"(interval={self._config.interval}s)"
        )

    async def stop(self) -> None:
        """
        Stop publishing heartbeats.

        Sends a final 'stopping' heartbeat before shutdown.
        """
        if not self._running:
            return

        self._running = False

        # Send final stopping heartbeat
        try:
            self.set_status(ComponentStatus.STOPPING)
            await self._publish_heartbeat()
        except Exception as e:
            logger.debug(f"[HeartbeatPublisher] Final heartbeat error: {e}")

        # Cancel task
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info(
            f"[HeartbeatPublisher] Stopped for {self._component_type.value} "
            f"(sent {self._heartbeat_count} heartbeats)"
        )

    async def _heartbeat_loop(self) -> None:
        """Background loop that publishes heartbeats."""
        try:
            while self._running:
                # Auto-detect status if enabled
                if self._config.auto_detect_status:
                    self._auto_detect_status()

                # Publish heartbeat
                await self._publish_heartbeat()

                # Wait for next interval
                await asyncio.sleep(self._config.interval)

        except asyncio.CancelledError:
            logger.debug(f"[HeartbeatPublisher] Loop cancelled")
        except Exception as e:
            logger.error(f"[HeartbeatPublisher] Loop error: {e}")

    def _auto_detect_status(self) -> None:
        """Automatically detect status based on conditions."""
        # Still in warmup period
        if self.uptime_seconds < self._config.warmup_seconds:
            if self._status != ComponentStatus.STARTING:
                self.set_status(ComponentStatus.STARTING)
            return

        # Check dependencies
        if self._dependencies:
            all_ready = all(
                self._dependencies_ready.get(dep.value, False)
                for dep in self._dependencies
            )
            if not all_ready:
                if self._status == ComponentStatus.READY:
                    self.set_status(ComponentStatus.DEGRADED)
                return

        # All conditions met - mark as ready
        if self._status == ComponentStatus.STARTING:
            self.set_status(ComponentStatus.READY)

    async def _publish_heartbeat(self) -> None:
        """Publish a single heartbeat."""
        if not self._ipc_bus:
            return

        try:
            # Collect metrics if enabled
            metrics = {}
            if self._config.include_metrics:
                metrics = self.metrics.collect()

            # Publish via IPC bus
            await self._ipc_bus.publish_heartbeat(
                component=self._component_type,
                status=self._status.value,
                pid=os.getpid(),
                metrics=metrics,
                dependencies_ready=self._dependencies_ready,
            )

            self._heartbeat_count += 1

        except Exception as e:
            logger.debug(f"[HeartbeatPublisher] Publish error: {e}")


# =============================================================================
# HEARTBEAT SUBSCRIBER
# =============================================================================


class HeartbeatSubscriber:
    """
    Subscribes to heartbeats from Trinity components.

    Provides:
    - Real-time component status monitoring
    - Callback notifications on status changes
    - Aggregate health computation

    Usage:
        subscriber = HeartbeatSubscriber()

        # Register callback for status changes
        subscriber.on_status_change(
            ComponentType.JARVIS_PRIME,
            lambda status: print(f"J-Prime is now {status}")
        )

        # Start monitoring
        await subscriber.start()

        # Check status
        status = await subscriber.get_status(ComponentType.JARVIS_PRIME)
    """

    def __init__(
        self,
        config: Optional[HeartbeatConfig] = None,
        ipc_bus: Optional[TrinityIPCBus] = None,
    ):
        self._config = config or HeartbeatConfig()
        self._ipc_bus = ipc_bus

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_status: Dict[ComponentType, str] = {}

        # Callbacks
        self._status_callbacks: Dict[
            ComponentType,
            List[Callable[[str], None]]
        ] = {}
        self._any_change_callbacks: List[
            Callable[[ComponentType, str, str], None]
        ] = []

    def on_status_change(
        self,
        component: ComponentType,
        callback: Callable[[str], None],
    ) -> None:
        """Register callback for component status changes."""
        if component not in self._status_callbacks:
            self._status_callbacks[component] = []
        self._status_callbacks[component].append(callback)

    def on_any_change(
        self,
        callback: Callable[[ComponentType, str, str], None],
    ) -> None:
        """Register callback for any component status change."""
        self._any_change_callbacks.append(callback)

    async def start(self) -> None:
        """Start monitoring heartbeats."""
        if self._running:
            return

        if self._ipc_bus is None:
            self._ipc_bus = await get_trinity_ipc_bus()

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

        logger.info("[HeartbeatSubscriber] Started monitoring")

    async def stop(self) -> None:
        """Stop monitoring."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("[HeartbeatSubscriber] Stopped monitoring")

    async def get_status(self, component: ComponentType) -> str:
        """Get current status of a component."""
        if self._ipc_bus is None:
            self._ipc_bus = await get_trinity_ipc_bus()

        heartbeat = await self._ipc_bus.read_heartbeat(
            component,
            max_age_seconds=self._config.timeout,
        )

        if heartbeat is None:
            return "offline"

        return heartbeat.status

    async def get_all_statuses(self) -> Dict[ComponentType, str]:
        """Get status of all components."""
        statuses = {}
        for component in ComponentType:
            statuses[component] = await self.get_status(component)
        return statuses

    async def get_health_score(self) -> float:
        """
        Compute aggregate health score (0.0 - 1.0).

        Weights:
        - JARVIS_BODY: 1.0 (critical)
        - JARVIS_PRIME: 0.7 (important)
        - REACTOR_CORE: 0.7 (important)
        - CODING_COUNCIL: 0.3 (optional)
        """
        weights = {
            ComponentType.JARVIS_BODY: 1.0,
            ComponentType.JARVIS_PRIME: 0.7,
            ComponentType.REACTOR_CORE: 0.7,
            ComponentType.CODING_COUNCIL: 0.3,
        }

        statuses = await self.get_all_statuses()

        total_weight = sum(weights.values())
        weighted_score = 0.0

        for component, weight in weights.items():
            status = statuses.get(component, "offline")
            if status == "ready":
                weighted_score += weight * 1.0
            elif status == "degraded":
                weighted_score += weight * 0.5
            elif status == "starting":
                weighted_score += weight * 0.3
            # offline, error, stopping = 0.0

        return weighted_score / total_weight

    async def wait_for_ready(
        self,
        component: ComponentType,
        timeout: float = 60.0,
    ) -> bool:
        """
        Wait for a component to become ready.

        Args:
            component: Component to wait for
            timeout: Maximum wait time

        Returns:
            True if component became ready, False if timeout
        """
        if self._ipc_bus is None:
            self._ipc_bus = await get_trinity_ipc_bus()

        start_time = time.time()

        while time.time() - start_time < timeout:
            status = await self.get_status(component)
            if status == "ready":
                return True

            await asyncio.sleep(0.5)

        return False

    async def wait_for_all_ready(
        self,
        components: List[ComponentType],
        timeout: float = 60.0,
    ) -> Dict[ComponentType, bool]:
        """
        Wait for multiple components to become ready.

        Returns:
            Dict mapping component to whether it became ready
        """
        results = {}
        remaining = set(components)
        start_time = time.time()

        while remaining and (time.time() - start_time < timeout):
            for component in list(remaining):
                status = await self.get_status(component)
                if status == "ready":
                    results[component] = True
                    remaining.discard(component)

            if remaining:
                await asyncio.sleep(0.5)

        # Mark remaining as not ready
        for component in remaining:
            results[component] = False

        return results

    async def _monitor_loop(self) -> None:
        """Background loop that monitors heartbeats."""
        try:
            while self._running:
                await self._check_heartbeats()
                await asyncio.sleep(self._config.interval / 2)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[HeartbeatSubscriber] Monitor error: {e}")

    async def _check_heartbeats(self) -> None:
        """Check all component heartbeats for changes."""
        for component in ComponentType:
            status = await self.get_status(component)
            old_status = self._last_status.get(component)

            if old_status != status:
                self._last_status[component] = status

                # Notify component-specific callbacks
                for callback in self._status_callbacks.get(component, []):
                    try:
                        callback(status)
                    except Exception as e:
                        logger.error(f"[HeartbeatSubscriber] Callback error: {e}")

                # Notify any-change callbacks
                for callback in self._any_change_callbacks:
                    try:
                        callback(component, old_status or "unknown", status)
                    except Exception as e:
                        logger.error(f"[HeartbeatSubscriber] Callback error: {e}")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


_publisher: Optional[HeartbeatPublisher] = None
_subscriber: Optional[HeartbeatSubscriber] = None


async def start_heartbeat_publishing(
    component_type: ComponentType,
    config: Optional[HeartbeatConfig] = None,
) -> HeartbeatPublisher:
    """
    Start heartbeat publishing for a component.

    Returns the global publisher instance.
    """
    global _publisher

    if _publisher is not None:
        logger.warning("[Heartbeat] Publisher already running")
        return _publisher

    _publisher = HeartbeatPublisher(
        component_type=component_type,
        config=config,
    )
    await _publisher.start()

    return _publisher


async def stop_heartbeat_publishing() -> None:
    """Stop heartbeat publishing."""
    global _publisher

    if _publisher is not None:
        await _publisher.stop()
        _publisher = None


def get_heartbeat_publisher() -> Optional[HeartbeatPublisher]:
    """Get the global heartbeat publisher."""
    return _publisher


async def start_heartbeat_monitoring(
    config: Optional[HeartbeatConfig] = None,
) -> HeartbeatSubscriber:
    """Start heartbeat monitoring."""
    global _subscriber

    if _subscriber is not None:
        return _subscriber

    _subscriber = HeartbeatSubscriber(config=config)
    await _subscriber.start()

    return _subscriber


async def stop_heartbeat_monitoring() -> None:
    """Stop heartbeat monitoring."""
    global _subscriber

    if _subscriber is not None:
        await _subscriber.stop()
        _subscriber = None


def get_heartbeat_subscriber() -> Optional[HeartbeatSubscriber]:
    """Get the global heartbeat subscriber."""
    return _subscriber

"""
JARVIS Neural Mesh - Advanced Health Monitor

Comprehensive health monitoring system for the Neural Mesh infrastructure.

Features:
- Component health tracking
- Anomaly detection
- Automatic recovery actions
- Health history and trending
- Alert management
- Dependency health checking
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    """Health status levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    RECOVERING = "recovering"


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class HealthCheck:
    """Definition of a health check."""
    name: str
    check_fn: Callable[[], Awaitable[HealthCheckResult]]
    interval_seconds: float = 30.0
    timeout_seconds: float = 10.0
    failure_threshold: int = 3
    recovery_threshold: int = 2
    dependencies: List[str] = field(default_factory=list)
    critical: bool = False  # If true, failing this check marks system unhealthy

    def __hash__(self) -> int:
        return hash(self.name)


@dataclass
class HealthCheckResult:
    """Result of a health check execution."""
    status: HealthStatus
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY


@dataclass
class ComponentHealth:
    """Health state of a component."""
    name: str
    status: HealthStatus = HealthStatus.UNKNOWN
    last_check: Optional[datetime] = None
    last_healthy: Optional[datetime] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_checks: int = 0
    total_failures: int = 0
    avg_latency_ms: float = 0.0
    last_error: Optional[str] = None
    history: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_result(self, result: HealthCheckResult) -> None:
        """Record a health check result."""
        self.last_check = result.timestamp
        self.total_checks += 1
        self.history.append(result)

        # Update latency rolling average
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = result.latency_ms
        else:
            self.avg_latency_ms = (self.avg_latency_ms * 0.9) + (result.latency_ms * 0.1)

        if result.is_healthy:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            self.last_healthy = result.timestamp
            self.last_error = None
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.total_failures += 1
            self.last_error = result.message

    def get_uptime_percentage(self, window_seconds: float = 3600) -> float:
        """Calculate uptime percentage over a time window."""
        if not self.history:
            return 0.0

        cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
        recent = [r for r in self.history if r.timestamp >= cutoff]

        if not recent:
            return 0.0

        healthy_count = sum(1 for r in recent if r.is_healthy)
        return (healthy_count / len(recent)) * 100

    def get_latency_stats(self, window_seconds: float = 300) -> Dict[str, float]:
        """Get latency statistics over a time window."""
        cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
        latencies = [
            r.latency_ms for r in self.history
            if r.timestamp >= cutoff and r.latency_ms > 0
        ]

        if not latencies:
            return {"min": 0, "max": 0, "avg": 0, "p95": 0}

        sorted_latencies = sorted(latencies)
        return {
            "min": min(latencies),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
            "p95": sorted_latencies[int(len(latencies) * 0.95)] if len(latencies) >= 20 else max(latencies),
        }


@dataclass
class HealthAlert:
    """A health alert."""
    id: str
    severity: AlertSeverity
    component: str
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    details: Dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """
    Advanced health monitoring system for Neural Mesh.

    Provides comprehensive health monitoring with:
    - Automatic health check scheduling
    - Dependency-aware checks
    - Anomaly detection
    - Alert management
    - Recovery actions

    Example:
        monitor = HealthMonitor()
        await monitor.start()

        # Register a health check
        async def check_database():
            try:
                await db.ping()
                return HealthCheckResult(status=HealthStatus.HEALTHY)
            except Exception as e:
                return HealthCheckResult(
                    status=HealthStatus.UNHEALTHY,
                    message=str(e)
                )

        monitor.register_check(HealthCheck(
            name="database",
            check_fn=check_database,
            interval_seconds=30,
            critical=True,
        ))

        # Get overall health
        health = await monitor.get_system_health()
    """

    def __init__(
        self,
        check_interval: float = 10.0,
        alert_retention_hours: float = 24.0,
    ) -> None:
        """Initialize the health monitor.

        Args:
            check_interval: Default interval between checks
            alert_retention_hours: How long to retain resolved alerts
        """
        self._checks: Dict[str, HealthCheck] = {}
        self._component_health: Dict[str, ComponentHealth] = {}
        self._alerts: Dict[str, HealthAlert] = {}
        self._alert_handlers: List[Callable[[HealthAlert], Awaitable[None]]] = []
        self._recovery_handlers: Dict[str, Callable[[], Awaitable[bool]]] = {}

        self._check_interval = check_interval
        self._alert_retention_hours = alert_retention_hours

        self._running = False
        self._check_tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

        # Anomaly detection
        self._latency_baselines: Dict[str, float] = {}
        self._anomaly_threshold = 3.0  # Standard deviations

    async def start(self) -> None:
        """Start the health monitor."""
        if self._running:
            return

        self._running = True

        # Start check tasks for all registered checks
        for check in self._checks.values():
            self._start_check_task(check)

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="health_monitor_cleanup"
        )

        logger.info("HealthMonitor started with %d checks", len(self._checks))

    async def stop(self) -> None:
        """Stop the health monitor."""
        self._running = False

        # Cancel all check tasks
        for task in self._check_tasks.values():
            task.cancel()

        if self._check_tasks:
            await asyncio.gather(*self._check_tasks.values(), return_exceptions=True)

        self._check_tasks.clear()

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        logger.info("HealthMonitor stopped")

    def register_check(self, check: HealthCheck) -> None:
        """Register a health check.

        Args:
            check: The health check to register
        """
        self._checks[check.name] = check
        self._component_health[check.name] = ComponentHealth(name=check.name)

        # Start task if already running
        if self._running:
            self._start_check_task(check)

        logger.debug("Registered health check: %s", check.name)

    def unregister_check(self, name: str) -> bool:
        """Unregister a health check.

        Args:
            name: Name of the check to unregister

        Returns:
            True if check was unregistered
        """
        if name not in self._checks:
            return False

        # Cancel task
        if name in self._check_tasks:
            self._check_tasks[name].cancel()
            del self._check_tasks[name]

        del self._checks[name]
        del self._component_health[name]

        logger.debug("Unregistered health check: %s", name)
        return True

    def register_recovery_handler(
        self,
        component: str,
        handler: Callable[[], Awaitable[bool]],
    ) -> None:
        """Register a recovery handler for a component.

        Args:
            component: Component name
            handler: Async function that attempts recovery, returns success
        """
        self._recovery_handlers[component] = handler

    def add_alert_handler(
        self,
        handler: Callable[[HealthAlert], Awaitable[None]],
    ) -> None:
        """Add an alert handler.

        Args:
            handler: Async function to call when alerts are raised
        """
        self._alert_handlers.append(handler)

    def _start_check_task(self, check: HealthCheck) -> None:
        """Start the periodic check task for a health check."""
        if check.name in self._check_tasks:
            return

        task = asyncio.create_task(
            self._check_loop(check),
            name=f"health_check_{check.name}"
        )
        self._check_tasks[check.name] = task

    async def _check_loop(self, check: HealthCheck) -> None:
        """Periodic health check loop."""
        while self._running:
            try:
                await self._execute_check(check)
                await asyncio.sleep(check.interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in health check %s: %s", check.name, e)
                await asyncio.sleep(check.interval_seconds)

    async def _execute_check(self, check: HealthCheck) -> HealthCheckResult:
        """Execute a single health check."""
        component = self._component_health[check.name]

        # Check dependencies first
        for dep_name in check.dependencies:
            dep = self._component_health.get(dep_name)
            if dep and dep.status == HealthStatus.UNHEALTHY:
                result = HealthCheckResult(
                    status=HealthStatus.UNKNOWN,
                    message=f"Dependency {dep_name} is unhealthy",
                )
                component.record_result(result)
                return result

        # Execute check with timeout
        start_time = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                check.check_fn(),
                timeout=check.timeout_seconds,
            )
            result.latency_ms = (time.perf_counter() - start_time) * 1000
        except asyncio.TimeoutError:
            result = HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Health check timed out after {check.timeout_seconds}s",
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )
        except Exception as e:
            result = HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        # Record result
        component.record_result(result)

        # Update status based on thresholds
        await self._update_component_status(check, component)

        # Check for anomalies
        await self._check_anomalies(check.name, result)

        return result

    async def _update_component_status(
        self,
        check: HealthCheck,
        component: ComponentHealth,
    ) -> None:
        """Update component status and trigger alerts/recovery."""
        old_status = component.status

        # Determine new status
        if component.consecutive_successes >= check.recovery_threshold:
            new_status = HealthStatus.HEALTHY
        elif component.consecutive_failures >= check.failure_threshold:
            new_status = HealthStatus.UNHEALTHY
        elif component.consecutive_failures > 0:
            new_status = HealthStatus.DEGRADED
        elif component.status == HealthStatus.RECOVERING:
            new_status = HealthStatus.RECOVERING
        else:
            new_status = old_status

        component.status = new_status

        # Handle status transitions
        if old_status != new_status:
            logger.info(
                "Component %s status changed: %s -> %s",
                check.name, old_status.value, new_status.value
            )

            # Raise alert on degradation
            if new_status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY):
                await self._raise_alert(
                    component=check.name,
                    severity=AlertSeverity.CRITICAL if new_status == HealthStatus.UNHEALTHY else AlertSeverity.WARNING,
                    message=f"Component {check.name} is {new_status.value}: {component.last_error}",
                )

                # Attempt recovery
                if new_status == HealthStatus.UNHEALTHY:
                    await self._attempt_recovery(check.name)

            # Resolve alerts on recovery
            elif old_status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY) and new_status == HealthStatus.HEALTHY:
                await self._resolve_component_alerts(check.name)

    async def _check_anomalies(
        self,
        component: str,
        result: HealthCheckResult,
    ) -> None:
        """Check for anomalous behavior."""
        if result.latency_ms <= 0:
            return

        # Establish baseline
        if component not in self._latency_baselines:
            self._latency_baselines[component] = result.latency_ms
            return

        baseline = self._latency_baselines[component]

        # Check for latency spike
        if result.latency_ms > baseline * self._anomaly_threshold:
            await self._raise_alert(
                component=component,
                severity=AlertSeverity.WARNING,
                message=f"Latency spike detected: {result.latency_ms:.1f}ms (baseline: {baseline:.1f}ms)",
                details={"latency_ms": result.latency_ms, "baseline_ms": baseline},
            )

        # Update baseline (exponential moving average)
        self._latency_baselines[component] = (baseline * 0.95) + (result.latency_ms * 0.05)

    async def _attempt_recovery(self, component: str) -> None:
        """Attempt to recover a failed component."""
        handler = self._recovery_handlers.get(component)
        if not handler:
            return

        comp = self._component_health[component]
        comp.status = HealthStatus.RECOVERING

        logger.info("Attempting recovery for component: %s", component)

        try:
            success = await handler()
            if success:
                logger.info("Recovery successful for component: %s", component)
            else:
                logger.warning("Recovery failed for component: %s", component)
        except Exception as e:
            logger.exception("Error during recovery for %s: %s", component, e)

    async def _raise_alert(
        self,
        component: str,
        severity: AlertSeverity,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Raise a health alert."""
        # Check for duplicate active alert
        for alert in self._alerts.values():
            if (
                not alert.resolved
                and alert.component == component
                and alert.message == message
            ):
                return  # Don't duplicate

        alert_id = f"{component}_{datetime.utcnow().timestamp()}"
        alert = HealthAlert(
            id=alert_id,
            severity=severity,
            component=component,
            message=message,
            details=details or {},
        )

        self._alerts[alert_id] = alert
        logger.warning("Health alert: [%s] %s - %s", severity.value, component, message)

        # Notify handlers
        for handler in self._alert_handlers:
            try:
                await handler(alert)
            except Exception as e:
                logger.exception("Error in alert handler: %s", e)

    async def _resolve_component_alerts(self, component: str) -> None:
        """Resolve all active alerts for a component."""
        now = datetime.utcnow()
        for alert in self._alerts.values():
            if alert.component == component and not alert.resolved:
                alert.resolved = True
                alert.resolved_at = now
                logger.info("Alert resolved: %s", alert.message)

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of old alerts."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Run every hour

                cutoff = datetime.utcnow() - timedelta(hours=self._alert_retention_hours)
                to_remove = [
                    alert_id for alert_id, alert in self._alerts.items()
                    if alert.resolved and alert.resolved_at and alert.resolved_at < cutoff
                ]

                for alert_id in to_remove:
                    del self._alerts[alert_id]

                if to_remove:
                    logger.debug("Cleaned up %d old alerts", len(to_remove))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in cleanup loop: %s", e)

    # =========================================================================
    # Query Methods
    # =========================================================================

    async def get_system_health(self) -> Dict[str, Any]:
        """Get overall system health status."""
        # Determine overall status
        critical_unhealthy = any(
            comp.status == HealthStatus.UNHEALTHY
            for name, comp in self._component_health.items()
            if self._checks.get(name, HealthCheck(name="", check_fn=lambda: None)).critical
        )

        any_unhealthy = any(
            comp.status == HealthStatus.UNHEALTHY
            for comp in self._component_health.values()
        )

        any_degraded = any(
            comp.status == HealthStatus.DEGRADED
            for comp in self._component_health.values()
        )

        if critical_unhealthy:
            overall_status = HealthStatus.UNHEALTHY
        elif any_unhealthy or any_degraded:
            overall_status = HealthStatus.DEGRADED
        elif self._component_health:
            overall_status = HealthStatus.HEALTHY
        else:
            overall_status = HealthStatus.UNKNOWN

        return {
            "healthy": overall_status == HealthStatus.HEALTHY,
            "status": overall_status.value,
            "timestamp": datetime.utcnow().isoformat(),
            "components": {
                name: {
                    "status": comp.status.value,
                    "last_check": comp.last_check.isoformat() if comp.last_check else None,
                    "uptime_1h": comp.get_uptime_percentage(3600),
                    "avg_latency_ms": round(comp.avg_latency_ms, 2),
                    "consecutive_failures": comp.consecutive_failures,
                    "last_error": comp.last_error,
                }
                for name, comp in self._component_health.items()
            },
            "active_alerts": len([a for a in self._alerts.values() if not a.resolved]),
            "total_checks": len(self._checks),
        }

    def get_component_health(self, name: str) -> Optional[ComponentHealth]:
        """Get health state of a specific component."""
        return self._component_health.get(name)

    def get_active_alerts(self) -> List[HealthAlert]:
        """Get all active (unresolved) alerts."""
        return [a for a in self._alerts.values() if not a.resolved]

    def get_all_alerts(
        self,
        severity: Optional[AlertSeverity] = None,
        component: Optional[str] = None,
        include_resolved: bool = True,
    ) -> List[HealthAlert]:
        """Get alerts with optional filtering."""
        alerts = list(self._alerts.values())

        if not include_resolved:
            alerts = [a for a in alerts if not a.resolved]

        if severity:
            alerts = [a for a in alerts if a.severity == severity]

        if component:
            alerts = [a for a in alerts if a.component == component]

        return sorted(alerts, key=lambda a: a.timestamp, reverse=True)

    async def run_check_now(self, name: str) -> Optional[HealthCheckResult]:
        """Manually run a health check immediately."""
        check = self._checks.get(name)
        if not check:
            return None

        return await self._execute_check(check)

    def summary(self) -> str:
        """Get a human-readable health summary."""
        lines = [
            "=== Neural Mesh Health Summary ===",
            "",
        ]

        for name, comp in self._component_health.items():
            status_icon = {
                HealthStatus.HEALTHY: "✓",
                HealthStatus.DEGRADED: "⚠",
                HealthStatus.UNHEALTHY: "✗",
                HealthStatus.UNKNOWN: "?",
                HealthStatus.RECOVERING: "↻",
            }.get(comp.status, "?")

            lines.append(f"{status_icon} {name}: {comp.status.value}")
            if comp.last_error:
                lines.append(f"   └─ {comp.last_error}")

        active_alerts = len([a for a in self._alerts.values() if not a.resolved])
        if active_alerts > 0:
            lines.append("")
            lines.append(f"Active Alerts: {active_alerts}")

        return "\n".join(lines)


# =============================================================================
# Global Instance
# =============================================================================

_global_monitor: Optional[HealthMonitor] = None


async def get_health_monitor() -> HealthMonitor:
    """Get or create the global health monitor."""
    global _global_monitor

    if _global_monitor is None:
        _global_monitor = HealthMonitor()
        await _global_monitor.start()

    return _global_monitor


async def shutdown_health_monitor() -> None:
    """Stop and clear the global health monitor singleton."""
    global _global_monitor

    if _global_monitor is not None:
        await _global_monitor.stop()
        _global_monitor = None

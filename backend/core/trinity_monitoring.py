"""
Trinity Unified Monitoring v2.7
===============================

Centralized monitoring and observability across Trinity repositories:
- Unified metrics aggregation
- Cross-repo health monitoring
- Distributed tracing with correlation IDs
- Real-time dashboards
- Alerting and anomaly detection

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   TRINITY MONITORING                              │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  ┌─────────────────────────────────────────────────────────┐    │
    │  │                  Metrics Collectors                      │    │
    │  │                                                          │    │
    │  │  JARVIS        PRIME          REACTOR                   │    │
    │  │  • Latency     • Inference    • Training                │    │
    │  │  • Memory      • Tokens/s     • GPU util                │    │
    │  │  • Requests    • Model perf   • Loss curves             │    │
    │  └─────────────────────────────────────────────────────────┘    │
    │                          │                                       │
    │                          ▼                                       │
    │  ┌─────────────────────────────────────────────────────────┐    │
    │  │              Aggregation & Storage                       │    │
    │  │                                                          │    │
    │  │  • Time-series DB    • Log aggregation                  │    │
    │  │  • Trace storage     • Alert rules                      │    │
    │  └─────────────────────────────────────────────────────────┘    │
    │                          │                                       │
    │                          ▼                                       │
    │  ┌─────────────────────────────────────────────────────────┐    │
    │  │              Outputs                                     │    │
    │  │                                                          │    │
    │  │  • Dashboard API     • Alerts                           │    │
    │  │  • Health endpoints  • Traces                           │    │
    │  └─────────────────────────────────────────────────────────┘    │
    └──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

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


class MonitoringConfig:
    """Monitoring configuration."""

    # Storage
    METRICS_PATH = _env_str(
        "TRINITY_METRICS_PATH",
        str(Path.home() / ".jarvis" / "trinity" / "metrics")
    )
    TRACES_PATH = _env_str(
        "TRINITY_TRACES_PATH",
        str(Path.home() / ".jarvis" / "trinity" / "traces")
    )

    # Collection intervals
    METRICS_INTERVAL = _env_float("METRICS_INTERVAL", 10.0)
    HEALTH_CHECK_INTERVAL = _env_float("HEALTH_CHECK_INTERVAL", 30.0)
    AGGREGATION_INTERVAL = _env_float("AGGREGATION_INTERVAL", 60.0)

    # Retention
    METRICS_RETENTION_HOURS = _env_int("METRICS_RETENTION_HOURS", 168)  # 7 days
    TRACES_RETENTION_HOURS = _env_int("TRACES_RETENTION_HOURS", 24)

    # Alerting
    ALERT_COOLDOWN_SECONDS = _env_float("ALERT_COOLDOWN", 300.0)

    # Dashboard
    DASHBOARD_PORT = _env_int("MONITORING_DASHBOARD_PORT", 9090)


# =============================================================================
# Enums and Types
# =============================================================================

class RepoType(Enum):
    """Trinity repository types."""
    JARVIS = "jarvis"
    PRIME = "prime"
    REACTOR = "reactor"


class HealthStatus(Enum):
    """Component health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class MetricType(Enum):
    """Metric types."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Metric:
    """A single metric data point."""
    name: str
    value: float
    metric_type: MetricType = MetricType.GAUGE
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    repo: RepoType = RepoType.JARVIS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "type": self.metric_type.value,
            "labels": self.labels,
            "timestamp": self.timestamp.isoformat(),
            "repo": self.repo.value,
        }


@dataclass
class HealthCheck:
    """Health check result."""
    component: str
    repo: RepoType
    status: HealthStatus
    latency_ms: float = 0.0
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "repo": self.repo.value,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class TraceSpan:
    """A span in a distributed trace."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    operation: str = ""
    repo: RepoType = RepoType.JARVIS
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0
    status: str = "ok"
    tags: Dict[str, str] = field(default_factory=dict)
    logs: List[Dict[str, Any]] = field(default_factory=list)

    def finish(self, status: str = "ok") -> None:
        self.end_time = datetime.now()
        self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.status = status

    def log(self, message: str, **kwargs: Any) -> None:
        self.logs.append({
            "timestamp": datetime.now().isoformat(),
            "message": message,
            **kwargs,
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "repo": self.repo.value,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "tags": self.tags,
            "logs": self.logs,
        }


@dataclass
class Alert:
    """An alert from the monitoring system."""
    alert_id: str = ""
    name: str = ""
    severity: AlertSeverity = AlertSeverity.WARNING
    message: str = ""
    repo: Optional[RepoType] = None
    component: Optional[str] = None
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    threshold: Optional[float] = None
    triggered_at: datetime = field(default_factory=datetime.now)
    resolved_at: Optional[datetime] = None
    acknowledged: bool = False

    def __post_init__(self):
        if not self.alert_id:
            self.alert_id = str(uuid.uuid4())[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "repo": self.repo.value if self.repo else None,
            "component": self.component,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "triggered_at": self.triggered_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "acknowledged": self.acknowledged,
        }


@dataclass
class AlertRule:
    """Alert rule configuration."""
    name: str
    metric_name: str
    condition: str  # "gt", "lt", "eq", "ne"
    threshold: float
    severity: AlertSeverity = AlertSeverity.WARNING
    duration_seconds: float = 0.0  # How long condition must be true
    labels: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class MonitoringStats:
    """Overall monitoring statistics."""
    total_metrics: int = 0
    total_traces: int = 0
    active_alerts: int = 0
    health_checks_passed: int = 0
    health_checks_failed: int = 0
    repos_online: int = 0
    repos_offline: int = 0
    uptime_seconds: float = 0.0


# =============================================================================
# Metrics Collector
# =============================================================================

class MetricsCollector:
    """Collects and aggregates metrics."""

    def __init__(self, max_history: int = 10000):
        self._metrics: Deque[Metric] = deque(maxlen=max_history)
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def record(self, metric: Metric) -> None:
        """Record a metric."""
        async with self._lock:
            self._metrics.append(metric)

            key = f"{metric.repo.value}:{metric.name}"

            if metric.metric_type == MetricType.COUNTER:
                self._counters[key] += metric.value
            elif metric.metric_type == MetricType.GAUGE:
                self._gauges[key] = metric.value
            elif metric.metric_type == MetricType.HISTOGRAM:
                self._histograms[key].append(metric.value)
                # Keep only recent values
                if len(self._histograms[key]) > 1000:
                    self._histograms[key] = self._histograms[key][-1000:]

    async def get_metrics(
        self,
        repo: Optional[RepoType] = None,
        name_filter: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Metric]:
        """Get recorded metrics."""
        async with self._lock:
            results = list(self._metrics)

        if repo:
            results = [m for m in results if m.repo == repo]
        if name_filter:
            results = [m for m in results if name_filter in m.name]
        if since:
            results = [m for m in results if m.timestamp >= since]

        return results[-limit:]

    async def get_summary(self, repo: Optional[RepoType] = None) -> Dict[str, Any]:
        """Get metrics summary."""
        async with self._lock:
            summary = {
                "counters": {},
                "gauges": {},
                "histograms": {},
            }

            for key, value in self._counters.items():
                r, name = key.split(":", 1)
                if repo is None or r == repo.value:
                    summary["counters"][key] = value

            for key, value in self._gauges.items():
                r, name = key.split(":", 1)
                if repo is None or r == repo.value:
                    summary["gauges"][key] = value

            for key, values in self._histograms.items():
                r, name = key.split(":", 1)
                if repo is None or r == repo.value:
                    if values:
                        summary["histograms"][key] = {
                            "count": len(values),
                            "min": min(values),
                            "max": max(values),
                            "mean": statistics.mean(values),
                            "p50": statistics.median(values),
                            "p95": statistics.quantiles(values, n=20)[18] if len(values) >= 20 else max(values),
                        }

        return summary


# =============================================================================
# Distributed Tracer
# =============================================================================

class DistributedTracer:
    """Distributed tracing across repos."""

    def __init__(self, local_repo: RepoType, max_traces: int = 1000):
        self.local_repo = local_repo
        self._traces: Dict[str, List[TraceSpan]] = {}
        self._trace_history: Deque[str] = deque(maxlen=max_traces)
        self._lock = asyncio.Lock()

    def start_trace(
        self,
        operation: str,
        trace_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> TraceSpan:
        """Start a new trace or span."""
        span = TraceSpan(
            trace_id=trace_id or str(uuid.uuid4()),
            span_id=str(uuid.uuid4())[:12],
            parent_span_id=parent_span_id,
            operation=operation,
            repo=self.local_repo,
            tags=tags or {},
        )

        return span

    async def finish_span(self, span: TraceSpan, status: str = "ok") -> None:
        """Finish and record a span."""
        span.finish(status)

        async with self._lock:
            if span.trace_id not in self._traces:
                self._traces[span.trace_id] = []
                self._trace_history.append(span.trace_id)

            self._traces[span.trace_id].append(span)

            # Cleanup old traces
            while len(self._trace_history) > 500:
                old_trace = self._trace_history.popleft()
                self._traces.pop(old_trace, None)

    async def get_trace(self, trace_id: str) -> Optional[List[TraceSpan]]:
        """Get all spans for a trace."""
        async with self._lock:
            return self._traces.get(trace_id)

    async def get_recent_traces(
        self,
        limit: int = 50,
        operation_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent traces with summary."""
        async with self._lock:
            traces = []

            for trace_id in list(self._trace_history)[-limit:]:
                spans = self._traces.get(trace_id, [])
                if not spans:
                    continue

                if operation_filter:
                    spans = [s for s in spans if operation_filter in s.operation]
                    if not spans:
                        continue

                root_span = next((s for s in spans if s.parent_span_id is None), spans[0])
                total_duration = sum(s.duration_ms for s in spans)

                traces.append({
                    "trace_id": trace_id,
                    "operation": root_span.operation,
                    "span_count": len(spans),
                    "duration_ms": total_duration,
                    "status": "error" if any(s.status == "error" for s in spans) else "ok",
                    "start_time": root_span.start_time.isoformat(),
                })

        return traces


# =============================================================================
# Health Monitor
# =============================================================================

class HealthMonitor:
    """Cross-repo health monitoring."""

    def __init__(self):
        self._health: Dict[str, HealthCheck] = {}
        self._history: Dict[str, Deque[HealthCheck]] = defaultdict(lambda: deque(maxlen=100))
        self._lock = asyncio.Lock()

    async def record_check(self, check: HealthCheck) -> None:
        """Record a health check result."""
        key = f"{check.repo.value}:{check.component}"

        async with self._lock:
            self._health[key] = check
            self._history[key].append(check)

    async def get_health(
        self,
        repo: Optional[RepoType] = None,
    ) -> Dict[str, HealthCheck]:
        """Get current health status."""
        async with self._lock:
            if repo:
                return {
                    k: v for k, v in self._health.items()
                    if v.repo == repo
                }
            return self._health.copy()

    async def get_overall_status(self) -> Tuple[HealthStatus, Dict[str, Any]]:
        """Get overall system health."""
        async with self._lock:
            if not self._health:
                return HealthStatus.UNKNOWN, {"message": "No health data"}

            unhealthy = [
                c for c in self._health.values()
                if c.status == HealthStatus.UNHEALTHY
            ]
            degraded = [
                c for c in self._health.values()
                if c.status == HealthStatus.DEGRADED
            ]

            if unhealthy:
                return HealthStatus.UNHEALTHY, {
                    "message": f"{len(unhealthy)} components unhealthy",
                    "components": [c.component for c in unhealthy],
                }
            elif degraded:
                return HealthStatus.DEGRADED, {
                    "message": f"{len(degraded)} components degraded",
                    "components": [c.component for c in degraded],
                }
            else:
                return HealthStatus.HEALTHY, {
                    "message": "All components healthy",
                    "component_count": len(self._health),
                }


# =============================================================================
# Alert Manager
# =============================================================================

class AlertManager:
    """Alert management and routing."""

    def __init__(self):
        self._rules: List[AlertRule] = []
        self._active_alerts: Dict[str, Alert] = {}
        self._alert_history: Deque[Alert] = deque(maxlen=1000)
        self._cooldowns: Dict[str, datetime] = {}
        self._handlers: List[Callable[[Alert], None]] = []
        self._lock = asyncio.Lock()

    def add_rule(self, rule: AlertRule) -> None:
        """Add an alert rule."""
        self._rules.append(rule)

    def add_handler(self, handler: Callable[[Alert], None]) -> None:
        """Add alert handler."""
        self._handlers.append(handler)

    async def evaluate(
        self,
        metrics: List[Metric],
    ) -> List[Alert]:
        """Evaluate metrics against rules."""
        new_alerts = []

        async with self._lock:
            for rule in self._rules:
                if not rule.enabled:
                    continue

                # Find matching metrics
                matching = [
                    m for m in metrics
                    if m.name == rule.metric_name
                ]

                for metric in matching:
                    triggered = False

                    if rule.condition == "gt" and metric.value > rule.threshold:
                        triggered = True
                    elif rule.condition == "lt" and metric.value < rule.threshold:
                        triggered = True
                    elif rule.condition == "eq" and metric.value == rule.threshold:
                        triggered = True
                    elif rule.condition == "ne" and metric.value != rule.threshold:
                        triggered = True

                    if triggered:
                        # Check cooldown
                        alert_key = f"{rule.name}:{metric.repo.value}"
                        if alert_key in self._cooldowns:
                            cooldown = self._cooldowns[alert_key]
                            if datetime.now() < cooldown:
                                continue

                        # Create alert
                        alert = Alert(
                            name=rule.name,
                            severity=rule.severity,
                            message=f"{rule.metric_name} {rule.condition} {rule.threshold} (value: {metric.value})",
                            repo=metric.repo,
                            metric_name=rule.metric_name,
                            metric_value=metric.value,
                            threshold=rule.threshold,
                        )

                        self._active_alerts[alert.alert_id] = alert
                        self._alert_history.append(alert)
                        self._cooldowns[alert_key] = (
                            datetime.now() + timedelta(seconds=MonitoringConfig.ALERT_COOLDOWN_SECONDS)
                        )

                        new_alerts.append(alert)

                        # Notify handlers
                        for handler in self._handlers:
                            try:
                                handler(alert)
                            except Exception as e:
                                logger.warning(f"Alert handler error: {e}")

        return new_alerts

    async def get_active_alerts(
        self,
        severity: Optional[AlertSeverity] = None,
    ) -> List[Alert]:
        """Get active alerts."""
        async with self._lock:
            alerts = list(self._active_alerts.values())

        if severity:
            alerts = [a for a in alerts if a.severity == severity]

        return alerts

    async def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an alert."""
        async with self._lock:
            if alert_id in self._active_alerts:
                self._active_alerts[alert_id].acknowledged = True
                return True
        return False

    async def resolve(self, alert_id: str) -> bool:
        """Resolve an alert."""
        async with self._lock:
            if alert_id in self._active_alerts:
                alert = self._active_alerts.pop(alert_id)
                alert.resolved_at = datetime.now()
                return True
        return False


# =============================================================================
# Trinity Monitoring System
# =============================================================================

class TrinityMonitoring:
    """
    Unified monitoring system for Trinity.

    Features:
    - Metrics collection and aggregation
    - Distributed tracing
    - Cross-repo health monitoring
    - Alerting and anomaly detection
    """

    def __init__(self, local_repo: RepoType = RepoType.JARVIS):
        self.local_repo = local_repo
        self._running = False
        self._start_time = time.time()

        # Components
        self._metrics = MetricsCollector()
        self._tracer = DistributedTracer(local_repo)
        self._health = HealthMonitor()
        self._alerts = AlertManager()

        # Event bus
        self._event_bus: Optional[Any] = None

        # Background tasks
        self._collector_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._aggregation_task: Optional[asyncio.Task] = None

        # Setup default alert rules
        self._setup_default_rules()

        logger.info(f"[Monitoring] Initialized for {local_repo.value}")

    def _setup_default_rules(self) -> None:
        """Setup default alert rules."""
        self._alerts.add_rule(AlertRule(
            name="high_latency",
            metric_name="request_latency_ms",
            condition="gt",
            threshold=5000,
            severity=AlertSeverity.WARNING,
        ))
        self._alerts.add_rule(AlertRule(
            name="high_error_rate",
            metric_name="error_rate",
            condition="gt",
            threshold=0.1,
            severity=AlertSeverity.ERROR,
        ))
        self._alerts.add_rule(AlertRule(
            name="high_memory",
            metric_name="memory_percent",
            condition="gt",
            threshold=90,
            severity=AlertSeverity.WARNING,
        ))
        self._alerts.add_rule(AlertRule(
            name="critical_memory",
            metric_name="memory_percent",
            condition="gt",
            threshold=95,
            severity=AlertSeverity.CRITICAL,
        ))

    @classmethod
    async def create(
        cls,
        local_repo: RepoType = RepoType.JARVIS,
    ) -> "TrinityMonitoring":
        """Create and initialize monitoring."""
        monitoring = cls(local_repo)
        await monitoring.initialize()
        return monitoring

    async def initialize(self) -> None:
        """Initialize monitoring."""
        # Ensure directories
        Path(MonitoringConfig.METRICS_PATH).mkdir(parents=True, exist_ok=True)
        Path(MonitoringConfig.TRACES_PATH).mkdir(parents=True, exist_ok=True)

        # Connect to event bus
        await self._connect_event_bus()

        self._running = True

        # Start background tasks
        self._collector_task = asyncio.create_task(self._collection_loop())
        self._health_task = asyncio.create_task(self._health_loop())
        self._aggregation_task = asyncio.create_task(self._aggregation_loop())

        logger.info("[Monitoring] Initialization complete")

    async def _connect_event_bus(self) -> None:
        """Connect to event bus."""
        try:
            from backend.core.trinity_event_bus import (
                get_trinity_event_bus,
                RepoType as EventRepoType,
            )
            self._event_bus = await get_trinity_event_bus(
                EventRepoType(self.local_repo.value)
            )

            # Subscribe to metrics events
            await self._event_bus.subscribe("monitoring.*", self._handle_monitoring_event)

            logger.info("[Monitoring] Connected to event bus")
        except ImportError:
            logger.warning("[Monitoring] Event bus not available")

    async def record_metric(
        self,
        name: str,
        value: float,
        metric_type: MetricType = MetricType.GAUGE,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a metric."""
        metric = Metric(
            name=name,
            value=value,
            metric_type=metric_type,
            labels=labels or {},
            repo=self.local_repo,
        )

        await self._metrics.record(metric)

        # Evaluate alerts
        await self._alerts.evaluate([metric])

    def start_span(
        self,
        operation: str,
        trace_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> TraceSpan:
        """Start a trace span."""
        return self._tracer.start_trace(operation, trace_id, parent_span_id, tags)

    async def finish_span(self, span: TraceSpan, status: str = "ok") -> None:
        """Finish a trace span."""
        await self._tracer.finish_span(span, status)

    async def record_health(self, check: HealthCheck) -> None:
        """Record a health check."""
        await self._health.record_check(check)

    async def get_dashboard_data(self) -> Dict[str, Any]:
        """Get data for monitoring dashboard."""
        health_status, health_details = await self._health.get_overall_status()
        active_alerts = await self._alerts.get_active_alerts()
        metrics_summary = await self._metrics.get_summary()
        recent_traces = await self._tracer.get_recent_traces(limit=20)

        return {
            "overall_health": {
                "status": health_status.value,
                **health_details,
            },
            "alerts": {
                "active_count": len(active_alerts),
                "alerts": [a.to_dict() for a in active_alerts[:10]],
            },
            "metrics": metrics_summary,
            "traces": {
                "recent": recent_traces,
            },
            "uptime_seconds": time.time() - self._start_time,
            "timestamp": datetime.now().isoformat(),
        }

    async def _handle_monitoring_event(self, event: Any) -> None:
        """Handle monitoring events from other repos."""
        try:
            if event.topic == "monitoring.metric":
                metric = Metric(
                    name=event.payload["name"],
                    value=event.payload["value"],
                    metric_type=MetricType(event.payload.get("type", "gauge")),
                    labels=event.payload.get("labels", {}),
                    repo=RepoType(event.payload.get("repo", "jarvis")),
                )
                await self._metrics.record(metric)

            elif event.topic == "monitoring.health":
                check = HealthCheck(
                    component=event.payload["component"],
                    repo=RepoType(event.payload["repo"]),
                    status=HealthStatus(event.payload["status"]),
                    latency_ms=event.payload.get("latency_ms", 0),
                    message=event.payload.get("message", ""),
                )
                await self._health.record_check(check)

        except Exception as e:
            logger.exception(f"[Monitoring] Event handling error: {e}")

    async def _collection_loop(self) -> None:
        """Background metrics collection."""
        while self._running:
            try:
                await asyncio.sleep(MonitoringConfig.METRICS_INTERVAL)

                # Collect system metrics
                import psutil

                await self.record_metric("cpu_percent", psutil.cpu_percent())
                await self.record_metric("memory_percent", psutil.virtual_memory().percent)
                await self.record_metric(
                    "memory_available_mb",
                    psutil.virtual_memory().available / (1024 * 1024)
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[Monitoring] Collection error: {e}")

    async def _health_loop(self) -> None:
        """Background health checking."""
        while self._running:
            try:
                await asyncio.sleep(MonitoringConfig.HEALTH_CHECK_INTERVAL)

                # Check Trinity components
                await self._check_trinity_health()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[Monitoring] Health check error: {e}")

    async def _check_trinity_health(self) -> None:
        """Check health of Trinity components."""
        import aiohttp

        checks = [
            (RepoType.JARVIS, "main", "http://localhost:8000/health"),
            (RepoType.PRIME, "inference", "http://localhost:8002/health"),
            (RepoType.REACTOR, "training", "http://localhost:8003/health"),
        ]

        async with aiohttp.ClientSession() as session:
            for repo, component, url in checks:
                start = time.perf_counter()
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        latency_ms = (time.perf_counter() - start) * 1000

                        if resp.status == 200:
                            status = HealthStatus.HEALTHY
                            message = "OK"
                        else:
                            status = HealthStatus.DEGRADED
                            message = f"HTTP {resp.status}"

                except aiohttp.ClientError as e:
                    latency_ms = (time.perf_counter() - start) * 1000
                    status = HealthStatus.UNHEALTHY
                    message = str(e)
                except Exception as e:
                    latency_ms = 0
                    status = HealthStatus.UNKNOWN
                    message = str(e)

                check = HealthCheck(
                    component=component,
                    repo=repo,
                    status=status,
                    latency_ms=latency_ms,
                    message=message,
                )
                await self._health.record_check(check)

    async def _aggregation_loop(self) -> None:
        """Background aggregation and persistence."""
        while self._running:
            try:
                await asyncio.sleep(MonitoringConfig.AGGREGATION_INTERVAL)

                # Persist metrics
                await self._persist_metrics()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[Monitoring] Aggregation error: {e}")

    async def _persist_metrics(self) -> None:
        """Persist metrics to storage."""
        summary = await self._metrics.get_summary()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = Path(MonitoringConfig.METRICS_PATH) / f"metrics_{timestamp}.json"

        try:
            import aiofiles
            async with aiofiles.open(filepath, "w") as f:
                await f.write(json.dumps(summary, indent=2, default=str))
        except Exception as e:
            logger.warning(f"[Monitoring] Persist error: {e}")

    def get_stats(self) -> MonitoringStats:
        """Get monitoring statistics."""
        return MonitoringStats(
            uptime_seconds=time.time() - self._start_time,
        )

    async def shutdown(self) -> None:
        """Shutdown monitoring."""
        logger.info("[Monitoring] Shutting down...")
        self._running = False

        for task in [self._collector_task, self._health_task, self._aggregation_task]:
            if task:
                task.cancel()

        # Final persist
        await self._persist_metrics()

        logger.info("[Monitoring] Shutdown complete")


# =============================================================================
# Global Instance
# =============================================================================

_monitoring: Optional[TrinityMonitoring] = None


async def get_trinity_monitoring(
    local_repo: RepoType = RepoType.JARVIS,
) -> TrinityMonitoring:
    """Get or create global monitoring."""
    global _monitoring

    if _monitoring is None:
        _monitoring = await TrinityMonitoring.create(local_repo)

    return _monitoring


async def shutdown_trinity_monitoring() -> None:
    """Shutdown global monitoring."""
    global _monitoring

    if _monitoring:
        await _monitoring.shutdown()
        _monitoring = None

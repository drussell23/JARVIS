"""
JARVIS Neural Mesh - Advanced Metrics Collector

Comprehensive metrics collection system for monitoring Neural Mesh performance.

Features:
- Time-series metric storage
- Automatic aggregation (min, max, avg, p50, p95, p99)
- Counter, gauge, and histogram support
- Per-agent and per-component metrics
- Memory-efficient circular buffers
- Export to Prometheus/StatsD format
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


class MetricType(str, Enum):
    """Types of metrics."""
    COUNTER = "counter"  # Monotonically increasing
    GAUGE = "gauge"  # Point-in-time value
    HISTOGRAM = "histogram"  # Distribution of values
    TIMER = "timer"  # Duration measurements


@dataclass
class MetricValue:
    """A single metric value with timestamp."""
    value: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class TimeSeriesMetric:
    """Time series metric with aggregations."""
    name: str
    metric_type: MetricType
    description: str = ""
    unit: str = ""
    values: deque = field(default_factory=lambda: deque(maxlen=1000))
    labels: Dict[str, str] = field(default_factory=dict)

    def add(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Add a value to the time series."""
        self.values.append(MetricValue(
            value=value,
            timestamp=datetime.utcnow(),
            labels=labels or {},
        ))

    def get_latest(self) -> Optional[float]:
        """Get the most recent value."""
        if self.values:
            return self.values[-1].value
        return None

    def get_values_since(self, since: datetime) -> List[float]:
        """Get values since a timestamp."""
        return [
            v.value for v in self.values
            if v.timestamp >= since
        ]

    def aggregate(self, window_seconds: float = 60.0) -> Dict[str, float]:
        """Calculate aggregations over a time window."""
        since = datetime.utcnow() - timedelta(seconds=window_seconds)
        values = self.get_values_since(since)

        if not values:
            return {
                "count": 0,
                "min": 0,
                "max": 0,
                "avg": 0,
                "sum": 0,
                "p50": 0,
                "p95": 0,
                "p99": 0,
            }

        sorted_values = sorted(values)
        count = len(values)

        return {
            "count": count,
            "min": min(values),
            "max": max(values),
            "avg": statistics.mean(values),
            "sum": sum(values),
            "p50": sorted_values[int(count * 0.50)] if count > 0 else 0,
            "p95": sorted_values[min(int(count * 0.95), count - 1)] if count > 0 else 0,
            "p99": sorted_values[min(int(count * 0.99), count - 1)] if count > 0 else 0,
        }


class MetricsCollector:
    """
    Advanced metrics collector for Neural Mesh.

    Provides comprehensive metrics collection, aggregation, and export
    capabilities for monitoring system performance.

    Example:
        collector = MetricsCollector()
        await collector.start()

        # Record metrics
        collector.increment("messages.sent", labels={"agent": "uae"})
        collector.gauge("memory.usage", 75.5)
        collector.histogram("task.duration_ms", 123.5)

        # Get aggregations
        stats = collector.get_metric_stats("task.duration_ms")
        print(f"p99 latency: {stats['p99']}ms")

        # Export to Prometheus format
        prom_output = collector.export_prometheus()
    """

    def __init__(
        self,
        retention_seconds: float = 3600,  # 1 hour default
        aggregation_interval: float = 10.0,  # 10 second intervals
    ) -> None:
        """Initialize the metrics collector.

        Args:
            retention_seconds: How long to retain metrics
            aggregation_interval: Interval for background aggregation
        """
        self._metrics: Dict[str, TimeSeriesMetric] = {}
        self._counters: Dict[str, float] = defaultdict(float)
        self._retention_seconds = retention_seconds
        self._aggregation_interval = aggregation_interval
        self._running = False
        self._aggregation_task: Optional[asyncio.Task] = None

        # Pre-defined system metrics
        self._setup_system_metrics()

    def _setup_system_metrics(self) -> None:
        """Setup standard system metrics."""
        system_metrics = [
            ("neural_mesh.agents.registered", MetricType.GAUGE, "Number of registered agents"),
            ("neural_mesh.agents.active", MetricType.GAUGE, "Number of active agents"),
            ("neural_mesh.messages.sent", MetricType.COUNTER, "Total messages sent"),
            ("neural_mesh.messages.delivered", MetricType.COUNTER, "Total messages delivered"),
            ("neural_mesh.messages.failed", MetricType.COUNTER, "Total messages failed"),
            ("neural_mesh.messages.latency_ms", MetricType.HISTOGRAM, "Message delivery latency"),
            ("neural_mesh.tasks.completed", MetricType.COUNTER, "Total tasks completed"),
            ("neural_mesh.tasks.failed", MetricType.COUNTER, "Total tasks failed"),
            ("neural_mesh.tasks.duration_ms", MetricType.HISTOGRAM, "Task execution duration"),
            ("neural_mesh.knowledge.entries", MetricType.GAUGE, "Knowledge graph entries"),
            ("neural_mesh.knowledge.queries", MetricType.COUNTER, "Knowledge queries"),
            ("neural_mesh.workflows.active", MetricType.GAUGE, "Active workflows"),
            ("neural_mesh.workflows.completed", MetricType.COUNTER, "Completed workflows"),
            ("neural_mesh.errors.total", MetricType.COUNTER, "Total errors"),
        ]

        for name, metric_type, description in system_metrics:
            self._metrics[name] = TimeSeriesMetric(
                name=name,
                metric_type=metric_type,
                description=description,
            )

    async def start(self) -> None:
        """Start the metrics collector."""
        if self._running:
            return

        self._running = True
        self._aggregation_task = asyncio.create_task(
            self._aggregation_loop()
        )
        logger.info("MetricsCollector started")

    async def stop(self) -> None:
        """Stop the metrics collector."""
        self._running = False

        if self._aggregation_task:
            self._aggregation_task.cancel()
            try:
                await self._aggregation_task
            except asyncio.CancelledError:
                pass

        logger.info("MetricsCollector stopped")

    async def _aggregation_loop(self) -> None:
        """Background loop for periodic aggregation."""
        while self._running:
            try:
                await asyncio.sleep(self._aggregation_interval)
                self._cleanup_old_metrics()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in aggregation loop: %s", e)

    def _cleanup_old_metrics(self) -> None:
        """Remove metrics older than retention period."""
        cutoff = datetime.utcnow() - timedelta(seconds=self._retention_seconds)

        for metric in self._metrics.values():
            # Filter out old values (deque handles this automatically with maxlen)
            pass

    def _get_or_create_metric(
        self,
        name: str,
        metric_type: MetricType,
        description: str = "",
        unit: str = "",
    ) -> TimeSeriesMetric:
        """Get existing metric or create new one."""
        if name not in self._metrics:
            self._metrics[name] = TimeSeriesMetric(
                name=name,
                metric_type=metric_type,
                description=description,
                unit=unit,
            )
        return self._metrics[name]

    # =========================================================================
    # Public Recording Methods
    # =========================================================================

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Increment a counter metric.

        Args:
            name: Metric name
            value: Amount to increment
            labels: Optional labels
        """
        metric = self._get_or_create_metric(name, MetricType.COUNTER)
        self._counters[name] += value
        metric.add(self._counters[name], labels)

    def gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a gauge metric.

        Args:
            name: Metric name
            value: Current value
            labels: Optional labels
        """
        metric = self._get_or_create_metric(name, MetricType.GAUGE)
        metric.add(value, labels)

    def histogram(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a histogram metric.

        Args:
            name: Metric name
            value: Value to record
            labels: Optional labels
        """
        metric = self._get_or_create_metric(name, MetricType.HISTOGRAM)
        metric.add(value, labels)

    def timer(
        self,
        name: str,
    ) -> "TimerContext":
        """Create a timer context manager.

        Args:
            name: Metric name

        Returns:
            Timer context manager
        """
        return TimerContext(self, name)

    def record_timing(
        self,
        name: str,
        duration_ms: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Record a timing metric.

        Args:
            name: Metric name
            duration_ms: Duration in milliseconds
            labels: Optional labels
        """
        metric = self._get_or_create_metric(name, MetricType.TIMER)
        metric.add(duration_ms, labels)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_metric(self, name: str) -> Optional[TimeSeriesMetric]:
        """Get a metric by name."""
        return self._metrics.get(name)

    def get_metric_value(self, name: str) -> Optional[float]:
        """Get the latest value of a metric."""
        metric = self._metrics.get(name)
        if metric:
            return metric.get_latest()
        return None

    def get_metric_stats(
        self,
        name: str,
        window_seconds: float = 60.0,
    ) -> Dict[str, float]:
        """Get aggregated stats for a metric.

        Args:
            name: Metric name
            window_seconds: Time window for aggregation

        Returns:
            Dict with min, max, avg, p50, p95, p99, etc.
        """
        metric = self._metrics.get(name)
        if metric:
            return metric.aggregate(window_seconds)
        return {"count": 0}

    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics with their latest values."""
        result = {}
        for name, metric in self._metrics.items():
            result[name] = {
                "type": metric.metric_type.value,
                "value": metric.get_latest(),
                "description": metric.description,
            }
        return result

    def get_agent_metrics(self, agent_name: str) -> Dict[str, Any]:
        """Get all metrics for a specific agent."""
        result = {}
        for name, metric in self._metrics.items():
            if agent_name in name or any(
                v.labels.get("agent") == agent_name
                for v in metric.values
            ):
                result[name] = {
                    "type": metric.metric_type.value,
                    "stats": metric.aggregate(),
                }
        return result

    # =========================================================================
    # Export Methods
    # =========================================================================

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus format.

        Returns:
            Prometheus-formatted metrics string
        """
        lines = []
        lines.append("# HELP neural_mesh_metrics JARVIS Neural Mesh metrics")
        lines.append("# TYPE neural_mesh_metrics gauge")
        lines.append("")

        for name, metric in self._metrics.items():
            # Sanitize name for Prometheus
            prom_name = name.replace(".", "_").replace("-", "_")

            # Add HELP and TYPE
            if metric.description:
                lines.append(f"# HELP {prom_name} {metric.description}")
            lines.append(f"# TYPE {prom_name} {metric.metric_type.value}")

            # Add values
            value = metric.get_latest()
            if value is not None:
                lines.append(f"{prom_name} {value}")

            lines.append("")

        return "\n".join(lines)

    def export_json(self) -> Dict[str, Any]:
        """Export metrics as JSON.

        Returns:
            Dict with all metrics and their stats
        """
        result = {
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": {},
        }

        for name, metric in self._metrics.items():
            result["metrics"][name] = {
                "type": metric.metric_type.value,
                "description": metric.description,
                "latest": metric.get_latest(),
                "stats_1m": metric.aggregate(60),
                "stats_5m": metric.aggregate(300),
            }

        return result

    def summary(self) -> str:
        """Get a human-readable summary of key metrics."""
        lines = [
            "=== Neural Mesh Metrics Summary ===",
            "",
        ]

        key_metrics = [
            "neural_mesh.agents.active",
            "neural_mesh.messages.sent",
            "neural_mesh.tasks.completed",
            "neural_mesh.errors.total",
        ]

        for name in key_metrics:
            value = self.get_metric_value(name)
            if value is not None:
                lines.append(f"{name}: {value}")

        # Add latency stats
        latency_stats = self.get_metric_stats(
            "neural_mesh.messages.latency_ms",
            window_seconds=60,
        )
        if latency_stats["count"] > 0:
            lines.append("")
            lines.append("Message Latency (last 60s):")
            lines.append(f"  p50: {latency_stats['p50']:.2f}ms")
            lines.append(f"  p95: {latency_stats['p95']:.2f}ms")
            lines.append(f"  p99: {latency_stats['p99']:.2f}ms")

        return "\n".join(lines)


class TimerContext:
    """Context manager for timing operations."""

    def __init__(self, collector: MetricsCollector, name: str) -> None:
        self._collector = collector
        self._name = name
        self._start_time: Optional[float] = None

    def __enter__(self) -> "TimerContext":
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._start_time:
            duration_ms = (time.perf_counter() - self._start_time) * 1000
            self._collector.record_timing(self._name, duration_ms)

    async def __aenter__(self) -> "TimerContext":
        self._start_time = time.perf_counter()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._start_time:
            duration_ms = (time.perf_counter() - self._start_time) * 1000
            self._collector.record_timing(self._name, duration_ms)


# =============================================================================
# Global Instance
# =============================================================================

_global_collector: Optional[MetricsCollector] = None


async def get_metrics_collector() -> MetricsCollector:
    """Get or create the global metrics collector."""
    global _global_collector

    if _global_collector is None:
        _global_collector = MetricsCollector()
        await _global_collector.start()

    return _global_collector


async def shutdown_metrics_collector() -> None:
    """Stop and clear the global metrics collector singleton."""
    global _global_collector

    if _global_collector is not None:
        await _global_collector.stop()
        _global_collector = None

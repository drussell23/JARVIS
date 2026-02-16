"""
Trinity Observability System v4.0 - Enterprise-Grade Distributed Monitoring
============================================================================

Comprehensive observability for the Trinity Ecosystem addressing all 10 gaps:

Gap 1: Distributed Tracing - W3C Trace Context propagation
Gap 2: Cross-Repo Metrics Collection - Prometheus-compatible metrics
Gap 3: Cross-Repo Logging - Centralized structured logging
Gap 4: Performance Profiling - Async profiling with flame graphs
Gap 5: Error Aggregation - Sentry-style error tracking
Gap 6: Health Dashboard - Unified health monitoring
Gap 7: Alert System - Rule-based alerting with deduplication
Gap 8: Dependency Graph Visualization - Dynamic graph generation
Gap 9: Request Flow Visualization - Request flow tracking
Gap 10: Resource Usage Monitoring - CPU/Memory/Disk/Network monitoring

Author: JARVIS AI System
Version: 4.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import psutil
import random
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from typing import (
    Any, Callable, Coroutine, Deque, Dict, Generic, List,
    Optional, Protocol, Set, Tuple, TypeVar, Union
)

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class ObservabilityConfig:
    """Configuration for the Trinity Observability System."""

    # Base directories
    base_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "observability")

    # Tracing config
    tracing_enabled: bool = True
    trace_sample_rate: float = 1.0  # 100% sampling
    trace_retention_hours: int = 24
    max_spans_per_trace: int = 1000

    # Metrics config
    metrics_enabled: bool = True
    metrics_flush_interval: float = 10.0
    metrics_retention_hours: int = 168  # 7 days
    histogram_buckets: List[float] = field(default_factory=lambda: [
        0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
    ])

    # Logging config
    logging_enabled: bool = True
    log_level: str = "INFO"
    log_retention_days: int = 7
    max_log_size_mb: int = 100
    structured_logging: bool = True

    # Profiling config
    profiling_enabled: bool = True
    profile_sample_rate: float = 0.01  # 1% sampling
    profile_duration_seconds: float = 30.0
    flame_graph_enabled: bool = True

    # Error aggregation config
    error_aggregation_enabled: bool = True
    error_retention_hours: int = 168  # 7 days
    error_fingerprint_frames: int = 5
    max_errors_per_group: int = 100

    # Health dashboard config
    health_check_interval: float = 5.0
    health_timeout: float = 3.0
    health_history_size: int = 100

    # Alert config
    alerting_enabled: bool = True
    alert_cooldown_seconds: float = 300.0
    alert_dedup_window_seconds: float = 60.0
    max_alerts_per_hour: int = 100

    # Resource monitoring config
    resource_monitoring_enabled: bool = True
    resource_check_interval: float = 5.0
    resource_history_size: int = 720  # 1 hour at 5s intervals

    # Node identification
    node_id: str = field(default_factory=lambda: f"jarvis-{os.getpid()}")
    service_name: str = "jarvis"

    def __post_init__(self):
        if isinstance(self.base_dir, str):
            self.base_dir = Path(self.base_dir)


# =============================================================================
# GAP 1: DISTRIBUTED TRACING (W3C Trace Context)
# =============================================================================

class SpanKind(str, Enum):
    """OpenTelemetry-compatible span kinds."""
    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


class SpanStatus(str, Enum):
    """Span status codes."""
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


@dataclass
class SpanContext:
    """W3C Trace Context compatible span context."""
    trace_id: str  # 32 hex chars (128 bits)
    span_id: str   # 16 hex chars (64 bits)
    trace_flags: int = 1  # 01 = sampled
    trace_state: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def generate(cls) -> 'SpanContext':
        """Generate a new span context."""
        return cls(
            trace_id=uuid.uuid4().hex + uuid.uuid4().hex[:16],
            span_id=uuid.uuid4().hex[:16],
            trace_flags=1
        )

    @classmethod
    def from_traceparent(cls, traceparent: str) -> Optional['SpanContext']:
        """Parse W3C traceparent header."""
        # Format: {version}-{trace-id}-{parent-id}-{trace-flags}
        match = re.match(
            r'^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$',
            traceparent.lower()
        )
        if not match:
            return None

        version, trace_id, span_id, flags = match.groups()
        return cls(
            trace_id=trace_id,
            span_id=span_id,
            trace_flags=int(flags, 16)
        )

    def to_traceparent(self) -> str:
        """Generate W3C traceparent header."""
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"

    def to_tracestate(self) -> str:
        """Generate W3C tracestate header."""
        return ",".join(f"{k}={v}" for k, v in self.trace_state.items())

    @property
    def is_sampled(self) -> bool:
        return bool(self.trace_flags & 0x01)


@dataclass
class Span:
    """A single span in a distributed trace."""
    context: SpanContext
    name: str
    kind: SpanKind
    parent_span_id: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.UNSET
    status_message: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    links: List[SpanContext] = field(default_factory=list)
    service_name: str = "jarvis"

    def set_attribute(self, key: str, value: Any) -> None:
        """Set a span attribute."""
        self.attributes[key] = value

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        """Add an event to the span."""
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {}
        })

    def set_status(self, status: SpanStatus, message: str = "") -> None:
        """Set the span status."""
        self.status = status
        self.status_message = message

    def end(self, end_time: Optional[float] = None) -> None:
        """End the span."""
        self.end_time = end_time or time.time()

    @property
    def duration_ms(self) -> float:
        """Get span duration in milliseconds."""
        if self.end_time is None:
            return (time.time() - self.start_time) * 1000
        return (self.end_time - self.start_time) * 1000

    def to_dict(self) -> Dict[str, Any]:
        """Convert span to dictionary for serialization."""
        return {
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind.value,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "status_message": self.status_message,
            "attributes": self.attributes,
            "events": self.events,
            "service_name": self.service_name
        }


class DistributedTracer:
    """
    Gap 1: Distributed Tracing with W3C Trace Context propagation.

    Features:
    - W3C Trace Context compatible (traceparent/tracestate)
    - Async-safe context propagation
    - Span sampling and rate limiting
    - Cross-repo trace correlation
    - Automatic span hierarchy management
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._traces_dir = config.base_dir / "traces"
        self._traces_dir.mkdir(parents=True, exist_ok=True)

        # Context storage (thread-local + async task context)
        self._context_var: Dict[int, List[Span]] = {}
        self._lock = asyncio.Lock()

        # Trace storage
        self._active_traces: Dict[str, List[Span]] = {}
        self._completed_traces: Deque[Dict[str, Any]] = deque(maxlen=1000)

        # Sampling
        self._sample_rate = config.trace_sample_rate
        self._sampled_traces: Set[str] = set()

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        logger.info(f"DistributedTracer initialized (sample_rate={self._sample_rate})")

    async def start(self) -> None:
        """Start the tracer background tasks."""
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("DistributedTracer started")

    async def stop(self) -> None:
        """Stop the tracer and flush pending traces."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_traces()
        logger.info("DistributedTracer stopped")

    def _should_sample(self, trace_id: str) -> bool:
        """Determine if a trace should be sampled."""
        if trace_id in self._sampled_traces:
            return True

        # Consistent sampling based on trace_id
        hash_val = int(hashlib.md5(trace_id.encode()).hexdigest()[:8], 16)
        sampled = (hash_val / 0xFFFFFFFF) < self._sample_rate

        if sampled:
            self._sampled_traces.add(trace_id)

        return sampled

    def _get_task_id(self) -> int:
        """Get current async task or thread ID."""
        try:
            task = asyncio.current_task()
            if task:
                return id(task)
        except RuntimeError:
            pass
        return threading.current_thread().ident or 0

    def _get_current_span(self) -> Optional[Span]:
        """Get the current active span."""
        task_id = self._get_task_id()
        spans = self._context_var.get(task_id, [])
        return spans[-1] if spans else None

    def _push_span(self, span: Span) -> None:
        """Push a span onto the context stack."""
        task_id = self._get_task_id()
        if task_id not in self._context_var:
            self._context_var[task_id] = []
        self._context_var[task_id].append(span)

    def _pop_span(self) -> Optional[Span]:
        """Pop a span from the context stack."""
        task_id = self._get_task_id()
        spans = self._context_var.get(task_id, [])
        if spans:
            return spans.pop()
        return None

    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        parent_context: Optional[SpanContext] = None,
        attributes: Optional[Dict[str, Any]] = None
    ):
        """Start a new span as an async context manager."""
        # Determine parent
        current_span = self._get_current_span()

        if parent_context:
            # Use provided parent context
            context = SpanContext(
                trace_id=parent_context.trace_id,
                span_id=uuid.uuid4().hex[:16],
                trace_flags=parent_context.trace_flags,
                trace_state=parent_context.trace_state.copy()
            )
            parent_span_id = parent_context.span_id
        elif current_span:
            # Child of current span
            context = SpanContext(
                trace_id=current_span.context.trace_id,
                span_id=uuid.uuid4().hex[:16],
                trace_flags=current_span.context.trace_flags,
                trace_state=current_span.context.trace_state.copy()
            )
            parent_span_id = current_span.context.span_id
        else:
            # New root span
            context = SpanContext.generate()
            parent_span_id = None

        # Check sampling
        if not self._should_sample(context.trace_id):
            # Return a no-op span for unsampled traces
            yield None
            return

        span = Span(
            context=context,
            name=name,
            kind=kind,
            parent_span_id=parent_span_id,
            attributes=attributes or {},
            service_name=self.config.service_name
        )

        self._push_span(span)

        # Track active trace
        if context.trace_id not in self._active_traces:
            self._active_traces[context.trace_id] = []
        self._active_traces[context.trace_id].append(span)

        try:
            yield span
            if span.status == SpanStatus.UNSET:
                span.set_status(SpanStatus.OK)
        except Exception as e:
            span.set_status(SpanStatus.ERROR, str(e))
            span.set_attribute("error.type", type(e).__name__)
            span.set_attribute("error.message", str(e))
            span.set_attribute("error.stack", traceback.format_exc())
            raise
        finally:
            span.end()
            self._pop_span()

    def inject_context(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Inject trace context into headers for cross-repo propagation."""
        current_span = self._get_current_span()
        if current_span and current_span.context.is_sampled:
            headers["traceparent"] = current_span.context.to_traceparent()
            if current_span.context.trace_state:
                headers["tracestate"] = current_span.context.to_tracestate()
        return headers

    def extract_context(self, headers: Dict[str, str]) -> Optional[SpanContext]:
        """Extract trace context from incoming headers."""
        traceparent = headers.get("traceparent") or headers.get("Traceparent")
        if traceparent:
            context = SpanContext.from_traceparent(traceparent)
            if context:
                # Parse tracestate if present
                tracestate = headers.get("tracestate") or headers.get("Tracestate")
                if tracestate:
                    for pair in tracestate.split(","):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            context.trace_state[k.strip()] = v.strip()
                return context
        return None

    def get_current_trace_id(self) -> Optional[str]:
        """Get the current trace ID."""
        span = self._get_current_span()
        return span.context.trace_id if span else None

    async def _flush_loop(self) -> None:
        """Background loop to flush completed traces."""
        while self._running:
            try:
                await asyncio.sleep(30.0)  # Flush every 30 seconds
                await self._flush_traces()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in trace flush loop: {e}")

    async def _flush_traces(self) -> None:
        """Flush completed traces to disk."""
        async with self._lock:
            # Find completed traces (all spans ended)
            completed = []
            for trace_id, spans in list(self._active_traces.items()):
                if all(s.end_time is not None for s in spans):
                    completed.append(trace_id)

            # Write completed traces to disk
            for trace_id in completed:
                spans = self._active_traces.pop(trace_id)
                trace_data = {
                    "trace_id": trace_id,
                    "spans": [s.to_dict() for s in spans],
                    "span_count": len(spans),
                    "start_time": min(s.start_time for s in spans),
                    "end_time": max(s.end_time for s in spans if s.end_time),
                    "duration_ms": sum(s.duration_ms for s in spans),
                    "service_names": list(set(s.service_name for s in spans))
                }

                # Store in memory
                self._completed_traces.append(trace_data)

                # Write to file
                trace_file = self._traces_dir / f"{trace_id}.json"
                try:
                    await asyncio.to_thread(
                        trace_file.write_text,
                        json.dumps(trace_data, indent=2)
                    )
                except Exception as e:
                    logger.error(f"Failed to write trace {trace_id}: {e}")

                # Cleanup sampled set
                self._sampled_traces.discard(trace_id)

    def get_metrics(self) -> Dict[str, Any]:
        """Get tracer metrics."""
        return {
            "active_traces": len(self._active_traces),
            "completed_traces": len(self._completed_traces),
            "sampled_traces": len(self._sampled_traces),
            "sample_rate": self._sample_rate,
            "running": self._running
        }


# =============================================================================
# GAP 2: CROSS-REPO METRICS COLLECTION (Prometheus-compatible)
# =============================================================================

class MetricType(str, Enum):
    """Types of metrics."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


@dataclass
class MetricSample:
    """A single metric sample."""
    name: str
    value: float
    timestamp: float
    labels: Dict[str, str]
    metric_type: MetricType


@dataclass
class HistogramBucket:
    """A histogram bucket."""
    le: float  # Less than or equal
    count: int = 0


class Counter:
    """Prometheus-compatible counter metric."""

    def __init__(self, name: str, description: str, labels: List[str]):
        self.name = name
        self.description = description
        self.label_names = labels
        self._values: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels) -> None:
        """Increment the counter."""
        if value < 0:
            raise ValueError("Counter can only be incremented")
        label_values = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[label_values] += value

    def labels(self, **label_values) -> 'Counter':
        """Return a child counter with labels."""
        # Returns self for chaining - labels are passed to inc()
        return self

    def get_samples(self) -> List[MetricSample]:
        """Get all metric samples."""
        samples = []
        with self._lock:
            for label_values, value in self._values.items():
                labels = dict(zip(self.label_names, label_values))
                samples.append(MetricSample(
                    name=self.name,
                    value=value,
                    timestamp=time.time(),
                    labels=labels,
                    metric_type=MetricType.COUNTER
                ))
        return samples


class Gauge:
    """Prometheus-compatible gauge metric."""

    def __init__(self, name: str, description: str, labels: List[str]):
        self.name = name
        self.description = description
        self.label_names = labels
        self._values: Dict[Tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels) -> None:
        """Set the gauge value."""
        label_values = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[label_values] = value

    def inc(self, value: float = 1.0, **labels) -> None:
        """Increment the gauge."""
        label_values = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            self._values[label_values] = self._values.get(label_values, 0) + value

    def dec(self, value: float = 1.0, **labels) -> None:
        """Decrement the gauge."""
        self.inc(-value, **labels)

    def get_samples(self) -> List[MetricSample]:
        """Get all metric samples."""
        samples = []
        with self._lock:
            for label_values, value in self._values.items():
                labels = dict(zip(self.label_names, label_values))
                samples.append(MetricSample(
                    name=self.name,
                    value=value,
                    timestamp=time.time(),
                    labels=labels,
                    metric_type=MetricType.GAUGE
                ))
        return samples


class Histogram:
    """Prometheus-compatible histogram metric."""

    def __init__(
        self,
        name: str,
        description: str,
        labels: List[str],
        buckets: Optional[List[float]] = None
    ):
        self.name = name
        self.description = description
        self.label_names = labels
        self.bucket_bounds = sorted(buckets or [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10])
        self._buckets: Dict[Tuple[str, ...], List[int]] = {}
        self._sums: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._counts: Dict[Tuple[str, ...], int] = defaultdict(int)
        self._lock = threading.Lock()

    def observe(self, value: float, **labels) -> None:
        """Record an observation."""
        label_values = tuple(labels.get(l, "") for l in self.label_names)
        with self._lock:
            if label_values not in self._buckets:
                self._buckets[label_values] = [0] * len(self.bucket_bounds)

            for i, bound in enumerate(self.bucket_bounds):
                if value <= bound:
                    self._buckets[label_values][i] += 1

            self._sums[label_values] += value
            self._counts[label_values] += 1

    @contextmanager
    def time(self, **labels):
        """Context manager to time a block of code."""
        start = time.time()
        try:
            yield
        finally:
            self.observe(time.time() - start, **labels)

    def get_samples(self) -> List[MetricSample]:
        """Get all metric samples (buckets, sum, count)."""
        samples = []
        now = time.time()
        with self._lock:
            for label_values, bucket_counts in self._buckets.items():
                labels = dict(zip(self.label_names, label_values))

                # Bucket samples
                cumulative = 0
                for i, bound in enumerate(self.bucket_bounds):
                    cumulative += bucket_counts[i]
                    bucket_labels = {**labels, "le": str(bound)}
                    samples.append(MetricSample(
                        name=f"{self.name}_bucket",
                        value=cumulative,
                        timestamp=now,
                        labels=bucket_labels,
                        metric_type=MetricType.HISTOGRAM
                    ))

                # +Inf bucket
                samples.append(MetricSample(
                    name=f"{self.name}_bucket",
                    value=self._counts[label_values],
                    timestamp=now,
                    labels={**labels, "le": "+Inf"},
                    metric_type=MetricType.HISTOGRAM
                ))

                # Sum and count
                samples.append(MetricSample(
                    name=f"{self.name}_sum",
                    value=self._sums[label_values],
                    timestamp=now,
                    labels=labels,
                    metric_type=MetricType.HISTOGRAM
                ))
                samples.append(MetricSample(
                    name=f"{self.name}_count",
                    value=self._counts[label_values],
                    timestamp=now,
                    labels=labels,
                    metric_type=MetricType.HISTOGRAM
                ))

        return samples


class MetricsCollector:
    """
    Gap 2: Cross-Repo Metrics Collection with Prometheus-compatible format.

    Features:
    - Counter, Gauge, Histogram, Summary metrics
    - Label support for dimensional metrics
    - Prometheus exposition format output
    - Cross-repo metrics aggregation via shared directory
    - Automatic metric cleanup and rotation
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._metrics_dir = config.base_dir / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)

        # Metric registries
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._lock = threading.Lock()

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Default metrics
        self._init_default_metrics()

        logger.info("MetricsCollector initialized")

    def _init_default_metrics(self) -> None:
        """Initialize default system metrics."""
        self.counter(
            "trinity_requests_total",
            "Total number of requests",
            ["service", "method", "status"]
        )
        self.histogram(
            "trinity_request_duration_seconds",
            "Request duration in seconds",
            ["service", "method"]
        )
        self.gauge(
            "trinity_active_requests",
            "Number of active requests",
            ["service"]
        )

    async def start(self) -> None:
        """Start the metrics collector."""
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("MetricsCollector started")

    async def stop(self) -> None:
        """Stop the metrics collector."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_metrics()
        logger.info("MetricsCollector stopped")

    def counter(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None
    ) -> Counter:
        """Create or get a counter metric."""
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, description, labels or [])
            return self._counters[name]

    def gauge(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None
    ) -> Gauge:
        """Create or get a gauge metric."""
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, description, labels or [])
            return self._gauges[name]

    def histogram(
        self,
        name: str,
        description: str,
        labels: Optional[List[str]] = None,
        buckets: Optional[List[float]] = None
    ) -> Histogram:
        """Create or get a histogram metric."""
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(
                    name, description, labels or [],
                    buckets or self.config.histogram_buckets
                )
            return self._histograms[name]

    def get_all_samples(self) -> List[MetricSample]:
        """Get all metric samples."""
        samples = []
        with self._lock:
            for counter in self._counters.values():
                samples.extend(counter.get_samples())
            for gauge in self._gauges.values():
                samples.extend(gauge.get_samples())
            for histogram in self._histograms.values():
                samples.extend(histogram.get_samples())
        return samples

    def to_prometheus_format(self) -> str:
        """Export metrics in Prometheus exposition format."""
        lines = []
        samples = self.get_all_samples()

        # Group by metric name
        by_name: Dict[str, List[MetricSample]] = defaultdict(list)
        for sample in samples:
            base_name = sample.name.rsplit("_", 1)[0] if sample.name.endswith(("_bucket", "_sum", "_count")) else sample.name
            by_name[base_name].append(sample)

        for name, metric_samples in by_name.items():
            if metric_samples:
                metric_type = metric_samples[0].metric_type
                lines.append(f"# TYPE {name} {metric_type.value}")

                for sample in metric_samples:
                    label_str = ",".join(f'{k}="{v}"' for k, v in sample.labels.items())
                    if label_str:
                        lines.append(f"{sample.name}{{{label_str}}} {sample.value}")
                    else:
                        lines.append(f"{sample.name} {sample.value}")

        return "\n".join(lines)

    async def _flush_loop(self) -> None:
        """Background loop to flush metrics."""
        while self._running:
            try:
                await asyncio.sleep(self.config.metrics_flush_interval)
                await self._flush_metrics()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics flush loop: {e}")

    async def _flush_metrics(self) -> None:
        """Flush metrics to shared directory."""
        try:
            metrics_file = self._metrics_dir / f"{self.config.node_id}.prom"
            content = self.to_prometheus_format()
            await asyncio.to_thread(metrics_file.write_text, content)
        except Exception as e:
            logger.error(f"Failed to flush metrics: {e}")

    async def collect_cross_repo_metrics(self) -> Dict[str, List[MetricSample]]:
        """Collect metrics from all repos."""
        all_metrics = {}
        try:
            for prom_file in self._metrics_dir.glob("*.prom"):
                node_id = prom_file.stem
                content = await asyncio.to_thread(prom_file.read_text)
                all_metrics[node_id] = self._parse_prometheus(content)
        except Exception as e:
            logger.error(f"Failed to collect cross-repo metrics: {e}")
        return all_metrics

    def _parse_prometheus(self, content: str) -> List[MetricSample]:
        """Parse Prometheus format metrics."""
        samples = []
        current_type = MetricType.GAUGE

        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                if line.startswith("# TYPE"):
                    parts = line.split()
                    if len(parts) >= 4:
                        type_str = parts[3]
                        current_type = MetricType(type_str) if type_str in MetricType.__members__.values() else MetricType.GAUGE
                continue

            # Parse metric line
            match = re.match(r'(\w+)(?:\{([^}]*)\})?\s+([0-9.e+-]+)', line)
            if match:
                name, labels_str, value = match.groups()
                labels = {}
                if labels_str:
                    for pair in labels_str.split(","):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            labels[k] = v.strip('"')

                samples.append(MetricSample(
                    name=name,
                    value=float(value),
                    timestamp=time.time(),
                    labels=labels,
                    metric_type=current_type
                ))

        return samples

    def get_metrics(self) -> Dict[str, Any]:
        """Get collector metrics."""
        return {
            "counters": len(self._counters),
            "gauges": len(self._gauges),
            "histograms": len(self._histograms),
            "total_samples": len(self.get_all_samples()),
            "running": self._running
        }


# =============================================================================
# GAP 3: CROSS-REPO LOGGING (Centralized Structured Logging)
# =============================================================================

class LogLevel(str, Enum):
    """Log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def numeric(self) -> int:
        """Get numeric log level."""
        return {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50
        }[self.value]


@dataclass
class LogEntry:
    """A structured log entry."""
    timestamp: float
    level: LogLevel
    message: str
    service: str
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    exception: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "level": self.level.value,
            "message": self.message,
            "service": self.service,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "attributes": self.attributes,
            "exception": self.exception
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class CentralizedLogger:
    """
    Gap 3: Cross-Repo Centralized Logging with correlation.

    Features:
    - Structured JSON logging
    - Trace ID correlation for cross-repo debugging
    - Log aggregation via shared directory
    - Automatic log rotation and cleanup
    - Real-time log streaming
    """

    def __init__(self, config: ObservabilityConfig, tracer: Optional[DistributedTracer] = None):
        self.config = config
        self.tracer = tracer
        self._logs_dir = config.base_dir / "logs"
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        self._log_level = LogLevel(config.log_level)
        self._buffer: Deque[LogEntry] = deque(maxlen=10000)
        self._lock = asyncio.Lock()

        # Log file management
        self._current_log_file: Optional[Path] = None
        self._current_log_size = 0
        self._max_log_size = config.max_log_size_mb * 1024 * 1024

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Subscribers for real-time streaming
        self._subscribers: List[Callable[[LogEntry], None]] = []

        logger.info("CentralizedLogger initialized")

    async def start(self) -> None:
        """Start the centralized logger."""
        self._running = True
        self._rotate_log_file()
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("CentralizedLogger started")

    async def stop(self) -> None:
        """Stop the centralized logger."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_logs()
        logger.info("CentralizedLogger stopped")

    def _rotate_log_file(self) -> None:
        """Rotate to a new log file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._current_log_file = self._logs_dir / f"{self.config.service_name}_{timestamp}.jsonl"
        self._current_log_size = 0

    def _should_log(self, level: LogLevel) -> bool:
        """Check if level should be logged."""
        return level.numeric >= self._log_level.numeric

    def _get_trace_context(self) -> Tuple[Optional[str], Optional[str]]:
        """Get current trace context."""
        if self.tracer:
            span = self.tracer._get_current_span()
            if span:
                return span.context.trace_id, span.context.span_id
        return None, None

    async def log(
        self,
        level: LogLevel,
        message: str,
        **attributes
    ) -> None:
        """Log a message."""
        if not self._should_log(level):
            return

        trace_id, span_id = self._get_trace_context()

        entry = LogEntry(
            timestamp=time.time(),
            level=level,
            message=message,
            service=self.config.service_name,
            trace_id=trace_id,
            span_id=span_id,
            attributes=attributes
        )

        async with self._lock:
            self._buffer.append(entry)

        # Notify subscribers
        for subscriber in self._subscribers:
            try:
                subscriber(entry)
            except Exception as e:
                logger.error(f"Log subscriber error: {e}")

    async def debug(self, message: str, **attrs) -> None:
        await self.log(LogLevel.DEBUG, message, **attrs)

    async def info(self, message: str, **attrs) -> None:
        await self.log(LogLevel.INFO, message, **attrs)

    async def warning(self, message: str, **attrs) -> None:
        await self.log(LogLevel.WARNING, message, **attrs)

    async def error(self, message: str, exception: Optional[Exception] = None, **attrs) -> None:
        if exception:
            attrs["exception"] = traceback.format_exc()
        await self.log(LogLevel.ERROR, message, **attrs)

    async def critical(self, message: str, exception: Optional[Exception] = None, **attrs) -> None:
        if exception:
            attrs["exception"] = traceback.format_exc()
        await self.log(LogLevel.CRITICAL, message, **attrs)

    def subscribe(self, callback: Callable[[LogEntry], None]) -> None:
        """Subscribe to log entries."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[LogEntry], None]) -> None:
        """Unsubscribe from log entries."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def _flush_loop(self) -> None:
        """Background loop to flush logs."""
        while self._running:
            try:
                await asyncio.sleep(1.0)  # Flush every second
                await self._flush_logs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in log flush loop: {e}")

    async def _flush_logs(self) -> None:
        """Flush buffered logs to disk."""
        async with self._lock:
            if not self._buffer:
                return

            entries = list(self._buffer)
            self._buffer.clear()

        if not entries:
            return

        try:
            # Check for rotation
            if self._current_log_size > self._max_log_size:
                self._rotate_log_file()

            # Write entries
            lines = [entry.to_json() for entry in entries]
            content = "\n".join(lines) + "\n"

            if self._current_log_file:
                await asyncio.to_thread(
                    lambda: self._current_log_file.open("a").write(content)
                )
                self._current_log_size += len(content)

        except Exception as e:
            logger.error(f"Failed to flush logs: {e}")

    async def search_logs(
        self,
        query: Optional[str] = None,
        level: Optional[LogLevel] = None,
        trace_id: Optional[str] = None,
        service: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100
    ) -> List[LogEntry]:
        """Search logs across all files."""
        results = []

        try:
            for log_file in sorted(self._logs_dir.glob("*.jsonl"), reverse=True):
                if len(results) >= limit:
                    break

                content = await asyncio.to_thread(log_file.read_text)
                for line in content.split("\n"):
                    if not line.strip():
                        continue

                    try:
                        data = json.loads(line)
                        entry = LogEntry(
                            timestamp=data["timestamp"],
                            level=LogLevel(data["level"]),
                            message=data["message"],
                            service=data["service"],
                            trace_id=data.get("trace_id"),
                            span_id=data.get("span_id"),
                            attributes=data.get("attributes", {}),
                            exception=data.get("exception")
                        )

                        # Apply filters
                        if level and entry.level != level:
                            continue
                        if trace_id and entry.trace_id != trace_id:
                            continue
                        if service and entry.service != service:
                            continue
                        if start_time and entry.timestamp < start_time:
                            continue
                        if end_time and entry.timestamp > end_time:
                            continue
                        if query and query.lower() not in entry.message.lower():
                            continue

                        results.append(entry)

                        if len(results) >= limit:
                            break

                    except (json.JSONDecodeError, KeyError):
                        continue

        except Exception as e:
            logger.error(f"Failed to search logs: {e}")

        return results

    def get_metrics(self) -> Dict[str, Any]:
        """Get logger metrics."""
        return {
            "buffer_size": len(self._buffer),
            "log_level": self._log_level.value,
            "subscribers": len(self._subscribers),
            "current_file": str(self._current_log_file) if self._current_log_file else None,
            "current_size_mb": self._current_log_size / (1024 * 1024),
            "running": self._running
        }


# =============================================================================
# GAP 4: PERFORMANCE PROFILING (Async Profiler)
# =============================================================================

@dataclass
class ProfileSample:
    """A profiling sample."""
    timestamp: float
    function: str
    file: str
    line: int
    duration_us: float
    call_count: int


@dataclass
class FlameNode:
    """A node in a flame graph."""
    name: str
    value: int = 0
    children: Dict[str, 'FlameNode'] = field(default_factory=dict)

    def add_stack(self, stack: List[str], value: int = 1) -> None:
        """Add a stack trace to the flame graph."""
        if not stack:
            self.value += value
            return

        frame = stack[0]
        if frame not in self.children:
            self.children[frame] = FlameNode(name=frame)
        self.children[frame].add_stack(stack[1:], value)
        self.value += value

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "value": self.value,
            "children": [child.to_dict() for child in self.children.values()]
        }


class PerformanceProfiler:
    """
    Gap 4: Performance Profiling with async support.

    Features:
    - Statistical sampling profiler
    - Async-aware profiling
    - Flame graph generation
    - Function-level timing
    - Cross-repo profile aggregation
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._profiles_dir = config.base_dir / "profiles"
        self._profiles_dir.mkdir(parents=True, exist_ok=True)

        self._sample_rate = config.profile_sample_rate
        self._samples: Deque[ProfileSample] = deque(maxlen=100000)
        self._flame_root = FlameNode(name="root")
        self._lock = asyncio.Lock()

        # Function timing
        self._function_times: Dict[str, List[float]] = defaultdict(list)
        self._function_counts: Dict[str, int] = defaultdict(int)

        # Background profiling
        self._profile_task: Optional[asyncio.Task] = None
        self._running = False
        self._profiling_active = False

        logger.info("PerformanceProfiler initialized")

    async def start(self) -> None:
        """Start the profiler."""
        self._running = True
        if self.config.profiling_enabled:
            self._profile_task = asyncio.create_task(self._profile_loop())
        logger.info("PerformanceProfiler started")

    async def stop(self) -> None:
        """Stop the profiler."""
        self._running = False
        self._profiling_active = False
        if self._profile_task:
            self._profile_task.cancel()
            try:
                await self._profile_task
            except asyncio.CancelledError:
                pass
        await self._save_profile()
        logger.info("PerformanceProfiler stopped")

    @asynccontextmanager
    async def profile_function(self, name: str):
        """Profile a function's execution time."""
        start = time.perf_counter()
        try:
            yield
        finally:
            duration = (time.perf_counter() - start) * 1_000_000  # microseconds

            async with self._lock:
                self._function_times[name].append(duration)
                self._function_counts[name] += 1

                # Keep only last 1000 samples per function
                if len(self._function_times[name]) > 1000:
                    self._function_times[name] = self._function_times[name][-1000:]

    def profile_sync(self, name: str):
        """Decorator for profiling synchronous functions."""
        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @wraps(func)
            def wrapper(*args, **kwargs) -> T:
                start = time.perf_counter()
                try:
                    return func(*args, **kwargs)
                finally:
                    duration = (time.perf_counter() - start) * 1_000_000
                    self._function_times[name].append(duration)
                    self._function_counts[name] += 1
            return wrapper
        return decorator

    def profile_async(self, name: str):
        """Decorator for profiling async functions."""
        def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
            @wraps(func)
            async def wrapper(*args, **kwargs) -> T:
                async with self.profile_function(name):
                    return await func(*args, **kwargs)
            return wrapper
        return decorator

    async def start_profiling_session(self, duration: Optional[float] = None) -> str:
        """Start a profiling session."""
        session_id = uuid.uuid4().hex[:8]
        self._profiling_active = True

        if duration:
            asyncio.create_task(self._end_session_after(session_id, duration))

        logger.info(f"Started profiling session {session_id}")
        return session_id

    async def _end_session_after(self, session_id: str, duration: float) -> None:
        """End a profiling session after a duration."""
        await asyncio.sleep(duration)
        self._profiling_active = False
        await self._save_profile(session_id)
        logger.info(f"Ended profiling session {session_id}")

    async def _profile_loop(self) -> None:
        """Background profiling loop."""
        while self._running:
            try:
                if self._profiling_active and random.random() < self._sample_rate:
                    await self._capture_sample()
                await asyncio.sleep(0.001)  # 1ms sampling interval
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in profile loop: {e}")

    async def _capture_sample(self) -> None:
        """Capture a profiling sample."""
        try:
            # Get current stack frames
            frames = []
            for frame_info in traceback.extract_stack()[:-2]:  # Exclude profiler frames
                frame_name = f"{frame_info.filename}:{frame_info.name}:{frame_info.lineno}"
                frames.append(frame_name)

            if frames:
                async with self._lock:
                    self._flame_root.add_stack(frames)

        except Exception as e:
            logger.error(f"Failed to capture sample: {e}")

    async def _save_profile(self, session_id: Optional[str] = None) -> None:
        """Save profile to disk."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            profile_id = session_id or timestamp

            # Save flame graph
            flame_file = self._profiles_dir / f"flame_{profile_id}.json"
            async with self._lock:
                flame_data = self._flame_root.to_dict()

            await asyncio.to_thread(
                flame_file.write_text,
                json.dumps(flame_data, indent=2)
            )

            # Save function stats
            stats_file = self._profiles_dir / f"stats_{profile_id}.json"
            stats = await self.get_function_stats()
            await asyncio.to_thread(
                stats_file.write_text,
                json.dumps(stats, indent=2)
            )

        except Exception as e:
            logger.error(f"Failed to save profile: {e}")

    async def get_function_stats(self) -> Dict[str, Any]:
        """Get function timing statistics."""
        stats = {}
        async with self._lock:
            for name, times in self._function_times.items():
                if times:
                    sorted_times = sorted(times)
                    stats[name] = {
                        "count": self._function_counts[name],
                        "total_us": sum(times),
                        "mean_us": sum(times) / len(times),
                        "min_us": min(times),
                        "max_us": max(times),
                        "p50_us": sorted_times[len(sorted_times) // 2],
                        "p95_us": sorted_times[int(len(sorted_times) * 0.95)] if len(sorted_times) > 20 else max(times),
                        "p99_us": sorted_times[int(len(sorted_times) * 0.99)] if len(sorted_times) > 100 else max(times)
                    }
        return stats

    async def get_hotspots(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get the hottest functions by total time."""
        stats = await self.get_function_stats()
        sorted_stats = sorted(
            stats.items(),
            key=lambda x: x[1]["total_us"],
            reverse=True
        )
        return [
            {"function": name, **data}
            for name, data in sorted_stats[:limit]
        ]

    def get_metrics(self) -> Dict[str, Any]:
        """Get profiler metrics."""
        return {
            "profiling_active": self._profiling_active,
            "sample_rate": self._sample_rate,
            "functions_tracked": len(self._function_times),
            "flame_nodes": self._count_flame_nodes(self._flame_root),
            "running": self._running
        }

    def _count_flame_nodes(self, node: FlameNode) -> int:
        """Count nodes in flame graph."""
        return 1 + sum(self._count_flame_nodes(child) for child in node.children.values())


# =============================================================================
# GAP 5: ERROR AGGREGATION (Sentry-style)
# =============================================================================

@dataclass
class ErrorFingerprint:
    """Unique fingerprint for an error type."""
    exception_type: str
    exception_message: str
    stack_hash: str
    service: str

    @classmethod
    def from_exception(cls, exc: Exception, service: str, frames: int = 5) -> 'ErrorFingerprint':
        """Create fingerprint from exception."""
        tb = traceback.format_exc()
        # Hash the top N frames for fingerprinting
        stack_lines = tb.split("\n")[-frames * 2:]
        stack_hash = hashlib.md5("".join(stack_lines).encode()).hexdigest()[:16]

        return cls(
            exception_type=type(exc).__name__,
            exception_message=str(exc)[:200],
            stack_hash=stack_hash,
            service=service
        )

    @property
    def fingerprint_id(self) -> str:
        """Get unique fingerprint ID."""
        data = f"{self.exception_type}:{self.stack_hash}:{self.service}"
        return hashlib.md5(data.encode()).hexdigest()[:16]


@dataclass
class ErrorEvent:
    """A single error occurrence."""
    fingerprint: ErrorFingerprint
    timestamp: float
    trace_id: Optional[str]
    span_id: Optional[str]
    stack_trace: str
    context: Dict[str, Any]
    user_context: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "fingerprint_id": self.fingerprint.fingerprint_id,
            "exception_type": self.fingerprint.exception_type,
            "exception_message": self.fingerprint.exception_message,
            "service": self.fingerprint.service,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "stack_trace": self.stack_trace,
            "context": self.context,
            "user_context": self.user_context,
            "tags": self.tags
        }


@dataclass
class ErrorGroup:
    """A group of similar errors."""
    fingerprint: ErrorFingerprint
    first_seen: float
    last_seen: float
    count: int
    events: Deque[ErrorEvent]
    status: str = "unresolved"  # unresolved, resolved, ignored

    def add_event(self, event: ErrorEvent) -> None:
        """Add an error event to the group."""
        self.events.append(event)
        self.last_seen = event.timestamp
        self.count += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "fingerprint_id": self.fingerprint.fingerprint_id,
            "exception_type": self.fingerprint.exception_type,
            "exception_message": self.fingerprint.exception_message,
            "service": self.fingerprint.service,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "status": self.status,
            "recent_events": [e.to_dict() for e in list(self.events)[-10:]]
        }


class ErrorAggregator:
    """
    Gap 5: Error Aggregation with Sentry-style grouping.

    Features:
    - Error fingerprinting and deduplication
    - Stack trace analysis
    - Error grouping by type
    - Cross-repo error correlation
    - Error trends and statistics
    """

    def __init__(self, config: ObservabilityConfig, tracer: Optional[DistributedTracer] = None):
        self.config = config
        self.tracer = tracer
        self._errors_dir = config.base_dir / "errors"
        self._errors_dir.mkdir(parents=True, exist_ok=True)

        self._groups: Dict[str, ErrorGroup] = {}
        self._lock = asyncio.Lock()
        self._max_events_per_group = config.max_errors_per_group

        # Background tasks
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        # Subscribers
        self._subscribers: List[Callable[[ErrorEvent], None]] = []

        logger.info("ErrorAggregator initialized")

    async def start(self) -> None:
        """Start the error aggregator."""
        self._running = True
        await self._load_existing_groups()
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("ErrorAggregator started")

    async def stop(self) -> None:
        """Stop the error aggregator."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._save_groups()
        logger.info("ErrorAggregator stopped")

    async def capture_exception(
        self,
        exc: Exception,
        context: Optional[Dict[str, Any]] = None,
        user_context: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> str:
        """Capture an exception."""
        fingerprint = ErrorFingerprint.from_exception(
            exc,
            self.config.service_name,
            self.config.error_fingerprint_frames
        )

        trace_id, span_id = None, None
        if self.tracer:
            span = self.tracer._get_current_span()
            if span:
                trace_id = span.context.trace_id
                span_id = span.context.span_id

        event = ErrorEvent(
            fingerprint=fingerprint,
            timestamp=time.time(),
            trace_id=trace_id,
            span_id=span_id,
            stack_trace=traceback.format_exc(),
            context=context or {},
            user_context=user_context or {},
            tags=tags or {}
        )

        async with self._lock:
            fp_id = fingerprint.fingerprint_id
            if fp_id not in self._groups:
                self._groups[fp_id] = ErrorGroup(
                    fingerprint=fingerprint,
                    first_seen=event.timestamp,
                    last_seen=event.timestamp,
                    count=0,
                    events=deque(maxlen=self._max_events_per_group)
                )
            self._groups[fp_id].add_event(event)

        # Notify subscribers
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception as e:
                logger.error(f"Error subscriber error: {e}")

        return fp_id

    @asynccontextmanager
    async def capture_errors(
        self,
        context: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        reraise: bool = True
    ):
        """Context manager to capture errors."""
        try:
            yield
        except Exception as e:
            await self.capture_exception(e, context=context, tags=tags)
            if reraise:
                raise

    async def get_error_groups(
        self,
        status: Optional[str] = None,
        service: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get error groups."""
        async with self._lock:
            groups = list(self._groups.values())

        # Filter
        if status:
            groups = [g for g in groups if g.status == status]
        if service:
            groups = [g for g in groups if g.fingerprint.service == service]

        # Sort by count
        groups.sort(key=lambda g: g.count, reverse=True)

        return [g.to_dict() for g in groups[:limit]]

    async def resolve_error_group(self, fingerprint_id: str) -> bool:
        """Mark an error group as resolved."""
        async with self._lock:
            if fingerprint_id in self._groups:
                self._groups[fingerprint_id].status = "resolved"
                return True
        return False

    async def ignore_error_group(self, fingerprint_id: str) -> bool:
        """Mark an error group as ignored."""
        async with self._lock:
            if fingerprint_id in self._groups:
                self._groups[fingerprint_id].status = "ignored"
                return True
        return False

    def subscribe(self, callback: Callable[[ErrorEvent], None]) -> None:
        """Subscribe to error events."""
        self._subscribers.append(callback)

    async def _load_existing_groups(self) -> None:
        """Load existing error groups from disk."""
        try:
            groups_file = self._errors_dir / "groups.json"
            if groups_file.exists():
                content = await asyncio.to_thread(groups_file.read_text)
                data = json.loads(content)
                for group_data in data.get("groups", []):
                    fp = ErrorFingerprint(
                        exception_type=group_data["exception_type"],
                        exception_message=group_data["exception_message"],
                        stack_hash=group_data.get("stack_hash", ""),
                        service=group_data["service"]
                    )
                    self._groups[group_data["fingerprint_id"]] = ErrorGroup(
                        fingerprint=fp,
                        first_seen=group_data["first_seen"],
                        last_seen=group_data["last_seen"],
                        count=group_data["count"],
                        events=deque(maxlen=self._max_events_per_group),
                        status=group_data.get("status", "unresolved")
                    )
        except Exception as e:
            logger.error(f"Failed to load error groups: {e}")

    async def _flush_loop(self) -> None:
        """Background loop to flush error groups."""
        while self._running:
            try:
                await asyncio.sleep(30.0)
                await self._save_groups()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in error flush loop: {e}")

    async def _save_groups(self) -> None:
        """Save error groups to disk."""
        try:
            async with self._lock:
                groups_data = [g.to_dict() for g in self._groups.values()]

            groups_file = self._errors_dir / "groups.json"
            await asyncio.to_thread(
                groups_file.write_text,
                json.dumps({"groups": groups_data}, indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save error groups: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get error aggregator metrics."""
        return {
            "total_groups": len(self._groups),
            "total_errors": sum(g.count for g in self._groups.values()),
            "unresolved_groups": sum(1 for g in self._groups.values() if g.status == "unresolved"),
            "subscribers": len(self._subscribers),
            "running": self._running
        }


# =============================================================================
# GAP 6: HEALTH DASHBOARD
# =============================================================================

class HealthStatus(str, Enum):
    """Health status levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheck:
    """A single health check result."""
    name: str
    status: HealthStatus
    message: str
    latency_ms: float
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
            "metadata": self.metadata
        }


class HealthChecker(Protocol):
    """Protocol for health check functions."""
    async def __call__(self) -> HealthCheck: ...


@dataclass
class ServiceHealth:
    """Health status of a service."""
    service: str
    status: HealthStatus
    checks: List[HealthCheck]
    timestamp: float

    @property
    def overall_status(self) -> HealthStatus:
        """Calculate overall status from checks."""
        if not self.checks:
            return HealthStatus.UNKNOWN
        if any(c.status == HealthStatus.UNHEALTHY for c in self.checks):
            return HealthStatus.UNHEALTHY
        if any(c.status == HealthStatus.DEGRADED for c in self.checks):
            return HealthStatus.DEGRADED
        if all(c.status == HealthStatus.HEALTHY for c in self.checks):
            return HealthStatus.HEALTHY
        return HealthStatus.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "service": self.service,
            "status": self.overall_status.value,
            "checks": [c.to_dict() for c in self.checks],
            "timestamp": self.timestamp
        }


class HealthDashboard:
    """
    Gap 6: Unified Health Dashboard for Trinity Ecosystem.

    Features:
    - Service health monitoring
    - Custom health checks
    - Cross-repo health aggregation
    - Health history tracking
    - Automatic health check scheduling
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._health_dir = config.base_dir / "health"
        self._health_dir.mkdir(parents=True, exist_ok=True)

        self._checks: Dict[str, Callable[[], Coroutine[Any, Any, HealthCheck]]] = {}
        self._history: Deque[ServiceHealth] = deque(maxlen=config.health_history_size)
        self._current_health: Optional[ServiceHealth] = None
        self._lock = asyncio.Lock()

        # Background tasks
        self._check_task: Optional[asyncio.Task] = None
        self._running = False

        # Default checks
        self._init_default_checks()

        logger.info("HealthDashboard initialized")

    def _init_default_checks(self) -> None:
        """Initialize default health checks."""
        async def check_memory() -> HealthCheck:
            start = time.time()
            try:
                memory = psutil.virtual_memory()
                used_percent = memory.percent
                status = HealthStatus.HEALTHY
                if used_percent > 90:
                    status = HealthStatus.UNHEALTHY
                elif used_percent > 80:
                    status = HealthStatus.DEGRADED

                return HealthCheck(
                    name="memory",
                    status=status,
                    message=f"Memory usage: {used_percent:.1f}%",
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time(),
                    metadata={"used_percent": used_percent, "available_gb": memory.available / (1024**3)}
                )
            except Exception as e:
                return HealthCheck(
                    name="memory",
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time()
                )

        async def check_disk() -> HealthCheck:
            start = time.time()
            try:
                disk = psutil.disk_usage("/")
                used_percent = disk.percent
                status = HealthStatus.HEALTHY
                if used_percent > 95:
                    status = HealthStatus.UNHEALTHY
                elif used_percent > 85:
                    status = HealthStatus.DEGRADED

                return HealthCheck(
                    name="disk",
                    status=status,
                    message=f"Disk usage: {used_percent:.1f}%",
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time(),
                    metadata={"used_percent": used_percent, "free_gb": disk.free / (1024**3)}
                )
            except Exception as e:
                return HealthCheck(
                    name="disk",
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time()
                )

        async def check_cpu() -> HealthCheck:
            start = time.time()
            try:
                # v258.0: Non-blocking via shared metrics service
                try:
                    from core.async_system_metrics import get_cpu_percent
                    cpu_percent = await get_cpu_percent()
                except ImportError:
                    cpu_percent = psutil.cpu_percent(interval=None)
                status = HealthStatus.HEALTHY
                if cpu_percent > 95:
                    status = HealthStatus.UNHEALTHY
                elif cpu_percent > 80:
                    status = HealthStatus.DEGRADED

                return HealthCheck(
                    name="cpu",
                    status=status,
                    message=f"CPU usage: {cpu_percent:.1f}%",
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time(),
                    metadata={"used_percent": cpu_percent, "cores": psutil.cpu_count()}
                )
            except Exception as e:
                return HealthCheck(
                    name="cpu",
                    status=HealthStatus.UNKNOWN,
                    message=str(e),
                    latency_ms=(time.time() - start) * 1000,
                    timestamp=time.time()
                )

        self._checks["memory"] = check_memory
        self._checks["disk"] = check_disk
        self._checks["cpu"] = check_cpu

    async def start(self) -> None:
        """Start the health dashboard."""
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("HealthDashboard started")

    async def stop(self) -> None:
        """Stop the health dashboard."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("HealthDashboard stopped")

    def register_check(
        self,
        name: str,
        check_fn: Callable[[], Coroutine[Any, Any, HealthCheck]]
    ) -> None:
        """Register a custom health check."""
        self._checks[name] = check_fn

    def unregister_check(self, name: str) -> None:
        """Unregister a health check."""
        if name in self._checks:
            del self._checks[name]

    async def run_checks(self) -> ServiceHealth:
        """Run all health checks."""
        checks = []

        for name, check_fn in self._checks.items():
            try:
                result = await asyncio.wait_for(
                    check_fn(),
                    timeout=self.config.health_timeout
                )
                checks.append(result)
            except asyncio.TimeoutError:
                checks.append(HealthCheck(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message="Health check timed out",
                    latency_ms=self.config.health_timeout * 1000,
                    timestamp=time.time()
                ))
            except Exception as e:
                checks.append(HealthCheck(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=str(e),
                    latency_ms=0,
                    timestamp=time.time()
                ))

        health = ServiceHealth(
            service=self.config.service_name,
            status=HealthStatus.UNKNOWN,
            checks=checks,
            timestamp=time.time()
        )
        health.status = health.overall_status

        async with self._lock:
            self._current_health = health
            self._history.append(health)

        # Save to disk for cross-repo access
        await self._save_health(health)

        return health

    async def _check_loop(self) -> None:
        """Background loop for health checks."""
        while self._running:
            try:
                await self.run_checks()
                await asyncio.sleep(self.config.health_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(1.0)

    async def _save_health(self, health: ServiceHealth) -> None:
        """Save health status to disk."""
        try:
            health_file = self._health_dir / f"{self.config.node_id}.json"
            await asyncio.to_thread(
                health_file.write_text,
                json.dumps(health.to_dict(), indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save health: {e}")

    async def get_current_health(self) -> Optional[ServiceHealth]:
        """Get current health status."""
        async with self._lock:
            return self._current_health

    async def get_health_history(self, limit: int = 100) -> List[ServiceHealth]:
        """Get health history."""
        async with self._lock:
            return list(self._history)[-limit:]

    async def get_cross_repo_health(self) -> Dict[str, ServiceHealth]:
        """Get health status from all repos."""
        all_health = {}
        try:
            for health_file in self._health_dir.glob("*.json"):
                try:
                    content = await asyncio.to_thread(health_file.read_text)
                    data = json.loads(content)
                    checks = [
                        HealthCheck(
                            name=c["name"],
                            status=HealthStatus(c["status"]),
                            message=c["message"],
                            latency_ms=c["latency_ms"],
                            timestamp=c["timestamp"],
                            metadata=c.get("metadata", {})
                        )
                        for c in data.get("checks", [])
                    ]
                    all_health[data["service"]] = ServiceHealth(
                        service=data["service"],
                        status=HealthStatus(data["status"]),
                        checks=checks,
                        timestamp=data["timestamp"]
                    )
                except Exception as e:
                    logger.error(f"Failed to parse health file {health_file}: {e}")
        except Exception as e:
            logger.error(f"Failed to get cross-repo health: {e}")
        return all_health

    def get_metrics(self) -> Dict[str, Any]:
        """Get health dashboard metrics."""
        return {
            "registered_checks": len(self._checks),
            "history_size": len(self._history),
            "current_status": self._current_health.overall_status.value if self._current_health else "unknown",
            "running": self._running
        }


# =============================================================================
# GAP 7: ALERT SYSTEM
# =============================================================================

class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertState(str, Enum):
    """Alert state."""
    FIRING = "firing"
    RESOLVED = "resolved"
    SILENCED = "silenced"


@dataclass
class Alert:
    """An alert instance."""
    alert_id: str
    name: str
    severity: AlertSeverity
    state: AlertState
    message: str
    source: str
    timestamp: float
    resolved_at: Optional[float] = None
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "alert_id": self.alert_id,
            "name": self.name,
            "severity": self.severity.value,
            "state": self.state.value,
            "message": self.message,
            "source": self.source,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "resolved_at": self.resolved_at,
            "labels": self.labels,
            "annotations": self.annotations
        }


@dataclass
class AlertRule:
    """An alert rule definition."""
    name: str
    condition: Callable[[], Coroutine[Any, Any, bool]]
    severity: AlertSeverity
    message_template: str
    cooldown_seconds: float = 300.0
    labels: Dict[str, str] = field(default_factory=dict)

    _last_fired: float = 0.0
    _current_alert: Optional[Alert] = None


class AlertManager:
    """
    Gap 7: Unified Alert System with deduplication.

    Features:
    - Rule-based alerting
    - Alert deduplication and grouping
    - Alert cooldown periods
    - Cross-repo alert aggregation
    - Multiple notification channels
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._alerts_dir = config.base_dir / "alerts"
        self._alerts_dir.mkdir(parents=True, exist_ok=True)

        self._rules: Dict[str, AlertRule] = {}
        self._active_alerts: Dict[str, Alert] = {}
        self._alert_history: Deque[Alert] = deque(maxlen=1000)
        self._lock = asyncio.Lock()

        # Rate limiting
        self._alerts_this_hour: int = 0
        self._hour_start: float = time.time()

        # Notification handlers
        self._handlers: List[Callable[[Alert], Coroutine[Any, Any, None]]] = []

        # Background tasks
        self._check_task: Optional[asyncio.Task] = None
        self._running = False

        logger.info("AlertManager initialized")

    async def start(self) -> None:
        """Start the alert manager."""
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("AlertManager started")

    async def stop(self) -> None:
        """Stop the alert manager."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        await self._save_alerts()
        logger.info("AlertManager stopped")

    def register_rule(self, rule: AlertRule) -> None:
        """Register an alert rule."""
        self._rules[rule.name] = rule

    def unregister_rule(self, name: str) -> None:
        """Unregister an alert rule."""
        if name in self._rules:
            del self._rules[name]

    def add_handler(self, handler: Callable[[Alert], Coroutine[Any, Any, None]]) -> None:
        """Add a notification handler."""
        self._handlers.append(handler)

    async def fire_alert(
        self,
        name: str,
        severity: AlertSeverity,
        message: str,
        labels: Optional[Dict[str, str]] = None
    ) -> Optional[Alert]:
        """Manually fire an alert."""
        # Check rate limiting
        now = time.time()
        if now - self._hour_start > 3600:
            self._hour_start = now
            self._alerts_this_hour = 0

        if self._alerts_this_hour >= self.config.max_alerts_per_hour:
            logger.warning("Alert rate limit reached")
            return None

        alert_id = hashlib.md5(f"{name}:{json.dumps(labels or {})}".encode()).hexdigest()[:16]

        async with self._lock:
            # Check for existing active alert
            if alert_id in self._active_alerts:
                existing = self._active_alerts[alert_id]
                # Update timestamp but don't create new alert
                existing.timestamp = now
                return existing

            alert = Alert(
                alert_id=alert_id,
                name=name,
                severity=severity,
                state=AlertState.FIRING,
                message=message,
                source=self.config.service_name,
                timestamp=now,
                labels=labels or {}
            )

            self._active_alerts[alert_id] = alert
            self._alert_history.append(alert)
            self._alerts_this_hour += 1

        # Notify handlers
        for handler in self._handlers:
            try:
                await handler(alert)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")

        return alert

    async def resolve_alert(self, alert_id: str) -> bool:
        """Resolve an alert."""
        async with self._lock:
            if alert_id in self._active_alerts:
                alert = self._active_alerts[alert_id]
                alert.state = AlertState.RESOLVED
                alert.resolved_at = time.time()
                del self._active_alerts[alert_id]
                return True
        return False

    async def silence_alert(self, alert_id: str, duration_seconds: float = 3600) -> bool:
        """Silence an alert for a duration."""
        async with self._lock:
            if alert_id in self._active_alerts:
                self._active_alerts[alert_id].state = AlertState.SILENCED
                # Schedule unsilence
                asyncio.create_task(self._unsilence_after(alert_id, duration_seconds))
                return True
        return False

    async def _unsilence_after(self, alert_id: str, duration: float) -> None:
        """Unsilence an alert after duration."""
        await asyncio.sleep(duration)
        async with self._lock:
            if alert_id in self._active_alerts:
                self._active_alerts[alert_id].state = AlertState.FIRING

    async def _check_loop(self) -> None:
        """Background loop to check alert rules."""
        while self._running:
            try:
                for name, rule in list(self._rules.items()):
                    await self._check_rule(rule)
                await asyncio.sleep(10.0)  # Check every 10 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in alert check loop: {e}")

    async def _check_rule(self, rule: AlertRule) -> None:
        """Check a single alert rule."""
        try:
            # Check cooldown
            now = time.time()
            if now - rule._last_fired < rule.cooldown_seconds:
                return

            # Evaluate condition
            should_fire = await rule.condition()

            if should_fire:
                rule._last_fired = now
                await self.fire_alert(
                    name=rule.name,
                    severity=rule.severity,
                    message=rule.message_template,
                    labels=rule.labels
                )
            elif rule._current_alert:
                # Condition cleared, resolve alert
                await self.resolve_alert(rule._current_alert.alert_id)
                rule._current_alert = None

        except Exception as e:
            logger.error(f"Error checking rule {rule.name}: {e}")

    async def get_active_alerts(self) -> List[Alert]:
        """Get all active alerts."""
        async with self._lock:
            return list(self._active_alerts.values())

    async def get_alert_history(self, limit: int = 100) -> List[Alert]:
        """Get alert history."""
        async with self._lock:
            return list(self._alert_history)[-limit:]

    async def _save_alerts(self) -> None:
        """Save alerts to disk."""
        try:
            async with self._lock:
                data = {
                    "active": [a.to_dict() for a in self._active_alerts.values()],
                    "history": [a.to_dict() for a in list(self._alert_history)[-100:]]
                }
            alerts_file = self._alerts_dir / f"{self.config.node_id}.json"
            await asyncio.to_thread(
                alerts_file.write_text,
                json.dumps(data, indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save alerts: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get alert manager metrics."""
        return {
            "registered_rules": len(self._rules),
            "active_alerts": len(self._active_alerts),
            "alerts_this_hour": self._alerts_this_hour,
            "history_size": len(self._alert_history),
            "running": self._running
        }


# =============================================================================
# GAP 8: DEPENDENCY GRAPH VISUALIZATION
# =============================================================================

@dataclass
class DependencyNode:
    """A node in the dependency graph."""
    name: str
    node_type: str  # service, module, function
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DependencyEdge:
    """An edge in the dependency graph."""
    source: str
    target: str
    edge_type: str  # calls, imports, depends_on
    weight: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


class DependencyGraph:
    """
    Gap 8: Dependency Graph Visualization.

    Features:
    - Dynamic dependency tracking
    - Service dependency mapping
    - Module/function level dependencies
    - GraphViz/Mermaid export
    - Cross-repo dependency aggregation
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._graphs_dir = config.base_dir / "graphs"
        self._graphs_dir.mkdir(parents=True, exist_ok=True)

        self._nodes: Dict[str, DependencyNode] = {}
        self._edges: List[DependencyEdge] = []
        self._lock = asyncio.Lock()

        logger.info("DependencyGraph initialized")

    async def add_node(
        self,
        name: str,
        node_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a node to the graph."""
        async with self._lock:
            self._nodes[name] = DependencyNode(
                name=name,
                node_type=node_type,
                metadata=metadata or {}
            )

    async def add_edge(
        self,
        source: str,
        target: str,
        edge_type: str = "depends_on",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add an edge to the graph."""
        async with self._lock:
            # Check for existing edge
            for edge in self._edges:
                if edge.source == source and edge.target == target:
                    edge.weight += 1
                    return

            self._edges.append(DependencyEdge(
                source=source,
                target=target,
                edge_type=edge_type,
                metadata=metadata or {}
            ))

    async def record_call(self, caller: str, callee: str) -> None:
        """Record a function call dependency."""
        await self.add_node(caller, "function")
        await self.add_node(callee, "function")
        await self.add_edge(caller, callee, "calls")

    async def record_import(self, importer: str, imported: str) -> None:
        """Record a module import dependency."""
        await self.add_node(importer, "module")
        await self.add_node(imported, "module")
        await self.add_edge(importer, imported, "imports")

    async def record_service_dependency(self, service: str, depends_on: str) -> None:
        """Record a service dependency."""
        await self.add_node(service, "service")
        await self.add_node(depends_on, "service")
        await self.add_edge(service, depends_on, "depends_on")

    async def to_mermaid(self) -> str:
        """Export graph as Mermaid diagram."""
        lines = ["graph TD"]

        async with self._lock:
            for name, node in self._nodes.items():
                safe_name = name.replace("-", "_").replace(".", "_")
                if node.node_type == "service":
                    lines.append(f"    {safe_name}[{name}]")
                elif node.node_type == "module":
                    lines.append(f"    {safe_name}({name})")
                else:
                    lines.append(f"    {safe_name}>{name}]")

            for edge in self._edges:
                src = edge.source.replace("-", "_").replace(".", "_")
                tgt = edge.target.replace("-", "_").replace(".", "_")
                if edge.edge_type == "calls":
                    lines.append(f"    {src} --> {tgt}")
                elif edge.edge_type == "imports":
                    lines.append(f"    {src} -.-> {tgt}")
                else:
                    lines.append(f"    {src} ==> {tgt}")

        return "\n".join(lines)

    async def to_graphviz(self) -> str:
        """Export graph as GraphViz DOT format."""
        lines = ["digraph Dependencies {", "    rankdir=LR;"]

        async with self._lock:
            for name, node in self._nodes.items():
                safe_name = name.replace("-", "_").replace(".", "_")
                shape = {"service": "box", "module": "ellipse", "function": "diamond"}.get(node.node_type, "ellipse")
                lines.append(f'    {safe_name} [label="{name}" shape={shape}];')

            for edge in self._edges:
                src = edge.source.replace("-", "_").replace(".", "_")
                tgt = edge.target.replace("-", "_").replace(".", "_")
                style = {"calls": "solid", "imports": "dashed", "depends_on": "bold"}.get(edge.edge_type, "solid")
                lines.append(f'    {src} -> {tgt} [style={style} label="{edge.edge_type}"];')

        lines.append("}")
        return "\n".join(lines)

    async def save_graph(self) -> None:
        """Save graph to disk."""
        try:
            mermaid = await self.to_mermaid()
            graphviz = await self.to_graphviz()

            mermaid_file = self._graphs_dir / f"{self.config.node_id}.mmd"
            graphviz_file = self._graphs_dir / f"{self.config.node_id}.dot"

            await asyncio.to_thread(mermaid_file.write_text, mermaid)
            await asyncio.to_thread(graphviz_file.write_text, graphviz)

            # Also save JSON
            async with self._lock:
                data = {
                    "nodes": [{"name": n.name, "type": n.node_type, "metadata": n.metadata} for n in self._nodes.values()],
                    "edges": [{"source": e.source, "target": e.target, "type": e.edge_type, "weight": e.weight} for e in self._edges]
                }
            json_file = self._graphs_dir / f"{self.config.node_id}.json"
            await asyncio.to_thread(json_file.write_text, json.dumps(data, indent=2))

        except Exception as e:
            logger.error(f"Failed to save graph: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Get graph metrics."""
        return {
            "nodes": len(self._nodes),
            "edges": len(self._edges),
            "node_types": list(set(n.node_type for n in self._nodes.values())),
            "edge_types": list(set(e.edge_type for e in self._edges))
        }


# =============================================================================
# GAP 9: REQUEST FLOW VISUALIZATION
# =============================================================================

@dataclass
class RequestStep:
    """A single step in a request flow."""
    step_id: str
    service: str
    operation: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"
    metadata: Dict[str, Any] = field(default_factory=dict)
    children: List['RequestStep'] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "service": self.service,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children]
        }


@dataclass
class RequestFlow:
    """A complete request flow."""
    flow_id: str
    name: str
    root_step: RequestStep
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "name": self.name,
            "root_step": self.root_step.to_dict(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_duration_ms": self.total_duration_ms,
            "status": self.status,
            "metadata": self.metadata
        }


class RequestFlowTracker:
    """
    Gap 9: Request Flow Visualization.

    Features:
    - Track request flow across services
    - Hierarchical step tracking
    - Timing analysis
    - Flow diagram generation
    - Bottleneck identification
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._flows_dir = config.base_dir / "flows"
        self._flows_dir.mkdir(parents=True, exist_ok=True)

        self._active_flows: Dict[str, RequestFlow] = {}
        self._completed_flows: Deque[RequestFlow] = deque(maxlen=1000)
        self._step_stack: Dict[str, List[RequestStep]] = {}
        self._lock = asyncio.Lock()

        logger.info("RequestFlowTracker initialized")

    async def start_flow(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Start tracking a new request flow."""
        flow_id = uuid.uuid4().hex[:16]
        now = time.time()

        root_step = RequestStep(
            step_id=f"{flow_id}_root",
            service=self.config.service_name,
            operation=name,
            start_time=now
        )

        flow = RequestFlow(
            flow_id=flow_id,
            name=name,
            root_step=root_step,
            start_time=now,
            metadata=metadata or {}
        )

        async with self._lock:
            self._active_flows[flow_id] = flow
            self._step_stack[flow_id] = [root_step]

        return flow_id

    async def add_step(
        self,
        flow_id: str,
        operation: str,
        service: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Add a step to an existing flow."""
        step_id = uuid.uuid4().hex[:8]

        step = RequestStep(
            step_id=step_id,
            service=service or self.config.service_name,
            operation=operation,
            start_time=time.time(),
            metadata=metadata or {}
        )

        async with self._lock:
            if flow_id not in self._step_stack:
                return ""

            stack = self._step_stack[flow_id]
            if stack:
                parent = stack[-1]
                parent.children.append(step)
            stack.append(step)

        return step_id

    async def end_step(self, flow_id: str, status: str = "success") -> None:
        """End the current step in a flow."""
        async with self._lock:
            if flow_id not in self._step_stack:
                return

            stack = self._step_stack[flow_id]
            if stack:
                step = stack.pop()
                step.end_time = time.time()
                step.status = status

    async def end_flow(self, flow_id: str, status: str = "success") -> Optional[RequestFlow]:
        """End a request flow."""
        async with self._lock:
            if flow_id not in self._active_flows:
                return None

            flow = self._active_flows.pop(flow_id)
            flow.end_time = time.time()
            flow.status = status
            flow.root_step.end_time = flow.end_time
            flow.root_step.status = status

            self._completed_flows.append(flow)

            if flow_id in self._step_stack:
                del self._step_stack[flow_id]

        # Save to disk
        await self._save_flow(flow)

        return flow

    async def _save_flow(self, flow: RequestFlow) -> None:
        """Save a completed flow to disk."""
        try:
            flow_file = self._flows_dir / f"{flow.flow_id}.json"
            await asyncio.to_thread(
                flow_file.write_text,
                json.dumps(flow.to_dict(), indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save flow: {e}")

    async def to_mermaid(self, flow: RequestFlow) -> str:
        """Generate Mermaid sequence diagram for a flow."""
        lines = ["sequenceDiagram"]
        participants = set()

        def add_steps(step: RequestStep, parent_service: str = "Client"):
            if step.service not in participants:
                participants.add(step.service)
                lines.insert(1, f"    participant {step.service}")

            lines.append(f"    {parent_service}->>+{step.service}: {step.operation}")

            for child in step.children:
                add_steps(child, step.service)

            status_note = "✓" if step.status == "success" else "✗"
            lines.append(f"    {step.service}-->>-{parent_service}: {status_note} ({step.duration_ms:.1f}ms)")

        add_steps(flow.root_step)
        return "\n".join(lines)

    async def get_active_flows(self) -> List[RequestFlow]:
        """Get all active flows."""
        async with self._lock:
            return list(self._active_flows.values())

    async def get_completed_flows(self, limit: int = 100) -> List[RequestFlow]:
        """Get completed flows."""
        async with self._lock:
            return list(self._completed_flows)[-limit:]

    async def find_bottlenecks(self, flow: RequestFlow) -> List[Dict[str, Any]]:
        """Find bottlenecks in a flow."""
        bottlenecks = []

        def analyze_step(step: RequestStep, depth: int = 0):
            if step.duration_ms > 100:  # Steps over 100ms
                bottlenecks.append({
                    "step_id": step.step_id,
                    "service": step.service,
                    "operation": step.operation,
                    "duration_ms": step.duration_ms,
                    "depth": depth
                })
            for child in step.children:
                analyze_step(child, depth + 1)

        analyze_step(flow.root_step)
        bottlenecks.sort(key=lambda x: x["duration_ms"], reverse=True)
        return bottlenecks

    def get_metrics(self) -> Dict[str, Any]:
        """Get tracker metrics."""
        return {
            "active_flows": len(self._active_flows),
            "completed_flows": len(self._completed_flows),
            "running": True
        }


# =============================================================================
# GAP 10: RESOURCE USAGE MONITORING
# =============================================================================

@dataclass
class ResourceSnapshot:
    """A snapshot of resource usage."""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    memory_available_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_free_gb: float
    network_bytes_sent: int
    network_bytes_recv: int
    open_files: int
    threads: int
    process_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp).isoformat(),
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
            "memory_used_gb": self.memory_used_gb,
            "memory_available_gb": self.memory_available_gb,
            "disk_percent": self.disk_percent,
            "disk_used_gb": self.disk_used_gb,
            "disk_free_gb": self.disk_free_gb,
            "network_bytes_sent": self.network_bytes_sent,
            "network_bytes_recv": self.network_bytes_recv,
            "open_files": self.open_files,
            "threads": self.threads,
            "process_count": self.process_count
        }


class ResourceMonitor:
    """
    Gap 10: Resource Usage Monitoring.

    Features:
    - CPU/Memory/Disk/Network monitoring
    - Historical resource tracking
    - Resource prediction
    - Cross-repo resource aggregation
    - Anomaly detection
    """

    def __init__(self, config: ObservabilityConfig):
        self.config = config
        self._resources_dir = config.base_dir / "resources"
        self._resources_dir.mkdir(parents=True, exist_ok=True)

        self._history: Deque[ResourceSnapshot] = deque(maxlen=config.resource_history_size)
        self._last_network = psutil.net_io_counters()
        self._lock = asyncio.Lock()

        # Background tasks
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

        logger.info("ResourceMonitor initialized")

    async def start(self) -> None:
        """Start the resource monitor."""
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("ResourceMonitor started")

    async def stop(self) -> None:
        """Stop the resource monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("ResourceMonitor stopped")

    async def collect_snapshot(self) -> ResourceSnapshot:
        """Collect current resource snapshot."""
        try:
            # v258.0: Non-blocking via shared metrics service
            try:
                from core.async_system_metrics import get_cpu_percent
                cpu = await get_cpu_percent()
            except ImportError:
                cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            network = psutil.net_io_counters()

            # Get process info
            try:
                process = psutil.Process()
                open_files = len(process.open_files())
                threads = process.num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                open_files = 0
                threads = 0

            snapshot = ResourceSnapshot(
                timestamp=time.time(),
                cpu_percent=cpu,
                memory_percent=memory.percent,
                memory_used_gb=memory.used / (1024**3),
                memory_available_gb=memory.available / (1024**3),
                disk_percent=disk.percent,
                disk_used_gb=disk.used / (1024**3),
                disk_free_gb=disk.free / (1024**3),
                network_bytes_sent=network.bytes_sent,
                network_bytes_recv=network.bytes_recv,
                open_files=open_files,
                threads=threads,
                process_count=len(psutil.pids())
            )

            async with self._lock:
                self._history.append(snapshot)
                self._last_network = network

            return snapshot

        except Exception as e:
            logger.error(f"Failed to collect resource snapshot: {e}")
            return ResourceSnapshot(
                timestamp=time.time(),
                cpu_percent=0, memory_percent=0, memory_used_gb=0,
                memory_available_gb=0, disk_percent=0, disk_used_gb=0,
                disk_free_gb=0, network_bytes_sent=0, network_bytes_recv=0,
                open_files=0, threads=0, process_count=0
            )

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while self._running:
            try:
                snapshot = await self.collect_snapshot()
                await self._save_snapshot(snapshot)
                await asyncio.sleep(self.config.resource_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in resource monitor loop: {e}")
                await asyncio.sleep(1.0)

    async def _save_snapshot(self, snapshot: ResourceSnapshot) -> None:
        """Save snapshot to disk."""
        try:
            snapshot_file = self._resources_dir / f"{self.config.node_id}.json"
            async with self._lock:
                history_data = [s.to_dict() for s in list(self._history)[-100:]]

            await asyncio.to_thread(
                snapshot_file.write_text,
                json.dumps({"current": snapshot.to_dict(), "history": history_data}, indent=2)
            )
        except Exception as e:
            logger.error(f"Failed to save resource snapshot: {e}")

    async def get_current(self) -> Optional[ResourceSnapshot]:
        """Get current resource snapshot."""
        async with self._lock:
            return self._history[-1] if self._history else None

    async def get_history(self, limit: int = 100) -> List[ResourceSnapshot]:
        """Get resource history."""
        async with self._lock:
            return list(self._history)[-limit:]

    async def get_averages(self, window_seconds: float = 300) -> Dict[str, float]:
        """Get average resource usage over a time window."""
        async with self._lock:
            now = time.time()
            recent = [s for s in self._history if now - s.timestamp < window_seconds]

        if not recent:
            return {}

        return {
            "cpu_percent_avg": sum(s.cpu_percent for s in recent) / len(recent),
            "memory_percent_avg": sum(s.memory_percent for s in recent) / len(recent),
            "disk_percent_avg": sum(s.disk_percent for s in recent) / len(recent),
            "samples": len(recent),
            "window_seconds": window_seconds
        }

    async def detect_anomalies(self) -> List[Dict[str, Any]]:
        """Detect resource usage anomalies."""
        anomalies = []
        averages = await self.get_averages()
        current = await self.get_current()

        if not current or not averages:
            return anomalies

        # CPU spike detection
        if current.cpu_percent > averages.get("cpu_percent_avg", 0) * 2:
            anomalies.append({
                "type": "cpu_spike",
                "current": current.cpu_percent,
                "average": averages.get("cpu_percent_avg"),
                "severity": "warning" if current.cpu_percent < 90 else "critical"
            })

        # Memory pressure detection
        if current.memory_percent > 85:
            anomalies.append({
                "type": "memory_pressure",
                "current": current.memory_percent,
                "severity": "warning" if current.memory_percent < 95 else "critical"
            })

        # Disk space detection
        if current.disk_percent > 90:
            anomalies.append({
                "type": "disk_space_low",
                "current": current.disk_percent,
                "severity": "warning" if current.disk_percent < 95 else "critical"
            })

        return anomalies

    def get_metrics(self) -> Dict[str, Any]:
        """Get monitor metrics."""
        return {
            "history_size": len(self._history),
            "check_interval": self.config.resource_check_interval,
            "running": self._running
        }


# =============================================================================
# UNIFIED TRINITY OBSERVABILITY SYSTEM
# =============================================================================

class TrinityObservability:
    """
    Unified Trinity Observability System v4.0

    Provides comprehensive observability for the Trinity Ecosystem:
    - Distributed Tracing (W3C Trace Context)
    - Prometheus-compatible Metrics
    - Centralized Structured Logging
    - Performance Profiling
    - Error Aggregation
    - Health Dashboard
    - Alert Management
    - Dependency Graph
    - Request Flow Tracking
    - Resource Monitoring
    """

    def __init__(self, config: Optional[ObservabilityConfig] = None):
        self.config = config or ObservabilityConfig()

        # Create base directory
        self.config.base_dir.mkdir(parents=True, exist_ok=True)

        # Initialize all components
        self.tracer = DistributedTracer(self.config)
        self.metrics = MetricsCollector(self.config)
        self.logger = CentralizedLogger(self.config, self.tracer)
        self.profiler = PerformanceProfiler(self.config)
        self.errors = ErrorAggregator(self.config, self.tracer)
        self.health = HealthDashboard(self.config)
        self.alerts = AlertManager(self.config)
        self.dependencies = DependencyGraph(self.config)
        self.flows = RequestFlowTracker(self.config)
        self.resources = ResourceMonitor(self.config)

        self._running = False

        logger.info(f"TrinityObservability initialized (node={self.config.node_id})")

    @classmethod
    async def create(
        cls,
        config: Optional[ObservabilityConfig] = None,
        auto_start: bool = True
    ) -> 'TrinityObservability':
        """Create and optionally start the observability system."""
        instance = cls(config)
        if auto_start:
            await instance.start()
        return instance

    async def start(self) -> None:
        """Start all observability components."""
        if self._running:
            return

        logger.info("Starting TrinityObservability...")

        # Start all components in parallel
        await asyncio.gather(
            self.tracer.start(),
            self.metrics.start(),
            self.logger.start(),
            self.profiler.start(),
            self.errors.start(),
            self.health.start(),
            self.alerts.start(),
            self.resources.start()
        )

        # Record service dependencies
        await self.dependencies.record_service_dependency("jarvis", "jarvis-prime")
        await self.dependencies.record_service_dependency("jarvis", "reactor-core")
        await self.dependencies.record_service_dependency("jarvis-prime", "reactor-core")

        self._running = True
        logger.info("TrinityObservability started successfully")

    async def stop(self) -> None:
        """Stop all observability components."""
        if not self._running:
            return

        logger.info("Stopping TrinityObservability...")

        # Save dependency graph
        await self.dependencies.save_graph()

        # Stop all components in parallel
        await asyncio.gather(
            self.tracer.stop(),
            self.metrics.stop(),
            self.logger.stop(),
            self.profiler.stop(),
            self.errors.stop(),
            self.health.stop(),
            self.alerts.stop(),
            self.resources.stop()
        )

        self._running = False
        logger.info("TrinityObservability stopped")

    def get_metrics(self) -> Dict[str, Any]:
        """Get metrics from all components."""
        return {
            "running": self._running,
            "node_id": self.config.node_id,
            "service_name": self.config.service_name,
            "tracer": self.tracer.get_metrics(),
            "metrics": self.metrics.get_metrics(),
            "logger": self.logger.get_metrics(),
            "profiler": self.profiler.get_metrics(),
            "errors": self.errors.get_metrics(),
            "health": self.health.get_metrics(),
            "alerts": self.alerts.get_metrics(),
            "dependencies": self.dependencies.get_metrics(),
            "flows": self.flows.get_metrics(),
            "resources": self.resources.get_metrics()
        }

    # Convenience methods for tracing
    def start_span(self, name: str, **kwargs):
        """Start a new trace span."""
        return self.tracer.start_span(name, **kwargs)

    def inject_trace_context(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Inject trace context into headers."""
        return self.tracer.inject_context(headers)

    def extract_trace_context(self, headers: Dict[str, str]) -> Optional[SpanContext]:
        """Extract trace context from headers."""
        return self.tracer.extract_context(headers)

    # Convenience methods for metrics
    def counter(self, name: str, description: str = "", labels: Optional[List[str]] = None) -> Counter:
        """Get or create a counter metric."""
        return self.metrics.counter(name, description, labels)

    def gauge(self, name: str, description: str = "", labels: Optional[List[str]] = None) -> Gauge:
        """Get or create a gauge metric."""
        return self.metrics.gauge(name, description, labels)

    def histogram(self, name: str, description: str = "", labels: Optional[List[str]] = None) -> Histogram:
        """Get or create a histogram metric."""
        return self.metrics.histogram(name, description, labels)

    # Convenience methods for logging
    async def log_debug(self, message: str, **attrs) -> None:
        await self.logger.debug(message, **attrs)

    async def log_info(self, message: str, **attrs) -> None:
        await self.logger.info(message, **attrs)

    async def log_warning(self, message: str, **attrs) -> None:
        await self.logger.warning(message, **attrs)

    async def log_error(self, message: str, **attrs) -> None:
        await self.logger.error(message, **attrs)

    # Convenience methods for errors
    async def capture_exception(self, exc: Exception, **kwargs) -> str:
        """Capture an exception."""
        return await self.errors.capture_exception(exc, **kwargs)

    def capture_errors(self, **kwargs):
        """Context manager to capture errors."""
        return self.errors.capture_errors(**kwargs)

    # Convenience methods for alerts
    async def fire_alert(self, name: str, severity: AlertSeverity, message: str, **kwargs) -> Optional[Alert]:
        """Fire an alert."""
        return await self.alerts.fire_alert(name, severity, message, **kwargs)

    # Convenience methods for request flow
    async def start_request_flow(self, name: str, **kwargs) -> str:
        """Start tracking a request flow."""
        return await self.flows.start_flow(name, **kwargs)

    async def end_request_flow(self, flow_id: str, status: str = "success") -> Optional[RequestFlow]:
        """End a request flow."""
        return await self.flows.end_flow(flow_id, status)


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Main class
    "TrinityObservability",
    "ObservabilityConfig",

    # Tracing
    "DistributedTracer",
    "SpanContext",
    "Span",
    "SpanKind",
    "SpanStatus",

    # Metrics
    "MetricsCollector",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricType",

    # Logging
    "CentralizedLogger",
    "LogEntry",
    "LogLevel",

    # Profiling
    "PerformanceProfiler",
    "FlameNode",

    # Errors
    "ErrorAggregator",
    "ErrorEvent",
    "ErrorGroup",
    "ErrorFingerprint",

    # Health
    "HealthDashboard",
    "HealthCheck",
    "HealthStatus",
    "ServiceHealth",

    # Alerts
    "AlertManager",
    "Alert",
    "AlertRule",
    "AlertSeverity",
    "AlertState",

    # Dependencies
    "DependencyGraph",
    "DependencyNode",
    "DependencyEdge",

    # Request Flow
    "RequestFlowTracker",
    "RequestFlow",
    "RequestStep",

    # Resources
    "ResourceMonitor",
    "ResourceSnapshot"
]

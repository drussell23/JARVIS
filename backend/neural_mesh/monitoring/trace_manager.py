"""
JARVIS Neural Mesh - Distributed Trace Manager

Comprehensive tracing system for tracking message flows and operations
across the Neural Mesh infrastructure.

Features:
- Distributed trace context propagation
- Span hierarchies with parent-child relationships
- Automatic timing and metadata collection
- Trace correlation across agents
- Export to multiple formats (JSON, Jaeger, Zipkin)
- Sampling strategies for performance
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


class SpanStatus(str, Enum):
    """Status of a span."""
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class SpanKind(str, Enum):
    """Kind of span."""
    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"


@dataclass
class SpanEvent:
    """An event that occurred during a span."""
    name: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SpanLink:
    """A link to another span."""
    trace_id: str
    span_id: str
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceContext:
    """Context for trace propagation."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    sampled: bool = True
    baggage: Dict[str, str] = field(default_factory=dict)

    def to_headers(self) -> Dict[str, str]:
        """Convert to HTTP headers for propagation."""
        return {
            "X-Trace-ID": self.trace_id,
            "X-Span-ID": self.span_id,
            "X-Parent-Span-ID": self.parent_span_id or "",
            "X-Sampled": "1" if self.sampled else "0",
            "X-Baggage": json.dumps(self.baggage),
        }

    @classmethod
    def from_headers(cls, headers: Dict[str, str]) -> Optional["TraceContext"]:
        """Create from HTTP headers."""
        trace_id = headers.get("X-Trace-ID")
        span_id = headers.get("X-Span-ID")

        if not trace_id or not span_id:
            return None

        baggage_str = headers.get("X-Baggage", "{}")
        try:
            baggage = json.loads(baggage_str)
        except json.JSONDecodeError:
            baggage = {}

        return cls(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=headers.get("X-Parent-Span-ID") or None,
            sampled=headers.get("X-Sampled", "1") == "1",
            baggage=baggage,
        )


@dataclass
class Span:
    """A span representing a unit of work."""
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    kind: SpanKind = SpanKind.INTERNAL
    status: SpanStatus = SpanStatus.UNSET
    status_message: str = ""

    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None

    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[SpanEvent] = field(default_factory=list)
    links: List[SpanLink] = field(default_factory=list)

    # Internal
    _manager: Optional["TraceManager"] = field(default=None, repr=False)

    @property
    def duration_ms(self) -> float:
        """Get span duration in milliseconds."""
        if not self.end_time:
            return (datetime.utcnow() - self.start_time).total_seconds() * 1000
        return (self.end_time - self.start_time).total_seconds() * 1000

    @property
    def is_finished(self) -> bool:
        """Check if span has ended."""
        return self.end_time is not None

    def set_attribute(self, key: str, value: Any) -> "Span":
        """Set a span attribute."""
        self.attributes[key] = value
        return self

    def set_status(self, status: SpanStatus, message: str = "") -> "Span":
        """Set the span status."""
        self.status = status
        self.status_message = message
        return self

    def add_event(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> "Span":
        """Add an event to the span."""
        self.events.append(SpanEvent(
            name=name,
            timestamp=datetime.utcnow(),
            attributes=attributes or {},
        ))
        return self

    def add_link(
        self,
        context: TraceContext,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> "Span":
        """Add a link to another trace."""
        self.links.append(SpanLink(
            trace_id=context.trace_id,
            span_id=context.span_id,
            attributes=attributes or {},
        ))
        return self

    def record_exception(self, exception: Exception) -> "Span":
        """Record an exception in the span."""
        self.add_event(
            "exception",
            {
                "exception.type": type(exception).__name__,
                "exception.message": str(exception),
            },
        )
        self.set_status(SpanStatus.ERROR, str(exception))
        return self

    def end(self) -> None:
        """End the span."""
        if self.end_time is None:
            self.end_time = datetime.utcnow()
            if self._manager:
                self._manager._on_span_end(self)

    def get_context(self) -> TraceContext:
        """Get the trace context for this span."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert span to dictionary."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "status_message": self.status_message,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "events": [
                {
                    "name": e.name,
                    "timestamp": e.timestamp.isoformat(),
                    "attributes": e.attributes,
                }
                for e in self.events
            ],
            "links": [
                {
                    "trace_id": l.trace_id,
                    "span_id": l.span_id,
                    "attributes": l.attributes,
                }
                for l in self.links
            ],
        }


class SamplingStrategy(str, Enum):
    """Trace sampling strategies."""
    ALWAYS = "always"  # Sample all traces
    NEVER = "never"  # Sample no traces
    RATIO = "ratio"  # Sample based on ratio
    RATE_LIMIT = "rate_limit"  # Limit traces per second


@dataclass
class SamplerConfig:
    """Configuration for trace sampling."""
    strategy: SamplingStrategy = SamplingStrategy.ALWAYS
    ratio: float = 1.0  # For RATIO strategy (0.0-1.0)
    rate_limit: float = 100.0  # For RATE_LIMIT (traces per second)


class TraceManager:
    """
    Distributed tracing manager for Neural Mesh.

    Provides comprehensive tracing capabilities with:
    - Automatic span creation and context propagation
    - Hierarchical span relationships
    - Configurable sampling
    - Multiple export formats

    Example:
        manager = TraceManager()
        await manager.start()

        # Create a trace
        with manager.start_span("process_message") as span:
            span.set_attribute("message.type", "task")

            # Create child spans
            with manager.start_span("validate", parent=span) as child:
                child.set_attribute("valid", True)

            with manager.start_span("execute", parent=span) as child:
                result = await do_work()
                child.set_attribute("result", result)

        # Query traces
        traces = manager.get_traces(limit=10)
    """

    def __init__(
        self,
        service_name: str = "neural_mesh",
        sampler_config: Optional[SamplerConfig] = None,
        max_traces: int = 10000,
        max_spans_per_trace: int = 1000,
    ) -> None:
        """Initialize the trace manager.

        Args:
            service_name: Name of the service for tracing
            sampler_config: Sampling configuration
            max_traces: Maximum traces to retain
            max_spans_per_trace: Maximum spans per trace
        """
        self._service_name = service_name
        self._sampler_config = sampler_config or SamplerConfig()
        self._max_traces = max_traces
        self._max_spans_per_trace = max_spans_per_trace

        # Storage
        self._traces: Dict[str, Dict[str, Span]] = {}  # trace_id -> {span_id -> Span}
        self._trace_order: deque = deque(maxlen=max_traces)  # For LRU eviction
        self._active_spans: Dict[str, Span] = {}  # span_id -> Span

        # Sampling state
        self._sample_count = 0
        self._last_sample_reset = time.time()

        # Context stack (for nested spans)
        self._context_stack: Dict[int, List[TraceContext]] = defaultdict(list)

        self._running = False
        self._cleanup_task: Optional[asyncio.Task] = None

        # Export handlers
        self._export_handlers: List[Callable[[Span], None]] = []

    async def start(self) -> None:
        """Start the trace manager."""
        if self._running:
            return

        self._running = True

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="trace_manager_cleanup"
        )

        logger.info("TraceManager started for service: %s", self._service_name)

    async def stop(self) -> None:
        """Stop the trace manager."""
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # End any active spans
        for span in list(self._active_spans.values()):
            span.end()

        logger.info("TraceManager stopped")

    def _should_sample(self) -> bool:
        """Determine if a new trace should be sampled."""
        config = self._sampler_config

        if config.strategy == SamplingStrategy.ALWAYS:
            return True

        if config.strategy == SamplingStrategy.NEVER:
            return False

        if config.strategy == SamplingStrategy.RATIO:
            import random
            return random.random() < config.ratio

        if config.strategy == SamplingStrategy.RATE_LIMIT:
            now = time.time()
            if now - self._last_sample_reset >= 1.0:
                self._sample_count = 0
                self._last_sample_reset = now

            if self._sample_count < config.rate_limit:
                self._sample_count += 1
                return True
            return False

        return True

    def _generate_id(self) -> str:
        """Generate a unique ID."""
        return uuid.uuid4().hex[:16]

    def _get_current_context(self) -> Optional[TraceContext]:
        """Get the current trace context from the stack."""
        task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
        stack = self._context_stack.get(task_id)
        if stack:
            return stack[-1]
        return None

    def _push_context(self, context: TraceContext) -> None:
        """Push a context onto the stack."""
        task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
        self._context_stack[task_id].append(context)

    def _pop_context(self) -> Optional[TraceContext]:
        """Pop a context from the stack."""
        task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
        stack = self._context_stack.get(task_id)
        if stack:
            return stack.pop()
        return None

    def create_span(
        self,
        name: str,
        parent: Optional[Union[Span, TraceContext]] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[Dict[str, Any]] = None,
        links: Optional[List[TraceContext]] = None,
    ) -> Span:
        """Create a new span.

        Args:
            name: Span name
            parent: Parent span or context (uses current if None)
            kind: Kind of span
            attributes: Initial attributes
            links: Links to other traces

        Returns:
            The created span
        """
        # Determine parent context
        if parent is None:
            parent_ctx = self._get_current_context()
        elif isinstance(parent, Span):
            parent_ctx = parent.get_context()
        else:
            parent_ctx = parent

        # Determine trace ID
        if parent_ctx:
            trace_id = parent_ctx.trace_id
            parent_span_id = parent_ctx.span_id
            sampled = parent_ctx.sampled
        else:
            trace_id = self._generate_id()
            parent_span_id = None
            sampled = self._should_sample()

        # Create span
        span = Span(
            name=name,
            trace_id=trace_id,
            span_id=self._generate_id(),
            parent_span_id=parent_span_id,
            kind=kind,
            attributes=attributes or {},
            _manager=self,
        )

        # Add default attributes
        span.attributes["service.name"] = self._service_name

        # Add links
        if links:
            for link_ctx in links:
                span.add_link(link_ctx)

        # Store if sampled
        if sampled:
            if trace_id not in self._traces:
                self._traces[trace_id] = {}
                self._trace_order.append(trace_id)

                # Evict old traces if needed
                while len(self._trace_order) > self._max_traces:
                    old_trace_id = self._trace_order.popleft()
                    self._traces.pop(old_trace_id, None)

            if len(self._traces[trace_id]) < self._max_spans_per_trace:
                self._traces[trace_id][span.span_id] = span

            self._active_spans[span.span_id] = span

        return span

    @contextmanager
    def start_span(
        self,
        name: str,
        parent: Optional[Union[Span, TraceContext]] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Generator[Span, None, None]:
        """Context manager for creating and managing a span.

        Args:
            name: Span name
            parent: Parent span or context
            kind: Kind of span
            attributes: Initial attributes

        Yields:
            The created span
        """
        span = self.create_span(name, parent, kind, attributes)
        self._push_context(span.get_context())

        try:
            yield span
            if span.status == SpanStatus.UNSET:
                span.set_status(SpanStatus.OK)
        except Exception as e:
            span.record_exception(e)
            raise
        finally:
            self._pop_context()
            span.end()

    @asynccontextmanager
    async def start_async_span(
        self,
        name: str,
        parent: Optional[Union[Span, TraceContext]] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Span, None]:
        """Async context manager for creating and managing a span.

        Args:
            name: Span name
            parent: Parent span or context
            kind: Kind of span
            attributes: Initial attributes

        Yields:
            The created span
        """
        span = self.create_span(name, parent, kind, attributes)
        self._push_context(span.get_context())

        try:
            yield span
            if span.status == SpanStatus.UNSET:
                span.set_status(SpanStatus.OK)
        except Exception as e:
            span.record_exception(e)
            raise
        finally:
            self._pop_context()
            span.end()

    def _on_span_end(self, span: Span) -> None:
        """Called when a span ends."""
        self._active_spans.pop(span.span_id, None)

        # Notify export handlers
        for handler in self._export_handlers:
            try:
                handler(span)
            except Exception as e:
                logger.exception("Error in span export handler: %s", e)

    def add_export_handler(self, handler: Callable[[Span], None]) -> None:
        """Add a handler to be called when spans end."""
        self._export_handlers.append(handler)

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of old traces."""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes

                # Remove completed traces older than 1 hour
                cutoff = datetime.utcnow() - timedelta(hours=1)
                to_remove = []

                for trace_id, spans in self._traces.items():
                    # Check if all spans are done and old
                    if all(
                        s.end_time and s.end_time < cutoff
                        for s in spans.values()
                    ):
                        to_remove.append(trace_id)

                for trace_id in to_remove:
                    del self._traces[trace_id]

                if to_remove:
                    logger.debug("Cleaned up %d old traces", len(to_remove))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in trace cleanup: %s", e)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Span]]:
        """Get all spans for a trace."""
        return self._traces.get(trace_id)

    def get_traces(
        self,
        limit: int = 100,
        service: Optional[str] = None,
        min_duration_ms: Optional[float] = None,
        has_error: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent traces with optional filtering.

        Args:
            limit: Maximum number of traces to return
            service: Filter by service name
            min_duration_ms: Minimum trace duration
            has_error: Filter by error status

        Returns:
            List of trace summaries
        """
        results = []

        for trace_id in reversed(list(self._trace_order)):
            if len(results) >= limit:
                break

            spans = self._traces.get(trace_id, {})
            if not spans:
                continue

            # Find root span
            root_span = None
            for span in spans.values():
                if span.parent_span_id is None:
                    root_span = span
                    break

            if not root_span:
                root_span = list(spans.values())[0]

            # Apply filters
            if service and root_span.attributes.get("service.name") != service:
                continue

            trace_duration = max(s.duration_ms for s in spans.values())
            if min_duration_ms and trace_duration < min_duration_ms:
                continue

            trace_has_error = any(s.status == SpanStatus.ERROR for s in spans.values())
            if has_error is not None and trace_has_error != has_error:
                continue

            results.append({
                "trace_id": trace_id,
                "root_span": root_span.name,
                "service": root_span.attributes.get("service.name"),
                "start_time": root_span.start_time.isoformat(),
                "duration_ms": trace_duration,
                "span_count": len(spans),
                "has_error": trace_has_error,
            })

        return results

    def get_span(self, trace_id: str, span_id: str) -> Optional[Span]:
        """Get a specific span."""
        trace = self._traces.get(trace_id)
        if trace:
            return trace.get(span_id)
        return None

    def get_active_spans(self) -> List[Span]:
        """Get all currently active spans."""
        return list(self._active_spans.values())

    def get_trace_tree(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get a trace as a tree structure."""
        spans = self._traces.get(trace_id)
        if not spans:
            return None

        # Build parent -> children map
        children_map: Dict[Optional[str], List[Span]] = defaultdict(list)
        for span in spans.values():
            children_map[span.parent_span_id].append(span)

        def build_tree(parent_id: Optional[str]) -> List[Dict[str, Any]]:
            result = []
            for span in sorted(children_map[parent_id], key=lambda s: s.start_time):
                node = span.to_dict()
                node["children"] = build_tree(span.span_id)
                result.append(node)
            return result

        return {
            "trace_id": trace_id,
            "spans": build_tree(None),
        }

    # =========================================================================
    # Export Methods
    # =========================================================================

    def export_json(self, trace_id: str) -> Optional[str]:
        """Export a trace as JSON."""
        tree = self.get_trace_tree(trace_id)
        if tree:
            return json.dumps(tree, indent=2, default=str)
        return None

    def export_jaeger(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Export a trace in Jaeger format."""
        spans = self._traces.get(trace_id)
        if not spans:
            return None

        jaeger_spans = []
        for span in spans.values():
            jaeger_spans.append({
                "traceID": trace_id,
                "spanID": span.span_id,
                "operationName": span.name,
                "references": [
                    {
                        "refType": "CHILD_OF",
                        "traceID": trace_id,
                        "spanID": span.parent_span_id,
                    }
                ] if span.parent_span_id else [],
                "startTime": int(span.start_time.timestamp() * 1_000_000),
                "duration": int(span.duration_ms * 1000),
                "tags": [
                    {"key": k, "type": "string", "value": str(v)}
                    for k, v in span.attributes.items()
                ],
                "logs": [
                    {
                        "timestamp": int(e.timestamp.timestamp() * 1_000_000),
                        "fields": [
                            {"key": "event", "type": "string", "value": e.name},
                            *[
                                {"key": k, "type": "string", "value": str(v)}
                                for k, v in e.attributes.items()
                            ],
                        ],
                    }
                    for e in span.events
                ],
                "processID": "p1",
                "warnings": None,
            })

        return {
            "data": [
                {
                    "traceID": trace_id,
                    "spans": jaeger_spans,
                    "processes": {
                        "p1": {
                            "serviceName": self._service_name,
                            "tags": [],
                        },
                    },
                },
            ],
        }

    def summary(self) -> str:
        """Get a summary of tracing activity."""
        total_traces = len(self._traces)
        total_spans = sum(len(spans) for spans in self._traces.values())
        active_spans = len(self._active_spans)

        error_traces = sum(
            1 for spans in self._traces.values()
            if any(s.status == SpanStatus.ERROR for s in spans.values())
        )

        lines = [
            "=== Neural Mesh Trace Summary ===",
            "",
            f"Total traces: {total_traces}",
            f"Total spans: {total_spans}",
            f"Active spans: {active_spans}",
            f"Error traces: {error_traces}",
            "",
            f"Sampling: {self._sampler_config.strategy.value}",
        ]

        if self._sampler_config.strategy == SamplingStrategy.RATIO:
            lines.append(f"  Ratio: {self._sampler_config.ratio:.1%}")
        elif self._sampler_config.strategy == SamplingStrategy.RATE_LIMIT:
            lines.append(f"  Rate limit: {self._sampler_config.rate_limit}/s")

        return "\n".join(lines)


# =============================================================================
# Global Instance
# =============================================================================

_global_trace_manager: Optional[TraceManager] = None


async def get_trace_manager() -> TraceManager:
    """Get or create the global trace manager."""
    global _global_trace_manager

    if _global_trace_manager is None:
        _global_trace_manager = TraceManager()
        await _global_trace_manager.start()

    return _global_trace_manager


async def shutdown_trace_manager() -> None:
    """Stop and clear the global trace manager singleton."""
    global _global_trace_manager

    if _global_trace_manager is not None:
        await _global_trace_manager.stop()
        _global_trace_manager = None

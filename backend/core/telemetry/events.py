"""
Comprehensive Telemetry and Observability Framework

This module provides a complete telemetry system for tracking all follow-up and context
operations with structured events. It supports multiple backends including logging,
Prometheus, OpenTelemetry, and in-memory storage for testing.

The framework is designed to be:
- Non-blocking and async-first
- Extensible with custom event sinks
- Production-ready with proper error handling
- Observable with structured logging and metrics

Example:
    >>> from backend.core.telemetry.events import get_telemetry, EventType
    >>> telemetry = get_telemetry()
    >>> await telemetry.emit(EventType.CONTEXT_CREATED, properties={"context_id": "123"})
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Event categories for telemetry tracking.
    
    Defines all possible event types that can be emitted by the telemetry system.
    Events are organized by functional area (context, intent, routing, etc.).
    """
    # Context lifecycle
    CONTEXT_CREATED = "context.created"
    CONTEXT_ACCESSED = "context.accessed"
    CONTEXT_CONSUMED = "context.consumed"
    CONTEXT_EXPIRED = "context.expired"
    CONTEXT_INVALIDATED = "context.invalidated"

    # Intent detection
    INTENT_DETECTED = "intent.detected"
    INTENT_CLASSIFICATION_FAILED = "intent.classification_failed"

    # Routing
    ROUTE_MATCHED = "route.matched"
    ROUTE_FAILED = "route.failed"
    ROUTE_NO_MATCH = "route.no_match"

    # Follow-up handling
    FOLLOWUP_INITIATED = "followup.initiated"
    FOLLOWUP_RESOLVED = "followup.resolved"
    FOLLOWUP_CONTEXT_MISSING = "followup.context_missing"

    # Vision operations
    VISION_SNAPSHOT_TAKEN = "vision.snapshot_taken"
    VISION_OCR_EXECUTED = "vision.ocr_executed"
    VISION_ANALYSIS_COMPLETED = "vision.analysis_completed"

    # Matching
    SEMANTIC_MATCH_EXECUTED = "semantic.match_executed"
    SEMANTIC_MATCH_FOUND = "semantic.match_found"
    SEMANTIC_MATCH_NONE = "semantic.match_none"

    # Authentication / voice_unlock
    VOICE_UNLOCK_GRANTED = "auth.voice_unlock.granted"
    VOICE_UNLOCK_DENIED = "auth.voice_unlock.denied"
    VOICE_UNLOCK_ROUTING = "auth.voice_unlock.routing"

    # Performance
    LATENCY_RECORDED = "latency.recorded"
    ERROR_OCCURRED = "error.occurred"


@dataclass
class TelemetryEvent:
    """Base telemetry event with structured data.
    
    Represents a single telemetry event with metadata, properties, metrics, and tags.
    All events have a unique ID and timestamp for tracking and correlation.
    
    Attributes:
        event_type: The type of event being recorded
        timestamp: When the event occurred (UTC)
        event_id: Unique identifier for this event
        session_id: Optional session identifier for correlation
        user_id: Optional user identifier for correlation
        properties: Arbitrary key-value properties
        metrics: Numeric metrics associated with the event
        tags: List of string tags for categorization
    
    Example:
        >>> event = TelemetryEvent(
        ...     event_type=EventType.CONTEXT_CREATED,
        ...     properties={"context_id": "123"},
        ...     metrics={"ttl_seconds": 300.0}
        ... )
    """
    event_type: EventType
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: str | None = None
    user_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize event to dictionary format.
        
        Converts the event to a dictionary suitable for JSON serialization
        or storage in external systems.
        
        Returns:
            Dictionary representation of the event with ISO timestamp
        """
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "event_id": self.event_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "properties": self.properties,
            "metrics": self.metrics,
            "tags": self.tags,
        }


class EventSink(Protocol):
    """Protocol for event sinks that receive and process telemetry events.
    
    Event sinks are responsible for persisting, forwarding, or processing
    telemetry events. Common implementations include loggers, metrics
    backends, and external monitoring systems.
    """

    async def emit(self, event: TelemetryEvent) -> None:
        """Emit event to the sink for processing.
        
        Args:
            event: The telemetry event to process
            
        Raises:
            Any implementation-specific exceptions
        """
        ...


class LoggingEventSink:
    """Event sink that emits events to Python logging system.
    
    Formats events as structured JSON and logs them at INFO level.
    Useful for development and systems that aggregate logs.
    
    Attributes:
        _logger: The logger instance to use for output
    """

    def __init__(self, logger: logging.Logger | None = None):
        """Initialize the logging event sink.
        
        Args:
            logger: Optional logger instance. If None, uses "telemetry" logger
        """
        self._logger = logger or logging.getLogger("telemetry")

    async def emit(self, event: TelemetryEvent) -> None:
        """Log event as structured JSON.
        
        Args:
            event: The telemetry event to log
        """
        import json

        self._logger.info(
            f"[{event.event_type.value}] {json.dumps(event.to_dict(), default=str)}"
        )


class PrometheusEventSink:
    """Event sink that emits metrics to Prometheus.
    
    Converts telemetry events into Prometheus metrics including counters,
    histograms, and gauges. Requires prometheus_client package.
    
    Attributes:
        _event_counter: Counter for total events by type
        _latency_histogram: Histogram for operation latencies
        _context_gauge: Gauge for active context count
        
    Raises:
        RuntimeError: If prometheus_client is not installed
    """

    def __init__(self):
        """Initialize Prometheus metrics.
        
        Raises:
            RuntimeError: If prometheus_client package is not available
        """
        try:
            from prometheus_client import Counter, Histogram, Gauge

            # Define metrics
            self._event_counter = Counter(
                "jarvis_events_total",
                "Total events emitted",
                ["event_type"],
            )

            self._latency_histogram = Histogram(
                "jarvis_operation_latency_seconds",
                "Operation latency",
                ["operation"],
            )

            self._context_gauge = Gauge(
                "jarvis_active_contexts",
                "Number of active contexts",
            )

        except ImportError:
            raise RuntimeError(
                "Prometheus support requires prometheus_client. "
                "Install: pip install prometheus-client"
            )

    async def emit(self, event: TelemetryEvent) -> None:
        """Update Prometheus metrics based on event.
        
        Increments counters, records latencies, and updates gauges
        based on the event type and contained metrics.
        
        Args:
            event: The telemetry event to process
        """
        # Increment event counter
        self._event_counter.labels(event_type=event.event_type.value).inc()

        # Record latency if present
        if "latency_seconds" in event.metrics:
            operation = event.properties.get("operation", "unknown")
            self._latency_histogram.labels(operation=operation).observe(
                event.metrics["latency_seconds"]
            )

        # Update context gauge
        if event.event_type == EventType.CONTEXT_CREATED:
            self._context_gauge.inc()
        elif event.event_type in (
            EventType.CONTEXT_CONSUMED,
            EventType.CONTEXT_EXPIRED,
            EventType.CONTEXT_INVALIDATED,
        ):
            self._context_gauge.dec()


class OpenTelemetryEventSink:
    """Event sink that emits events to OpenTelemetry.
    
    Integrates with OpenTelemetry for distributed tracing and metrics.
    Requires opentelemetry-api and opentelemetry-sdk packages.
    
    Attributes:
        _tracer: OpenTelemetry tracer instance
        _meter: OpenTelemetry meter instance
        _event_counter: Counter instrument for events
        
    Raises:
        RuntimeError: If OpenTelemetry packages are not installed
    """

    def __init__(self):
        """Initialize OpenTelemetry instruments.
        
        Raises:
            RuntimeError: If OpenTelemetry packages are not available
        """
        try:
            from opentelemetry import trace, metrics

            self._tracer = trace.get_tracer(__name__)
            self._meter = metrics.get_meter(__name__)

            # Create instruments
            self._event_counter = self._meter.create_counter(
                "jarvis.events.total",
                description="Total events emitted",
            )

        except ImportError:
            raise RuntimeError(
                "OpenTelemetry support requires opentelemetry-api. "
                "Install: pip install opentelemetry-api opentelemetry-sdk"
            )

    async def emit(self, event: TelemetryEvent) -> None:
        """Emit event to OpenTelemetry.
        
        Records the event as a counter increment with attributes
        from the event properties.
        
        Args:
            event: The telemetry event to emit
        """
        # Record event
        self._event_counter.add(
            1,
            attributes={
                "event_type": event.event_type.value,
                **event.properties,
            },
        )


class InMemoryEventSink:
    """Event sink that stores events in memory for testing and debugging.
    
    Maintains a bounded list of events with LRU eviction. Useful for
    unit tests and development debugging.
    
    Attributes:
        _events: List of stored events
        _max_events: Maximum number of events to retain
    """

    def __init__(self, max_events: int = 10000):
        """Initialize in-memory storage.
        
        Args:
            max_events: Maximum number of events to store before eviction
        """
        self._events: list[TelemetryEvent] = []
        self._max_events = max_events

    async def emit(self, event: TelemetryEvent) -> None:
        """Store event in memory with LRU eviction.
        
        Args:
            event: The telemetry event to store
        """
        self._events.append(event)

        # LRU eviction
        if len(self._events) > self._max_events:
            self._events.pop(0)

    def get_events(
        self, event_type: EventType | None = None, limit: int | None = None
    ) -> list[TelemetryEvent]:
        """Retrieve stored events with optional filtering.
        
        Args:
            event_type: Optional event type filter
            limit: Optional limit on number of events returned
            
        Returns:
            List of events matching the criteria
        """
        events = self._events

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        if limit:
            events = events[-limit:]

        return events

    def clear(self) -> None:
        """Clear all stored events."""
        self._events.clear()

    @property
    def count(self) -> int:
        """Get the current number of stored events.
        
        Returns:
            Number of events currently stored
        """
        return len(self._events)


class TelemetryManager:
    """Central telemetry manager that coordinates multiple sinks.
    
    Provides a high-level API for emitting events and manages multiple
    event sinks. Supports session/user context and can be enabled/disabled.
    
    Attributes:
        _sinks: List of registered event sinks
        _default_session_id: Default session ID for events
        _default_user_id: Default user ID for events
        _enabled: Whether telemetry is currently enabled
    """

    def __init__(self):
        """Initialize the telemetry manager with empty configuration."""
        self._sinks: list[EventSink] = []
        self._default_session_id: str | None = None
        self._default_user_id: str | None = None
        self._enabled = True

    def add_sink(self, sink: EventSink) -> None:
        """Register an event sink to receive events.
        
        Args:
            sink: The event sink to register
        """
        self._sinks.append(sink)
        logger.info(f"Added telemetry sink: {type(sink).__name__}")

    def set_session(self, session_id: str, user_id: str | None = None) -> None:
        """Set default session and user IDs for subsequent events.
        
        Args:
            session_id: Session identifier to use as default
            user_id: Optional user identifier to use as default
        """
        self._default_session_id = session_id
        self._default_user_id = user_id

    def enable(self) -> None:
        """Enable telemetry event emission."""
        self._enabled = True

    def disable(self) -> None:
        """Disable telemetry event emission."""
        self._enabled = False

    async def emit(
        self,
        event_type: EventType,
        properties: dict[str, Any] | None = None,
        metrics: dict[str, float] | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Emit event to all registered sinks.
        
        Creates a telemetry event and sends it to all registered sinks
        concurrently. If telemetry is disabled, this is a no-op.
        
        Args:
            event_type: Type of event to emit
            properties: Optional event properties
            metrics: Optional numeric metrics
            tags: Optional string tags
            session_id: Optional session ID (overrides default)
            user_id: Optional user ID (overrides default)
        """
        if not self._enabled:
            return

        event = TelemetryEvent(
            event_type=event_type,
            session_id=session_id or self._default_session_id,
            user_id=user_id or self._default_user_id,
            properties=properties or {},
            metrics=metrics or {},
            tags=tags or [],
        )

        # Emit to all sinks concurrently
        import asyncio

        tasks = [sink.emit(event) for sink in self._sinks]
        await asyncio.gather(*tasks, return_exceptions=True)

    # Convenience methods for common events

    async def track_context_created(
        self,
        context_id: str,
        category: str,
        priority: str,
        ttl_seconds: int,
        **extra,
    ) -> None:
        """Track context creation event.
        
        Args:
            context_id: Unique identifier for the context
            category: Context category
            priority: Context priority level
            ttl_seconds: Time-to-live in seconds
            **extra: Additional properties to include
        """
        await self.emit(
            EventType.CONTEXT_CREATED,
            properties={
                "context_id": context_id,
                "category": category,
                "priority": priority,
                "ttl_seconds": ttl_seconds,
                **extra,
            },
        )

    async def track_context_accessed(self, context_id: str, access_count: int) -> None:
        """Track context access event.
        
        Args:
            context_id: Unique identifier for the context
            access_count: Number of times context has been accessed
        """
        await self.emit(
            EventType.CONTEXT_ACCESSED,
            properties={"context_id": context_id},
            metrics={"access_count": float(access_count)},
        )

    async def track_intent_detected(
        self, intent_label: str, confidence: float, classifiers: list[str]
    ) -> None:
        """Track intent detection event.
        
        Args:
            intent_label: The detected intent label
            confidence: Confidence score (0.0 to 1.0)
            classifiers: List of classifiers that contributed
        """
        await self.emit(
            EventType.INTENT_DETECTED,
            properties={
                "intent_label": intent_label,
                "classifiers": classifiers,
            },
            metrics={"confidence": confidence},
        )

    async def track_route_matched(
        self, intent_label: str, handler_name: str, latency_ms: float
    ) -> None:
        """Track successful routing event.
        
        Args:
            intent_label: The intent that was routed
            handler_name: Name of the handler that will process the intent
            latency_ms: Routing latency in milliseconds
        """
        await self.emit(
            EventType.ROUTE_MATCHED,
            properties={
                "intent_label": intent_label,
                "handler_name": handler_name,
            },
            metrics={"latency_ms": latency_ms},
        )

    async def track_followup_resolved(
        self,
        context_id: str,
        window_type: str,
        response_type: str,
        latency_ms: float,
    ) -> None:
        """Track follow-up resolution event.
        
        Args:
            context_id: Context that was used for resolution
            window_type: Type of window context (e.g., "active", "background")
            response_type: Type of response generated
            latency_ms: Resolution latency in milliseconds
        """
        await self.emit(
            EventType.FOLLOWUP_RESOLVED,
            properties={
                "context_id": context_id,
                "window_type": window_type,
                "response_type": response_type,
            },
            metrics={"latency_ms": latency_ms},
        )

    async def track_semantic_match(
        self,
        input_text: str,
        match_count: int,
        top_score: float | None,
        latency_ms: float,
    ) -> None:
        """Track semantic matching operation.
        
        Args:
            input_text: The input text that was matched
            match_count: Number of matches found
            top_score: Highest match score, if any
            latency_ms: Matching latency in milliseconds
        """
        event_type = (
            EventType.SEMANTIC_MATCH_FOUND
            if match_count > 0
            else EventType.SEMANTIC_MATCH_NONE
        )

        await self.emit(
            event_type,
            properties={
                "input_length": len(input_text),
                "match_count": match_count,
            },
            metrics={
                "top_score": top_score or 0.0,
                "latency_ms": latency_ms,
            },
        )

    async def track_error(
        self, operation: str, error_type: str, error_message: str
    ) -> None:
        """Track error occurrence.
        
        Args:
            operation: Name of the operation that failed
            error_type: Type/class of the error
            error_message: Human-readable error message
        """
        await self.emit(
            EventType.ERROR_OCCURRED,
            properties={
                "operation": operation,
                "error_type": error_type,
                "error_message": error_message,
            },
        )


# Global telemetry instance
_global_telemetry: TelemetryManager | None = None


def get_telemetry() -> TelemetryManager:
    """Get the global telemetry manager instance.
    
    Creates a default telemetry manager with logging sink if none exists.
    
    Returns:
        The global telemetry manager instance
    """
    global _global_telemetry
    if _global_telemetry is None:
        _global_telemetry = TelemetryManager()
        # Add default logging sink
        _global_telemetry.add_sink(LoggingEventSink())
    return _global_telemetry


def init_telemetry(
    sinks: list[EventSink] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> TelemetryManager:
    """Initialize global telemetry with custom configuration.
    
    Replaces the global telemetry manager with a new instance configured
    with the provided sinks and default session/user context.
    
    Args:
        sinks: List of event sinks to register
        session_id: Default session ID for events
        user_id: Default user ID for events
        
    Returns:
        The newly initialized telemetry manager
        
    Example:
        >>> from backend.core.telemetry.events import init_telemetry, LoggingEventSink
        >>> telemetry = init_telemetry(
        ...     sinks=[LoggingEventSink()],
        ...     session_id="session_123"
        ... )
    """
    global _global_telemetry
    _global_telemetry = TelemetryManager()

    if sinks:
        for sink in sinks:
            _global_telemetry.add_sink(sink)

    if session_id:
        _global_telemetry.set_session(session_id, user_id)

    return _global_telemetry


class LatencyTracker:
    """Context manager for tracking operation latency.
    
    Automatically measures the duration of an operation and emits
    a latency event. Also tracks errors if they occur during the operation.
    
    Attributes:
        _operation: Name of the operation being tracked
        _telemetry: Telemetry manager to use for events
        _start_time: Start time of the operation
        
    Example:
        >>> async with LatencyTracker("database_query"):
        ...     result = await database.query("SELECT * FROM users")
    """

    def __init__(self, operation: str, telemetry: TelemetryManager | None = None):
        """Initialize the latency tracker.
        
        Args:
            operation: Name of the operation to track
            telemetry: Optional telemetry manager (uses global if None)
        """
        self._operation = operation
        self._telemetry = telemetry or get_telemetry()
        self._start_time: float | None = None

    async def __aenter__(self):
        """Start timing the operation.
        
        Returns:
            Self for use in with statement
        """
        self._start_time = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop timing and emit latency event.
        
        Args:
            exc_type: Exception type if an error occurred
            exc_val: Exception value if an error occurred
            exc_tb: Exception traceback if an error occurred
        """
        if self._start_time is not None:
            latency_seconds = time.perf_counter() - self._start_time
            await self._telemetry.emit(
                EventType.LATENCY_RECORDED,
                properties={"operation": self._operation},
                metrics={"latency_seconds": latency_seconds},
            )

        if exc_type is not None:
            await self._telemetry.track_error(
                operation=self._operation,
                error_type=exc_type.__name__,
                error_message=str(exc_val),
            )
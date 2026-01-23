"""
Enterprise-Grade Lifecycle Event System for Dependency Injection

This module provides a comprehensive event system for tracking service lifecycle
events with full observability, distributed tracing support, and memory safety.

Features:
- ServiceEvent enum for all lifecycle states
- EventData with distributed tracing context
- WeakRef handlers to prevent memory leaks
- Async and sync handler support with timeout protection
- Event filtering by service type
- Batch emission for performance
- Event history/replay for debugging
- Thread-safe subscription management
- Metrics and observability hooks

Author: JARVIS DI Framework
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import gc
import hashlib
import logging
import threading
import time
import traceback
import uuid
import weakref
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Deque,
    Dict,
    FrozenSet,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    runtime_checkable,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
TService = TypeVar("TService")

# Context variable for current trace context propagation
_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_trace_id",
    default=None
)

_current_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_span_id",
    default=None
)


# =============================================================================
# SECTION 1: ServiceEvent Enum
# =============================================================================

class ServiceEvent(Enum):
    """
    Comprehensive service lifecycle events.

    These events track the complete lifecycle of a service from registration
    through disposal, including health state changes and recovery.

    Event Flow:
        REGISTERED -> INITIALIZING -> INITIALIZED -> STARTING -> STARTED
                                                              -> STOPPING -> STOPPED -> DISPOSED
                                                              -> FAILED -> RECOVERED -> STARTED
    """
    # Registration phase
    REGISTERED = "registered"           # Service type registered with container

    # Initialization phase
    INITIALIZING = "initializing"       # Service factory being called
    INITIALIZED = "initialized"         # Service instance created successfully

    # Startup phase
    STARTING = "starting"               # Service startup method called
    STARTED = "started"                 # Service fully operational

    # Shutdown phase
    STOPPING = "stopping"               # Service shutdown initiated
    STOPPED = "stopped"                 # Service stopped successfully

    # Error states
    FAILED = "failed"                   # Service failed during lifecycle
    RECOVERED = "recovered"             # Service recovered from failure

    # Health monitoring
    HEALTH_CHANGED = "health_changed"   # Health status changed

    # Cleanup
    DISPOSED = "disposed"               # Service resources released

    # Dependency events
    DEPENDENCY_RESOLVED = "dependency_resolved"  # Dependency successfully resolved
    DEPENDENCY_FAILED = "dependency_failed"      # Dependency resolution failed

    # Container events
    CONTAINER_INITIALIZED = "container_initialized"  # Container ready
    CONTAINER_SHUTDOWN = "container_shutdown"        # Container shutting down

    def __str__(self) -> str:
        return self.value

    @property
    def is_error_state(self) -> bool:
        """Check if this event represents an error state."""
        return self in (ServiceEvent.FAILED, ServiceEvent.DEPENDENCY_FAILED)

    @property
    def is_terminal(self) -> bool:
        """Check if this event represents a terminal state."""
        return self in (ServiceEvent.STOPPED, ServiceEvent.DISPOSED, ServiceEvent.FAILED)

    @property
    def is_transient(self) -> bool:
        """Check if this event represents a transient/intermediate state."""
        return self in (
            ServiceEvent.INITIALIZING,
            ServiceEvent.STARTING,
            ServiceEvent.STOPPING,
        )


# =============================================================================
# SECTION 2: EventData Dataclass
# =============================================================================

@dataclass(frozen=True)
class EventData:
    """
    Immutable event data with full observability support.

    This dataclass captures all information about a service lifecycle event,
    including timing, errors, distributed tracing context, and custom metadata.

    Attributes:
        event: The type of lifecycle event
        service_type: Optional type of the service (for filtering)
        service_name: Human-readable name of the service
        timestamp: When the event occurred (Unix timestamp)
        duration_ms: Duration of the operation that triggered this event
        error: Optional exception that occurred
        metadata: Additional key-value data for observability
        trace_id: Distributed tracing trace ID
        span_id: Distributed tracing span ID
        parent_span_id: Parent span for hierarchical tracing
        event_id: Unique identifier for this event
        correlation_id: Correlation ID for related events
        severity: Event severity level (info, warning, error)
        source: Source component/module that emitted the event
        tags: Set of string tags for categorization
    """
    event: ServiceEvent
    service_name: str
    service_type: Optional[Type] = None
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    error: Optional[Exception] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None
    severity: str = "info"
    source: str = "di.container"
    tags: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Validate and set derived fields."""
        # Ensure metadata is a dict (for frozen dataclass compatibility)
        if not isinstance(self.metadata, dict):
            object.__setattr__(self, 'metadata', dict(self.metadata))

        # Set severity based on event type if not explicitly set
        if self.severity == "info" and self.event.is_error_state:
            object.__setattr__(self, 'severity', 'error')
        elif self.severity == "info" and self.event.is_transient:
            object.__setattr__(self, 'severity', 'debug')

        # Ensure tags is a frozenset
        if not isinstance(self.tags, frozenset):
            object.__setattr__(self, 'tags', frozenset(self.tags))

    @property
    def service_type_name(self) -> str:
        """Get the fully qualified name of the service type."""
        if self.service_type is None:
            return "unknown"
        return f"{self.service_type.__module__}.{self.service_type.__qualname__}"

    @property
    def error_message(self) -> Optional[str]:
        """Get the error message if an error occurred."""
        if self.error is None:
            return None
        return str(self.error)

    @property
    def error_type(self) -> Optional[str]:
        """Get the error type name if an error occurred."""
        if self.error is None:
            return None
        return type(self.error).__name__

    @property
    def timestamp_iso(self) -> str:
        """Get the timestamp in ISO 8601 format."""
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Convert event data to dictionary for serialization."""
        return {
            "event": self.event.value,
            "service_name": self.service_name,
            "service_type": self.service_type_name,
            "timestamp": self.timestamp,
            "timestamp_iso": self.timestamp_iso,
            "duration_ms": self.duration_ms,
            "error": self.error_message,
            "error_type": self.error_type,
            "metadata": self.metadata,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
            "severity": self.severity,
            "source": self.source,
            "tags": list(self.tags),
        }

    def with_metadata(self, **kwargs: Any) -> "EventData":
        """Create a new EventData with additional metadata."""
        new_metadata = {**self.metadata, **kwargs}
        return EventData(
            event=self.event,
            service_name=self.service_name,
            service_type=self.service_type,
            timestamp=self.timestamp,
            duration_ms=self.duration_ms,
            error=self.error,
            metadata=new_metadata,
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            event_id=self.event_id,
            correlation_id=self.correlation_id,
            severity=self.severity,
            source=self.source,
            tags=self.tags,
        )

    def with_tags(self, *tags: str) -> "EventData":
        """Create a new EventData with additional tags."""
        new_tags = self.tags | frozenset(tags)
        return EventData(
            event=self.event,
            service_name=self.service_name,
            service_type=self.service_type,
            timestamp=self.timestamp,
            duration_ms=self.duration_ms,
            error=self.error,
            metadata=self.metadata,
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            event_id=self.event_id,
            correlation_id=self.correlation_id,
            severity=self.severity,
            source=self.source,
            tags=new_tags,
        )


# =============================================================================
# SECTION 3: EventData Builder
# =============================================================================

class EventDataBuilder:
    """
    Fluent builder for creating EventData instances.

    Provides a clean API for constructing events with proper defaults
    and automatic trace context propagation.

    Example:
        event = (EventDataBuilder()
            .with_event(ServiceEvent.STARTED)
            .with_service("MyService", MyService)
            .with_duration(150.5)
            .with_metadata(status="healthy")
            .build())
    """

    def __init__(self) -> None:
        self._event: Optional[ServiceEvent] = None
        self._service_name: str = "unknown"
        self._service_type: Optional[Type] = None
        self._timestamp: Optional[float] = None
        self._duration_ms: float = 0.0
        self._error: Optional[Exception] = None
        self._metadata: Dict[str, Any] = {}
        self._trace_id: Optional[str] = None
        self._span_id: Optional[str] = None
        self._parent_span_id: Optional[str] = None
        self._correlation_id: Optional[str] = None
        self._severity: Optional[str] = None
        self._source: str = "di.container"
        self._tags: Set[str] = set()
        self._auto_trace: bool = True

    def with_event(self, event: ServiceEvent) -> "EventDataBuilder":
        """Set the event type."""
        self._event = event
        return self

    def with_service(
        self,
        name: str,
        service_type: Optional[Type] = None
    ) -> "EventDataBuilder":
        """Set service information."""
        self._service_name = name
        self._service_type = service_type
        return self

    def with_timestamp(self, timestamp: float) -> "EventDataBuilder":
        """Set explicit timestamp."""
        self._timestamp = timestamp
        return self

    def with_duration(self, duration_ms: float) -> "EventDataBuilder":
        """Set operation duration in milliseconds."""
        self._duration_ms = duration_ms
        return self

    def with_error(self, error: Exception) -> "EventDataBuilder":
        """Set error information."""
        self._error = error
        return self

    def with_metadata(self, **kwargs: Any) -> "EventDataBuilder":
        """Add metadata key-value pairs."""
        self._metadata.update(kwargs)
        return self

    def with_trace(
        self,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None
    ) -> "EventDataBuilder":
        """Set distributed tracing context."""
        self._trace_id = trace_id
        self._span_id = span_id
        self._parent_span_id = parent_span_id
        return self

    def with_correlation(self, correlation_id: str) -> "EventDataBuilder":
        """Set correlation ID for related events."""
        self._correlation_id = correlation_id
        return self

    def with_severity(self, severity: str) -> "EventDataBuilder":
        """Set explicit severity level."""
        self._severity = severity
        return self

    def with_source(self, source: str) -> "EventDataBuilder":
        """Set event source."""
        self._source = source
        return self

    def with_tags(self, *tags: str) -> "EventDataBuilder":
        """Add tags for categorization."""
        self._tags.update(tags)
        return self

    def without_auto_trace(self) -> "EventDataBuilder":
        """Disable automatic trace context propagation."""
        self._auto_trace = False
        return self

    def build(self) -> EventData:
        """Build the EventData instance."""
        if self._event is None:
            raise ValueError("Event type is required")

        # Auto-propagate trace context if enabled
        trace_id = self._trace_id
        span_id = self._span_id

        if self._auto_trace:
            if trace_id is None:
                trace_id = _current_trace_id.get()
            if span_id is None:
                span_id = str(uuid.uuid4())[:16]

        return EventData(
            event=self._event,
            service_name=self._service_name,
            service_type=self._service_type,
            timestamp=self._timestamp or time.time(),
            duration_ms=self._duration_ms,
            error=self._error,
            metadata=self._metadata.copy(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=self._parent_span_id,
            correlation_id=self._correlation_id,
            severity=self._severity or ("error" if self._error else "info"),
            source=self._source,
            tags=frozenset(self._tags),
        )


# =============================================================================
# SECTION 4: Subscription Dataclass
# =============================================================================

# Type aliases for handlers
SyncHandler = Callable[[EventData], None]
AsyncHandler = Callable[[EventData], Awaitable[None]]
Handler = Union[SyncHandler, AsyncHandler]


class WeakMethodRef:
    """
    Weak reference wrapper for bound methods.

    Standard weakref cannot reference bound methods because they are
    created on-the-fly. This wrapper stores the object and method name
    separately to reconstruct the bound method when needed.

    This prevents memory leaks when subscribing instance methods as handlers.
    """

    def __init__(self, method: Callable) -> None:
        if hasattr(method, '__self__') and hasattr(method, '__func__'):
            # Bound method
            self._obj_ref: Optional[weakref.ref] = weakref.ref(method.__self__)
            self._func: Callable = method.__func__
            self._is_bound = True
        else:
            # Regular function or static method
            self._obj_ref = None
            self._func = method
            self._is_bound = False

    def __call__(self) -> Optional[Callable]:
        """Get the method, or None if the object has been garbage collected."""
        if not self._is_bound:
            return self._func

        if self._obj_ref is None:
            return None

        obj = self._obj_ref()
        if obj is None:
            return None

        return self._func.__get__(obj, type(obj))

    def is_alive(self) -> bool:
        """Check if the referenced object is still alive."""
        if not self._is_bound:
            return True
        if self._obj_ref is None:
            return False
        return self._obj_ref() is not None


@dataclass
class Subscription:
    """
    Represents a subscription to service lifecycle events.

    Subscriptions can filter by:
    - Specific event type (or None for all events)
    - Specific service type (or None for all services)

    Memory safety is ensured through weak references that allow
    handlers to be garbage collected without explicit unsubscription.

    Attributes:
        id: Unique subscription identifier
        event: Optional event filter (None = all events)
        handler: The callback handler (stored as weak ref if weak_ref=True)
        service_type: Optional service type filter
        is_async: Whether the handler is async
        weak_ref: Whether to use weak reference for the handler
        priority: Handler execution priority (lower = earlier)
        timeout_ms: Handler timeout in milliseconds
        created_at: Subscription creation timestamp
        description: Human-readable description
        one_shot: If True, subscription is removed after first invocation
    """
    id: str
    event: Optional[ServiceEvent]
    handler: Union[Handler, WeakMethodRef]
    service_type: Optional[Type] = None
    is_async: bool = False
    weak_ref: bool = False
    priority: int = 100
    timeout_ms: float = 5000.0
    created_at: float = field(default_factory=time.time)
    description: str = ""
    one_shot: bool = False
    _invocation_count: int = field(default=0, repr=False)
    _last_invocation: Optional[float] = field(default=None, repr=False)
    _error_count: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        """Initialize derived fields."""
        # Ensure ID is set
        if not self.id:
            self.id = str(uuid.uuid4())

    @classmethod
    def create(
        cls,
        handler: Handler,
        event: Optional[ServiceEvent] = None,
        service_type: Optional[Type] = None,
        weak_ref: bool = True,
        priority: int = 100,
        timeout_ms: float = 5000.0,
        description: str = "",
        one_shot: bool = False,
    ) -> "Subscription":
        """
        Factory method to create a subscription with proper handler wrapping.

        Args:
            handler: The callback function or coroutine
            event: Optional event type filter
            service_type: Optional service type filter
            weak_ref: Whether to use weak reference for memory safety
            priority: Execution priority (lower = earlier)
            timeout_ms: Handler timeout
            description: Human-readable description
            one_shot: Remove subscription after first invocation

        Returns:
            Configured Subscription instance
        """
        is_async = asyncio.iscoroutinefunction(handler)

        # Wrap in weak reference if requested
        wrapped_handler: Union[Handler, WeakMethodRef]
        if weak_ref:
            wrapped_handler = WeakMethodRef(handler)
        else:
            wrapped_handler = handler

        return cls(
            id=str(uuid.uuid4()),
            event=event,
            handler=wrapped_handler,
            service_type=service_type,
            is_async=is_async,
            weak_ref=weak_ref,
            priority=priority,
            timeout_ms=timeout_ms,
            description=description or _get_handler_description(handler),
            one_shot=one_shot,
        )

    def get_handler(self) -> Optional[Handler]:
        """
        Get the actual handler function.

        Returns None if the handler was garbage collected.
        """
        if isinstance(self.handler, WeakMethodRef):
            return self.handler()
        return self.handler

    def is_alive(self) -> bool:
        """Check if the handler is still alive (not garbage collected)."""
        if isinstance(self.handler, WeakMethodRef):
            return self.handler.is_alive()
        return True

    def matches(self, event_data: EventData) -> bool:
        """
        Check if this subscription matches the given event data.

        Returns True if both event type and service type filters match.
        """
        # Check event filter
        if self.event is not None and self.event != event_data.event:
            return False

        # Check service type filter
        if self.service_type is not None:
            if event_data.service_type is None:
                return False
            # Allow subclass matching
            if not issubclass(event_data.service_type, self.service_type):
                return False

        return True

    def record_invocation(self, success: bool = True) -> None:
        """Record an invocation of this handler."""
        self._invocation_count += 1
        self._last_invocation = time.time()
        if not success:
            self._error_count += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for debugging/monitoring."""
        return {
            "id": self.id,
            "event": self.event.value if self.event else None,
            "service_type": (
                f"{self.service_type.__module__}.{self.service_type.__qualname__}"
                if self.service_type else None
            ),
            "is_async": self.is_async,
            "weak_ref": self.weak_ref,
            "priority": self.priority,
            "timeout_ms": self.timeout_ms,
            "created_at": self.created_at,
            "description": self.description,
            "one_shot": self.one_shot,
            "invocation_count": self._invocation_count,
            "last_invocation": self._last_invocation,
            "error_count": self._error_count,
            "is_alive": self.is_alive(),
        }


def _get_handler_description(handler: Handler) -> str:
    """Extract a description from a handler function."""
    if hasattr(handler, '__qualname__'):
        return handler.__qualname__
    if hasattr(handler, '__name__'):
        return handler.__name__
    return repr(handler)


# =============================================================================
# SECTION 5: Observability Protocols
# =============================================================================

@runtime_checkable
class MetricsCollector(Protocol):
    """Protocol for metrics collection backends."""

    def record_event(self, event_data: EventData) -> None:
        """Record an event occurrence."""
        ...

    def record_handler_duration(
        self,
        handler_id: str,
        duration_ms: float,
        success: bool
    ) -> None:
        """Record handler execution duration."""
        ...

    def record_error(self, event_data: EventData, error: Exception) -> None:
        """Record an error occurrence."""
        ...


@runtime_checkable
class EventExporter(Protocol):
    """Protocol for event export backends (logging, external systems)."""

    async def export(self, event_data: EventData) -> None:
        """Export an event to external system."""
        ...

    async def export_batch(self, events: List[EventData]) -> None:
        """Export a batch of events."""
        ...


class LoggingExporter:
    """Default exporter that logs events to the Python logging system."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger("di.events")

    async def export(self, event_data: EventData) -> None:
        """Log a single event."""
        log_level = self._get_log_level(event_data.severity)

        message = (
            f"[{event_data.event.value}] {event_data.service_name}"
            f" (duration={event_data.duration_ms:.2f}ms)"
        )

        if event_data.error:
            message += f" error={event_data.error_message}"

        if event_data.trace_id:
            message += f" trace_id={event_data.trace_id}"

        self._logger.log(log_level, message, extra=event_data.to_dict())

    async def export_batch(self, events: List[EventData]) -> None:
        """Log a batch of events."""
        for event in events:
            await self.export(event)

    def _get_log_level(self, severity: str) -> int:
        """Map severity to logging level."""
        return {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(severity.lower(), logging.INFO)


class NoOpMetricsCollector:
    """No-operation metrics collector for when metrics are disabled."""

    def record_event(self, event_data: EventData) -> None:
        pass

    def record_handler_duration(
        self,
        handler_id: str,
        duration_ms: float,
        success: bool
    ) -> None:
        pass

    def record_error(self, event_data: EventData, error: Exception) -> None:
        pass


# =============================================================================
# SECTION 6: Event History
# =============================================================================

@dataclass
class EventHistoryEntry:
    """Entry in the event history for replay/debugging."""
    event_data: EventData
    handlers_invoked: int
    handlers_succeeded: int
    handlers_failed: int
    total_duration_ms: float
    recorded_at: float = field(default_factory=time.time)


class EventHistory:
    """
    Thread-safe event history with configurable retention.

    Stores recent events for debugging and replay purposes.
    Uses a ring buffer to limit memory usage.
    """

    def __init__(
        self,
        max_events: int = 10000,
        max_age_seconds: float = 3600.0
    ) -> None:
        self._events: Deque[EventHistoryEntry] = deque(maxlen=max_events)
        self._max_age = max_age_seconds
        self._lock = threading.Lock()
        self._by_service: Dict[str, Deque[EventHistoryEntry]] = {}
        self._by_event: Dict[ServiceEvent, Deque[EventHistoryEntry]] = {}
        self._service_buffer_size = 100

    def record(self, entry: EventHistoryEntry) -> None:
        """Record an event in history."""
        with self._lock:
            self._events.append(entry)

            # Index by service
            service_name = entry.event_data.service_name
            if service_name not in self._by_service:
                self._by_service[service_name] = deque(
                    maxlen=self._service_buffer_size
                )
            self._by_service[service_name].append(entry)

            # Index by event type
            event = entry.event_data.event
            if event not in self._by_event:
                self._by_event[event] = deque(maxlen=1000)
            self._by_event[event].append(entry)

    def get_recent(
        self,
        limit: int = 100,
        event_type: Optional[ServiceEvent] = None,
        service_name: Optional[str] = None,
        since: Optional[float] = None
    ) -> List[EventHistoryEntry]:
        """
        Get recent events with optional filtering.

        Args:
            limit: Maximum number of events to return
            event_type: Filter by event type
            service_name: Filter by service name
            since: Only events after this timestamp

        Returns:
            List of matching events, most recent first
        """
        with self._lock:
            # Choose the best index
            if service_name and service_name in self._by_service:
                events = list(self._by_service[service_name])
            elif event_type and event_type in self._by_event:
                events = list(self._by_event[event_type])
            else:
                events = list(self._events)

            # Apply filters
            if since:
                events = [e for e in events if e.recorded_at >= since]

            if event_type:
                events = [
                    e for e in events
                    if e.event_data.event == event_type
                ]

            if service_name:
                events = [
                    e for e in events
                    if e.event_data.service_name == service_name
                ]

            # Return most recent first
            return list(reversed(events[-limit:]))

    def get_by_trace(self, trace_id: str) -> List[EventHistoryEntry]:
        """Get all events for a specific trace."""
        with self._lock:
            return [
                e for e in self._events
                if e.event_data.trace_id == trace_id
            ]

    def get_errors(
        self,
        since: Optional[float] = None,
        limit: int = 100
    ) -> List[EventHistoryEntry]:
        """Get recent error events."""
        with self._lock:
            errors = [
                e for e in self._events
                if e.event_data.error is not None
            ]

            if since:
                errors = [e for e in errors if e.recorded_at >= since]

            return list(reversed(errors[-limit:]))

    def get_stats(self) -> Dict[str, Any]:
        """Get history statistics."""
        with self._lock:
            by_event = {}
            for event, entries in self._by_event.items():
                by_event[event.value] = len(entries)

            return {
                "total_events": len(self._events),
                "unique_services": len(self._by_service),
                "by_event": by_event,
                "oldest_event": (
                    self._events[0].recorded_at if self._events else None
                ),
                "newest_event": (
                    self._events[-1].recorded_at if self._events else None
                ),
            }

    def clear(self) -> int:
        """Clear all history. Returns number of events cleared."""
        with self._lock:
            count = len(self._events)
            self._events.clear()
            self._by_service.clear()
            self._by_event.clear()
            return count

    def cleanup_old(self) -> int:
        """Remove events older than max_age. Returns count removed."""
        cutoff = time.time() - self._max_age
        removed = 0

        with self._lock:
            while self._events and self._events[0].recorded_at < cutoff:
                entry = self._events.popleft()
                removed += 1

                # Clean up indices
                service = entry.event_data.service_name
                if service in self._by_service:
                    try:
                        self._by_service[service].remove(entry)
                    except ValueError:
                        pass

                event = entry.event_data.event
                if event in self._by_event:
                    try:
                        self._by_event[event].remove(entry)
                    except ValueError:
                        pass

        return removed


# =============================================================================
# SECTION 7: EventEmitter Class
# =============================================================================

class EventEmitter:
    """
    Thread-safe event emitter with advanced features.

    This is the core class for emitting and subscribing to service
    lifecycle events. It supports:

    - Sync and async handlers
    - Weak references to prevent memory leaks
    - Handler timeouts to prevent blocking
    - Event filtering by type and service
    - Batch emission for performance
    - Event history for debugging
    - Metrics and observability hooks

    Thread Safety:
        All subscription operations are thread-safe. Event emission
        is safe to call from any thread or async context.

    Memory Safety:
        By default, handlers are stored as weak references. This means
        if the object containing the handler is garbage collected, the
        subscription is automatically removed. Set weak_ref=False to
        keep handlers alive.

    Example:
        emitter = EventEmitter()

        # Subscribe to specific event
        sub_id = emitter.subscribe(
            ServiceEvent.STARTED,
            lambda e: print(f"Service started: {e.service_name}")
        )

        # Subscribe to all events for a service type
        emitter.subscribe_all(
            handler=async_handler,
            service_type=MyService
        )

        # Emit event
        await emitter.emit_async(event_data)

        # Unsubscribe
        emitter.unsubscribe(sub_id)
    """

    def __init__(
        self,
        max_history: int = 10000,
        default_timeout_ms: float = 5000.0,
        enable_history: bool = True,
        enable_metrics: bool = True,
        cleanup_interval: float = 60.0,
    ) -> None:
        """
        Initialize the event emitter.

        Args:
            max_history: Maximum events to retain in history
            default_timeout_ms: Default handler timeout
            enable_history: Whether to record event history
            enable_metrics: Whether to collect metrics
            cleanup_interval: Interval for cleaning up dead subscriptions
        """
        self._subscriptions: Dict[str, Subscription] = {}
        self._lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

        # Configuration
        self._default_timeout_ms = default_timeout_ms
        self._enable_history = enable_history
        self._enable_metrics = enable_metrics
        self._cleanup_interval = cleanup_interval

        # History
        self._history = EventHistory(max_events=max_history) if enable_history else None

        # Metrics
        self._metrics: MetricsCollector = NoOpMetricsCollector()

        # Exporters
        self._exporters: List[EventExporter] = []

        # Error aggregation
        self._error_counts: Dict[str, int] = {}
        self._error_lock = threading.Lock()

        # Cleanup tracking
        self._last_cleanup = time.time()
        self._cleanup_task: Optional[asyncio.Task] = None

        # Batch emission buffer
        self._batch_buffer: List[EventData] = []
        self._batch_lock = threading.Lock()
        self._batch_size = 100
        self._batch_flush_interval = 1.0

        logger.debug("EventEmitter initialized")

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create async lock."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------

    def subscribe(
        self,
        event: ServiceEvent,
        handler: Handler,
        service_type: Optional[Type] = None,
        weak_ref: bool = True,
        priority: int = 100,
        timeout_ms: Optional[float] = None,
        description: str = "",
        one_shot: bool = False,
    ) -> str:
        """
        Subscribe to a specific event type.

        Args:
            event: The event type to subscribe to
            handler: Callback function (sync or async)
            service_type: Optional filter for specific service types
            weak_ref: Use weak reference to prevent memory leaks
            priority: Execution priority (lower = earlier)
            timeout_ms: Handler timeout (uses default if None)
            description: Human-readable description
            one_shot: Remove subscription after first invocation

        Returns:
            Subscription ID for later unsubscription
        """
        subscription = Subscription.create(
            handler=handler,
            event=event,
            service_type=service_type,
            weak_ref=weak_ref,
            priority=priority,
            timeout_ms=timeout_ms or self._default_timeout_ms,
            description=description,
            one_shot=one_shot,
        )

        with self._lock:
            self._subscriptions[subscription.id] = subscription

        logger.debug(
            f"Subscription created: {subscription.id} for {event.value}"
        )
        return subscription.id

    def subscribe_all(
        self,
        handler: Handler,
        service_type: Optional[Type] = None,
        weak_ref: bool = True,
        priority: int = 100,
        timeout_ms: Optional[float] = None,
        description: str = "",
    ) -> str:
        """
        Subscribe to all event types.

        Args:
            handler: Callback function (sync or async)
            service_type: Optional filter for specific service types
            weak_ref: Use weak reference to prevent memory leaks
            priority: Execution priority (lower = earlier)
            timeout_ms: Handler timeout
            description: Human-readable description

        Returns:
            Subscription ID for later unsubscription
        """
        subscription = Subscription.create(
            handler=handler,
            event=None,  # None means all events
            service_type=service_type,
            weak_ref=weak_ref,
            priority=priority,
            timeout_ms=timeout_ms or self._default_timeout_ms,
            description=description,
        )

        with self._lock:
            self._subscriptions[subscription.id] = subscription

        logger.debug(
            f"Subscription created: {subscription.id} for all events"
        )
        return subscription.id

    def subscribe_once(
        self,
        event: ServiceEvent,
        handler: Handler,
        service_type: Optional[Type] = None,
        timeout_ms: Optional[float] = None,
    ) -> str:
        """
        Subscribe to receive exactly one event.

        The subscription is automatically removed after the first invocation.

        Args:
            event: The event type to subscribe to
            handler: Callback function
            service_type: Optional service type filter
            timeout_ms: Handler timeout

        Returns:
            Subscription ID
        """
        return self.subscribe(
            event=event,
            handler=handler,
            service_type=service_type,
            weak_ref=False,  # Keep alive until invoked
            timeout_ms=timeout_ms,
            one_shot=True,
        )

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        Remove a subscription.

        Args:
            subscription_id: The ID returned from subscribe()

        Returns:
            True if subscription was found and removed
        """
        with self._lock:
            if subscription_id in self._subscriptions:
                del self._subscriptions[subscription_id]
                logger.debug(f"Subscription removed: {subscription_id}")
                return True
            return False

    def clear(self) -> int:
        """
        Remove all subscriptions.

        Returns:
            Number of subscriptions removed
        """
        with self._lock:
            count = len(self._subscriptions)
            self._subscriptions.clear()
            logger.debug(f"Cleared {count} subscriptions")
            return count

    # -------------------------------------------------------------------------
    # Event Emission
    # -------------------------------------------------------------------------

    def emit(self, event_data: EventData) -> Tuple[int, int]:
        """
        Emit an event synchronously.

        Invokes all matching sync handlers. Async handlers are scheduled
        to run in the background if an event loop is available.

        Args:
            event_data: The event data to emit

        Returns:
            Tuple of (handlers_invoked, handlers_failed)
        """
        start_time = time.time()
        invoked = 0
        failed = 0
        to_remove: List[str] = []

        # Get matching subscriptions
        with self._lock:
            subscriptions = [
                (sub_id, sub) for sub_id, sub in self._subscriptions.items()
            ]

        # Sort by priority
        subscriptions.sort(key=lambda x: x[1].priority)

        for sub_id, subscription in subscriptions:
            if not subscription.matches(event_data):
                continue

            handler = subscription.get_handler()
            if handler is None:
                # Handler was garbage collected
                to_remove.append(sub_id)
                continue

            invoked += 1

            try:
                if subscription.is_async:
                    # Schedule async handler
                    self._schedule_async_handler(handler, event_data, subscription)
                else:
                    # Invoke sync handler with timeout
                    self._invoke_sync_handler(handler, event_data, subscription)

                subscription.record_invocation(success=True)

                if subscription.one_shot:
                    to_remove.append(sub_id)

            except Exception as e:
                failed += 1
                subscription.record_invocation(success=False)
                self._handle_handler_error(subscription, event_data, e)

        # Cleanup dead subscriptions
        if to_remove:
            with self._lock:
                for sub_id in to_remove:
                    self._subscriptions.pop(sub_id, None)

        # Record to history
        duration_ms = (time.time() - start_time) * 1000
        self._record_emission(event_data, invoked, invoked - failed, failed, duration_ms)

        # Metrics
        if self._enable_metrics:
            self._metrics.record_event(event_data)

        return (invoked, failed)

    async def emit_async(self, event_data: EventData) -> Tuple[int, int]:
        """
        Emit an event asynchronously.

        All handlers (both sync and async) are awaited with proper timeout
        handling.

        Args:
            event_data: The event data to emit

        Returns:
            Tuple of (handlers_invoked, handlers_failed)
        """
        start_time = time.time()
        invoked = 0
        failed = 0
        to_remove: List[str] = []

        # Get matching subscriptions
        async with self._get_async_lock():
            subscriptions = [
                (sub_id, sub) for sub_id, sub in self._subscriptions.items()
            ]

        # Sort by priority
        subscriptions.sort(key=lambda x: x[1].priority)

        tasks: List[Tuple[str, Subscription, asyncio.Task]] = []

        for sub_id, subscription in subscriptions:
            if not subscription.matches(event_data):
                continue

            handler = subscription.get_handler()
            if handler is None:
                to_remove.append(sub_id)
                continue

            invoked += 1

            # Create task for handler
            if subscription.is_async:
                task = asyncio.create_task(
                    self._invoke_async_handler(handler, event_data, subscription)
                )
            else:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        self._invoke_sync_handler,
                        handler,
                        event_data,
                        subscription
                    )
                )

            tasks.append((sub_id, subscription, task))

        # Wait for all handlers with individual timeouts
        for sub_id, subscription, task in tasks:
            timeout_s = subscription.timeout_ms / 1000

            try:
                await asyncio.wait_for(task, timeout=timeout_s)
                subscription.record_invocation(success=True)

                if subscription.one_shot:
                    to_remove.append(sub_id)

            except asyncio.TimeoutError:
                failed += 1
                subscription.record_invocation(success=False)
                logger.warning(
                    f"Handler timeout: {subscription.description} "
                    f"after {subscription.timeout_ms}ms"
                )
                task.cancel()

            except Exception as e:
                failed += 1
                subscription.record_invocation(success=False)
                self._handle_handler_error(subscription, event_data, e)

        # Cleanup
        if to_remove:
            async with self._get_async_lock():
                for sub_id in to_remove:
                    self._subscriptions.pop(sub_id, None)

        # Export to external systems
        await self._export_event(event_data)

        # Record to history
        duration_ms = (time.time() - start_time) * 1000
        self._record_emission(
            event_data, invoked, invoked - failed, failed, duration_ms
        )

        # Metrics
        if self._enable_metrics:
            self._metrics.record_event(event_data)

        return (invoked, failed)

    async def emit_batch(
        self,
        events: List[EventData],
        parallel: bool = True
    ) -> Dict[str, Tuple[int, int]]:
        """
        Emit multiple events in a batch.

        Args:
            events: List of events to emit
            parallel: If True, emit all events concurrently

        Returns:
            Dict mapping event_id to (invoked, failed) counts
        """
        results: Dict[str, Tuple[int, int]] = {}

        if parallel:
            # Emit all events concurrently
            tasks = [
                (event.event_id, asyncio.create_task(self.emit_async(event)))
                for event in events
            ]

            for event_id, task in tasks:
                try:
                    results[event_id] = await task
                except Exception as e:
                    logger.error(f"Batch emit error for {event_id}: {e}")
                    results[event_id] = (0, 1)
        else:
            # Emit sequentially
            for event in events:
                try:
                    results[event.event_id] = await self.emit_async(event)
                except Exception as e:
                    logger.error(f"Batch emit error for {event.event_id}: {e}")
                    results[event.event_id] = (0, 1)

        # Batch export
        await self._export_batch(events)

        return results

    def queue_for_batch(self, event_data: EventData) -> None:
        """
        Queue an event for batch emission.

        Events are accumulated and emitted together for efficiency.
        Call flush_batch() to emit queued events.
        """
        with self._batch_lock:
            self._batch_buffer.append(event_data)

            if len(self._batch_buffer) >= self._batch_size:
                # Auto-flush when buffer is full
                self._schedule_batch_flush()

    async def flush_batch(self) -> Dict[str, Tuple[int, int]]:
        """
        Flush and emit all queued batch events.

        Returns:
            Dict mapping event_id to (invoked, failed) counts
        """
        with self._batch_lock:
            events = self._batch_buffer[:]
            self._batch_buffer.clear()

        if not events:
            return {}

        return await self.emit_batch(events)

    # -------------------------------------------------------------------------
    # Handler Invocation
    # -------------------------------------------------------------------------

    def _invoke_sync_handler(
        self,
        handler: Handler,
        event_data: EventData,
        subscription: Subscription
    ) -> None:
        """Invoke a synchronous handler."""
        # Cast to sync handler - caller ensures this is not async
        cast(SyncHandler, handler)(event_data)

    async def _invoke_async_handler(
        self,
        handler: Handler,
        event_data: EventData,
        subscription: Subscription
    ) -> None:
        """Invoke an async handler."""
        # Cast to async handler - caller ensures this is async
        await cast(AsyncHandler, handler)(event_data)

    def _schedule_async_handler(
        self,
        handler: Handler,
        event_data: EventData,
        subscription: Subscription
    ) -> None:
        """Schedule an async handler to run in the background."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._run_async_handler_with_timeout(
                    handler, event_data, subscription
                )
            )
        except RuntimeError:
            # No running loop - can't schedule async handler
            logger.warning(
                f"Cannot schedule async handler {subscription.description}: "
                "no running event loop"
            )

    async def _run_async_handler_with_timeout(
        self,
        handler: Handler,
        event_data: EventData,
        subscription: Subscription
    ) -> None:
        """Run an async handler with timeout protection."""
        timeout_s = subscription.timeout_ms / 1000

        try:
            # Cast to async handler - caller ensures this is async
            await asyncio.wait_for(cast(AsyncHandler, handler)(event_data), timeout=timeout_s)
            subscription.record_invocation(success=True)
        except asyncio.TimeoutError:
            subscription.record_invocation(success=False)
            logger.warning(
                f"Async handler timeout: {subscription.description}"
            )
        except Exception as e:
            subscription.record_invocation(success=False)
            self._handle_handler_error(subscription, event_data, e)

    def _handle_handler_error(
        self,
        subscription: Subscription,
        event_data: EventData,
        error: Exception
    ) -> None:
        """Handle errors from event handlers."""
        error_key = f"{subscription.id}:{type(error).__name__}"

        with self._error_lock:
            self._error_counts[error_key] = (
                self._error_counts.get(error_key, 0) + 1
            )

        logger.error(
            f"Handler error in {subscription.description}: {error}",
            exc_info=True
        )

        if self._enable_metrics:
            self._metrics.record_error(event_data, error)

    def _schedule_batch_flush(self) -> None:
        """Schedule a batch flush if not already scheduled."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(
                self._batch_flush_interval,
                lambda: asyncio.create_task(self.flush_batch())
            )
        except RuntimeError:
            pass  # No loop available

    # -------------------------------------------------------------------------
    # History and Export
    # -------------------------------------------------------------------------

    def _record_emission(
        self,
        event_data: EventData,
        invoked: int,
        succeeded: int,
        failed: int,
        duration_ms: float
    ) -> None:
        """Record event emission to history."""
        if self._history is None:
            return

        entry = EventHistoryEntry(
            event_data=event_data,
            handlers_invoked=invoked,
            handlers_succeeded=succeeded,
            handlers_failed=failed,
            total_duration_ms=duration_ms,
        )
        self._history.record(entry)

    async def _export_event(self, event_data: EventData) -> None:
        """Export event to registered exporters."""
        for exporter in self._exporters:
            try:
                await exporter.export(event_data)
            except Exception as e:
                logger.error(f"Export error: {e}")

    async def _export_batch(self, events: List[EventData]) -> None:
        """Export batch of events."""
        for exporter in self._exporters:
            try:
                await exporter.export_batch(events)
            except Exception as e:
                logger.error(f"Batch export error: {e}")

    # -------------------------------------------------------------------------
    # Cleanup and Maintenance
    # -------------------------------------------------------------------------

    def cleanup_dead_subscriptions(self) -> int:
        """
        Remove subscriptions with garbage-collected handlers.

        Returns:
            Number of subscriptions removed
        """
        to_remove: List[str] = []

        with self._lock:
            for sub_id, subscription in self._subscriptions.items():
                if not subscription.is_alive():
                    to_remove.append(sub_id)

            for sub_id in to_remove:
                del self._subscriptions[sub_id]

        if to_remove:
            logger.debug(f"Cleaned up {len(to_remove)} dead subscriptions")

        self._last_cleanup = time.time()
        return len(to_remove)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is not None:
            return

        async def cleanup_loop() -> None:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                self.cleanup_dead_subscriptions()

                if self._history:
                    self._history.cleanup_old()

        self._cleanup_task = asyncio.create_task(cleanup_loop())

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    def set_metrics_collector(self, collector: MetricsCollector) -> None:
        """Set the metrics collector."""
        self._metrics = collector

    def add_exporter(self, exporter: EventExporter) -> None:
        """Add an event exporter."""
        self._exporters.append(exporter)

    def remove_exporter(self, exporter: EventExporter) -> bool:
        """Remove an event exporter."""
        try:
            self._exporters.remove(exporter)
            return True
        except ValueError:
            return False

    # -------------------------------------------------------------------------
    # Introspection
    # -------------------------------------------------------------------------

    def get_subscription_count(self) -> int:
        """Get number of active subscriptions."""
        with self._lock:
            return len(self._subscriptions)

    def get_subscriptions(self) -> List[Dict[str, Any]]:
        """Get all subscription details."""
        with self._lock:
            return [sub.to_dict() for sub in self._subscriptions.values()]

    def get_subscription(
        self,
        subscription_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get details for a specific subscription."""
        with self._lock:
            sub = self._subscriptions.get(subscription_id)
            return sub.to_dict() if sub else None

    def get_history(
        self,
        limit: int = 100,
        event_type: Optional[ServiceEvent] = None,
        service_name: Optional[str] = None,
        since: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Get event history."""
        if self._history is None:
            return []

        entries = self._history.get_recent(
            limit=limit,
            event_type=event_type,
            service_name=service_name,
            since=since,
        )

        return [
            {
                "event": e.event_data.to_dict(),
                "handlers_invoked": e.handlers_invoked,
                "handlers_succeeded": e.handlers_succeeded,
                "handlers_failed": e.handlers_failed,
                "total_duration_ms": e.total_duration_ms,
                "recorded_at": e.recorded_at,
            }
            for e in entries
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get emitter statistics."""
        with self._lock:
            by_event: Dict[str, int] = {}
            by_service_type: Dict[str, int] = {}
            alive_count = 0

            for sub in self._subscriptions.values():
                event_key = sub.event.value if sub.event else "all"
                by_event[event_key] = by_event.get(event_key, 0) + 1

                if sub.service_type:
                    type_name = sub.service_type.__name__
                    by_service_type[type_name] = (
                        by_service_type.get(type_name, 0) + 1
                    )

                if sub.is_alive():
                    alive_count += 1

        history_stats = self._history.get_stats() if self._history else {}

        with self._error_lock:
            error_counts = dict(self._error_counts)

        return {
            "total_subscriptions": len(self._subscriptions),
            "alive_subscriptions": alive_count,
            "subscriptions_by_event": by_event,
            "subscriptions_by_service_type": by_service_type,
            "exporters": len(self._exporters),
            "history": history_stats,
            "error_counts": error_counts,
            "last_cleanup": self._last_cleanup,
            "batch_buffer_size": len(self._batch_buffer),
        }


# =============================================================================
# SECTION 8: Convenience Functions and Decorators
# =============================================================================

def on_event(
    emitter: EventEmitter,
    event: ServiceEvent,
    service_type: Optional[Type] = None,
    weak_ref: bool = True,
    priority: int = 100,
):
    """
    Decorator to register a function as an event handler.

    Example:
        @on_event(emitter, ServiceEvent.STARTED)
        async def handle_started(event_data: EventData):
            print(f"Service started: {event_data.service_name}")
    """
    def decorator(handler: Handler) -> Handler:
        emitter.subscribe(
            event=event,
            handler=handler,
            service_type=service_type,
            weak_ref=weak_ref,
            priority=priority,
        )
        return handler
    return decorator


def on_all_events(
    emitter: EventEmitter,
    service_type: Optional[Type] = None,
    weak_ref: bool = True,
    priority: int = 100,
):
    """
    Decorator to register a function as a handler for all events.
    """
    def decorator(handler: Handler) -> Handler:
        emitter.subscribe_all(
            handler=handler,
            service_type=service_type,
            weak_ref=weak_ref,
            priority=priority,
        )
        return handler
    return decorator


@asynccontextmanager
async def event_scope(
    emitter: EventEmitter,
    service_name: str,
    service_type: Optional[Type] = None,
    correlation_id: Optional[str] = None,
):
    """
    Context manager that emits lifecycle events for a scope.

    Emits STARTING on entry and STOPPED/FAILED on exit.

    Example:
        async with event_scope(emitter, "my_operation"):
            # Operation code here
            pass
    """
    start_time = time.time()
    correlation_id = correlation_id or str(uuid.uuid4())

    # Emit starting event
    starting_event = (EventDataBuilder()
        .with_event(ServiceEvent.STARTING)
        .with_service(service_name, service_type)
        .with_correlation(correlation_id)
        .build())

    await emitter.emit_async(starting_event)

    try:
        yield

        # Emit stopped event on success
        duration_ms = (time.time() - start_time) * 1000
        stopped_event = (EventDataBuilder()
            .with_event(ServiceEvent.STOPPED)
            .with_service(service_name, service_type)
            .with_duration(duration_ms)
            .with_correlation(correlation_id)
            .build())

        await emitter.emit_async(stopped_event)

    except Exception as e:
        # Emit failed event on error
        duration_ms = (time.time() - start_time) * 1000
        failed_event = (EventDataBuilder()
            .with_event(ServiceEvent.FAILED)
            .with_service(service_name, service_type)
            .with_duration(duration_ms)
            .with_error(e)
            .with_correlation(correlation_id)
            .build())

        await emitter.emit_async(failed_event)
        raise


@contextmanager
def event_scope_sync(
    emitter: EventEmitter,
    service_name: str,
    service_type: Optional[Type] = None,
):
    """Synchronous version of event_scope."""
    start_time = time.time()
    correlation_id = str(uuid.uuid4())

    # Emit starting event
    starting_event = (EventDataBuilder()
        .with_event(ServiceEvent.STARTING)
        .with_service(service_name, service_type)
        .with_correlation(correlation_id)
        .build())

    emitter.emit(starting_event)

    try:
        yield

        # Emit stopped event on success
        duration_ms = (time.time() - start_time) * 1000
        stopped_event = (EventDataBuilder()
            .with_event(ServiceEvent.STOPPED)
            .with_service(service_name, service_type)
            .with_duration(duration_ms)
            .with_correlation(correlation_id)
            .build())

        emitter.emit(stopped_event)

    except Exception as e:
        # Emit failed event on error
        duration_ms = (time.time() - start_time) * 1000
        failed_event = (EventDataBuilder()
            .with_event(ServiceEvent.FAILED)
            .with_service(service_name, service_type)
            .with_duration(duration_ms)
            .with_error(e)
            .with_correlation(correlation_id)
            .build())

        emitter.emit(failed_event)
        raise


# =============================================================================
# SECTION 9: Trace Context Management
# =============================================================================

def set_trace_context(
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """
    Set trace context for the current async context.

    Returns the previous trace and span IDs.
    """
    prev_trace = _current_trace_id.get()
    prev_span = _current_span_id.get()

    if trace_id is not None:
        _current_trace_id.set(trace_id)
    if span_id is not None:
        _current_span_id.set(span_id)

    return (prev_trace, prev_span)


def get_trace_context() -> Tuple[Optional[str], Optional[str]]:
    """Get the current trace and span IDs."""
    return (_current_trace_id.get(), _current_span_id.get())


def clear_trace_context() -> None:
    """Clear the current trace context."""
    _current_trace_id.set(None)
    _current_span_id.set(None)


@asynccontextmanager
async def trace_context(
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None
):
    """
    Context manager for trace context propagation.

    Automatically generates trace/span IDs if not provided.

    Example:
        async with trace_context() as (trace_id, span_id):
            # All events emitted here will have this trace context
            await emitter.emit_async(event)
    """
    trace_id = trace_id or str(uuid.uuid4())
    span_id = str(uuid.uuid4())[:16]

    prev_trace, prev_span = set_trace_context(trace_id, span_id)

    try:
        yield (trace_id, span_id)
    finally:
        set_trace_context(prev_trace, prev_span)


# =============================================================================
# SECTION 10: Global Emitter Instance
# =============================================================================

_global_emitter: Optional[EventEmitter] = None
_emitter_lock = threading.Lock()


def get_emitter() -> EventEmitter:
    """
    Get the global event emitter instance.

    Creates a default emitter with logging exporter if none exists.
    """
    global _global_emitter

    if _global_emitter is None:
        with _emitter_lock:
            if _global_emitter is None:
                _global_emitter = EventEmitter()
                _global_emitter.add_exporter(LoggingExporter())

    return _global_emitter


def init_emitter(
    max_history: int = 10000,
    default_timeout_ms: float = 5000.0,
    enable_history: bool = True,
    enable_metrics: bool = True,
    exporters: Optional[List[EventExporter]] = None,
    metrics_collector: Optional[MetricsCollector] = None,
) -> EventEmitter:
    """
    Initialize the global event emitter with custom configuration.

    Replaces any existing global emitter.

    Args:
        max_history: Maximum events to retain in history
        default_timeout_ms: Default handler timeout
        enable_history: Whether to record event history
        enable_metrics: Whether to collect metrics
        exporters: List of event exporters
        metrics_collector: Custom metrics collector

    Returns:
        The newly initialized emitter
    """
    global _global_emitter

    with _emitter_lock:
        emitter = EventEmitter(
            max_history=max_history,
            default_timeout_ms=default_timeout_ms,
            enable_history=enable_history,
            enable_metrics=enable_metrics,
        )

        if exporters:
            for exporter in exporters:
                emitter.add_exporter(exporter)
        else:
            emitter.add_exporter(LoggingExporter())

        if metrics_collector:
            emitter.set_metrics_collector(metrics_collector)

        _global_emitter = emitter
        return emitter


async def shutdown_emitter() -> None:
    """Shutdown the global emitter and cleanup resources."""
    global _global_emitter

    if _global_emitter is not None:
        await _global_emitter.stop_cleanup_task()
        _global_emitter.clear()
        _global_emitter = None


# =============================================================================
# SECTION 11: Testing Utilities
# =============================================================================

class EventCapture:
    """
    Utility for capturing events in tests.

    Example:
        async with EventCapture(emitter) as capture:
            await service.start()

        assert capture.has_event(ServiceEvent.STARTED)
        assert capture.count(ServiceEvent.STARTED) == 1
    """

    def __init__(
        self,
        emitter: EventEmitter,
        events: Optional[Iterable[ServiceEvent]] = None,
        service_type: Optional[Type] = None,
    ) -> None:
        self._emitter = emitter
        self._events = set(events) if events else None
        self._service_type = service_type
        self._captured: List[EventData] = []
        self._subscription_ids: List[str] = []

    async def __aenter__(self) -> "EventCapture":
        """Start capturing events."""
        if self._events:
            for event in self._events:
                sub_id = self._emitter.subscribe(
                    event=event,
                    handler=self._capture,
                    service_type=self._service_type,
                    weak_ref=False,
                )
                self._subscription_ids.append(sub_id)
        else:
            sub_id = self._emitter.subscribe_all(
                handler=self._capture,
                service_type=self._service_type,
                weak_ref=False,
            )
            self._subscription_ids.append(sub_id)

        return self

    async def __aexit__(self, *args) -> None:
        """Stop capturing events."""
        for sub_id in self._subscription_ids:
            self._emitter.unsubscribe(sub_id)

    def _capture(self, event_data: EventData) -> None:
        """Capture an event."""
        self._captured.append(event_data)

    @property
    def events(self) -> List[EventData]:
        """Get all captured events."""
        return list(self._captured)

    def has_event(
        self,
        event: ServiceEvent,
        service_name: Optional[str] = None
    ) -> bool:
        """Check if an event was captured."""
        for e in self._captured:
            if e.event == event:
                if service_name is None or e.service_name == service_name:
                    return True
        return False

    def count(
        self,
        event: Optional[ServiceEvent] = None,
        service_name: Optional[str] = None
    ) -> int:
        """Count captured events with optional filtering."""
        count = 0
        for e in self._captured:
            if event is not None and e.event != event:
                continue
            if service_name is not None and e.service_name != service_name:
                continue
            count += 1
        return count

    def get_events(
        self,
        event: Optional[ServiceEvent] = None,
        service_name: Optional[str] = None
    ) -> List[EventData]:
        """Get captured events with optional filtering."""
        result = []
        for e in self._captured:
            if event is not None and e.event != event:
                continue
            if service_name is not None and e.service_name != service_name:
                continue
            result.append(e)
        return result

    def clear(self) -> None:
        """Clear captured events."""
        self._captured.clear()

    async def wait_for(
        self,
        event: ServiceEvent,
        service_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> Optional[EventData]:
        """
        Wait for a specific event to be captured.

        Returns the event if captured, or None if timeout.
        """
        start = time.time()

        while time.time() - start < timeout:
            for e in self._captured:
                if e.event == event:
                    if service_name is None or e.service_name == service_name:
                        return e
            await asyncio.sleep(0.01)

        return None


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Enums
    "ServiceEvent",

    # Data classes
    "EventData",
    "EventDataBuilder",
    "Subscription",
    "EventHistoryEntry",

    # Core classes
    "EventEmitter",
    "EventHistory",
    "WeakMethodRef",

    # Protocols
    "MetricsCollector",
    "EventExporter",

    # Built-in implementations
    "LoggingExporter",
    "NoOpMetricsCollector",

    # Decorators and context managers
    "on_event",
    "on_all_events",
    "event_scope",
    "event_scope_sync",
    "trace_context",

    # Trace context
    "set_trace_context",
    "get_trace_context",
    "clear_trace_context",

    # Global instance
    "get_emitter",
    "init_emitter",
    "shutdown_emitter",

    # Testing
    "EventCapture",
]

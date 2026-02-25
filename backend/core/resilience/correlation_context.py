"""
Correlation Context for Cross-Repo Request Tracing
==================================================

Provides request correlation IDs that propagate across repo boundaries.

Features:
    - Unique correlation IDs for request tracing
    - Context propagation via contextvars
    - Automatic ID generation with configurable format
    - Nested context support (parent-child relationships)
    - Integration with file-based RPC and Redis pub/sub
    - Structured logging support

Author: JARVIS Cross-Repo Resilience
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# TraceEnvelope integration (optional — never breaks if module unavailable)
# ---------------------------------------------------------------------------
try:
    from backend.core.trace_envelope import (
        TraceEnvelope, TraceEnvelopeFactory, LamportClock, BoundaryType,
    )
    _TRACE_ENVELOPE_AVAILABLE = True
except ImportError:
    _TRACE_ENVELOPE_AVAILABLE = False

_envelope_factory: Optional["TraceEnvelopeFactory"] = None
_envelope_factory_lock = threading.Lock()


def _get_envelope_factory() -> Optional["TraceEnvelopeFactory"]:
    """Lazy-init the envelope factory (thread-safe, double-checked locking).

    Returns None if trace_envelope module is not available.
    """
    global _envelope_factory
    if _envelope_factory is not None:
        return _envelope_factory
    if not _TRACE_ENVELOPE_AVAILABLE:
        return None
    with _envelope_factory_lock:
        if _envelope_factory is not None:
            return _envelope_factory
        _envelope_factory = TraceEnvelopeFactory(
            repo="jarvis",
            boot_id=os.environ.get("JARVIS_BOOT_ID", uuid.uuid4().hex[:16]),
            runtime_epoch_id=os.environ.get("JARVIS_RUNTIME_EPOCH_ID", uuid.uuid4().hex[:16]),
            node_id=os.environ.get("JARVIS_NODE_ID", os.uname().nodename),
            producer_version=os.environ.get("JARVIS_VERSION", "dev"),
        )
        return _envelope_factory


def _reset_envelope_factory() -> None:
    """Reset the factory singleton. For testing only."""
    global _envelope_factory
    _envelope_factory = None

# Context variable for current correlation context
_current_context: contextvars.ContextVar[Optional["CorrelationContext"]] = contextvars.ContextVar(
    "correlation_context",
    default=None,
)


@dataclass
class SpanInfo:
    """Information about a single span in the trace."""

    span_id: str
    operation: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"  # in_progress, success, error
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    children: List["SpanInfo"] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class CorrelationContext:
    """
    Context for correlating requests across repos.

    Carries correlation IDs, timing information, and trace data
    that propagates through file-based RPC and Redis pub/sub.
    """

    correlation_id: str
    parent_id: Optional[str] = None
    source_repo: str = "jarvis"
    source_component: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    # Trace information
    root_span: Optional[SpanInfo] = None
    current_span: Optional[SpanInfo] = None

    # Baggage: key-value pairs that propagate with context
    baggage: Dict[str, str] = field(default_factory=dict)

    # Timing
    request_deadline: Optional[float] = None  # Unix timestamp when request times out

    # TraceEnvelope backing store (None if trace_envelope module unavailable)
    envelope: Optional[Any] = None  # Type is TraceEnvelope but uses Any for import safety

    @classmethod
    def generate_id(cls, prefix: str = "") -> str:
        """Generate a unique correlation ID."""
        timestamp = int(time.time() * 1000)
        unique = uuid.uuid4().hex[:12]
        pid = os.getpid()

        if prefix:
            return f"{prefix}-{timestamp}-{pid}-{unique}"
        return f"{timestamp}-{pid}-{unique}"

    @classmethod
    def create(
        cls,
        operation: str = "",
        source_repo: str = "jarvis",
        source_component: Optional[str] = None,
        parent: Optional["CorrelationContext"] = None,
        timeout: Optional[float] = None,
    ) -> "CorrelationContext":
        """
        Create a new correlation context.

        Args:
            operation: Name of the operation starting this context
            source_repo: Source repository name
            source_component: Source component name
            parent: Parent context (for nested operations)
            timeout: Request timeout in seconds

        Returns:
            New correlation context
        """
        correlation_id = cls.generate_id(source_repo[:3] if source_repo else "")
        parent_id = parent.correlation_id if parent else None

        # Inherit baggage from parent
        baggage = dict(parent.baggage) if parent else {}

        # Set deadline
        deadline = None
        if timeout:
            deadline = time.time() + timeout
        elif parent and parent.request_deadline:
            deadline = parent.request_deadline  # Inherit deadline

        ctx = cls(
            correlation_id=correlation_id,
            parent_id=parent_id,
            source_repo=source_repo,
            source_component=source_component,
            baggage=baggage,
            request_deadline=deadline,
        )

        # Create root span
        if operation:
            ctx.root_span = SpanInfo(
                span_id=cls.generate_id("span"),
                operation=operation,
                start_time=time.time(),
            )
            ctx.current_span = ctx.root_span

        # Attach TraceEnvelope if available
        factory = _get_envelope_factory()
        if factory is not None:
            try:
                if parent and parent.envelope is not None:
                    ctx.envelope = factory.create_child(
                        parent=parent.envelope,
                        component=source_component or "",
                        operation=operation or "",
                    )
                else:
                    ctx.envelope = factory.create_root(
                        component=source_component or "",
                        operation=operation or "",
                    )
            except Exception:
                logger.debug("Failed to create TraceEnvelope for context", exc_info=True)

        return ctx

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CorrelationContext":
        """Deserialize context from dictionary."""
        # Handle root_span — filter out computed properties like duration_ms
        root_span = None
        if data.get("root_span"):
            span_data = {
                k: v for k, v in data["root_span"].items()
                if k in ("span_id", "operation", "start_time", "end_time",
                         "status", "error_message", "metadata", "children")
            }
            root_span = SpanInfo(**span_data)

        ctx = cls(
            correlation_id=data["correlation_id"],
            parent_id=data.get("parent_id"),
            source_repo=data.get("source_repo", "jarvis"),
            source_component=data.get("source_component"),
            created_at=data.get("created_at", time.time()),
            root_span=root_span,
            baggage=data.get("baggage", {}),
            request_deadline=data.get("request_deadline"),
        )

        # Restore envelope if present
        if _TRACE_ENVELOPE_AVAILABLE and "_trace_envelope" in data:
            try:
                ctx.envelope = TraceEnvelope.from_dict(data["_trace_envelope"])
            except Exception:
                logger.debug("Failed to restore TraceEnvelope from dict", exc_info=True)

        return ctx

    def to_dict(self) -> Dict[str, Any]:
        """Serialize context to dictionary."""
        result = {
            "correlation_id": self.correlation_id,
            "parent_id": self.parent_id,
            "source_repo": self.source_repo,
            "source_component": self.source_component,
            "created_at": self.created_at,
            "root_span": self.root_span.to_dict() if self.root_span else None,
            "baggage": self.baggage,
            "request_deadline": self.request_deadline,
        }
        if self.envelope is not None:
            try:
                result["_trace_envelope"] = self.envelope.to_dict()
            except Exception:
                logger.debug("Failed to serialize TraceEnvelope in to_dict", exc_info=True)
        return result

    def to_headers(self) -> Dict[str, str]:
        """Convert context to headers for HTTP/RPC propagation."""
        headers = {
            "X-Correlation-ID": self.correlation_id,
            "X-Source-Repo": self.source_repo,
        }

        if self.parent_id:
            headers["X-Parent-Correlation-ID"] = self.parent_id

        if self.source_component:
            headers["X-Source-Component"] = self.source_component

        if self.request_deadline:
            headers["X-Request-Deadline"] = str(self.request_deadline)

        # Propagate baggage
        for key, value in self.baggage.items():
            headers[f"X-Baggage-{key}"] = value

        # Add TraceEnvelope headers (alongside existing correlation headers)
        if self.envelope is not None:
            try:
                headers.update(self.envelope.to_headers())
            except Exception:
                logger.debug("Failed to serialize TraceEnvelope to headers", exc_info=True)

        return headers

    @classmethod
    def from_headers(cls, headers: Dict[str, str]) -> Optional["CorrelationContext"]:
        """Create context from headers."""
        correlation_id = headers.get("X-Correlation-ID")
        if not correlation_id:
            return None

        # Extract baggage
        baggage = {}
        for key, value in headers.items():
            if key.startswith("X-Baggage-"):
                baggage_key = key[10:]  # Remove "X-Baggage-" prefix
                baggage[baggage_key] = value

        deadline = headers.get("X-Request-Deadline")

        ctx = cls(
            correlation_id=correlation_id,
            parent_id=headers.get("X-Parent-Correlation-ID"),
            source_repo=headers.get("X-Source-Repo", "unknown"),
            source_component=headers.get("X-Source-Component"),
            baggage=baggage,
            request_deadline=float(deadline) if deadline else None,
        )

        # Try to restore TraceEnvelope from headers
        if _TRACE_ENVELOPE_AVAILABLE:
            try:
                envelope = TraceEnvelope.from_headers(headers)
                if envelope is not None:
                    ctx.envelope = envelope
                else:
                    # Backward compat: auto-generate envelope for old-style headers
                    factory = _get_envelope_factory()
                    if factory:
                        ctx.envelope = factory.create_root(
                            component=ctx.source_component or "",
                            operation="",
                        )
            except Exception:
                logger.debug("Failed to restore TraceEnvelope from headers", exc_info=True)

        return ctx

    @property
    def remaining_time(self) -> Optional[float]:
        """Get remaining time before deadline."""
        if self.request_deadline is None:
            return None
        return max(0, self.request_deadline - time.time())

    @property
    def is_expired(self) -> bool:
        """Check if context has expired."""
        if self.request_deadline is None:
            return False
        return time.time() > self.request_deadline

    def set_baggage(self, key: str, value: str) -> None:
        """Set a baggage item."""
        self.baggage[key] = value

    def get_baggage(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a baggage item."""
        return self.baggage.get(key, default)

    def start_span(self, operation: str, metadata: Optional[Dict[str, Any]] = None) -> SpanInfo:
        """
        Start a new span as child of current span.

        Args:
            operation: Operation name
            metadata: Additional metadata

        Returns:
            The new span
        """
        span = SpanInfo(
            span_id=self.generate_id("span"),
            operation=operation,
            start_time=time.time(),
            metadata=metadata or {},
        )

        if self.current_span:
            self.current_span.children.append(span)
        elif self.root_span:
            self.root_span.children.append(span)
        else:
            self.root_span = span

        self.current_span = span
        return span

    def end_span(
        self,
        span: Optional[SpanInfo] = None,
        status: str = "success",
        error: Optional[str] = None,
    ) -> None:
        """
        End a span.

        Args:
            span: Span to end (default: current span)
            status: Status (success, error)
            error: Error message if status is error
        """
        span = span or self.current_span
        if not span:
            return

        span.end_time = time.time()
        span.status = status
        if error:
            span.error_message = error

    @contextmanager
    def span(self, operation: str, metadata: Optional[Dict[str, Any]] = None):
        """
        Context manager for creating a span.

        Usage:
            with ctx.span("database_query") as span:
                # Do work
                span.metadata["rows"] = 10
        """
        span = self.start_span(operation, metadata)
        try:
            yield span
            self.end_span(span, status="success")
        except Exception as e:
            self.end_span(span, status="error", error=str(e))
            raise

    def get_trace(self) -> Optional[Dict[str, Any]]:
        """Get the full trace tree."""
        if not self.root_span:
            return None
        return self.root_span.to_dict()


def get_current_correlation_id() -> Optional[str]:
    """Get the current correlation ID from context."""
    ctx = _current_context.get()
    return ctx.correlation_id if ctx else None


def get_current_context() -> Optional[CorrelationContext]:
    """Get the current correlation context."""
    return _current_context.get()


def set_current_context(ctx: Optional[CorrelationContext]) -> None:
    """Set the current correlation context."""
    _current_context.set(ctx)


@contextmanager
def with_correlation(
    operation: str = "",
    source_component: Optional[str] = None,
    timeout: Optional[float] = None,
    inherit: bool = True,
):
    """
    Context manager for correlation context.

    Usage:
        with with_correlation("process_request", source_component="api"):
            # All operations in this block share the correlation ID
            await do_work()

    Args:
        operation: Operation name for root span
        source_component: Component name
        timeout: Request timeout
        inherit: Whether to inherit from parent context
    """
    parent = _current_context.get() if inherit else None

    ctx = CorrelationContext.create(
        operation=operation,
        source_component=source_component,
        parent=parent,
        timeout=timeout,
    )

    token = _current_context.set(ctx)
    try:
        yield ctx
        if ctx.root_span:
            ctx.end_span(ctx.root_span, status="success")
    except Exception as e:
        if ctx.root_span:
            ctx.end_span(ctx.root_span, status="error", error=str(e))
        raise
    finally:
        _current_context.reset(token)


def correlate(
    operation: Optional[str] = None,
    source_component: Optional[str] = None,
):
    """
    Decorator for adding correlation context to a function.

    Usage:
        @correlate(operation="process_request")
        async def my_handler(request):
            # Function has correlation context
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        op_name = operation or func.__name__
        component = source_component or func.__module__

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            with with_correlation(op_name, component):
                return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            with with_correlation(op_name, component):
                return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


class CorrelatedLogger(logging.LoggerAdapter):
    """
    Logger adapter that automatically includes correlation ID.

    Usage:
        logger = CorrelatedLogger(logging.getLogger(__name__))
        logger.info("Processing request")
        # Output: [correlation_id=xxx] Processing request
    """

    def process(self, msg: str, kwargs: Dict[str, Any]) -> tuple:
        ctx = _current_context.get()
        if ctx:
            prefix = f"[{ctx.correlation_id[:16]}]"
            if ctx.source_component:
                prefix = f"[{ctx.correlation_id[:16]}:{ctx.source_component}]"
            return f"{prefix} {msg}", kwargs
        return msg, kwargs


def get_correlated_logger(name: str) -> CorrelatedLogger:
    """Get a logger that includes correlation IDs."""
    return CorrelatedLogger(logging.getLogger(name), {})


# ============================================================================
# File-Based RPC Integration
# ============================================================================


def inject_correlation(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inject correlation context into RPC request data.

    Use this when writing file-based RPC requests.
    The envelope is embedded inside _correlation via to_dict().
    """
    ctx = _current_context.get()
    if ctx:
        # to_dict() includes _trace_envelope when envelope is present
        data["_correlation"] = ctx.to_dict()
    return data


def extract_correlation(data: Dict[str, Any]) -> Optional[CorrelationContext]:
    """
    Extract correlation context from RPC request data.

    Use this when reading file-based RPC requests.
    The envelope is restored inside from_dict() from _trace_envelope.
    """
    if "_correlation" not in data:
        return None
    return CorrelationContext.from_dict(data["_correlation"])


def apply_correlation(data: Dict[str, Any]) -> contextvars.Token:
    """
    Extract and apply correlation from RPC data.

    Returns a token for resetting context later.
    """
    ctx = extract_correlation(data)
    if ctx:
        # Create child context for this repo's processing
        child = CorrelationContext.create(
            source_repo="jarvis",
            parent=ctx,
        )
        return _current_context.set(child)
    return _current_context.set(None)


# ============================================================================
# Trace Export
# ============================================================================


def export_trace_json(ctx: CorrelationContext) -> str:
    """Export trace as JSON for debugging."""
    return json.dumps(
        {
            "correlation_id": ctx.correlation_id,
            "parent_id": ctx.parent_id,
            "source_repo": ctx.source_repo,
            "source_component": ctx.source_component,
            "duration_ms": ctx.root_span.duration_ms if ctx.root_span else None,
            "trace": ctx.get_trace(),
            "baggage": ctx.baggage,
        },
        indent=2,
    )


def print_trace_summary(ctx: CorrelationContext) -> None:
    """Print a human-readable trace summary."""

    def print_span(span: SpanInfo, indent: int = 0) -> None:
        prefix = "  " * indent
        duration = f"{span.duration_ms:.1f}ms" if span.duration_ms else "running"
        status_icon = "✓" if span.status == "success" else "✗" if span.status == "error" else "⋯"
        print(f"{prefix}{status_icon} {span.operation} [{duration}]")
        if span.error_message:
            print(f"{prefix}  Error: {span.error_message}")
        for child in span.children:
            print_span(child, indent + 1)

    print(f"\nTrace: {ctx.correlation_id}")
    print(f"Source: {ctx.source_repo}/{ctx.source_component or 'unknown'}")
    if ctx.root_span:
        print_span(ctx.root_span)
    print()

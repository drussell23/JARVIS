"""
Distributed Tracing v1.0 — Trace ID propagation for Trinity IPC.

Adds trace/span IDs to cross-repo messages for log correlation.
Lightweight implementation — no external tracing backends required,
just structured log fields.

Usage:
    from backend.core.tracing import Tracer, get_tracer

    tracer = get_tracer()

    # Start a new trace (e.g., incoming user command)
    with tracer.start_trace("voice-command") as ctx:
        logger.info("Processing command", extra=ctx.log_extra())

        # Create child span for sub-operation
        with tracer.start_span("speaker-verify", parent=ctx) as child:
            logger.info("Verifying speaker", extra=child.log_extra())
            result = await verify_speaker(audio)

    # Attach to IPC messages
    msg = {"type": "command", "data": {...}}
    ctx.stamp_message(msg)

    # Receive IPC message and continue trace
    incoming_ctx = tracer.from_message(msg)
    with tracer.start_span("process-remote", parent=incoming_ctx) as span:
        ...
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvis.tracing")

# Context variable for implicit trace propagation
_current_context: ContextVar[Optional["TraceContext"]] = ContextVar(
    "trace_context", default=None
)


@dataclass
class TraceContext:
    """
    Immutable trace context carrying IDs through the call chain.
    """
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    operation: str = ""
    start_time: float = field(default_factory=time.time)
    attributes: Dict[str, Any] = field(default_factory=dict)
    _children: List[str] = field(default_factory=list, repr=False)

    def log_extra(self) -> Dict[str, str]:
        """Return dict suitable for logger `extra` kwarg."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id or "",
            "operation": self.operation,
        }

    def stamp_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Add tracing fields to an IPC message dict."""
        msg["_trace_id"] = self.trace_id
        msg["_span_id"] = self.span_id
        msg["_parent_span_id"] = self.parent_span_id or ""
        msg["_trace_op"] = self.operation
        return msg

    def elapsed_ms(self) -> float:
        return (time.time() - self.start_time) * 1000

    def __enter__(self) -> "TraceContext":
        self._token = _current_context.set(self)
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = self.elapsed_ms()
        _current_context.reset(self._token)
        logger.debug(
            f"[Trace] span={self.operation} elapsed={elapsed:.1f}ms "
            f"trace={self.trace_id[:8]} span={self.span_id[:8]}"
        )


class Tracer:
    """
    Lightweight distributed tracer.

    Creates trace/span contexts and propagates them via IPC messages.
    """

    _instance: Optional["Tracer"] = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        self._service_name = os.getenv("JARVIS_SERVICE_NAME", "jarvis-body")
        self._active_traces: int = 0
        self._total_traces: int = 0
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "Tracer":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def start_trace(
        self,
        operation: str,
        *,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> TraceContext:
        """
        Start a new trace (top-level operation).

        Returns a TraceContext that can be used as a context manager.
        """
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        ctx = TraceContext(
            trace_id=trace_id,
            span_id=span_id,
            operation=operation,
            attributes=attributes or {},
        )
        with self._lock:
            self._active_traces += 1
            self._total_traces += 1
        return ctx

    def start_span(
        self,
        operation: str,
        *,
        parent: Optional[TraceContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> TraceContext:
        """
        Start a child span within an existing trace.

        If parent is None, uses the current context variable.
        """
        parent = parent or _current_context.get()
        if parent is None:
            # No parent — start a new trace
            return self.start_trace(operation, attributes=attributes)

        span_id = uuid.uuid4().hex[:16]
        ctx = TraceContext(
            trace_id=parent.trace_id,
            span_id=span_id,
            parent_span_id=parent.span_id,
            operation=operation,
            attributes=attributes or {},
        )
        parent._children.append(span_id)
        return ctx

    def from_message(self, msg: Dict[str, Any]) -> Optional[TraceContext]:
        """
        Extract trace context from an incoming IPC message.

        Returns None if no trace info present.
        """
        trace_id = msg.get("_trace_id")
        if not trace_id:
            return None

        return TraceContext(
            trace_id=trace_id,
            span_id=msg.get("_span_id", uuid.uuid4().hex[:16]),
            parent_span_id=msg.get("_parent_span_id") or None,
            operation=msg.get("_trace_op", "remote"),
        )

    @staticmethod
    def current() -> Optional[TraceContext]:
        """Get the current trace context (from context variable)."""
        return _current_context.get()

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "service": self._service_name,
                "active_traces": self._active_traces,
                "total_traces": self._total_traces,
            }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def get_tracer() -> Tracer:
    """Get the singleton tracer instance."""
    return Tracer.get_instance()


def current_trace() -> Optional[TraceContext]:
    """Get the current trace context."""
    return _current_context.get()


def trace_id() -> str:
    """Get the current trace ID, or empty string if no trace active."""
    ctx = _current_context.get()
    return ctx.trace_id if ctx else ""

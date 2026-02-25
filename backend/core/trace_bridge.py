"""Bridge between tracing.py and CorrelationContext.

Synchronizes the two parallel tracing systems so that spans created
in either system are visible to the other.

The codebase has TWO context-variable-based tracing systems:
    1. ``backend.core.tracing`` -- OpenTelemetry-like Tracer with ``_current_span`` ContextVar
    2. ``backend.core.resilience.correlation_context`` -- CorrelationContext with
       ``_current_context`` ContextVar

This module provides:
    - ``unified_trace`` -- context manager that activates both systems simultaneously
    - ``sync_to_tracer`` -- push CorrelationContext state into tracing.py
    - ``sync_from_tracer`` -- pull tracing.py state into a CorrelationContext

Usage::

    from backend.core.trace_bridge import unified_trace

    with unified_trace("operation-name", component="my-component"):
        # Both tracing.py and CorrelationContext have active spans
        pass

Author: JARVIS Production Trace Integration
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import Token
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded imports -- either system may be unavailable
# ---------------------------------------------------------------------------

try:
    from backend.core.resilience.correlation_context import (
        CorrelationContext,
        get_current_context,
        # Private access required: set_current_context() does not return a
        # Token for later reset.  We need the raw ContextVar to call
        # .set()/.reset() for proper nesting in unified_trace().
        _current_context,
    )

    _CORRELATION_AVAILABLE = True
except ImportError:
    _CORRELATION_AVAILABLE = False

try:
    from backend.core.tracing import get_tracer, Span as _TracerSpan
    _TRACER_AVAILABLE = True
except ImportError:
    _TRACER_AVAILABLE = False


# ---------------------------------------------------------------------------
# One-off synchronization helpers
# ---------------------------------------------------------------------------


def sync_to_tracer(ctx: Any) -> Optional[Any]:
    """Push CorrelationContext state into tracing.py's Tracer.

    Creates a tracer span that mirrors the correlation context's current
    operation name.  The caller is responsible for entering/exiting the
    returned span (it is **not** entered automatically).

    Returns the tracer Span, or ``None`` if tracing.py is unavailable or
    there is no operation to mirror.
    """
    if not _TRACER_AVAILABLE:
        return None
    try:
        tracer = get_tracer()
        operation = ""
        if hasattr(ctx, "current_span") and ctx.current_span:
            operation = ctx.current_span.operation
        elif hasattr(ctx, "root_span") and ctx.root_span:
            operation = ctx.root_span.operation
        if operation:
            return tracer.start_span(operation)
        return None
    except Exception:
        logger.debug("Failed to sync correlation to tracer", exc_info=True)
        return None


def sync_from_tracer() -> Optional[Any]:
    """Pull tracing.py state into a new CorrelationContext.

    Creates a :class:`CorrelationContext` whose root operation mirrors the
    tracer's current span name.  The new context is **not** activated on the
    ``_current_context`` ContextVar -- the caller decides what to do with it.

    Returns the context, or ``None`` if either system is unavailable or
    there is no active tracer span.
    """
    if not _TRACER_AVAILABLE or not _CORRELATION_AVAILABLE:
        return None
    try:
        tracer = get_tracer()
        current = tracer.get_current_span()
        if current is None:
            return None
        op_name = getattr(current.context, "operation_name", "unknown")
        ctx = CorrelationContext.create(
            operation=op_name,
            source_component="tracer_bridge",
        )
        return ctx
    except Exception:
        logger.debug("Failed to sync tracer to correlation", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# unified_trace -- the main bridging context manager
# ---------------------------------------------------------------------------


@contextmanager
def unified_trace(
    operation: str,
    component: str = "",
    *,
    timeout: Optional[float] = None,
):
    """Context manager that activates both tracing systems simultaneously.

    On entry:
        1. Creates a :class:`CorrelationContext` (child of any active context)
           and pushes it onto ``_current_context``.
        2. Creates a tracing.py ``Span`` and enters it (pushes onto
           ``_current_span``).

    On exit (normal or exception):
        1. Ends the CorrelationContext root span (``success`` or ``error``).
        2. Exits the tracing.py span (which records status automatically).
        3. Restores the previous ``_current_context`` via its ContextVar token.

    Yields the :class:`CorrelationContext` (or ``None`` if correlation_context
    is unavailable).

    Parameters
    ----------
    operation:
        Name of the logical operation.
    component:
        Source component tag stored on the CorrelationContext.
    timeout:
        Optional deadline (seconds) forwarded to
        :meth:`CorrelationContext.create`.
    """
    correlation_token: Optional[Token] = None
    tracer_span: Optional[Any] = None
    ctx: Optional[Any] = None

    try:
        # --- Correlation Context -----------------------------------------
        if _CORRELATION_AVAILABLE:
            parent = get_current_context()
            ctx = CorrelationContext.create(
                operation=operation,
                source_component=component or None,
                parent=parent,
                timeout=timeout,
            )
            correlation_token = _current_context.set(ctx)

        # --- Tracer Span -------------------------------------------------
        if _TRACER_AVAILABLE:
            try:
                tracer = get_tracer()
                tracer_span = tracer.start_span(operation)
                # Enter the span so it becomes the active _current_span
                tracer_span.__enter__()
            except Exception:
                logger.debug("Failed to start tracer span", exc_info=True)
                tracer_span = None

        yield ctx

        # --- Normal exit: end spans as success ---------------------------
        if ctx is not None and ctx.root_span:
            ctx.end_span(ctx.root_span, status="success")

        if tracer_span is not None:
            tracer_span.__exit__(None, None, None)
            tracer_span = None  # prevent double-exit in finally

    except BaseException as exc:
        # --- Error exit: end spans as error ------------------------------
        if ctx is not None and ctx.root_span:
            try:
                ctx.end_span(ctx.root_span, status="error", error=str(exc))
            except Exception:
                pass  # never mask the original error

        if tracer_span is not None:
            try:
                tracer_span.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            tracer_span = None

        raise

    finally:
        # --- Restore previous correlation context ------------------------
        if correlation_token is not None:
            try:
                _current_context.reset(correlation_token)
            except ValueError:
                pass  # token already reset (shouldn't happen, but be safe)

        # Safety net: if tracer_span wasn't exited yet (e.g. error in
        # ctx.end_span path), exit it now.
        if tracer_span is not None:
            try:
                tracer_span.__exit__(None, None, None)
            except Exception:
                pass

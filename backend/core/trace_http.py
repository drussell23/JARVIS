"""HTTP Trace Header Utilities.

Provides helpers for injecting/extracting trace context from HTTP headers.
Used by PrimeClient and any other HTTP boundary.

Usage:
    from backend.core.trace_http import get_trace_headers, merge_trace_headers

    # Get trace headers for current context
    headers = get_trace_headers()

    # Or merge with existing headers
    request_headers = merge_trace_headers({"Content-Type": "application/json"})
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from backend.core.resilience.correlation_context import (
        get_current_context,
        CorrelationContext,
    )
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    get_current_context = None  # type: ignore[assignment]
    CorrelationContext = None  # type: ignore[assignment,misc]


def get_trace_headers() -> Dict[str, str]:
    """Get trace headers from the current CorrelationContext.

    Returns an empty dict if no context is active or modules unavailable.
    """
    if not _AVAILABLE or get_current_context is None:
        return {}
    try:
        ctx = get_current_context()
        if ctx is None:
            return {}
        return ctx.to_headers()
    except Exception:
        logger.debug("Failed to get trace headers", exc_info=True)
        return {}


def merge_trace_headers(existing: Dict[str, str]) -> Dict[str, str]:
    """Merge trace headers into an existing headers dict.

    Trace headers are added without overwriting existing keys.
    Returns a new dict (does not mutate the input).
    """
    result = dict(existing)
    trace_headers = get_trace_headers()
    for key, value in trace_headers.items():
        if key not in result:
            result[key] = value
    return result


def extract_trace_from_response(
    response_headers: Dict[str, str],
) -> Optional[Any]:
    """Extract CorrelationContext from response headers.

    Useful for correlating server responses back to the original request.
    """
    if not _AVAILABLE or CorrelationContext is None:
        return None
    try:
        return CorrelationContext.from_headers(response_headers)
    except Exception:
        logger.debug("Failed to extract trace from response", exc_info=True)
        return None

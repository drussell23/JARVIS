"""GCP VM Trace Metadata Utilities.

Generates trace-specific metadata items for GCP VM instance creation.
The VM's startup script reads these to initialize its own traceability
with the parent trace context.

Usage in gcp_vm_manager.py:
    from backend.core.trace_vm import get_trace_metadata_items
    metadata_items.extend(get_trace_metadata_items())
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

try:
    from backend.core.resilience.correlation_context import get_current_context
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    get_current_context = None  # type: ignore[assignment]


def get_trace_metadata_items() -> List[Dict[str, str]]:
    """Generate GCP metadata items from current trace context.

    Returns a list of {"key": ..., "value": ...} dicts compatible with
    compute_v1.Items construction. Returns empty list if no context.
    """
    if not _AVAILABLE or get_current_context is None:
        return []

    try:
        ctx = get_current_context()
        if ctx is None:
            return []

        items = [
            {"key": "jarvis-correlation-id", "value": ctx.correlation_id},
            {"key": "jarvis-source-repo", "value": ctx.source_repo},
        ]

        if ctx.source_component:
            items.append(
                {"key": "jarvis-source-component", "value": ctx.source_component}
            )

        if ctx.parent_id:
            items.append(
                {"key": "jarvis-parent-correlation-id", "value": ctx.parent_id}
            )

        # Add envelope-level trace IDs
        envelope = getattr(ctx, "envelope", None)
        if envelope is not None:
            items.append({"key": "jarvis-trace-id", "value": envelope.trace_id})
            items.append({"key": "jarvis-parent-span-id", "value": envelope.span_id})
            if hasattr(envelope, "event_id"):
                items.append({"key": "jarvis-parent-event-id", "value": envelope.event_id})

        return items

    except Exception:
        logger.debug("Failed to generate trace metadata items", exc_info=True)
        return []


def get_trace_env_vars() -> Dict[str, str]:
    """Generate environment variables for trace context propagation.

    Used to inject trace context into startup scripts via env vars
    that the child process can read.
    """
    if not _AVAILABLE or get_current_context is None:
        return {}

    try:
        ctx = get_current_context()
        if ctx is None:
            return {}

        env_vars = {
            "JARVIS_PARENT_CORRELATION_ID": ctx.correlation_id,
            "JARVIS_PARENT_SOURCE_REPO": ctx.source_repo,
        }

        if ctx.source_component:
            env_vars["JARVIS_PARENT_SOURCE_COMPONENT"] = ctx.source_component

        envelope = getattr(ctx, "envelope", None)
        if envelope is not None:
            env_vars["JARVIS_PARENT_TRACE_ID"] = envelope.trace_id
            env_vars["JARVIS_PARENT_SPAN_ID"] = envelope.span_id

        return env_vars

    except Exception:
        logger.debug("Failed to generate trace env vars", exc_info=True)
        return {}

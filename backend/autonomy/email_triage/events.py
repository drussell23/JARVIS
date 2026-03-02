"""Structured observability events for the email triage system.

All events are emitted as JSON to the ``jarvis.email_triage`` logger.
7 event types cover the full triage lifecycle.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

logger = logging.getLogger("jarvis.email_triage")

# Event type constants
EVENT_CYCLE_STARTED = "triage_cycle_started"
EVENT_EMAIL_TRIAGED = "email_triaged"
EVENT_NOTIFICATION_SENT = "notification_sent"
EVENT_NOTIFICATION_SUPPRESSED = "notification_suppressed"
EVENT_SUMMARY_FLUSHED = "summary_flushed"
EVENT_CYCLE_COMPLETED = "triage_cycle_completed"
EVENT_TRIAGE_ERROR = "triage_error"

_ERROR_EVENTS = frozenset({EVENT_TRIAGE_ERROR})


def emit_triage_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Emit a structured triage event to the logger.

    Args:
        event_type: One of the EVENT_* constants.
        payload: Event-specific data (must be JSON-serializable).
    """
    event = {
        "event": event_type,
        "timestamp": time.time(),
        **payload,
    }
    msg = json.dumps(event, default=str)
    if event_type in _ERROR_EVENTS:
        logger.warning(msg)
    else:
        logger.info(msg)

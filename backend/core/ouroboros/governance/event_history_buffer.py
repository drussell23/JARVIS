"""Event History Buffer — the review half of "everything connected".

The unified breadcrumb router (serpent_flow ``_event_breadcrumb_router``) surfaces
backend events LIVE, but they scroll away. This is the bounded in-memory ring the
router also appends EVERY event to (regardless of the live verbosity filter), so
``/events`` can tail/review what flew by — filtered by severity or category,
rendered through the SAME ``event_breadcrumb_registry`` (no second formatter).

A process-local singleton (``get_default_history``) shared by the router (writer)
and the ``/events`` verb (reader) — same process, no store, no network. Bounded
deque so memory is capped. Never raises.
"""

from __future__ import annotations

import collections
import os
import threading
import time
from dataclasses import dataclass
from typing import Deque, List, Optional

_CAP_ENV = "JARVIS_TUI_EVENT_HISTORY_SIZE"
_DEFAULT_CAP = 500


def _cap() -> int:
    try:
        return max(16, int(os.environ.get(_CAP_ENV, str(_DEFAULT_CAP))))
    except (TypeError, ValueError):
        return _DEFAULT_CAP


@dataclass(frozen=True)
class EventRecord:
    ts: float
    event_type: str
    payload: dict
    severity: int
    category: str


class EventHistoryBuffer:
    """Bounded, thread-safe ring of recent events."""

    def __init__(self, capacity: Optional[int] = None) -> None:
        self._buf: Deque[EventRecord] = collections.deque(maxlen=capacity or _cap())
        self._lock = threading.Lock()

    def append(
        self, event_type: str, payload: Optional[dict], *,
        severity: int = 1, category: str = "general", ts: Optional[float] = None,
    ) -> None:
        if not event_type:
            return
        try:
            rec = EventRecord(
                ts=float(ts) if ts is not None else time.time(),
                event_type=event_type,
                payload=dict(payload or {}),
                severity=int(severity),
                category=category or "general",
            )
            with self._lock:
                self._buf.append(rec)
        except Exception:  # noqa: BLE001 — history append never perturbs the router
            pass

    def recent(
        self, n: int = 20, *, min_severity: Optional[int] = None,
        category: Optional[str] = None, event_type: Optional[str] = None,
    ) -> List[EventRecord]:
        """Newest-first, filtered. Never raises."""
        try:
            with self._lock:
                items = list(self._buf)
        except Exception:  # noqa: BLE001
            return []
        out: List[EventRecord] = []
        for rec in reversed(items):
            if min_severity is not None and rec.severity < min_severity:
                continue
            if category is not None and rec.category != category:
                continue
            if event_type is not None and rec.event_type != event_type:
                continue
            out.append(rec)
            if len(out) >= max(1, n):
                break
        return out

    def size(self) -> int:
        with self._lock:
            return len(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_default: Optional[EventHistoryBuffer] = None
_default_lock = threading.Lock()


def get_default_history() -> EventHistoryBuffer:
    """Process-local singleton shared by the router (writer) + /events (reader)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = EventHistoryBuffer()
    return _default


__all__ = [
    "EventHistoryBuffer",
    "EventRecord",
    "get_default_history",
]

"""Event-sourced startup telemetry for Disease 10 wiring.

Single event stream consumed by structured logger, metrics collector,
TUI bridge, and FSM journal.  Every Disease 10 component emits
``StartupEvent`` instances through a ``StartupEventBus``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "StartupEvent",
    "EventConsumer",
    "StartupEventBus",
    "StructuredLogger",
    "MetricsCollector",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core event type
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class StartupEvent:
    """Immutable record of a single startup-lifecycle event."""

    trace_id: str
    event_type: str
    timestamp: float
    wall_clock: str
    authority_state: str
    phase: Optional[str]
    detail: Dict[str, Any]


# ---------------------------------------------------------------------------
# Consumer ABC
# ---------------------------------------------------------------------------

class EventConsumer(ABC):
    """Base class for anything that reacts to startup events."""

    @abstractmethod
    async def consume(self, event: StartupEvent) -> None:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

class StartupEventBus:
    """Fan-out broadcaster for :class:`StartupEvent` instances.

    Parameters
    ----------
    trace_id:
        Boot-level UUID stamped onto every event created by this bus.
    """

    def __init__(self, trace_id: str) -> None:
        self._trace_id = trace_id
        self._consumers: List[EventConsumer] = []
        self._history: List[StartupEvent] = []

    # -- subscription -------------------------------------------------------

    def subscribe(self, consumer: EventConsumer) -> None:
        """Register *consumer* to receive future events."""
        self._consumers.append(consumer)

    # -- emission -----------------------------------------------------------

    async def emit(self, event: StartupEvent) -> None:
        """Broadcast *event* to every subscribed consumer.

        Per-consumer errors are logged but never propagate; one broken
        consumer must not block the others.
        """
        self._history.append(event)
        for consumer in self._consumers:
            try:
                await consumer.consume(event)
            except Exception:
                logger.exception(
                    "Consumer %r failed on event %s",
                    consumer,
                    event.event_type,
                )

    # -- factory ------------------------------------------------------------

    def create_event(
        self,
        event_type: str,
        detail: Dict[str, Any],
        phase: Optional[str] = None,
        authority_state: str = "",
    ) -> StartupEvent:
        """Build a :class:`StartupEvent` pre-stamped with this bus's trace id."""
        return StartupEvent(
            trace_id=self._trace_id,
            event_type=event_type,
            timestamp=time.monotonic(),
            wall_clock=datetime.now(timezone.utc).isoformat(),
            authority_state=authority_state,
            phase=phase,
            detail=detail,
        )

    # -- history ------------------------------------------------------------

    @property
    def event_history(self) -> List[StartupEvent]:
        """Return a shallow copy of the internal event history."""
        return list(self._history)


# ---------------------------------------------------------------------------
# Built-in consumers
# ---------------------------------------------------------------------------

class StructuredLogger(EventConsumer):
    """Appends one JSON-lines record per event to *log_path*."""

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path

    async def consume(self, event: StartupEvent) -> None:
        line = json.dumps(dataclasses.asdict(event), default=str)
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class MetricsCollector(EventConsumer):
    """Lightweight in-memory aggregator of event counts and durations."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = defaultdict(int)
        self.phase_durations: Dict[str, float] = {}
        self.budget_wait_times: List[float] = []

    async def consume(self, event: StartupEvent) -> None:
        self.counts[event.event_type] += 1

        if event.event_type == "phase_gate" and "duration_s" in event.detail:
            if event.phase is not None:
                self.phase_durations[event.phase] = event.detail["duration_s"]

    def snapshot(self) -> dict:
        """Return a point-in-time copy of collected metrics."""
        return {
            "counts": dict(self.counts),
            "phase_durations": dict(self.phase_durations),
            "budget_wait_times": list(self.budget_wait_times),
        }

"""Faults panel -- active and resolved faults data layer."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List

from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class FaultEntry:
    """Single fault event with optional resolution metadata."""

    event_id: str
    fault_class: str
    component: str
    message: str
    recovery_policy: str
    terminal: bool
    timestamp: float
    resolved: bool = False
    resolution: str = ""
    duration_ms: float = 0.0


class FaultsData:
    """Pure data layer for the faults panel.

    Consumes ``fault.raised@*`` and ``fault.resolved@*`` envelopes.
    Active faults move to the resolved deque when a matching
    ``fault.resolved`` envelope arrives (matched by ``event_id``).
    """

    def __init__(self) -> None:
        self.active_faults: List[FaultEntry] = []
        self.resolved_faults: Deque[FaultEntry] = deque(maxlen=20)

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Ingest one telemetry envelope; ignore unrelated schemas."""
        p = envelope.payload
        if envelope.event_schema.startswith("fault.raised"):
            self.active_faults.append(FaultEntry(
                event_id=envelope.event_id,
                fault_class=p.get("fault_class", ""),
                component=p.get("component", ""),
                message=p.get("message", ""),
                recovery_policy=p.get("recovery_policy", ""),
                terminal=p.get("terminal", False),
                timestamp=envelope.emitted_at,
            ))
        elif envelope.event_schema.startswith("fault.resolved"):
            fault_id = p.get("fault_id", "")
            for i, f in enumerate(self.active_faults):
                if f.event_id == fault_id:
                    f.resolved = True
                    f.resolution = p.get("resolution", "")
                    f.duration_ms = p.get("duration_ms", 0.0)
                    self.resolved_faults.append(f)
                    self.active_faults.pop(i)
                    break

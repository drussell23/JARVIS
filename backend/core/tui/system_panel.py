"""System panel -- lifecycle, gate, bus stats data layer."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict

from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class TransitionEntry:
    """Single lifecycle or gate state transition."""

    timestamp: float
    domain: str
    from_state: str
    to_state: str
    trigger: str


class SystemData:
    """Pure data layer for the system panel.

    Consumes ``lifecycle.transition@*`` and ``reasoning.activation@*``
    envelopes to track lifecycle state, gate state, and a bounded log
    of recent transitions.
    """

    def __init__(self) -> None:
        self.lifecycle_state: str = "UNKNOWN"
        self.lifecycle_restarts: int = 0
        self.gate_state: str = "DISABLED"
        self.gate_sequence: int = 0
        self.gate_deps: Dict[str, str] = {}
        self.recent_transitions: Deque[TransitionEntry] = deque(maxlen=20)

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Ingest one telemetry envelope; ignore unrelated schemas."""
        p = envelope.payload
        if envelope.event_schema.startswith("lifecycle.transition"):
            self.lifecycle_state = p.get("to_state", self.lifecycle_state)
            self.lifecycle_restarts = p.get(
                "restarts_in_window", self.lifecycle_restarts
            )
            self.recent_transitions.append(TransitionEntry(
                timestamp=envelope.emitted_at,
                domain="lifecycle",
                from_state=p.get("from_state", "?"),
                to_state=p.get("to_state", "?"),
                trigger=p.get("trigger", "?"),
            ))
        elif envelope.event_schema.startswith("reasoning.activation"):
            self.gate_state = p.get("to_state", self.gate_state)
            self.gate_sequence = p.get("gate_sequence", self.gate_sequence)
            self.gate_deps = p.get("critical_deps", self.gate_deps)
            self.recent_transitions.append(TransitionEntry(
                timestamp=envelope.emitted_at,
                domain="gate",
                from_state=p.get("from_state", "?"),
                to_state=p.get("to_state", "?"),
                trigger=p.get("trigger", "?"),
            ))

"""Pipeline panel -- command trace log data layer."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List

from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class CommandTrace:
    """Single command flowing through the reasoning pipeline."""

    trace_id: str
    command: str
    is_proactive: bool
    confidence: float
    signals: List[str]
    phase: str
    expanded_intents: List[str]
    mind_requests: int
    delegations: int
    total_ms: float
    success_rate: float
    timestamp: float = 0.0


class PipelineData:
    """Pure data layer for the pipeline panel.

    Consumes ``reasoning.decision@*`` envelopes and maintains a bounded
    deque of :class:`CommandTrace` entries.
    """

    def __init__(self, max_commands: int = 50) -> None:
        self.commands: Deque[CommandTrace] = deque(maxlen=max_commands)
        self.total_commands: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Ingest one telemetry envelope; ignore non-decision schemas."""
        if not envelope.event_schema.startswith("reasoning.decision"):
            return
        p = envelope.payload
        self.commands.append(CommandTrace(
            trace_id=envelope.trace_id,
            command=p.get("command", ""),
            is_proactive=p.get("is_proactive", False),
            confidence=p.get("confidence", 0.0),
            signals=p.get("signals", []),
            phase=p.get("phase", ""),
            expanded_intents=p.get("expanded_intents", []),
            mind_requests=p.get("mind_requests", 0),
            delegations=p.get("delegations", 0),
            total_ms=p.get("total_ms", 0.0),
            success_rate=p.get("success_rate", 0.0),
            timestamp=envelope.emitted_at,
        ))
        self.total_commands += 1

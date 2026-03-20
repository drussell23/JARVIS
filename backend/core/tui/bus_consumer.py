"""Routes TelemetryEnvelopes to dashboard panels by event schema domain."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from backend.core.telemetry_contract import TelemetryEnvelope
from backend.core.tui.pipeline_panel import PipelineData
from backend.core.tui.agents_panel import AgentsData
from backend.core.tui.system_panel import SystemData
from backend.core.tui.faults_panel import FaultsData


@dataclass
class StatusBarData:
    """One-line summary of system state for the TUI status bar.

    Updated by every envelope that passes through the bus consumer.
    Lightweight -- no bounded collections, just scalar counters and strings.
    """

    lifecycle_state: str = "UNKNOWN"
    gate_state: str = "DISABLED"
    agent_count: str = "?/?"
    fault_count: int = 0
    command_count: int = 0
    bus_emitted: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Extract headline stats from *envelope*."""
        p = envelope.payload
        if envelope.event_schema.startswith("lifecycle.transition"):
            self.lifecycle_state = p.get("to_state", self.lifecycle_state)
        elif envelope.event_schema.startswith("reasoning.activation"):
            self.gate_state = p.get("to_state", self.gate_state)
        elif envelope.event_schema.startswith("scheduler.graph_state"):
            self.agent_count = f"{p.get('initialized', 0)}/{p.get('total_agents', 0)}"
        elif envelope.event_schema.startswith("reasoning.decision"):
            self.command_count += 1
        elif envelope.event_schema.startswith("fault.raised"):
            self.fault_count += 1
        elif envelope.event_schema.startswith("fault.resolved"):
            self.fault_count = max(0, self.fault_count - 1)
        self.bus_emitted += 1

    def to_string(self) -> str:
        """Render a single-line summary suitable for the TUI footer."""
        return (
            f"J-Prime:{self.lifecycle_state} | Gate:{self.gate_state} | "
            f"Agents:{self.agent_count} | Faults:{self.fault_count} | "
            f"Cmds:{self.command_count} | Bus:{self.bus_emitted}"
        )


class TelemetryBusConsumer:
    """Fan-out router: dispatches each envelope to the appropriate panel(s).

    Domain prefixes are extracted from the ``event_schema`` field
    (everything before the first ``'.'``).  A single envelope may reach
    multiple panels when its domain maps to more than one target
    (e.g. ``reasoning`` feeds both the pipeline *and* system panels).

    The ``StatusBarData`` instance is always updated for every envelope,
    regardless of domain.
    """

    def __init__(
        self,
        pipeline: PipelineData,
        agents: AgentsData,
        system: SystemData,
        faults: FaultsData,
        status: StatusBarData,
    ) -> None:
        self._pipeline = pipeline
        self._agents = agents
        self._system = system
        self._faults = faults
        self._status = status
        self._routing: Dict[str, List[Any]] = {
            "reasoning": [self._pipeline, self._system],
            "lifecycle": [self._system],
            "scheduler": [self._agents],
            "fault": [self._faults],
            "recovery": [self._faults],
        }

    def handle_sync(self, envelope: TelemetryEnvelope) -> None:
        """Synchronous dispatch -- updates status bar and all matching panels."""
        self._status.update(envelope)
        domain = envelope.event_schema.split(".")[0]
        for panel in self._routing.get(domain, []):
            panel.update(envelope)

    async def handle(self, envelope: TelemetryEnvelope) -> None:
        """Async-compatible dispatch (delegates to :meth:`handle_sync`)."""
        self.handle_sync(envelope)

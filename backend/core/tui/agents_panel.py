"""Agents panel -- agent inventory data layer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from backend.core.telemetry_contract import TelemetryEnvelope


@dataclass
class AgentEntry:
    """State snapshot for a single agent."""

    name: str
    state: str = "unknown"
    tasks_completed: int = 0
    errors: int = 0


class AgentsData:
    """Pure data layer for the agents panel.

    Consumes ``scheduler.graph_state@*`` and ``scheduler.unit_state@*``
    envelopes to maintain a live agent inventory.
    """

    def __init__(self) -> None:
        self.agents: Dict[str, AgentEntry] = {}
        self.total_agents: int = 0
        self.initialized: int = 0
        self.failed: int = 0

    def update(self, envelope: TelemetryEnvelope) -> None:
        """Ingest one telemetry envelope; ignore non-scheduler schemas."""
        p = envelope.payload
        if envelope.event_schema.startswith("scheduler.graph_state"):
            self.total_agents = p.get("total_agents", 0)
            self.initialized = p.get("initialized", 0)
            self.failed = p.get("failed", 0)
            for name in p.get("agent_names", []):
                if name not in self.agents:
                    self.agents[name] = AgentEntry(name=name, state="idle")
        elif envelope.event_schema.startswith("scheduler.unit_state"):
            name = p.get("agent_name", "")
            if name in self.agents:
                self.agents[name].state = p.get("state", "unknown")
                self.agents[name].tasks_completed = p.get(
                    "tasks_completed", self.agents[name].tasks_completed
                )

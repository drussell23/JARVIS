"""The Universal Semantic Envelope for the ov hive feed (Phase 12, Hive Step 1).

Every agent/subsystem across the six fragmented message fabrics (TrinityEventBus,
the IDE SSE observability broker, CommProtocol, TelemetryBus, AgentCommunicationBus,
legacy core.event_bus) maps its native activity into ONE polymorphic Pydantic type
so the ov hive TUI renders any actor predictably — a real Gmail send, a governance
gate, an ephemeral agent's birth/death — with the same shape.

This module is PURE DATA + read-only adapters. It never publishes, never mutates a
source bus (mandate 1). ``from_trinity_event`` / ``from_ide_sse_event`` are the two
Step-1 adapters; more fabrics plug in later behind the same envelope.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Literal, Mapping, Optional

from pydantic import BaseModel, Field

HIVE_ENV_SCHEMA = "hive.env.1"

#: Topic-prefix → (subsystem, actor family) for TrinityEventBus events.
_TRINITY_PREFIX_MAP = {
    "training.": ("training", "training.pipeline"),
    "tier.": ("routing", "tier.router"),
    "autonomy.": ("swarm", "autonomy"),
    "workflow.": ("mesh", "mesh.orchestrator"),
    "gap.": ("sensor", "sensor.capability_gap"),
    "fs.": ("sensor", "fs.watcher"),
    "command.": ("governance", "command"),
    "ouroboros.": ("governance", "ouroboros"),
    "intake.": ("sensor", "intake.router"),
    "reactor.": ("training", "reactor_core"),
}

#: IDE-SSE event_type prefix → subsystem for the observability broker.
_SSE_SUBSYSTEM_HINTS = (
    ("swarm_", "swarm"),
    ("task_", "governance"),
    ("gate", "governance"),
    ("tool_", "governance"),
    ("fsm", "governance"),
    ("operation", "governance"),
    ("posture", "governance"),
    ("circuit", "routing"),
    ("provider_", "routing"),
    ("drift", "sensor"),
    ("curiosity", "consciousness"),
)


class HiveTelemetryEnvelope(BaseModel):
    """Universal semantic envelope — the lingua franca of the ov hive feed.

    Polymorphic via ``kind``; subclasses inherit these strict base fields and add
    their own typed detail. Extra source-specific fields ride in ``detail``.
    """

    # --- strict base identity (required of EVERY actor) ---
    actor_id: str                       # "vision.screen" / "mcp.gmail" / "ouroboros.gate"
    subsystem: str                      # perception|actuation|mcp|governance|sensor|swarm|routing|training|mesh|consciousness|persona
    intent: str                         # WHY (short)
    action_summary: str                 # WHAT — one human line for the feed
    trace_id: str                       # correlation → the feed thread (op_id / goal_id)

    # --- envelope metadata ---
    kind: str = "generic"
    schema_version: str = HIVE_ENV_SCHEMA
    source_fabric: str = "unknown"      # trinity | ide_sse | commprotocol | telemetry | ...
    event_id: str = ""
    ts: float = Field(default_factory=time.time)
    severity: Literal["info", "success", "warn", "error"] = "info"
    detail: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    def to_bus_payload(self) -> Dict[str, Any]:
        """Flatten to a dict that BOTH the ov hive parser AND the existing
        ``governance_sse_bridge._render`` understand (mandate 3): it carries the
        full envelope PLUS the bridge's required aliases (type / narration_text /
        source_brain / narration_priority)."""
        payload = self.model_dump()
        payload.update({
            "type": self.kind,
            "narration_text": self.action_summary,
            "source_brain": self.actor_id,
            "narration_priority": "high" if self.severity in ("warn", "error") else "normal",
        })
        return payload


class SensorEnvelope(HiveTelemetryEnvelope):
    kind: Literal["sensor"] = "sensor"
    signal_urgency: Optional[str] = None


class GovernanceEnvelope(HiveTelemetryEnvelope):
    kind: Literal["governance"] = "governance"
    phase: Optional[str] = None
    risk_tier: Optional[str] = None


class SwarmEnvelope(HiveTelemetryEnvelope):
    kind: Literal["swarm"] = "swarm"
    lifecycle: Optional[str] = None     # spawned | kept | disposed | vaporized | promoted


class RoutingEnvelope(HiveTelemetryEnvelope):
    kind: Literal["routing"] = "routing"
    provider: Optional[str] = None


class ActorEnvelope(HiveTelemetryEnvelope):
    """A silent-actor emission (Step 2): MCP tools, web, voice, core contexts,
    ghost-hands actuation, perception, memory. ``coalesced_n > 1`` means the
    Edge-Level Debouncer folded a burst of granular events into this ONE
    semantic envelope (``span_ms`` covers first→last)."""
    kind: Literal["actor"] = "actor"
    coalesced_n: int = 1
    span_ms: float = 0.0


# ---------------------------------------------------------------------------
# Read-only source adapters (mandate 1 — cast, never mutate/republish)
# ---------------------------------------------------------------------------

def _summarize(text: str, fallback: str) -> str:
    t = (text or "").strip()
    return (t[:160] if t else fallback)


def _subsystem_for_trinity(topic: str) -> tuple:
    for prefix, (sub, actor) in _TRINITY_PREFIX_MAP.items():
        if topic.startswith(prefix):
            return sub, actor
    return "bus", topic.split(".")[0] or "bus"


def from_trinity_event(
    *, topic: str, payload: Mapping[str, Any], event_id: str = "",
    ts: Optional[float] = None,
) -> HiveTelemetryEnvelope:
    """Cast a TrinityEventBus event into the Universal Envelope. NEVER raises."""
    try:
        p = dict(payload) if isinstance(payload, Mapping) else {}
        subsystem, actor_base = _subsystem_for_trinity(topic)
        verb = topic.split(".")[-1]
        actor_id = f"{actor_base}.{verb}" if actor_base == "autonomy" else actor_base
        narration = str(p.get("narration_text") or p.get("event") or "")
        summary = _summarize(narration, f"{actor_base}: {verb}")
        trace = str(p.get("op_id") or p.get("trace_id") or p.get("correlation_id")
                    or p.get("command_id") or "—")
        sev = "error" if (p.get("success") is False or "fail" in verb or "error" in verb) else "info"
        base = dict(actor_id=actor_id, subsystem=subsystem, intent=verb,
                    action_summary=summary, trace_id=trace, source_fabric="trinity",
                    event_id=str(event_id or p.get("event_id") or ""),
                    ts=float(ts if ts is not None else p.get("ts", time.time())),
                    severity=sev, detail=p)
        if subsystem == "sensor":
            return SensorEnvelope(signal_urgency=p.get("urgency"), **base)
        if subsystem in ("governance",):
            return GovernanceEnvelope(phase=p.get("phase"), risk_tier=p.get("risk_tier"), **base)
        if subsystem == "swarm":
            return SwarmEnvelope(lifecycle=p.get("lifecycle") or p.get("state"), **base)
        if subsystem == "routing":
            return RoutingEnvelope(provider=p.get("provider"), **base)
        return HiveTelemetryEnvelope(**base)
    except Exception:  # noqa: BLE001 — never break the aggregator on one bad frame
        return HiveTelemetryEnvelope(
            actor_id="trinity", subsystem="bus", intent="event",
            action_summary=f"trinity:{topic}", trace_id="—", source_fabric="trinity",
            ts=float(ts or time.time()))


def _subsystem_for_sse(event_type: str) -> str:
    et = (event_type or "").lower()
    for prefix, sub in _SSE_SUBSYSTEM_HINTS:
        if et.startswith(prefix) or prefix in et:
            return sub
    return "governance"


def from_ide_sse_event(
    *, event_type: str, op_id: str = "", payload: Mapping[str, Any],
    event_id: str = "", ts: Optional[float] = None,
) -> HiveTelemetryEnvelope:
    """Cast an IDE SSE StreamEvent into the Universal Envelope. NEVER raises."""
    try:
        p = dict(payload) if isinstance(payload, Mapping) else {}
        subsystem = _subsystem_for_sse(event_type)
        narration = str(p.get("narration_text") or p.get("summary")
                        or p.get("message") or "")
        summary = _summarize(narration, event_type.replace("_", " "))
        trace = str(op_id or p.get("op_id") or "—")
        sev = "error" if ("fail" in event_type or "error" in event_type
                          or "deadlock" in event_type or p.get("success") is False) else "info"
        base = dict(actor_id=f"ov.{subsystem}", subsystem=subsystem, intent=event_type,
                    action_summary=summary, trace_id=trace, source_fabric="ide_sse",
                    event_id=str(event_id or ""),
                    ts=float(ts if ts is not None else p.get("ts", time.time())),
                    severity=sev, detail=p)
        if subsystem == "swarm":
            life = ("spawned" if "spawned" in event_type else
                    "vaporized" if "vaporized" in event_type else
                    "disposed" if "dispos" in event_type else None)
            return SwarmEnvelope(lifecycle=life, actor_id="ov.swarm", **{k: v for k, v in base.items() if k != "actor_id"})
        if subsystem == "governance":
            return GovernanceEnvelope(phase=p.get("phase") or p.get("terminal_phase"),
                                      risk_tier=p.get("risk_tier"), **base)
        if subsystem == "routing":
            return RoutingEnvelope(provider=p.get("provider"), **base)
        if subsystem == "sensor":
            return SensorEnvelope(**base)
        return HiveTelemetryEnvelope(**base)
    except Exception:  # noqa: BLE001
        return HiveTelemetryEnvelope(
            actor_id="ov.observability", subsystem="governance", intent=event_type or "event",
            action_summary=str(event_type or "sse event"), trace_id=str(op_id or "—"),
            source_fabric="ide_sse", ts=float(ts or time.time()))


__all__ = [
    "HIVE_ENV_SCHEMA", "HiveTelemetryEnvelope", "SensorEnvelope",
    "GovernanceEnvelope", "SwarmEnvelope", "RoutingEnvelope",
    "from_trinity_event", "from_ide_sse_event",
]

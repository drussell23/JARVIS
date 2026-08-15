"""The one typed frame O+V's activity crosses a process boundary in.

WHY AN ENVELOPE AND NOT N EVENT TYPES
-------------------------------------
``StreamEventBroker`` validates against a CLOSED vocabulary and drops an
unknown ``event_type`` **silently** (``publish`` returns None and logs at
DEBUG). O+V's activity, by contrast, is an open set of TrinityEventBus
topics — ``autonomy.#``, ``governance.#``, ``ouroboros.#`` — that grows
whenever a subsystem starts narrating.

Widening the broker's vocabulary per topic would mean every new topic is one
forgotten edit away from vanishing without a trace, which is the failure this
repo has already paid for. So the whole open set crosses as ONE registered
type carrying its topic as DATA. The vocabulary stops being something anyone
has to remember.

WHY STRICT, AND WHAT "STRICT" BUYS
-----------------------------------
The payload arrives from another process. Decoding is therefore a trust
boundary, and the rule here is that a malformed frame produces ``None`` —
never a partially-populated envelope, and never an exception into the pump
that decoded it. A half-valid envelope is worse than a rejected one: it
reaches a renderer that then has to defend itself against every field
independently, which is exactly the defensive sprawl this type exists to
stop.

The frame carries an ALREADY-RENDERED payload rather than a raw event. That
is deliberate: rendering happens once, in the process that owns the loop, and
the consumer's renderer is never fed cross-process input at all. The
alternative — ship raw, render on arrival — puts untrusted data into
``GovernanceSSEBridge._render`` on every frame, and "it never raises" is a
weaker guarantee than "it is never called".

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("Ouroboros.GovernanceEnvelope")

GOVERNANCE_ENVELOPE_SCHEMA_VERSION: str = "governance_envelope.1"

#: The single registered broker event type. Must also appear in
#: ``ide_observability_stream._VALID_EVENT_TYPES`` — a type absent from that
#: frozenset is dropped by ``publish`` with no error anywhere.
GOVERNANCE_FORWARD_EVENT_TYPE: str = "governance_forward"

__all__ = [
    "GOVERNANCE_ENVELOPE_SCHEMA_VERSION",
    "GOVERNANCE_FORWARD_EVENT_TYPE",
    "GovernanceEnvelope",
    "max_payload_bytes",
]


def max_payload_bytes() -> int:
    """Ceiling on one frame's rendered payload.

    ``JARVIS_GOVERNANCE_ENVELOPE_MAX_BYTES`` — default 64 KiB. A frame is a
    narration for a phone screen; anything approaching this size is a bug
    upstream, and shipping it would let one pathological event consume the
    whole bounded queue behind it. Oversize is REJECTED rather than
    truncated: a truncated JSON payload is a malformed one, and the consumer
    would be the place that discovered it.
    """
    try:
        raw = (os.environ.get("JARVIS_GOVERNANCE_ENVELOPE_MAX_BYTES") or "").strip()
        return max(1024, min(4 * 1024 * 1024, int(raw or 65536)))
    except Exception:  # noqa: BLE001
        return 65536


@dataclass(frozen=True)
class GovernanceEnvelope:
    """One O+V activity frame, in flight between two processes.

    Frozen: an envelope that could be mutated after validation is one whose
    validation proves nothing about what is eventually rendered.
    """

    #: The TrinityEventBus topic this came from — carried as data so the
    #: broker's closed vocabulary never has to grow.
    topic: str
    #: The already-rendered, Swift-``DaemonEvent``-shaped payload.
    payload: Dict[str, Any]
    #: Which op this belongs to, when the source knew. "" is legitimate:
    #: lifecycle narration is not op-scoped.
    op_id: str = ""
    #: Who produced it. Provenance for an operator staring at two processes;
    #: never used for routing, so a spoofed value cannot redirect anything.
    source_id: str = ""
    schema_version: str = GOVERNANCE_ENVELOPE_SCHEMA_VERSION

    # -- encode ----------------------------------------------------------

    def to_payload(self) -> Dict[str, Any]:
        """The dict handed to ``broker.publish``. NEVER raises."""
        return {
            "schema_version": self.schema_version,
            "topic": self.topic,
            "op_id": self.op_id,
            "source_id": self.source_id,
            "payload": dict(self.payload),
        }

    @classmethod
    def of(cls, topic: str, payload: Mapping[str, Any], *,
           op_id: str = "", source_id: str = "") -> Optional["GovernanceEnvelope"]:
        """Build from a producer's own values, applying the SAME rules the
        decoder applies. NEVER raises, returns None when unusable.

        Validating on the way out as well as in is deliberate: a frame that
        cannot be decoded should never have been sent, and discovering that
        at the consumer means discovering it in the process that cannot fix
        it.
        """
        try:
            if not isinstance(payload, Mapping):
                return None
            body = dict(payload)
            if not _within_size(body):
                logger.debug("[GovernanceEnvelope] oversize payload dropped "
                             "topic=%r", topic)
                return None
            return cls(
                topic=str(topic or ""),
                payload=body,
                op_id=str(op_id or ""),
                source_id=str(source_id or ""),
            )
        except Exception:  # noqa: BLE001
            return None

    # -- decode ----------------------------------------------------------

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["GovernanceEnvelope"]:
        """Decode a frame from another process. NEVER raises.

        Returns None for anything that is not exactly what this type
        promises. There is no partial success: every caller downstream may
        assume a returned envelope has a string topic and a dict payload,
        which is the entire point of the boundary.
        """
        try:
            if not isinstance(raw, Mapping):
                return None
            if raw.get("schema_version") != GOVERNANCE_ENVELOPE_SCHEMA_VERSION:
                # A version this process does not implement is not a frame it
                # can honestly render. Refusing beats guessing at a shape.
                return None
            topic = raw.get("topic")
            payload = raw.get("payload")
            if not isinstance(topic, str) or not isinstance(payload, Mapping):
                return None
            op_id = raw.get("op_id", "")
            source_id = raw.get("source_id", "")
            if not isinstance(op_id, str) or not isinstance(source_id, str):
                return None
            body = dict(payload)
            if not _within_size(body):
                return None
            return cls(topic=topic, payload=body, op_id=op_id,
                       source_id=source_id)
        except Exception:  # noqa: BLE001
            logger.debug("[GovernanceEnvelope] decode degraded", exc_info=True)
            return None


def _within_size(body: Mapping[str, Any]) -> bool:
    """True when *body* serialises and fits. NEVER raises.

    Serialisability is checked HERE rather than at the socket: a payload
    holding a non-JSON value would otherwise fail inside the transport's
    send path, where the failure looks like a link fault instead of a bad
    frame.
    """
    try:
        return len(json.dumps(body, default=str).encode("utf-8")) <= max_payload_bytes()
    except Exception:  # noqa: BLE001
        return False

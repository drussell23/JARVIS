"""
Telemetry Contract v1
=====================

Unified envelope schema for all JARVIS telemetry events.

Dual versioning:
  envelope_version -- transport/routing contract (stable)
  event_schema     -- domain payload contract (name@semver, evolves per-type)

Delivery: at-least-once, idempotent consumers via idempotency_key.
Ordering: per-partition_key monotonic sequence; no global ordering.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = "1.0.0"

V1_EVENT_SCHEMAS: List[str] = [
    "lifecycle.transition@1.0.0",
    "lifecycle.health@1.0.0",
    "reasoning.activation@1.0.0",
    "reasoning.decision@1.0.0",
    "scheduler.graph_state@1.0.0",
    "scheduler.unit_state@1.0.0",
    "recovery.attempt@1.0.0",
    "fault.raised@1.0.0",
    "fault.resolved@1.0.0",
]


class SequenceCounter:
    """Per-partition monotonic counter.

    Each partition_key gets an independent counter starting at 1.
    Thread-safe within a single process (GIL-protected dict ops).
    """

    def __init__(self) -> None:
        self._counters: Dict[str, int] = defaultdict(int)

    def next(self, partition_key: str) -> int:
        self._counters[partition_key] += 1
        return self._counters[partition_key]


# Module-level singleton used by TelemetryEnvelope.create().
_sequence_counter = SequenceCounter()


@dataclass(frozen=True)
class TelemetryEnvelope:
    """Immutable telemetry event envelope (v1.0.0).

    Fields:
        envelope_version -- transport contract version (semver)
        event_id         -- globally unique event identifier (UUID4)
        event_schema     -- domain payload contract (name@semver)
        emitted_at       -- epoch seconds (float, monotonic-ish via time.time())
        sequence         -- per-partition monotonic counter value
        trace_id         -- distributed trace identifier
        span_id          -- span within the trace
        causal_parent_id -- optional parent span for causal chaining
        op_id            -- optional Ouroboros operation identifier
        idempotency_key  -- deterministic dedup key (schema:trace:seq)
        partition_key    -- routing/ordering partition
        source           -- emitting component name
        severity         -- log-level-like severity (info, warn, error, critical)
        payload          -- domain-specific data dict
    """

    envelope_version: str
    event_id: str
    event_schema: str
    emitted_at: float
    sequence: int
    trace_id: str
    span_id: str
    causal_parent_id: Optional[str]
    op_id: Optional[str]
    idempotency_key: str
    partition_key: str
    source: str
    severity: str
    payload: Dict[str, Any]

    @classmethod
    def create(
        cls,
        event_schema: str,
        source: str,
        trace_id: str,
        span_id: str,
        partition_key: str,
        payload: Dict[str, Any],
        severity: str = "info",
        causal_parent_id: Optional[str] = None,
        op_id: Optional[str] = None,
    ) -> TelemetryEnvelope:
        """Factory that stamps event_id, emitted_at, sequence, and idempotency_key."""
        seq = _sequence_counter.next(partition_key)
        return cls(
            envelope_version=ENVELOPE_VERSION,
            event_id=str(uuid.uuid4()),
            event_schema=event_schema,
            emitted_at=time.time(),
            sequence=seq,
            trace_id=trace_id,
            span_id=span_id,
            causal_parent_id=causal_parent_id,
            op_id=op_id,
            idempotency_key=f"{event_schema}:{trace_id}:{seq}",
            partition_key=partition_key,
            source=source,
            severity=severity,
            payload=payload,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding."""
        return asdict(self)


class EventRegistry:
    """Validates event schemas against registered types.

    Supports semver-aware compatibility: consumers registered at major
    version N accept any event at major version N (minor/patch may differ).
    """

    def __init__(self) -> None:
        self._schemas: Dict[str, str] = {}  # name -> version

    def register(self, event_schema: str) -> None:
        """Register an event schema (name@semver)."""
        name, version = self.parse_schema(event_schema)
        self._schemas[name] = version

    def is_registered(self, event_schema: str) -> bool:
        """Check if the exact event name (ignoring version) is registered."""
        try:
            name, _ = self.parse_schema(event_schema)
            return name in self._schemas
        except ValueError:
            return False

    def is_compatible(self, event_schema: str) -> bool:
        """Check major-version compatibility with the registered version."""
        try:
            name, version = self.parse_schema(event_schema)
            registered = self._schemas.get(name)
            if registered is None:
                return False
            return registered.split(".")[0] == version.split(".")[0]
        except (ValueError, IndexError):
            return False

    @staticmethod
    def parse_schema(event_schema: str) -> Tuple[str, str]:
        """Parse 'name@version' into (name, version). Raises ValueError if malformed."""
        if "@" not in event_schema:
            raise ValueError(
                f"Invalid event_schema '{event_schema}': missing @version"
            )
        name, version = event_schema.rsplit("@", 1)
        return name, version

    @classmethod
    def with_v1_defaults(cls) -> EventRegistry:
        """Create a registry pre-loaded with the v1 event schemas."""
        registry = cls()
        for schema in V1_EVENT_SCHEMAS:
            registry.register(schema)
        return registry

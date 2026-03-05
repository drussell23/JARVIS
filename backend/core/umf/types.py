"""UMF canonical envelope types -- stdlib only, zero JARVIS imports.

Defines the wire-format dataclasses, enums, and helpers that every UMF
participant (supervisor, reactor-core, jarvis-prime) must agree on.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only.
* ``UmfMessage`` is a plain ``@dataclass`` (not frozen) so that
  ``__post_init__`` can fill auto-generated defaults.
* ``MessageSource`` and ``MessageTarget`` are frozen value objects.
* ``to_json()`` produces deterministic output (sorted keys, no indent).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

# ── Schema version constant ────────────────────────────────────────

UMF_SCHEMA_VERSION: str = "umf.v1"

# ── Enums ───────────────────────────────────────────────────────────


class Stream(str, Enum):
    """Logical message stream (topic partition)."""

    lifecycle = "lifecycle"
    command = "command"
    event = "event"
    heartbeat = "heartbeat"
    telemetry = "telemetry"


class Kind(str, Enum):
    """Message kind within a stream."""

    command = "command"
    event = "event"
    heartbeat = "heartbeat"
    ack = "ack"
    nack = "nack"


class Priority(str, Enum):
    """Routing priority for delivery ordering."""

    critical = "critical"
    high = "high"
    normal = "normal"
    low = "low"


class RejectReason(str, Enum):
    """Canonical NACK / rejection reason codes."""

    schema_mismatch = "schema_mismatch"
    sig_invalid = "sig_invalid"
    capability_mismatch = "capability_mismatch"
    ttl_expired = "ttl_expired"
    deadline_expired = "deadline_expired"
    dedup_duplicate = "dedup_duplicate"
    route_unavailable = "route_unavailable"
    backpressure_drop = "backpressure_drop"
    circuit_open = "circuit_open"
    handler_timeout = "handler_timeout"


class ReserveResult(str, Enum):
    """Idempotency ledger reservation outcome."""

    reserved = "reserved"
    duplicate = "duplicate"
    conflict = "conflict"


# ── Frozen value objects ────────────────────────────────────────────


@dataclass(frozen=True)
class MessageSource:
    """Origin identity of a UMF message."""

    repo: str
    component: str
    instance_id: str
    session_id: str


@dataclass(frozen=True)
class MessageTarget:
    """Destination identity of a UMF message."""

    repo: str
    component: str


# ── Helpers ─────────────────────────────────────────────────────────


def _uuid4_hex() -> str:
    """Return a UUID4 as a 32-char hex string (no dashes)."""
    return uuid.uuid4().hex


def _now_ms() -> int:
    """Current wall-clock time in milliseconds since epoch."""
    return int(time.time() * 1000)


# ── Main envelope ───────────────────────────────────────────────────


@dataclass
class UmfMessage:
    """Canonical UMF envelope -- the single message shape on the wire.

    Required fields must be provided by the caller.  All other fields
    carry sensible auto-generated defaults and can be overridden.
    """

    # ── required ────────────────────────────────────────────────────
    stream: Stream
    kind: Kind
    source: MessageSource
    target: MessageTarget
    payload: Dict[str, Any]

    # ── auto-generated identity ─────────────────────────────────────
    schema_version: str = field(default="")
    message_id: str = field(default="")
    idempotency_key: str = field(default="")

    # ── routing ─────────────────────────────────────────────────────
    routing_partition_key: str = field(default="")
    routing_priority: Priority = field(default=Priority.normal)
    routing_ttl_ms: int = field(default=30_000)
    routing_deadline_unix_ms: int = field(default=0)

    # ── causality ───────────────────────────────────────────────────
    causality_trace_id: str = field(default="")
    causality_span_id: str = field(default="")
    causality_parent_message_id: Optional[str] = field(default=None)
    causality_sequence: int = field(default=0)

    # ── contract ────────────────────────────────────────────────────
    contract_capability_hash: str = field(default="")
    contract_schema_hash: str = field(default="")
    contract_compat_window: str = field(default="N|N-1")

    # ── timing ──────────────────────────────────────────────────────
    observed_at_unix_ms: int = field(default=0)

    # ── signature ───────────────────────────────────────────────────
    signature_alg: str = field(default="")
    signature_key_id: str = field(default="")
    signature_value: str = field(default="")

    def __post_init__(self) -> None:
        """Fill auto-generated defaults for fields left at sentinel values."""
        if not self.schema_version:
            self.schema_version = UMF_SCHEMA_VERSION

        if not self.message_id:
            self.message_id = _uuid4_hex()

        if not self.idempotency_key:
            self.idempotency_key = self.message_id

        if not self.routing_partition_key:
            self.routing_partition_key = (
                f"{self.source.repo}.{self.source.component}"
            )

        if not self.causality_trace_id:
            self.causality_trace_id = _uuid4_hex()[:16]

        if not self.causality_span_id:
            self.causality_span_id = _uuid4_hex()[:8]

        if not self.observed_at_unix_ms:
            self.observed_at_unix_ms = _now_ms()

        # Coerce enum types from strings (for from_dict round-trips)
        if isinstance(self.stream, str):
            self.stream = Stream(self.stream)
        if isinstance(self.kind, str):
            self.kind = Kind(self.kind)
        if isinstance(self.routing_priority, str):
            self.routing_priority = Priority(self.routing_priority)

    # ── expiry ──────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        """Return ``True`` if the message has exceeded its TTL.

        Checks wall-clock time against ``observed_at_unix_ms + routing_ttl_ms``.
        A TTL of 0 means "never expires".
        """
        if self.routing_ttl_ms <= 0:
            return False
        deadline = self.observed_at_unix_ms + self.routing_ttl_ms
        return _now_ms() > deadline

    # ── serialization ───────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a plain dict suitable for JSON serialization."""
        d: Dict[str, Any] = {}

        # Identity
        d["schema_version"] = self.schema_version
        d["message_id"] = self.message_id
        d["idempotency_key"] = self.idempotency_key

        # Core
        d["stream"] = self.stream.value
        d["kind"] = self.kind.value
        d["source"] = asdict(self.source)
        d["target"] = asdict(self.target)
        d["payload"] = self.payload

        # Routing
        d["routing_partition_key"] = self.routing_partition_key
        d["routing_priority"] = self.routing_priority.value
        d["routing_ttl_ms"] = self.routing_ttl_ms
        d["routing_deadline_unix_ms"] = self.routing_deadline_unix_ms

        # Causality
        d["causality_trace_id"] = self.causality_trace_id
        d["causality_span_id"] = self.causality_span_id
        d["causality_parent_message_id"] = self.causality_parent_message_id
        d["causality_sequence"] = self.causality_sequence

        # Contract
        d["contract_capability_hash"] = self.contract_capability_hash
        d["contract_schema_hash"] = self.contract_schema_hash
        d["contract_compat_window"] = self.contract_compat_window

        # Timing
        d["observed_at_unix_ms"] = self.observed_at_unix_ms

        # Signature
        d["signature_alg"] = self.signature_alg
        d["signature_key_id"] = self.signature_key_id
        d["signature_value"] = self.signature_value

        return d

    def to_json(self) -> str:
        """Deterministic JSON: sorted keys, no indent, ASCII-safe."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UmfMessage":
        """Reconstruct a ``UmfMessage`` from a dict (e.g. parsed JSON).

        Handles enum coercion and nested dataclass reconstruction.
        """
        source_raw = d["source"]
        target_raw = d["target"]

        return cls(
            stream=d["stream"],
            kind=d["kind"],
            source=MessageSource(**source_raw),
            target=MessageTarget(**target_raw),
            payload=d["payload"],
            schema_version=d.get("schema_version", UMF_SCHEMA_VERSION),
            message_id=d.get("message_id", ""),
            idempotency_key=d.get("idempotency_key", ""),
            routing_partition_key=d.get("routing_partition_key", ""),
            routing_priority=d.get("routing_priority", "normal"),
            routing_ttl_ms=d.get("routing_ttl_ms", 30_000),
            routing_deadline_unix_ms=d.get("routing_deadline_unix_ms", 0),
            causality_trace_id=d.get("causality_trace_id", ""),
            causality_span_id=d.get("causality_span_id", ""),
            causality_parent_message_id=d.get("causality_parent_message_id"),
            causality_sequence=d.get("causality_sequence", 0),
            contract_capability_hash=d.get("contract_capability_hash", ""),
            contract_schema_hash=d.get("contract_schema_hash", ""),
            contract_compat_window=d.get("contract_compat_window", "N|N-1"),
            observed_at_unix_ms=d.get("observed_at_unix_ms", 0),
            signature_alg=d.get("signature_alg", ""),
            signature_key_id=d.get("signature_key_id", ""),
            signature_value=d.get("signature_value", ""),
        )

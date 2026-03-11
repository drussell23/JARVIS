"""backend/core/ouroboros/governance/autonomy/autonomy_types.py

Command & Event Envelopes — Shared Infrastructure for C+ Layered Architecture.

All inter-layer communication (L2->L1, L3->L1, L4->L1) uses typed envelopes.
Every subsequent C+ task depends on these types.

Design:
    - CommandEnvelope: frozen dataclass with auto UUID, deterministic
      idempotency key, TTL expiry, and priority via FAILURE_PRECEDENCE.
    - EventEnvelope: frozen dataclass with auto UUID and optional op_id.
    - CommandType / EventType: str enums for exhaustive pattern matching.
    - FAILURE_PRECEDENCE: priority mapping (0=safety, 3=learning).
    - EnvelopeContractGate: validates schema versions against supported set.
    - IdempotencyLRU: bounded OrderedDict for duplicate command detection.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version constant
# ---------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION: str = "1.0"

# ---------------------------------------------------------------------------
# Command types
# ---------------------------------------------------------------------------


class CommandType(str, enum.Enum):
    """Typed command verbs for inter-layer communication."""

    GENERATE_BACKLOG_ENTRY = "generate_backlog_entry"
    ADJUST_BRAIN_HINT = "adjust_brain_hint"
    REQUEST_MODE_SWITCH = "request_mode_switch"
    REPORT_ROLLBACK_CAUSE = "report_rollback_cause"
    SIGNAL_HUMAN_PRESENCE = "signal_human_presence"
    REQUEST_SAGA_SUBMIT = "request_saga_submit"
    REPORT_CONSENSUS = "report_consensus"
    RECOMMEND_TIER_CHANGE = "recommend_tier_change"


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(str, enum.Enum):
    """Typed event kinds emitted by L1 for downstream consumers."""

    OP_COMPLETED = "op_completed"
    OP_ROLLED_BACK = "op_rolled_back"
    TRUST_TIER_CHANGED = "trust_tier_changed"
    DEGRADATION_MODE_CHANGED = "degradation_mode_changed"
    HEALTH_PROBE_RESULT = "health_probe_result"
    CURRICULUM_PUBLISHED = "curriculum_published"
    ATTRIBUTION_SCORED = "attribution_scored"
    ROLLBACK_ANALYZED = "rollback_analyzed"
    INCIDENT_DETECTED = "incident_detected"
    SAGA_STATE_CHANGED = "saga_state_changed"


# ---------------------------------------------------------------------------
# Failure precedence (priority mapping)
# ---------------------------------------------------------------------------

FAILURE_PRECEDENCE: Dict[CommandType, int] = {
    # 0 = highest priority (safety-critical)
    CommandType.REPORT_ROLLBACK_CAUSE: 0,
    CommandType.SIGNAL_HUMAN_PRESENCE: 0,
    # 1 = operational
    CommandType.REQUEST_MODE_SWITCH: 1,
    CommandType.ADJUST_BRAIN_HINT: 1,
    # 2 = coordination
    CommandType.REQUEST_SAGA_SUBMIT: 2,
    CommandType.REPORT_CONSENSUS: 2,
    CommandType.RECOMMEND_TIER_CHANGE: 2,
    # 3 = learning (lowest priority)
    CommandType.GENERATE_BACKLOG_ENTRY: 3,
}

# ---------------------------------------------------------------------------
# Idempotency key helpers
# ---------------------------------------------------------------------------


def _deterministic_key(
    source_layer: str,
    target_layer: str,
    command_type: CommandType,
    payload: Dict[str, Any],
) -> str:
    """Compute a deterministic SHA-256 idempotency key from envelope fields.

    The key is derived from the semantic identity of the command (source,
    target, type, payload) so that identical commands produce identical keys
    regardless of timing or UUID.
    """
    canonical = json.dumps(
        {
            "source_layer": source_layer,
            "target_layer": target_layer,
            "command_type": command_type.value,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CommandEnvelope
# ---------------------------------------------------------------------------


def _make_uuid() -> str:
    return str(uuid.uuid4())


def _monotonic_ns() -> int:
    return time.monotonic_ns()


@dataclass(frozen=True)
class CommandEnvelope:
    """Immutable command envelope for inter-layer communication.

    Auto-populates:
        - command_id: UUID4
        - idempotency_key: deterministic SHA-256 of (source, target, type, payload)
        - issued_at_ns: monotonic nanosecond timestamp
        - schema_version: current schema version
    """

    source_layer: str
    target_layer: str
    command_type: CommandType
    payload: Dict[str, Any]
    ttl_s: float

    # Auto-populated fields
    schema_version: str = field(default=CURRENT_SCHEMA_VERSION)
    command_id: str = field(default_factory=_make_uuid)
    idempotency_key: str = field(default="")
    issued_at_ns: int = field(default_factory=_monotonic_ns)

    def __post_init__(self) -> None:
        # Frozen dataclasses require object.__setattr__ for post-init mutation
        if not self.idempotency_key:
            key = _deterministic_key(
                self.source_layer,
                self.target_layer,
                self.command_type,
                self.payload,
            )
            object.__setattr__(self, "idempotency_key", key)

    def is_expired(self) -> bool:
        """Return True if this command has exceeded its TTL."""
        elapsed_ns = time.monotonic_ns() - self.issued_at_ns
        elapsed_s = elapsed_ns / 1_000_000_000
        return elapsed_s >= self.ttl_s

    @property
    def priority(self) -> int:
        """Return priority from FAILURE_PRECEDENCE (0=highest, 3=lowest)."""
        return FAILURE_PRECEDENCE.get(self.command_type, 3)


# ---------------------------------------------------------------------------
# EventEnvelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventEnvelope:
    """Immutable event envelope emitted by L1 for downstream consumers.

    Auto-populates:
        - event_id: UUID4
        - emitted_at_ns: monotonic nanosecond timestamp
        - schema_version: current schema version
    """

    source_layer: str
    event_type: EventType
    payload: Dict[str, Any]

    # Auto-populated fields
    schema_version: str = field(default=CURRENT_SCHEMA_VERSION)
    event_id: str = field(default_factory=_make_uuid)
    emitted_at_ns: int = field(default_factory=_monotonic_ns)
    op_id: Optional[str] = None


# ---------------------------------------------------------------------------
# EnvelopeContractGate
# ---------------------------------------------------------------------------


class EnvelopeContractGate:
    """Validates envelope schema versions against a supported set.

    By default supports the current schema version.  Pass a custom
    ``supported_versions`` frozenset to override.
    """

    def __init__(
        self,
        supported_versions: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._supported: FrozenSet[str] = (
            supported_versions
            if supported_versions is not None
            else frozenset({CURRENT_SCHEMA_VERSION})
        )

    def validate(self, version: str) -> bool:
        """Return True if *version* is in the supported set."""
        return version in self._supported


# ---------------------------------------------------------------------------
# IdempotencyLRU
# ---------------------------------------------------------------------------


class IdempotencyLRU:
    """Bounded LRU cache for duplicate command detection.

    ``seen(key)`` returns ``True`` if the key was already recorded
    (duplicate), ``False`` if it is new.  The oldest entry is evicted
    when capacity is reached.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._cache: OrderedDict[str, bool] = OrderedDict()

    def seen(self, key: str) -> bool:
        """Check and record *key*.

        Returns ``True`` if *key* was already present (duplicate),
        ``False`` if new (first occurrence).
        """
        if self._capacity <= 0:
            return False

        if key in self._cache:
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return True

        # New key — insert and maybe evict
        self._cache[key] = True
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)  # evict oldest
        return False

    def __len__(self) -> int:
        return len(self._cache)

"""backend/core/ouroboros/governance/autonomy/saga_messages.py

Saga Message Types & In-Memory Message Bus — L4 Cross-Repo Coordination.

Extracted from the deprecated ``neural_mesh.py`` MeshMessage pattern and
specialised for cross-repo saga coordination in L4 AdvancedAutonomyService.

Design:
    - ``SagaMessage``: dataclass with auto UUID, monotonic_ns timestamp,
      TTL-based expiry, and full serialisation round-trip.
    - ``SagaMessageBus``: synchronous in-memory pub-sub bus with per-type
      handler subscriptions, TTL pruning, capacity enforcement, and
      query/filter support.
    - Factory functions: ``create_apply_request``, ``create_vote_request``
      for the most common message patterns.

Key decisions:
    - In-memory only — no network transport (that belongs to L1).
    - ``time.monotonic_ns()`` for timestamps (consistent with C+
      ``CommandEnvelope``).
    - ``correlation_id`` for request/response pairing across repos.
    - TTL expiry keeps memory bounded even without explicit pruning.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.SagaMessages")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SagaMessageType(str, Enum):
    """Types of messages in saga coordination."""

    # Saga lifecycle
    SAGA_CREATED = "saga_created"
    SAGA_ADVANCED = "saga_advanced"
    SAGA_COMPLETED = "saga_completed"
    SAGA_FAILED = "saga_failed"
    SAGA_ROLLED_BACK = "saga_rolled_back"
    SAGA_PARTIAL_PROMOTE = "saga_partial_promote"
    TARGET_MOVED = "target_moved"
    ANCESTRY_VIOLATION = "ancestry_violation"

    # Repo coordination
    REPO_APPLY_REQUEST = "repo_apply_request"
    REPO_APPLY_RESULT = "repo_apply_result"
    REPO_VERIFY_REQUEST = "repo_verify_request"
    REPO_VERIFY_RESULT = "repo_verify_result"

    # Consensus
    VOTE_REQUEST = "vote_request"
    VOTE_CAST = "vote_cast"
    CONSENSUS_REACHED = "consensus_reached"


class MessagePriority(Enum):
    """Priority levels for saga messages."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


# ---------------------------------------------------------------------------
# SagaMessage
# ---------------------------------------------------------------------------


def _make_message_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class SagaMessage:
    """A message in the saga coordination system.

    Adapted from ``neural_mesh.py`` ``MeshMessage`` but specialised for
    cross-repo saga coordination.  Uses ``time.monotonic_ns()`` for
    timestamps (consistent with ``CommandEnvelope``).
    """

    message_id: str = field(default_factory=_make_message_id)
    message_type: SagaMessageType = SagaMessageType.SAGA_CREATED
    saga_id: str = ""
    source_repo: str = ""
    target_repo: Optional[str] = None  # None = broadcast to all saga repos
    priority: MessagePriority = MessagePriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp_ns: int = field(default_factory=time.monotonic_ns)
    correlation_id: Optional[str] = None  # For request/response pairing
    ttl_s: float = 300.0

    def is_expired(self) -> bool:
        """Check if message has exceeded its TTL."""
        elapsed_ns = time.monotonic_ns() - self.timestamp_ns
        return elapsed_ns / 1_000_000_000 >= self.ttl_s

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for logging / persistence."""
        return {
            "message_id": self.message_id,
            "message_type": self.message_type.value,
            "saga_id": self.saga_id,
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "priority": self.priority.value,
            "payload": self.payload,
            "timestamp_ns": self.timestamp_ns,
            "correlation_id": self.correlation_id,
            "ttl_s": self.ttl_s,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SagaMessage:
        """Deserialise from dict."""
        return cls(
            message_id=data.get("message_id", _make_message_id()),
            message_type=SagaMessageType(data.get("message_type", "saga_created")),
            saga_id=data.get("saga_id", ""),
            source_repo=data.get("source_repo", ""),
            target_repo=data.get("target_repo"),
            priority=MessagePriority(data.get("priority", 1)),
            payload=data.get("payload", {}),
            timestamp_ns=data.get("timestamp_ns", time.monotonic_ns()),
            correlation_id=data.get("correlation_id"),
            ttl_s=data.get("ttl_s", 300.0),
        )


# ---------------------------------------------------------------------------
# SagaMessageBus
# ---------------------------------------------------------------------------


class SagaMessageBus:
    """In-memory message bus for saga coordination.

    Provides pub-sub messaging between repos within a saga.  Messages are
    stored in-memory and expire based on TTL.

    Parameters
    ----------
    max_messages:
        Upper bound on retained messages.  When exceeded the oldest
        messages are pruned.
    """

    def __init__(self, max_messages: int = 500) -> None:
        self._messages: List[SagaMessage] = []
        self._handlers: Dict[str, List[Callable[[SagaMessage], None]]] = defaultdict(
            list
        )
        self._max_messages = max_messages

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    def send(self, message: SagaMessage) -> bool:
        """Send a message.

        Returns ``True`` if delivered to at least one handler.
        Prunes expired messages.  Respects max capacity.
        """
        # Store the message regardless of handlers
        self._messages.append(message)

        # Enforce capacity — drop oldest when over limit
        if len(self._messages) > self._max_messages:
            overflow = len(self._messages) - self._max_messages
            self._messages = self._messages[overflow:]

        # Dispatch to subscribed handlers
        handlers = self._handlers.get(message.message_type.value, [])
        delivered = False
        for handler in handlers:
            try:
                handler(message)
                delivered = True
            except Exception:
                logger.exception(
                    "SagaMessageBus: handler raised for %s", message.message_type.value
                )
        return delivered

    # ------------------------------------------------------------------
    # subscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        message_type: SagaMessageType,
        handler: Callable[[SagaMessage], None],
    ) -> None:
        """Subscribe a handler to a message type."""
        self._handlers[message_type.value].append(handler)

    # ------------------------------------------------------------------
    # query helpers
    # ------------------------------------------------------------------

    def get_messages(
        self,
        saga_id: Optional[str] = None,
        message_type: Optional[SagaMessageType] = None,
        limit: int = 50,
    ) -> List[SagaMessage]:
        """Query messages with optional filters.  Returns newest first."""
        result = self._messages
        if saga_id is not None:
            result = [m for m in result if m.saga_id == saga_id]
        if message_type is not None:
            result = [m for m in result if m.message_type == message_type]
        # Newest first — reverse order
        result = list(reversed(result))
        return result[:limit]

    def get_conversation(self, correlation_id: str) -> List[SagaMessage]:
        """Get all messages in a request/response conversation."""
        return [m for m in self._messages if m.correlation_id == correlation_id]

    # ------------------------------------------------------------------
    # pruning
    # ------------------------------------------------------------------

    def prune_expired(self) -> int:
        """Remove expired messages.  Returns count removed."""
        before = len(self._messages)
        self._messages = [m for m in self._messages if not m.is_expired()]
        removed = before - len(self._messages)
        if removed:
            logger.debug("SagaMessageBus: pruned %d expired messages", removed)
        return removed

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Summary for telemetry."""
        handler_count = sum(len(hs) for hs in self._handlers.values())
        return {
            "total_messages": len(self._messages),
            "handler_count": handler_count,
            "max_messages": self._max_messages,
            "message_types": {
                mt.value: sum(1 for m in self._messages if m.message_type == mt)
                for mt in SagaMessageType
                if any(m.message_type == mt for m in self._messages)
            },
        }


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def create_apply_request(
    saga_id: str,
    source_repo: str,
    target_repo: str,
    patch_data: Dict[str, Any],
) -> SagaMessage:
    """Factory: create a REPO_APPLY_REQUEST message."""
    return SagaMessage(
        message_type=SagaMessageType.REPO_APPLY_REQUEST,
        saga_id=saga_id,
        source_repo=source_repo,
        target_repo=target_repo,
        priority=MessagePriority.HIGH,
        payload=patch_data,
    )


def create_vote_request(
    saga_id: str,
    source_repo: str,
    op_id: str,
) -> SagaMessage:
    """Factory: create a VOTE_REQUEST message (broadcast)."""
    return SagaMessage(
        message_type=SagaMessageType.VOTE_REQUEST,
        saga_id=saga_id,
        source_repo=source_repo,
        target_repo=None,  # broadcast
        priority=MessagePriority.HIGH,
        payload={"op_id": op_id},
        correlation_id=uuid.uuid4().hex[:16],
    )

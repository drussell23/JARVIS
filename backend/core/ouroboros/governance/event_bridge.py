# backend/core/ouroboros/governance/event_bridge.py
"""
Event Bus Bridge -- Governance-to-CrossRepo Event Mapping
==========================================================

Maps governance :class:`CommMessage` lifecycle events to
:class:`CrossRepoEvent` for propagation across JARVIS/PRIME/REACTOR
repos via the existing :class:`CrossRepoEventBus`.

Only INTENT, DECISION, and POSTMORTEM are bridged.  HEARTBEAT is
too noisy for cross-repo propagation and is filtered out.

Fault isolation: event bus failures are logged but never block the
governance pipeline.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    MessageType,
)

logger = logging.getLogger("Ouroboros.EventBridge")

# Lazy imports to avoid circular dependencies with cross_repo module
_CrossRepoEvent = None
_EventType = None
_RepoType = None


def _ensure_imports():
    """Lazy-import cross-repo types to break circular deps."""
    global _CrossRepoEvent, _EventType, _RepoType
    if _CrossRepoEvent is None:
        from backend.core.ouroboros.cross_repo import (
            CrossRepoEvent,
            EventType,
            RepoType,
        )
        _CrossRepoEvent = CrossRepoEvent
        _EventType = EventType
        _RepoType = RepoType


# Mapping: (MessageType, outcome) -> EventType name
_DECISION_OUTCOME_MAP = {
    "applied": "IMPROVEMENT_COMPLETE",
    "candidate_validated": "IMPROVEMENT_COMPLETE",
    "blocked": "IMPROVEMENT_FAILED",
    "escalated": "IMPROVEMENT_FAILED",
    "all_candidates_failed": "IMPROVEMENT_FAILED",
    "no_candidates": "IMPROVEMENT_FAILED",
    "validation_failed": "IMPROVEMENT_FAILED",
}


class GovernanceEventMapper:
    """Maps governance CommMessages to CrossRepoEvents."""

    @staticmethod
    def map(msg: CommMessage) -> Any:
        """Map a governance message to a cross-repo event.

        Returns None for message types that should not be bridged.
        """
        _ensure_imports()

        if msg.msg_type == MessageType.INTENT:
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType["IMPROVEMENT_REQUEST"],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        if msg.msg_type == MessageType.DECISION:
            outcome = msg.payload.get("outcome", "")
            event_type_name = _DECISION_OUTCOME_MAP.get(
                outcome, "IMPROVEMENT_FAILED"
            )
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType[event_type_name],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        if msg.msg_type == MessageType.POSTMORTEM:
            return _CrossRepoEvent(
                id=str(uuid.uuid4()),
                type=_EventType["IMPROVEMENT_FAILED"],
                source_repo=_RepoType.JARVIS,
                target_repo=None,
                payload={"op_id": msg.op_id, **msg.payload},
                timestamp=time.time(),
            )

        # HEARTBEAT and others are not bridged
        return None


class EventBridge:
    """Fault-isolated bridge from governance CommProtocol to CrossRepoEventBus.

    Can be used as a CommProtocol transport by calling :meth:`send`.
    """

    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    async def forward(self, msg: CommMessage) -> None:
        """Forward a governance message to the cross-repo event bus.

        Messages that don't map to cross-repo events are silently skipped.
        Event bus failures are logged but never propagated.
        """
        event = GovernanceEventMapper.map(msg)
        if event is None:
            return

        try:
            await self._event_bus.emit(event)
        except Exception as exc:
            logger.warning(
                "EventBridge: failed to emit event for op=%s: %s",
                msg.op_id, exc,
            )

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol-compatible transport interface."""
        await self.forward(msg)

"""CrossRepoNarrator: converts inbound CrossRepoEventBus events to CommProtocol narration.

Loop-safety note
----------------
EventBridge (a CommProtocol transport) emits events to the same CrossRepoEventBus that
CrossRepoNarrator listens on, always with ``source_repo == RepoType.JARVIS``.  To prevent
a reflexive loop (JARVIS emits → narrator narrates → EventBridge re-emits → …) each
handler checks the event source and silently discards events that originated from JARVIS
itself.  Only events from external repos (Prime, Reactor-Core, etc.) are narrated.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    from backend.core.ouroboros.cross_repo import CrossRepoEvent

logger = logging.getLogger(__name__)

# String value of the local repo as set in RepoType.JARVIS — used for loop-break guard.
_LOCAL_REPO_VALUE = "jarvis"


def _repo_value(event: "CrossRepoEvent") -> str:
    src = event.source_repo
    return src.value if hasattr(src, "value") else str(src)


class CrossRepoNarrator:
    """Handles inbound CrossRepoEventBus events and routes them to CommProtocol.

    Only events from EXTERNAL repos are narrated.  Events with
    ``source_repo == RepoType.JARVIS`` are silently skipped to prevent a
    reflexive loop through EventBridge.

    Usage::

        narrator = CrossRepoNarrator(comm=stack.comm)
        event_bus.register_handler(EventType.IMPROVEMENT_REQUEST, narrator.on_improvement_request)
        event_bus.register_handler(EventType.IMPROVEMENT_COMPLETE, narrator.on_improvement_complete)
        event_bus.register_handler(EventType.IMPROVEMENT_FAILED, narrator.on_improvement_failed)
    """

    def __init__(self, comm: "CommProtocol") -> None:
        self._comm = comm

    async def on_improvement_request(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_REQUEST: narrate that an external repo detected work."""
        try:
            repo = _repo_value(event)
            if repo == _LOCAL_REPO_VALUE:
                return  # own event re-entering from EventBridge — skip to break loop
            op_id = event.payload.get("op_id") or f"cross-{repo}-{uuid.uuid4().hex[:8]}"
            goal = event.payload.get("goal", "improvement detected")
            await self._comm.emit_intent(
                op_id=op_id,
                goal=f"[{repo}] {goal}",
                target_files=[],
                risk_tier="unknown",
                blast_radius=1,
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_request failed; swallowing")

    async def on_improvement_complete(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_COMPLETE: narrate that an external repo change was applied."""
        try:
            repo = _repo_value(event)
            if repo == _LOCAL_REPO_VALUE:
                return
            op_id = event.payload.get("op_id") or f"cross-{repo}-{uuid.uuid4().hex[:8]}"
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="cross_repo_applied",
                diff_summary=f"Change applied to {repo}",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_complete failed; swallowing")

    async def on_improvement_failed(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_FAILED: narrate that an external repo change failed."""
        try:
            repo = _repo_value(event)
            if repo == _LOCAL_REPO_VALUE:
                return
            op_id = event.payload.get("op_id") or f"cross-{repo}-{uuid.uuid4().hex[:8]}"
            reason = event.payload.get("reason_code", "unknown_failure")
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=reason,
                failed_phase="apply",
                next_safe_action="review_cross_repo_logs",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_failed failed; swallowing")

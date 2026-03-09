"""CrossRepoNarrator: converts inbound CrossRepoEventBus events to CommProtocol narration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    from backend.core.ouroboros.cross_repo import CrossRepoEvent

logger = logging.getLogger(__name__)


class CrossRepoNarrator:
    """Handles inbound CrossRepoEventBus events and routes them to CommProtocol.

    Usage::

        narrator = CrossRepoNarrator(comm=stack.comm)
        event_bus.register_handler(EventType.IMPROVEMENT_REQUEST, narrator.on_improvement_request)
        event_bus.register_handler(EventType.IMPROVEMENT_COMPLETE, narrator.on_improvement_complete)
        event_bus.register_handler(EventType.IMPROVEMENT_FAILED, narrator.on_improvement_failed)
    """

    def __init__(self, comm: "CommProtocol") -> None:
        self._comm = comm

    async def on_improvement_request(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_REQUEST: narrate that JARVIS detected work in a remote repo."""
        try:
            repo = event.source_repo.value if hasattr(event.source_repo, "value") else str(event.source_repo)
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
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
        """IMPROVEMENT_COMPLETE: narrate that a remote repo change was applied."""
        try:
            repo = event.source_repo.value if hasattr(event.source_repo, "value") else str(event.source_repo)
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
            await self._comm.emit_decision(
                op_id=op_id,
                outcome="applied",
                reason_code="cross_repo_applied",
                diff_summary=f"Change applied to {repo}",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_complete failed; swallowing")

    async def on_improvement_failed(self, event: "CrossRepoEvent") -> None:
        """IMPROVEMENT_FAILED: narrate that a remote repo change failed."""
        try:
            repo = event.source_repo.value if hasattr(event.source_repo, "value") else str(event.source_repo)
            op_id = event.payload.get("op_id", f"cross-{repo}-unknown")
            reason = event.payload.get("reason_code", "unknown_failure")
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause=reason,
                failed_phase="apply",
                next_safe_action="review_cross_repo_logs",
            )
        except Exception:
            logger.exception("[CrossRepoNarrator] on_improvement_failed failed; swallowing")

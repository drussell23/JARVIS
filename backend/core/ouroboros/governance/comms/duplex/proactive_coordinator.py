"""Proactive Cross-Space Coordinator — the FSM-tick binding.

Operator authorization 2026-07-19. The ONE seam that ties the whole
proactive chain onto the orchestrator's existing heartbeat cadence
(mandate 1 — no new poll loop; it rides the sanctioned Chronos
heartbeat the GovernedLoop already runs):

    tick ⇒ cross_space_gaze.tick(windows)         (ghost/dedup/decay/thermal)
         ⇒ proposal_queue.submit(proactive insights)
         ⇒ proposal_queue.evict_stale(current topology dhash)
         ⇒ present_if_idle → route through the EXISTING [Y/n] gate

``windows_source`` is injected (the daemon may be headless — a source
returning ``{}`` is honored and the coordinator is a silent no-op).
``present_sink`` receives an idle-gated proposal for the caller to
route through SerpentFlow's Iron Gate — this module NEVER mutates.

Master ``JARVIS_CROSSSPACE_PROACTIVE_ENABLED`` default OFF (§33.1: a
surface that proactively interrupts the operator graduates, it does
not default on). NEVER raises anywhere.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ProactiveCoordinator")

_TRUTHY = ("1", "true", "yes", "on")


def proactive_enabled() -> bool:
    """Master gate — default OFF. NEVER raises."""
    return os.environ.get(
        "JARVIS_CROSSSPACE_PROACTIVE_ENABLED", "",
    ).strip().lower() in _TRUTHY


class ProactiveCrossSpaceCoordinator:
    """Owns the gaze + queue; ``tick()`` is called BY the heartbeat.

    Collaborators injected (production defaults resolve the real
    CrossSpaceGaze + ProposalQueue). ``windows_source`` → per-Space
    window lists; ``present_sink`` → an idle-gated proposal callback.
    """

    def __init__(
        self,
        *,
        windows_source: Optional[Callable[[], Dict[int, List[dict]]]] = None,
        present_sink: Optional[Callable[[Any], None]] = None,
        gaze: Any = None,
        queue: Any = None,
    ) -> None:
        self._windows = windows_source or (lambda: {})
        self._present = present_sink or (lambda _p: None)
        self._gaze = gaze
        self._queue = queue
        self.stats: Dict[str, int] = {
            "ticks": 0, "submitted": 0, "presented": 0, "skipped_gate": 0,
        }

    def _get_gaze(self) -> Any:
        if self._gaze is None:
            from .cross_space_gaze import CrossSpaceGaze  # noqa: PLC0415
            self._gaze = CrossSpaceGaze()
        return self._gaze

    def _get_queue(self) -> Any:
        if self._queue is None:
            from .proposal_queue import ProposalQueue  # noqa: PLC0415
            self._queue = ProposalQueue()
        return self._queue

    def note_focus(self, space_id: int) -> None:
        """Orchestrator focus-change hook → the gaze's decay clock."""
        try:
            self._get_gaze().note_focus(space_id)
        except Exception:  # noqa: BLE001
            pass

    def tick(self) -> Dict[str, Any]:
        """One heartbeat step. Master-gated; a headless/empty desktop
        is a clean no-op. Returns a small telemetry dict. NEVER
        raises, NEVER blocks (all collaborators are sync/non-blocking
        by contract)."""
        try:
            if not proactive_enabled():
                self.stats["skipped_gate"] += 1
                return {"active": False, "reason": "gate_off"}
            self.stats["ticks"] += 1
            windows = self._windows() or {}
            gaze = self._get_gaze()
            result = gaze.tick(windows)
            queue = self._get_queue()
            # Spatial eviction keyed on the gaze's current topology hash
            # (the SAME dhash the gaze computed — DRY, no recompute).
            current_hash = getattr(gaze, "_last_hash", None)
            queue.evict_stale(current_dhash=current_hash)
            if result.get("synthesized") and current_hash:
                for insight in (result.get("proactive") or []):
                    spaces = gaze._insight_spaces(insight)
                    if queue.submit(insight, spaces, current_hash):
                        self.stats["submitted"] += 1
            # Idle-gated presentation (the queue owns the flow check).
            proposal = queue.present_if_idle()
            if proposal is not None:
                self.stats["presented"] += 1
                try:
                    self._present(proposal)
                except Exception:  # noqa: BLE001
                    logger.debug("[Proactive] present sink degraded",
                                 exc_info=True)
            return {
                "active": True, "queue_depth": queue.depth,
                "synthesized": bool(result.get("synthesized")),
                "presented": proposal is not None,
            }
        except Exception:  # noqa: BLE001
            logger.debug("[Proactive] tick degraded", exc_info=True)
            return {"active": False, "reason": "error"}


__all__ = ["ProactiveCrossSpaceCoordinator", "proactive_enabled"]

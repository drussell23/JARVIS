"""
HUD Relay Agent — Bus-to-IPC projection layer.

Bridges the AgentCommunicationBus to the native HUD process via IPC
(newline-delimited JSON on TCP port 8742).  Every outbound envelope
carries a monotonic ``_seq`` counter so the HUD can detect gaps and
reorder if needed.

The relay is resilient: if IPC is down the message is logged and
silently dropped — the bus must never stall on a display concern.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from backend.hive.thread_models import HiveMessage

logger = logging.getLogger(__name__)


async def _noop_sender(payload: Dict[str, Any]) -> None:  # pragma: no cover
    """Default no-op IPC sender used when no real transport is wired."""


class HudRelayAgent:
    """Projects Hive messages onto the IPC channel consumed by the HUD process.

    Parameters
    ----------
    ipc_send:
        An async callable ``(dict) -> None`` that delivers the serialised
        envelope to the HUD.  Defaults to a silent no-op so the relay can
        be instantiated before the IPC transport is ready.
    """

    def __init__(
        self,
        ipc_send: Optional[Callable[[Dict[str, Any]], Coroutine]] = None,
    ) -> None:
        self._ipc_send: Callable[[Dict[str, Any]], Coroutine] = (
            ipc_send or _noop_sender
        )
        self._seq: int = 0

    # ------------------------------------------------------------------
    # Sequence bookkeeping
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        """Increment and return the next monotonic sequence number."""
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    async def _safe_send(self, envelope: Dict[str, Any]) -> None:
        """Send *envelope* via IPC, swallowing any transport errors."""
        try:
            await self._ipc_send(envelope)
        except Exception:
            logger.warning(
                "HUD IPC send failed for seq=%s event_type=%s — continuing",
                envelope.get("data", {}).get("_seq"),
                envelope.get("event_type"),
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Public projection API
    # ------------------------------------------------------------------

    async def project_message(self, msg: HiveMessage) -> None:
        """Convert a :class:`HiveMessage` to an IPC envelope and send it.

        The envelope format is::

            {"event_type": "<msg.type>", "data": {<msg.to_dict()>, "_seq": N}}
        """
        payload = msg.to_dict()
        payload["_seq"] = self._next_seq()
        envelope: Dict[str, Any] = {
            "event_type": msg.type,
            "data": payload,
        }
        await self._safe_send(envelope)

    async def project_lifecycle(
        self,
        thread_id: str,
        state: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Project a thread-lifecycle event to the HUD.

        Parameters
        ----------
        thread_id:
            The thread whose lifecycle changed.
        state:
            New lifecycle state (e.g. ``"open"``, ``"resolved"``).
        metadata:
            Optional extra fields merged into the data payload.
        """
        data: Dict[str, Any] = {
            "thread_id": thread_id,
            "state": state,
            "_seq": self._next_seq(),
        }
        if metadata:
            data.update(metadata)
        envelope: Dict[str, Any] = {
            "event_type": "thread_lifecycle",
            "data": data,
        }
        await self._safe_send(envelope)

    async def project_cognitive_transition(
        self,
        from_state: str,
        to_state: str,
        reason_code: str,
    ) -> None:
        """Project a cognitive-state transition to the HUD.

        Parameters
        ----------
        from_state:
            Previous cognitive state.
        to_state:
            New cognitive state.
        reason_code:
            Machine-readable reason for the transition.
        """
        envelope: Dict[str, Any] = {
            "event_type": "cognitive_transition",
            "data": {
                "from_state": from_state,
                "to_state": to_state,
                "reason_code": reason_code,
                "_seq": self._next_seq(),
            },
        }
        await self._safe_send(envelope)

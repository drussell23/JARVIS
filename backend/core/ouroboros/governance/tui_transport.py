"""
TUI Transport for CommProtocol
===============================

A pluggable transport that delivers governance ``CommMessage`` objects to the
TUI dashboard.  Messages are:

1. Formatted into TUI-friendly dicts by :class:`TUIMessageFormatter`.
2. Delivered to registered async callbacks (if any).
3. Queued in memory if no callback is registered (TUI offline).
4. Fault-isolated: a crashing TUI callback never blocks the pipeline.

Usage::

    tui_transport = TUITransport()
    tui_transport.on_message(my_tui_display_callback)
    comm = CommProtocol(transports=[LogTransport(), tui_transport])

When the TUI reconnects, call ``await tui_transport.drain()`` to deliver
all queued messages.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage

logger = logging.getLogger("Ouroboros.TUITransport")


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TUIMessageFormatter:
    """Formats CommMessage objects into TUI-friendly dictionaries."""

    @staticmethod
    def format(msg: CommMessage) -> Dict[str, Any]:
        """Convert a CommMessage to a TUI-displayable dict.

        The returned dict always contains:
        - ``type``: The message type name (INTENT, PLAN, etc.)
        - ``op_id``: The operation identifier
        - ``seq``: Sequence number
        - ``causal_parent_seq``: Causal parent sequence number
        - ``timestamp``: Wall-clock timestamp

        Plus all payload fields merged in.
        """
        base: Dict[str, Any] = {
            "type": msg.msg_type.value,
            "op_id": msg.op_id,
            "seq": msg.seq,
            "causal_parent_seq": msg.causal_parent_seq,
            "timestamp": msg.timestamp,
        }
        # Merge payload fields into the base dict
        base.update(msg.payload)
        return base


# ---------------------------------------------------------------------------
# TUITransport
# ---------------------------------------------------------------------------


class TUITransport:
    """Fault-isolated transport that delivers governance messages to the TUI.

    Messages are stored in an internal list and optionally forwarded to
    registered async callbacks.  If a callback fails, the message is still
    stored (fault isolation).  When no callbacks are registered, formatted
    messages queue in ``_pending_drain`` for later delivery via
    :meth:`drain`.
    """

    def __init__(self) -> None:
        self.messages: List[CommMessage] = []
        self._callbacks: List[Callable[[Dict[str, Any]], Any]] = []
        self._pending_drain: List[Dict[str, Any]] = []

    def on_message(
        self,
        callback: Callable[[Dict[str, Any]], Any],
    ) -> None:
        """Register an async callback to receive formatted messages.

        Parameters
        ----------
        callback:
            An async callable that receives a formatted message dict.
        """
        self._callbacks.append(callback)

    async def send(self, msg: CommMessage) -> None:
        """Store the message and forward to registered callbacks.

        Callback failures are logged but never block message storage.
        If no callbacks are registered, the formatted message is queued
        in ``_pending_drain`` for later delivery via :meth:`drain`.
        """
        self.messages.append(msg)
        formatted = TUIMessageFormatter.format(msg)

        if not self._callbacks:
            # Queue for later drain
            self._pending_drain.append(formatted)
            return

        for callback in self._callbacks:
            try:
                result = callback(formatted)
                # Support both sync and async callbacks
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.warning(
                    "TUI callback failed for op=%s seq=%d -- message still queued",
                    msg.op_id,
                    msg.seq,
                    exc_info=True,
                )

    async def drain(self) -> None:
        """Deliver all pending (queued while offline) messages to callbacks.

        If no callbacks are registered, this method is a no-op and pending
        messages are preserved.  Once drained, the pending queue is cleared.
        """
        if not self._callbacks or not self._pending_drain:
            return

        pending = list(self._pending_drain)
        self._pending_drain.clear()

        for formatted in pending:
            for callback in self._callbacks:
                try:
                    result = callback(formatted)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.warning(
                        "TUI callback failed during drain -- skipping",
                        exc_info=True,
                    )

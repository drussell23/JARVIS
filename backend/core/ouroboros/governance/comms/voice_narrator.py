"""backend/core/ouroboros/governance/comms/voice_narrator.py

CommProtocol transport that narrates pipeline events via speech.
Subscribes to INTENT, DECISION, POSTMORTEM messages. Skips HEARTBEAT and PLAN.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Coroutine

from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType

from .narrator_script import format_narration

logger = logging.getLogger(__name__)

# Message types that trigger narration
_NARRATE_TYPES = {MessageType.INTENT, MessageType.DECISION, MessageType.POSTMORTEM}

# Bounded LRU cap for idempotency tracking (~16h at 20 ops/day * 3 types)
_MAX_NARRATED_IDS = 1000


class VoiceNarrator:
    """CommProtocol transport that narrates pipeline events via safe_say()."""

    def __init__(
        self,
        say_fn: Callable[..., Coroutine[Any, Any, bool]],
        debounce_s: float = 60.0,
        source: str = "intent_engine",
    ) -> None:
        self._say_fn = say_fn
        self._debounce_s = debounce_s
        self._source = source
        self._last_narration: float = float("-inf")  # monotonic; -inf so first msg always passes
        self._narrated_ids: OrderedDict[str, None] = OrderedDict()  # bounded LRU for idempotency

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface. Called for every pipeline message."""
        if msg.msg_type not in _NARRATE_TYPES:
            return

        # Idempotency: don't repeat same op_id + msg_type
        notification_id = hashlib.sha256(
            f"{msg.op_id}:{msg.msg_type.name}".encode()
        ).hexdigest()[:12]
        if notification_id in self._narrated_ids:
            return

        # Debounce: only throttle INTENT — DECISION and POSTMORTEM always narrate
        # (a suppressed failure is a P0 silent-killer)
        now = time.monotonic()
        if msg.msg_type == MessageType.INTENT:
            if (now - self._last_narration) < self._debounce_s:
                return

        # Build narration text
        phase = self._map_phase(msg)
        context = dict(msg.payload)
        context["op_id"] = msg.op_id
        # Extract file from target_files if present
        target_files = context.get("target_files", [])
        if target_files and isinstance(target_files, (list, tuple)):
            context.setdefault("file", target_files[0])

        text = format_narration(phase, context)
        if text is None:
            logger.debug("VoiceNarrator: suppressed narration for op %s (incomplete context)", msg.op_id)
            return

        try:
            await self._say_fn(text, source=self._source)
            # Mark as narrated only on success (so failed TTS can retry)
            self._narrated_ids[notification_id] = None
            if len(self._narrated_ids) > _MAX_NARRATED_IDS:
                self._narrated_ids.popitem(last=False)  # evict oldest
            self._last_narration = now
        except Exception:
            logger.debug("VoiceNarrator: say_fn failed for op %s", msg.op_id)

    @staticmethod
    def _map_phase(msg: CommMessage) -> str:
        """Map CommMessage type + payload to narrator script phase."""
        if msg.msg_type == MessageType.INTENT:
            return "signal_detected"
        elif msg.msg_type == MessageType.POSTMORTEM:
            return "postmortem"
        elif msg.msg_type == MessageType.DECISION:
            outcome = msg.payload.get("outcome", "")
            if outcome in ("applied", "validated"):
                return "applied"
            elif outcome == "blocked":
                return "approve"
            else:
                return "applied"
        return "signal_detected"

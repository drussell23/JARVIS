"""backend/core/ouroboros/governance/comms/voice_narrator.py

CommProtocol transport that narrates pipeline events via speech.
Subscribes to INTENT, DECISION, POSTMORTEM messages. Skips HEARTBEAT and PLAN.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Callable, Coroutine, Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType

from .duplex.protocols import Priority, SpeechRequest
from .karen_synth.ledger_view import LedgerView

logger = logging.getLogger(__name__)

# Message types that trigger narration
_NARRATE_TYPES = {MessageType.INTENT, MessageType.DECISION, MessageType.POSTMORTEM}

# Bounded LRU cap for idempotency tracking (~16h at 20 ops/day * 3 types)
_MAX_NARRATED_IDS = 1000

# P2-1: Bounded narration queue — prevents TTS backlog under burst load.
# DROP_OLDEST policy: newer events (DECISION, POSTMORTEM) displace stale INTENTs.
_NARRATE_QUEUE_MAXSIZE = 50


def _karen_synth_enabled() -> bool:
    """Read at call time (not cached) so tests/ops can toggle live."""
    raw = os.environ.get("JARVIS_KAREN_SYNTH_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class VoiceNarrator:
    """CommProtocol transport that narrates pipeline events via safe_say().

    Respects OUROBOROS_NARRATOR_ENABLED env var (shared with DaemonNarrator).
    When disabled, on_message() returns immediately without speech.
    """

    def __init__(
        self,
        say_fn: Callable[..., Coroutine[Any, Any, bool]],
        debounce_s: float = 60.0,
        source: str = "intent_engine",
        voice: str = "Karen",
        synthesizer: Optional[Any] = None,
        arbiter: Optional[Any] = None,
    ) -> None:
        self._enabled = os.environ.get("OUROBOROS_NARRATOR_ENABLED", "true").lower() in ("true", "1", "yes")
        self._say_fn = say_fn
        self._debounce_s = debounce_s
        self._source = source
        self._voice = voice
        # Sprint 2 (Karen Full-Duplex Voice): dynamic synthesis replaces the
        # eradicated static template module. Both are optional injections —
        # production wiring (integration.py) leaves them unset until a
        # DW-backed synthesizer + arbiter are constructed there (follow-up).
        self._synthesizer = synthesizer
        self._arbiter = arbiter
        self._last_narration: float = float("-inf")  # monotonic; -inf so first msg always passes
        self._narrated_ids: OrderedDict[str, None] = OrderedDict()  # bounded LRU for idempotency
        if not self._enabled:
            logger.info("[VoiceNarrator] DISABLED via OUROBOROS_NARRATOR_ENABLED=false")
        # P2-1: internal bounded queue + lazy drain worker
        self._narrate_queue: "asyncio.Queue[CommMessage]" = asyncio.Queue(
            maxsize=_NARRATE_QUEUE_MAXSIZE
        )
        self._drain_task: Optional["asyncio.Task[None]"] = None
        self._shed_count: int = 0

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface.

        Non-blocking: enqueues the message for background narration.
        Returns immediately so downstream transports are not stalled by TTS.
        If the queue is full, the oldest pending message is shed (DROP_OLDEST).
        """
        if not self._enabled:
            return

        if msg.msg_type not in _NARRATE_TYPES:
            return

        # Fast idempotency pre-check before enqueue (saves queue slot for dupes)
        notification_id = hashlib.sha256(
            f"{msg.op_id}:{msg.msg_type.name}".encode()
        ).hexdigest()[:12]
        if notification_id in self._narrated_ids:
            return

        # NOTE: Debounce is checked at DEQUEUE time (in _narrate_one) using the
        # up-to-date _last_narration.  Checking here would allow rapid sends to
        # slip through before any TTS completes (race with drain worker).

        # P2-1: Enqueue with DROP_OLDEST shedding; start drain worker on first use
        self._ensure_drain_started()
        if self._narrate_queue.full():
            try:
                self._narrate_queue.get_nowait()
                self._shed_count += 1
                logger.debug(
                    "VoiceNarrator: queue full — shed oldest (total_shed=%d)",
                    self._shed_count,
                )
            except asyncio.QueueEmpty:
                pass
        try:
            self._narrate_queue.put_nowait(msg)
        except asyncio.QueueFull:
            self._shed_count += 1

    async def drain(self) -> None:
        """Wait until all enqueued narration messages have been processed.

        Useful in tests and graceful-shutdown paths to ensure no messages
        are silently dropped before the drain loop is cancelled.
        """
        if not self._narrate_queue.empty():
            await self._narrate_queue.join()

    def _ensure_drain_started(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.ensure_future(self._drain_loop())

    async def _drain_loop(self) -> None:
        """Background worker: dequeue and narrate one message at a time."""
        while True:
            try:
                msg = await self._narrate_queue.get()
                try:
                    await self._narrate_one(msg)
                except Exception:
                    logger.debug(
                        "VoiceNarrator: drain error for op %s", msg.op_id
                    )
                finally:
                    self._narrate_queue.task_done()
            except asyncio.CancelledError:
                break

    async def _narrate_one(self, msg: CommMessage) -> None:
        """Drive dynamic Karen speech synthesis for a single message (called
        from drain loop). Fault-isolated: never raises into the transport —
        any failure here is swallowed and logged at debug level.

        Sprint 2: the static template module is eradicated. When a
        synthesizer + arbiter are injected AND JARVIS_KAREN_SYNTH_ENABLED is
        truthy, this builds a LedgerView from the message, fires a local
        filler to mask synth latency, then streams synthesized sentences into
        the arbiter. Otherwise this is a silent no-op — there is no template
        fallback.
        """
        notification_id = hashlib.sha256(
            f"{msg.op_id}:{msg.msg_type.name}".encode()
        ).hexdigest()[:12]
        if notification_id in self._narrated_ids:
            return

        # Debounce check at dequeue time — uses current _last_narration which
        # reflects the most recent successful narration from this drain loop.
        if msg.msg_type == MessageType.INTENT:
            if (time.monotonic() - self._last_narration) < self._debounce_s:
                return

        phase = self._map_phase(msg)
        context = dict(msg.payload)
        context["op_id"] = msg.op_id
        target_files = context.get("target_files", [])
        if target_files and isinstance(target_files, (list, tuple)):
            context.setdefault("file", target_files[0])

        if self._synthesizer is None or self._arbiter is None or not _karen_synth_enabled():
            logger.debug(
                "VoiceNarrator: synth path unavailable/disabled for op %s (phase=%s)",
                msg.op_id, phase,
            )
            return

        try:
            view = LedgerView.from_payload(phase, context)
            self._arbiter.fire_filler()
            spoke_any = False
            async for sentence in self._synthesizer.synthesize(view):
                self._arbiter.submit(SpeechRequest(sentence, Priority.PROACTIVE_INFO))
                spoke_any = True
            if spoke_any:
                self._narrated_ids[notification_id] = None
                if len(self._narrated_ids) > _MAX_NARRATED_IDS:
                    self._narrated_ids.popitem(last=False)
                self._last_narration = time.monotonic()
        except Exception:
            logger.debug("VoiceNarrator: synthesis failed for op %s", msg.op_id)

    @staticmethod
    def _map_phase(msg: CommMessage) -> str:
        """Map CommMessage type + payload to narrator script phase."""
        if msg.msg_type == MessageType.INTENT:
            return "signal_detected"
        elif msg.msg_type == MessageType.POSTMORTEM:
            root_cause = msg.payload.get("root_cause", "")
            if isinstance(root_cause, str) and root_cause.startswith("verify_regression"):
                return "verify_regression"
            return "postmortem"
        elif msg.msg_type == MessageType.DECISION:
            reason = msg.payload.get("reason_code", "")
            if reason == "duplication":
                return "duplication_blocked"
            if reason == "similarity_escalation":
                return "similarity_escalated"
            outcome = msg.payload.get("outcome", "")
            if outcome in ("applied", "validated"):
                return "applied"
            elif outcome == "blocked":
                return "approve"
            else:
                return "applied"
        return "signal_detected"

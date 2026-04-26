"""P2 Slice 2 — ConversationOrchestrator + ChatSession.

Per OUROBOROS_VENOM_PRD.md §9 Phase 3 P2:

  > SerpentFlow gets a real REPL conversational mode. Operator types
  > natural language → routed through a new ConversationOrchestrator
  > that:
  >   1. Classifies intent (do-this-now vs explore-this vs
  >      explain-that)
  >   2. For do-this-now: synthesizes a backlog entry on the fly +
  >      dispatches
  >   3. For explore-this: spawns a read-only subagent
  >   4. For explain-that: directly queries Claude with relevant
  >      context
  >   5. All conversational turns feed ConversationBridge buffer
  >      (already-built primitive)

This module is the **routing dispatch + per-session memory** layer. It
turns Slice 1's :class:`IntentClassification` into a
:class:`ChatRoutingDecision` describing *what* the orchestrator
*would* do, without actually emitting backlog entries / spawning
subagents / calling Claude. Slice 3 wires those side effects via the
SerpentFlow REPL surface; Slice 4 graduates the master flag.

Multi-turn context preservation:
  * ``ChatSession`` keeps a bounded ring buffer of the last
    ``MAX_TURNS_PER_SESSION`` turns so the orchestrator can reach
    back when a CONTEXT_PASTE arrives — the paste attaches to the
    *previous turn's* intent rather than triggering a fresh
    classification (PRD §9 P2 edge-case spec).
  * The bridge feed is best-effort — failures never break the loop.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: ``intent_classifier`` (own slice) + ``conversation_bridge``
    (already-built primitive). No subprocess / file I/O / env mutation.
  * Best-effort — bridge feed wrapped in ``try / except``; classifier
    is pure-data so it can't raise; the dispatch path is allocation-
    only (no I/O).
  * Bounded — ``MAX_TURNS_PER_SESSION`` + ``MAX_SESSIONS_TRACKED``
    keep memory finite even for a long-running daemon.
  * Default-off behind ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` (Slice 4
    graduates). Module is importable / callable; gating happens at the
    Slice 3 caller.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intent_classifier import (
    ChatIntent,
    IntentClassification,
    classify,
)

logger = logging.getLogger(__name__)


# Per-session ring-buffer cap. Long conversations stay observable
# without unbounded memory.
MAX_TURNS_PER_SESSION: int = 32

# Process-wide session cap. FIFO eviction at this size so a long
# daemon session that creates many session_ids can't accumulate.
MAX_SESSIONS_TRACKED: int = 16

# Bounded reason / payload caps so a runaway message can't pin the
# bridge or REPL renderer.
MAX_REASON_CHARS: int = 240
MAX_PAYLOAD_TEXT_CHARS: int = 4096


# ---------------------------------------------------------------------------
# Routing decision shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatRoutingDecision:
    """Slice 2 emits this; Slice 3 renders + Slice 3 wires the actual
    backlog / subagent / Claude side effect.

    ``action`` is one of:
      * ``"backlog_dispatch"`` — Slice 3 should synthesize a backlog
        entry from ``payload['message']`` and submit via the existing
        backlog ingestion path.
      * ``"subagent_explore"`` — Slice 3 should spawn a read-only
        subagent with ``payload['message']`` as the task.
      * ``"claude_query"`` — Slice 3 should query Claude with
        ``payload['message']`` + recent session turns as context.
      * ``"context_attach"`` — Slice 3 should append
        ``payload['message']`` to the previous turn's payload (the
        operator pasted code/error related to the last request).
        ``target_turn_id`` carries the previous turn's id; ``None``
        when there is no previous turn (degraded — the renderer
        should treat as a fresh EXPLANATION).
      * ``"noop"`` — empty / whitespace input; no action.
    """

    action: str
    intent: ChatIntent
    confidence: float
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    payload: Dict[str, str] = field(default_factory=dict)
    reason: str = ""
    target_turn_id: Optional[str] = None
    truncated: bool = False


# ---------------------------------------------------------------------------
# Per-turn record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatTurn:
    """One operator message + the orchestrator's verdict. Frozen for
    audit-trail integrity. Slice 3 may attach a response field via
    ``with_response`` (returns a new instance)."""

    turn_id: str
    session_id: str
    operator_message: str
    classification: IntentClassification
    decision: ChatRoutingDecision
    created_unix: float
    response_text: str = ""

    def with_response(self, text: str) -> "ChatTurn":
        """Return a copy with ``response_text`` set. Frozen → cannot
        mutate in place."""
        return ChatTurn(
            turn_id=self.turn_id,
            session_id=self.session_id,
            operator_message=self.operator_message,
            classification=self.classification,
            decision=self.decision,
            created_unix=self.created_unix,
            response_text=str(text)[:MAX_PAYLOAD_TEXT_CHARS],
        )


# ---------------------------------------------------------------------------
# Per-session ring buffer
# ---------------------------------------------------------------------------


@dataclass
class ChatSession:
    """One operator chat session — bounded ring buffer of recent turns.

    Mutable container; thread-safe via the orchestrator's lock when
    accessed through :class:`ConversationOrchestrator`. Direct
    construction is permitted for tests + Slice 3 REPL state."""

    session_id: str
    turns: Deque[ChatTurn] = field(
        default_factory=lambda: deque(maxlen=MAX_TURNS_PER_SESSION),
    )
    created_unix: float = 0.0

    def append(self, turn: ChatTurn) -> None:
        self.turns.append(turn)

    def previous_turn(self) -> Optional[ChatTurn]:
        """Most recent turn before the one being added, or None."""
        if not self.turns:
            return None
        return self.turns[-1]

    def snapshot(self) -> List[ChatTurn]:
        return list(self.turns)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ConversationOrchestrator:
    """Routes one operator message through the classifier and into a
    :class:`ChatRoutingDecision`. Bounded per-session memory; thread-
    safe via a single coarse lock (mirrors the P3 queue pattern).

    Public surface (Slice 2):
      * ``dispatch(session_id, message)`` — main entry; returns a
        ``(ChatTurn, ChatRoutingDecision)`` tuple.
      * ``get_session(session_id)`` — read-only snapshot.
      * ``forget(session_id)`` — drop one session.
      * ``known_session_ids()`` — for /chat REPL + telemetry.
      * ``record_response(turn_id, text)`` — Slice 3 calls when the
        Claude / subagent response lands; updates the stored turn.

    Bridge integration:
      * On each ``dispatch``, the operator message is fed into the
        provided ``conversation_bridge`` (default: process-wide
        singleton). Bridge gating is the bridge's responsibility —
        we always offer the turn; the bridge decides admission.
    """

    def __init__(
        self,
        conversation_bridge=None,
        clock=time.time,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, ChatSession] = {}
        self._turn_index: Dict[str, ChatTurn] = {}
        self._bridge = conversation_bridge
        self._clock = clock

    # ---- public API ----

    def dispatch(
        self,
        message: str,
        session_id: str = "default",
    ) -> Tuple[ChatTurn, ChatRoutingDecision]:
        """Classify ``message`` and emit a :class:`ChatRoutingDecision`.

        Multi-turn behaviour:
          * For non-paste verdicts, the decision is a fresh route.
          * For ``CONTEXT_PASTE`` verdicts, the decision attaches to
            the *previous turn* in the session (per PRD §9 P2 edge
            case): the operator pasted code related to the last
            request, not a brand-new ask.
          * For empty / whitespace input, the decision is ``"noop"``
            so the renderer can short-circuit without consuming
            session capacity.
        """
        verdict = classify(message)
        now = self._clock()
        turn_id = self._mint_turn_id()

        with self._lock:
            session = self._get_or_create_session(session_id, now)
            previous = session.previous_turn()

            decision = self._build_decision(
                message=message,
                verdict=verdict,
                previous=previous,
            )

            turn = ChatTurn(
                turn_id=turn_id,
                session_id=session_id,
                operator_message=str(message or "")[:MAX_PAYLOAD_TEXT_CHARS],
                classification=verdict,
                decision=decision,
                created_unix=now,
            )
            # Noop turns don't consume ring-buffer capacity — they're
            # essentially "empty enter key" — but we still index them
            # so a Slice 3 renderer can find the verdict by id.
            if decision.action != "noop":
                session.append(turn)
            self._turn_index[turn_id] = turn

        # Bridge feed is best-effort, OUTSIDE the lock so a slow
        # bridge doesn't pin orchestrator dispatch.
        self._feed_bridge(turn)
        return turn, decision

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def get_turn(self, turn_id: str) -> Optional[ChatTurn]:
        with self._lock:
            return self._turn_index.get(turn_id)

    def forget(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            for t in session.turns:
                self._turn_index.pop(t.turn_id, None)
            return True

    def known_session_ids(self) -> List[str]:
        with self._lock:
            return list(self._sessions.keys())

    def record_response(self, turn_id: str, response_text: str) -> bool:
        """Slice 3 calls this when the Claude / subagent response
        lands. Updates the indexed turn + the session ring entry.
        Idempotent — overwrites prior response."""
        with self._lock:
            turn = self._turn_index.get(turn_id)
            if turn is None:
                return False
            updated = turn.with_response(response_text)
            self._turn_index[turn_id] = updated
            session = self._sessions.get(turn.session_id)
            if session is not None:
                # Replace within the deque (find by id; deque doesn't
                # support direct index by predicate, so rebuild).
                rebuilt = deque(
                    (updated if t.turn_id == turn_id else t)
                    for t in session.turns
                )
                rebuilt = deque(
                    rebuilt, maxlen=session.turns.maxlen,
                )
                session.turns = rebuilt
            return True

    # ---- internals ----

    def _mint_turn_id(self) -> str:
        return f"chat-{uuid.uuid4().hex[:12]}"

    def _get_or_create_session(
        self, session_id: str, now: float,
    ) -> ChatSession:
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        if len(self._sessions) >= MAX_SESSIONS_TRACKED:
            # FIFO eviction of the oldest session.
            try:
                oldest = next(iter(self._sessions))
                evicted = self._sessions.pop(oldest, None)
                if evicted is not None:
                    for t in evicted.turns:
                        self._turn_index.pop(t.turn_id, None)
            except StopIteration:
                pass
        session = ChatSession(
            session_id=session_id, created_unix=now,
        )
        self._sessions[session_id] = session
        return session

    def _build_decision(
        self,
        *,
        message: str,
        verdict: IntentClassification,
        previous: Optional[ChatTurn],
    ) -> ChatRoutingDecision:
        msg = str(message or "")
        clipped = msg[:MAX_PAYLOAD_TEXT_CHARS]

        # Empty / whitespace → noop.
        if not msg.strip():
            return ChatRoutingDecision(
                action="noop", intent=verdict.intent,
                confidence=verdict.confidence,
                reasons=verdict.reasons,
                payload={},
                reason="empty input",
                truncated=verdict.truncated,
            )

        if verdict.intent is ChatIntent.CONTEXT_PASTE:
            target_id = previous.turn_id if previous is not None else None
            return ChatRoutingDecision(
                action="context_attach",
                intent=verdict.intent,
                confidence=verdict.confidence,
                reasons=verdict.reasons,
                payload={"message": clipped},
                reason=self._truncate_reason(
                    "paste attached to previous turn"
                    if target_id is not None
                    else "paste with no prior turn — degraded to fresh"
                ),
                target_turn_id=target_id,
                truncated=verdict.truncated,
            )

        if verdict.intent is ChatIntent.ACTION_REQUEST:
            return ChatRoutingDecision(
                action="backlog_dispatch",
                intent=verdict.intent,
                confidence=verdict.confidence,
                reasons=verdict.reasons,
                payload={"message": clipped},
                reason=self._truncate_reason(
                    f"action verb match (conf={verdict.confidence:.2f})",
                ),
                truncated=verdict.truncated,
            )

        if verdict.intent is ChatIntent.EXPLORATION:
            return ChatRoutingDecision(
                action="subagent_explore",
                intent=verdict.intent,
                confidence=verdict.confidence,
                reasons=verdict.reasons,
                payload={"message": clipped},
                reason=self._truncate_reason(
                    f"exploration verb match (conf={verdict.confidence:.2f})",
                ),
                truncated=verdict.truncated,
            )

        # EXPLANATION default.
        return ChatRoutingDecision(
            action="claude_query",
            intent=verdict.intent,
            confidence=verdict.confidence,
            reasons=verdict.reasons,
            payload={"message": clipped},
            reason=self._truncate_reason(
                f"explanation verb / question shape "
                f"(conf={verdict.confidence:.2f})",
            ),
            truncated=verdict.truncated,
        )

    @staticmethod
    def _truncate_reason(text: str) -> str:
        if len(text) <= MAX_REASON_CHARS:
            return text
        return text[: MAX_REASON_CHARS - 3] + "..."

    def _feed_bridge(self, turn: ChatTurn) -> None:
        """Best-effort feed into ConversationBridge. Failures swallowed
        so a misconfigured bridge never breaks the orchestrator."""
        bridge = self._bridge
        if bridge is None:
            try:
                from backend.core.ouroboros.governance.conversation_bridge import (
                    get_default_bridge,
                )
                bridge = get_default_bridge()
            except Exception:
                return
        try:
            bridge.record_turn(
                role="user",
                text=turn.operator_message,
                source="tui_user",
                op_id=turn.turn_id,
            )
        except Exception as exc:
            logger.warning(
                "[ConversationOrchestrator] bridge feed failed: %s", exc,
            )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_orchestrator: Optional[ConversationOrchestrator] = None
_default_lock = threading.Lock()


def get_default_orchestrator() -> ConversationOrchestrator:
    """Process-wide orchestrator. Lazy-construct on first call.

    No master flag on the accessor — the orchestrator is callable +
    queryable even when ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` is off
    so operators can inspect prior dispatch decisions after a revert
    (mirrors the P3 ``get_default_queue`` pattern)."""
    global _default_orchestrator
    with _default_lock:
        if _default_orchestrator is None:
            _default_orchestrator = ConversationOrchestrator()
    return _default_orchestrator


def reset_default_orchestrator() -> None:
    """Reset the singleton — for tests."""
    global _default_orchestrator
    with _default_lock:
        _default_orchestrator = None


__all__ = [
    "ChatRoutingDecision",
    "ChatSession",
    "ChatTurn",
    "ConversationOrchestrator",
    "MAX_PAYLOAD_TEXT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_SESSIONS_TRACKED",
    "MAX_TURNS_PER_SESSION",
    "get_default_orchestrator",
    "reset_default_orchestrator",
]

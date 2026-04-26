"""P2 Slice 3 — /chat REPL dispatcher + decision rendering + executor protocol.

Per OUROBOROS_VENOM_PRD.md §9 Phase 3 P2:

  > New REPL command: ``/chat <message>`` (or just bare text in
  > interactive mode)
  > ConversationOrchestrator routes appropriately, returns response +
  > any spawned ops

This module is the **REPL surface** layer for the conversational
mode. It parses operator input lines, dispatches through Slice 2's
:class:`ConversationOrchestrator`, renders the
:class:`ChatRoutingDecision` to ASCII text, and exposes a hook for a
Slice 4 caller to commit the actual side effect (backlog dispatch /
subagent spawn / Claude query) via :class:`ChatActionExecutor`.

Slice 3 ships **NO default executor** — the dispatcher returns the
decision + rendered text only. Slice 4 graduation wires a real
executor against the live FSM. This split keeps Slice 3:

  * Authority-clean (no orchestrator / policy / etc imports).
  * Unit-testable end-to-end (no mocks of the live FSM needed).
  * Hot-revert-friendly (master-off → SerpentFlow doesn't even
    construct a dispatcher).

Subcommands (parsed after a leading ``/chat`` token):

  * ``/chat <message>``      — dispatch as a new turn.
  * ``/chat history [N]``    — list last N turns in the session
                               (default 10, max 32 = ring cap).
  * ``/chat why <turn-id>``  — show the verdict reasons for a turn.
  * ``/chat clear``          — forget the current session.
  * ``/chat help``           — list subcommands.

Bare text (no ``/chat`` prefix) is treated as a fresh ``/chat
<message>`` — the SerpentFlow caller can decide whether to admit
bare lines as chat (i.e. operator opted into conversational mode)
or pass them through to the regular slash-command parser.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * No subprocess / file I/O / env mutation / network libs.
  * Best-effort — every executor call is wrapped in ``try/except``
    so a broken executor cannot break the REPL surface.
  * ASCII-only rendering — pinned by tests (mirrors P3 inline
    approval renderer).
  * Master flag default-off until Slice 4 graduation; module is
    importable + callable so tests + telemetry remain dormant-safe.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Protocol

from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatRoutingDecision,
    ChatTurn,
    ConversationOrchestrator,
    get_default_orchestrator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Subcommand defaults. ``HISTORY_DEFAULT_N`` is what /chat history
# returns when no count is given; capped at the orchestrator's ring
# size so the REPL never asks for more than is held.
HISTORY_DEFAULT_N: int = 10
HISTORY_MAX_N: int = 32  # mirrors MAX_TURNS_PER_SESSION

# Cap on rendered output bytes per call so a runaway summary can't
# saturate the SerpentFlow pane.
MAX_RENDERED_BYTES: int = 16 * 1024  # 16 KiB

# Default chat session id. SerpentFlow may pass a per-pane id; the
# dispatcher itself defaults to a stable string so single-session
# REPL use is zero-config.
DEFAULT_SESSION_ID: str = "repl"


def is_enabled() -> bool:
    """Master flag — ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` (default
    false until Slice 4 graduation).

    SerpentFlow is the gating caller — when off, the REPL doesn't even
    construct the dispatcher. This module's behaviour does not change
    based on the flag; the helper is exported for SerpentFlow's
    convenience + symmetry with the P3 renderer pattern."""
    return os.environ.get(
        "JARVIS_CONVERSATIONAL_MODE_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class ChatReplStatus(str, enum.Enum):
    DISPATCHED = "DISPATCHED"          # message routed; decision + rendered_text valid
    SUBCOMMAND = "SUBCOMMAND"          # history/why/clear/help; decision is None
    EMPTY = "EMPTY"                    # blank input; nothing happened
    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    UNKNOWN_TURN = "UNKNOWN_TURN"      # /chat why <id> with bad id
    EXECUTOR_FAILED = "EXECUTOR_FAILED"  # action raised; rendered_text describes
    EXECUTOR_OK = "EXECUTOR_OK"        # action committed; rendered_text describes


@dataclass(frozen=True)
class ChatReplResult:
    """Bundle returned to the SerpentFlow caller.

    ``rendered_text`` is what the operator should see in the pane.
    ``decision`` is populated for DISPATCHED + EXECUTOR_OK +
    EXECUTOR_FAILED; ``None`` for subcommands.
    ``executor_response`` is whatever the executor returned (Slice 4
    will pass back e.g. backlog op_id, subagent task_id, Claude
    response text)."""

    status: ChatReplStatus
    rendered_text: str
    decision: Optional[ChatRoutingDecision] = None
    turn: Optional[ChatTurn] = None
    executor_response: Optional[str] = None


# ---------------------------------------------------------------------------
# Executor protocol (Slice 4 wires the real implementation)
# ---------------------------------------------------------------------------


class ChatActionExecutor(Protocol):
    """Protocol for the side-effecting layer.

    Slice 4 will wire concrete implementations against the existing
    backlog ingestion / subagent_scheduler / Claude provider. Each
    method MUST return a short status string (or raise on failure —
    the dispatcher catches and renders the exception)."""

    def dispatch_backlog(self, message: str, turn: ChatTurn) -> str: ...

    def spawn_subagent(self, message: str, turn: ChatTurn) -> str: ...

    def query_claude(
        self,
        message: str,
        turn: ChatTurn,
        recent_turns: List[ChatTurn],
    ) -> str: ...

    def attach_context(
        self,
        message: str,
        turn: ChatTurn,
        target_turn: ChatTurn,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_decision(
    turn: ChatTurn,
    decision: ChatRoutingDecision,
    *,
    indent: str = "  ",
) -> str:
    """Render a :class:`ChatRoutingDecision` to ASCII text.

    Mirrors the P3 renderer's ASCII-strict contract so the chat pane
    behaves identically on strict-ASCII terminals."""
    lines = [
        f"[chat] turn={turn.turn_id} session={turn.session_id}",
        f"{indent}intent: {decision.intent.name} "
        f"(conf={decision.confidence:.2f})",
        f"{indent}action: {decision.action}",
    ]
    if decision.reasons:
        lines.append(f"{indent}reasons: {', '.join(decision.reasons)}")
    if decision.target_turn_id:
        lines.append(f"{indent}attaches-to: {decision.target_turn_id}")
    if decision.payload.get("message"):
        msg = decision.payload["message"]
        clipped = msg if len(msg) <= 200 else msg[:200] + "..."
        lines.append(f"{indent}message: {clipped}")
    if decision.reason:
        lines.append(f"{indent}reason: {decision.reason}")
    if decision.truncated:
        lines.append(f"{indent}(input was truncated to MAX_MESSAGE_CHARS)")
    return _clip(_ascii_safe("\n".join(lines)))


def render_history(turns: List[ChatTurn], session_id: str) -> str:
    """Render a session history list."""
    if not turns:
        return f"[chat] history (session={session_id}): (empty)"
    lines = [f"[chat] history (session={session_id}, turns={len(turns)}):"]
    for i, t in enumerate(turns, 1):
        d = t.decision
        msg = (
            t.operator_message[:60] + "..."
            if len(t.operator_message) > 60
            else t.operator_message
        )
        lines.append(
            f"  {i:2d}. [{t.turn_id}] {d.intent.name:<14s} "
            f"{d.action:<18s} {msg!r}"
        )
    return _clip(_ascii_safe("\n".join(lines)))


def render_why(turn: ChatTurn) -> str:
    d = turn.decision
    c = turn.classification
    lines = [
        f"[chat] why turn={turn.turn_id}",
        f"  message:    {turn.operator_message[:200]!r}",
        f"  intent:     {d.intent.name} (conf={d.confidence:.2f})",
        f"  action:     {d.action}",
        f"  reasons:    {', '.join(c.reasons) or '(none)'}",
        f"  reason:     {d.reason or '(none)'}",
    ]
    if d.target_turn_id:
        lines.append(f"  attached-to: {d.target_turn_id}")
    if c.truncated:
        lines.append("  (input was truncated)")
    return _clip(_ascii_safe("\n".join(lines)))


def render_help() -> str:
    return _clip(_ascii_safe("\n".join([
        "[chat] /chat subcommands:",
        "  /chat <message>     dispatch a new turn",
        "  /chat history [N]   show last N turns (default 10, max 32)",
        "  /chat why <turn-id> show verdict reasons for a turn",
        "  /chat clear         forget current session",
        "  /chat help          this listing",
    ])))


def _ascii_safe(text: str) -> str:
    """Drop any non-ASCII codepoints. Pinned by tests."""
    return text.encode("ascii", errors="replace").decode("ascii")


def _clip(text: str) -> str:
    if len(text) <= MAX_RENDERED_BYTES:
        return text
    return text[: MAX_RENDERED_BYTES - 30] + "\n... (rendered output clipped)"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_SUBCOMMANDS = frozenset({"history", "why", "clear", "help"})

# A turn-id always starts with the "chat-" prefix (see
# ``ConversationOrchestrator._mint_turn_id``). The ``why`` subcommand
# requires this exact shape so a natural-language ``/chat why is X
# happening?`` falls through to message dispatch instead of being
# misparsed as a subcommand.
_TURN_ID_PREFIX = "chat-"


@dataclass
class ChatReplDispatcher:
    """Parses operator input, routes through the orchestrator, renders
    the verdict, and (when an executor is wired) commits the side
    effect.

    Slice 3 ships executor=None — the dispatcher returns the decision
    + rendered text only. Slice 4 graduation wires a concrete
    :class:`ChatActionExecutor`."""

    orchestrator: Optional[ConversationOrchestrator] = None
    executor: Optional[ChatActionExecutor] = None
    default_session_id: str = DEFAULT_SESSION_ID

    def _orch(self) -> ConversationOrchestrator:
        return self.orchestrator or get_default_orchestrator()

    # ---- public API ----

    def handle(
        self,
        line: str,
        session_id: Optional[str] = None,
    ) -> ChatReplResult:
        """Dispatch a single operator input line.

        Accepts:
          * ``"/chat <message>"`` or ``"/chat history|why|clear|help"``
          * Bare ``"<message>"`` (treated as a new turn)
          * Empty / whitespace → :class:`ChatReplStatus.EMPTY`
        """
        sid = session_id or self.default_session_id
        if not line or not line.strip():
            return ChatReplResult(
                status=ChatReplStatus.EMPTY,
                rendered_text="(empty input)",
            )

        stripped = line.strip()

        # /chat prefix routing.
        if stripped.startswith("/chat"):
            tail = stripped[len("/chat"):].lstrip()
            if not tail:
                return self._dispatch_message("", sid)
            first, _, rest = tail.partition(" ")
            first = first.strip().lower()
            if first in _SUBCOMMANDS and self._args_match_subcommand(
                first, rest.strip(),
            ):
                return self._handle_subcommand(first, rest.strip(), sid)
            # Treat as the message body — falls through to dispatch so
            # natural-language "/chat why is X?" doesn't get misparsed
            # as the ``why`` subcommand.
            return self._dispatch_message(tail, sid)

        # Bare text path.
        return self._dispatch_message(stripped, sid)

    @staticmethod
    def _args_match_subcommand(sub: str, args: str) -> bool:
        """Subcommands fire only when their args match the expected
        shape — else we fall through to message dispatch so the
        operator's natural-language overlap (e.g. ``/chat why is X
        happening?``) isn't misrouted."""
        if sub == "help":
            return not args  # /chat help with anything else → message
        if sub == "clear":
            return not args  # /chat clear with anything else → message
        if sub == "history":
            # Empty args = default count; a single non-negative int
            # also valid. Anything else (e.g. ``of changes``) → message.
            if not args:
                return True
            parts = args.split()
            if len(parts) != 1:
                return False
            try:
                return int(parts[0]) >= 0
            except ValueError:
                return False
        if sub == "why":
            # Requires a single turn-id token starting with "chat-".
            parts = args.split()
            return (
                len(parts) == 1 and parts[0].startswith(_TURN_ID_PREFIX)
            )
        return False

    def handle_bare_text(
        self,
        text: str,
        session_id: Optional[str] = None,
    ) -> ChatReplResult:
        """Convenience for SerpentFlow when the operator has explicitly
        opted into chat mode and types without the slash prefix."""
        return self._dispatch_message(text or "", session_id)

    # ---- subcommands ----

    def _handle_subcommand(
        self, sub: str, args: str, session_id: str,
    ) -> ChatReplResult:
        if sub == "help":
            return ChatReplResult(
                status=ChatReplStatus.SUBCOMMAND,
                rendered_text=render_help(),
            )
        if sub == "clear":
            dropped = self._orch().forget(session_id)
            text = (
                f"[chat] cleared session={session_id}"
                if dropped else
                f"[chat] no session to clear (session={session_id})"
            )
            return ChatReplResult(
                status=ChatReplStatus.SUBCOMMAND, rendered_text=text,
            )
        if sub == "history":
            n = self._parse_history_count(args)
            session = self._orch().get_session(session_id)
            turns = session.snapshot() if session is not None else []
            tail = turns[-n:] if n > 0 else []
            return ChatReplResult(
                status=ChatReplStatus.SUBCOMMAND,
                rendered_text=render_history(tail, session_id),
            )
        if sub == "why":
            turn_id = args.strip().split()[0] if args.strip() else ""
            if not turn_id:
                return ChatReplResult(
                    status=ChatReplStatus.UNKNOWN_TURN,
                    rendered_text="[chat] /chat why <turn-id> requires an id",
                )
            turn = self._orch().get_turn(turn_id)
            if turn is None:
                return ChatReplResult(
                    status=ChatReplStatus.UNKNOWN_TURN,
                    rendered_text=f"[chat] no turn with id {turn_id!r}",
                )
            return ChatReplResult(
                status=ChatReplStatus.SUBCOMMAND,
                rendered_text=render_why(turn),
                turn=turn,
            )
        return ChatReplResult(
            status=ChatReplStatus.UNKNOWN_SUBCOMMAND,
            rendered_text=f"[chat] unknown subcommand: {sub!r}\n{render_help()}",
        )

    @staticmethod
    def _parse_history_count(args: str) -> int:
        if not args:
            return HISTORY_DEFAULT_N
        try:
            n = int(args.strip().split()[0])
        except (TypeError, ValueError, IndexError):
            return HISTORY_DEFAULT_N
        if n <= 0:
            return HISTORY_DEFAULT_N
        return min(n, HISTORY_MAX_N)

    # ---- message dispatch ----

    def _dispatch_message(
        self, message: str, session_id: str,
    ) -> ChatReplResult:
        orch = self._orch()
        turn, decision = orch.dispatch(message, session_id=session_id)

        rendered = render_decision(turn, decision)

        # No executor wired — Slice 3 returns just the decision.
        if self.executor is None or decision.action == "noop":
            return ChatReplResult(
                status=ChatReplStatus.DISPATCHED,
                rendered_text=rendered,
                decision=decision,
                turn=turn,
            )

        # Executor wired (Slice 4) — commit the side effect.
        try:
            response = self._invoke_executor(turn, decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ChatReplDispatcher] executor raised: %s", exc,
            )
            return ChatReplResult(
                status=ChatReplStatus.EXECUTOR_FAILED,
                rendered_text=(
                    f"{rendered}\n[chat] executor failed: {exc}"
                ),
                decision=decision,
                turn=turn,
                executor_response=str(exc),
            )

        # Persist the response back onto the indexed turn.
        if response is not None:
            try:
                orch.record_response(turn.turn_id, str(response))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ChatReplDispatcher] record_response failed: %s",
                    exc,
                )
        return ChatReplResult(
            status=ChatReplStatus.EXECUTOR_OK,
            rendered_text=(
                f"{rendered}\n[chat] => {response}"
            ),
            decision=decision,
            turn=turn,
            executor_response=str(response) if response is not None else None,
        )

    def _invoke_executor(
        self,
        turn: ChatTurn,
        decision: ChatRoutingDecision,
    ) -> Optional[str]:
        """Route the decision to the right executor method. Slice 3
        only invokes the executor when one is wired; Slice 4 wires
        a concrete impl."""
        executor = self.executor
        if executor is None:
            return None
        msg = decision.payload.get("message", "")
        action = decision.action
        if action == "backlog_dispatch":
            return executor.dispatch_backlog(msg, turn)
        if action == "subagent_explore":
            return executor.spawn_subagent(msg, turn)
        if action == "claude_query":
            session = self._orch().get_session(turn.session_id)
            recent = session.snapshot() if session is not None else []
            return executor.query_claude(msg, turn, recent)
        if action == "context_attach":
            target_turn_id = decision.target_turn_id or ""
            target = self._orch().get_turn(target_turn_id)
            if target is None:
                # Degraded — paste with no prior. Treat as Claude
                # query against the bare paste.
                session = self._orch().get_session(turn.session_id)
                recent = session.snapshot() if session is not None else []
                return executor.query_claude(msg, turn, recent)
            return executor.attach_context(msg, turn, target)
        return None


__all__ = [
    "ChatActionExecutor",
    "ChatReplDispatcher",
    "ChatReplResult",
    "ChatReplStatus",
    "DEFAULT_SESSION_ID",
    "HISTORY_DEFAULT_N",
    "HISTORY_MAX_N",
    "MAX_RENDERED_BYTES",
    "is_enabled",
    "render_decision",
    "render_help",
    "render_history",
    "render_why",
]

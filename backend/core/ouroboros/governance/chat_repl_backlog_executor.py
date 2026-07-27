"""P3 P2 Slice 4 deferred follow-up — concrete BacklogChatActionExecutor.

Closes one of three deferred follow-ups from Phase 3 P2 Slice 4
graduation (Conversational mode `/chat` REPL): the safe-default
`LoggingChatActionExecutor` only logs operator chat turns; this module
ships the FIRST of three concrete executors that hit real subsystems.

This executor wires `ChatActionExecutor.dispatch_backlog` against the
existing `.jarvis/backlog.json` writer (the same file `BacklogSensor`
watches and `/backlog auto-proposed approve` already appends to). When
the operator says "/chat add a backlog item: clean up X" the chat
classifier routes to action=backlog_dispatch, the dispatcher calls
`executor.dispatch_backlog(message, turn)`, this executor appends an
entry to backlog.json with `source="chat_repl"` + the turn_id, and
the BacklogSensor picks it up on the next scan and routes through the
standard FSM intake path.

The other three Protocol methods (spawn_subagent / query_claude /
attach_context) delegate to a fallback executor (defaults to
`LoggingChatActionExecutor`) — this is the **per-method composition
pattern** so each concrete executor can land in its own PR with its
own pin suite (per the operator's 3-PR mini-arc plan). Subsequent
PRs swap each fallback method out for a concrete implementation.

## Authority surface

  * Writes EXACTLY one file: `<project_root>/.jarvis/backlog.json`,
    via the same `_append_to_backlog_json` helper used by the existing
    `/backlog auto-proposed` REPL. No other FS writes, no subprocess,
    no network, no env mutation.
  * Bounded message length (`MAX_BACKLOG_DESCRIPTION_CHARS = 1024`).
  * Per-call dedup: task_id is `chat:{turn_id}` so re-emission of the
    same turn (e.g. operator-driven retry) is structurally idempotent
    at the BacklogSensor scan layer.
  * Default-off: `JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED` (default false
    until graduation). When off, `build_chat_repl_dispatcher_with_
    backlog()` returns the same dispatcher the legacy factory builds
    (LoggingChatActionExecutor) — zero behavior change.
  * Master flag JARVIS_CONVERSATIONAL_MODE_ENABLED (graduated true)
    still gates the entire `/chat` surface above this executor.

## Why per-method composition

Each concrete executor crosses a different authority boundary:
backlog (FS write of one JSON file), subagent (worker thread spawn),
Claude (LLM API call with cost). Per the operator binding the
3-PR mini-arc lets each one ship + soak independently. Composition
keeps the wiring trivial — to enable subagent in a future PR, the
factory just swaps `subagent_chat_executor` into the composite slot.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Any, Dict, List, Optional

from backend.core.ouroboros.governance.backlog_auto_proposed_repl import (
    _append_to_backlog_json,
)
from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatActionExecutor,
    ChatReplDispatcher,
    DEFAULT_SESSION_ID,
    LoggingChatActionExecutor,
    build_chat_repl_dispatcher,
)
from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatTurn,
    ConversationOrchestrator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Cap on the message text written into the backlog entry's
# ``description`` field. Defends against an operator pasting a
# multi-MB blob into /chat which would balloon backlog.json.
MAX_BACKLOG_DESCRIPTION_CHARS: int = 1_024

# Default backlog file location (mirrors `_backlog_path` in the
# auto-proposed REPL — we don't import that helper directly because
# it expects the project_root, which the executor receives via
# constructor injection).
_BACKLOG_FILENAME: str = "backlog.json"
_JARVIS_DIR: str = ".jarvis"

# Default priority for chat-originated backlog entries. Picks the
# same default the auto-proposed REPL writes (`priority=3` —
# normal-priority backlog item). Operators can re-prioritize via the
# existing `/backlog` REPL once the entry lands.
_DEFAULT_CHAT_BACKLOG_PRIORITY: int = 3


def is_enabled() -> bool:
    """Master flag — ``JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED`` (default
    false until graduation).

    When off, the factory falls back to the safe-default
    `LoggingChatActionExecutor` for ALL four methods (zero behavior
    change vs the post-graduation Slice 4 wiring).

    When on, the factory wires `BacklogChatActionExecutor` so the
    `dispatch_backlog` method writes to `.jarvis/backlog.json`; the
    other three methods still delegate to LoggingChatActionExecutor
    until their own concrete executors land in subsequent PRs."""
    return os.environ.get(
        "JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _backlog_path(project_root: Path) -> Path:
    """Return the backlog.json path under the project's .jarvis dir."""
    return Path(project_root) / _JARVIS_DIR / _BACKLOG_FILENAME


# ---------------------------------------------------------------------------
# Concrete executor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The operator-dispatch seam
# ---------------------------------------------------------------------------
#
# ONE registry rather than a `submit_now=` parameter threaded through three
# nested factories (chat_text_bridge → with_claude → with_backlog). Threading
# it would mean every intermediate factory grows a parameter it does not use
# and must remember to forward — and a factory that forgets produces an
# executor that silently files goals instead of running them, which is
# exactly the wired-but-inert failure this codebase keeps paying for.
#
# Same shape as `set_active_canvas` in bipartite_layout: the daemon installs
# its capability once at boot; every consumer asks rather than being handed.

_OPERATOR_DISPATCH: Optional[Callable[[str, Any], Optional[str]]] = None


def set_operator_dispatcher(
    fn: Optional[Callable[[str, Any], Optional[str]]],
) -> None:
    """Install the daemon's immediate-dispatch capability. None disarms it.

    Called once at harness boot. Absent it, every goal is filed and the reply
    honestly says so — the degradation is visible rather than silent.
    """
    global _OPERATOR_DISPATCH
    _OPERATOR_DISPATCH = fn


def get_operator_dispatcher() -> Optional[Callable[[str, Any], Optional[str]]]:
    return _OPERATOR_DISPATCH


class BacklogChatActionExecutor:
    """Concrete ChatActionExecutor that wires `dispatch_backlog`
    against the real `.jarvis/backlog.json` writer.

    Other three Protocol methods (spawn_subagent / query_claude /
    attach_context) delegate to ``self._fallback`` (defaults to
    `LoggingChatActionExecutor`) so this executor can be wired in
    standalone before the other two concrete executors land.

    Constructor:
      project_root: where .jarvis/backlog.json lives. Required.
      fallback:     ChatActionExecutor used for the three non-backlog
                    methods (defaults to LoggingChatActionExecutor).
                    Subsequent PRs swap a wired SubagentChatActionExecutor
                    or ClaudeChatActionExecutor into this slot via the
                    composite factory.
    """

    def __init__(
        self,
        project_root: Path,
        fallback: Optional[ChatActionExecutor] = None,
        submit_now: Optional[Callable[[str, ChatTurn], Optional[str]]] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._fallback: ChatActionExecutor = (
            fallback or LoggingChatActionExecutor()
        )
        # INJECTED, not imported: this module writes JSON and must stay
        # testable without an intake router, an event loop or a daemon. The
        # harness supplies the real submitter; absent one, the backlog write
        # below remains the behaviour — degraded, but never lost.
        self._submit_now = submit_now
        # Audit list — tests + operator inspection. Each entry is the
        # task_id returned (or the failure token).
        self.calls: List[str] = []

    def dispatch_backlog(self, message: str, turn: ChatTurn) -> str:
        """Append an entry to .jarvis/backlog.json. Returns the
        synthesized task_id on success, an `error-...` token on
        failure (so the chat dispatcher can render it).

        Idempotency: task_id = ``chat:{turn_id}``. Re-emission of the
        same turn appends a duplicate row, but the BacklogSensor
        deduplicates on task_id so the FSM only fires once.
        """
        msg_clipped = (message or "")[:MAX_BACKLOG_DESCRIPTION_CHARS]

        # IMMEDIATE PATH — a human is watching the screen.
        #
        # Writing to backlog.json makes the goal carry source="backlog",
        # which routes BACKGROUND (DW only, cost-optimized) and waits for a
        # sensor sweep. That is right for the Backlog SENSOR mining a task
        # list and wrong for someone who just pressed Enter: the token
        # described where the goal was STORED, not who ASKED for it.
        #
        # When a submitter is wired, the goal enters intake as
        # `operator_chat` — human-origin, SOVEREIGN, IMMEDIATE-eligible —
        # and the op runs now, narrating itself through the same chrome
        # every autonomous op uses.
        # Explicit injection wins (tests, callers that know better); the
        # registry is the daemon's standing answer.
        submitter = self._submit_now or get_operator_dispatcher()
        if submitter is not None and msg_clipped.strip():
            try:
                op_id = submitter(msg_clipped, turn)
                if op_id:
                    self.calls.append(str(op_id))
                    logger.info(
                        "[BacklogChatExecutor] turn=%s dispatched IMMEDIATE "
                        "op=%s (source=operator_chat)", turn.turn_id, op_id,
                    )
                    return str(op_id)
            except Exception as exc:  # noqa: BLE001
                # Fall through to the durable write. An intake fault must
                # never LOSE an operator's goal — a queued goal is a
                # degradation; a dropped one is a betrayal.
                logger.warning(
                    "[BacklogChatExecutor] turn=%s immediate dispatch failed "
                    "(%s) — falling back to backlog", turn.turn_id, exc,
                )

        if not msg_clipped.strip():
            logger.warning(
                "[BacklogChatExecutor] turn=%s empty message — refusing "
                "to write empty backlog entry",
                turn.turn_id,
            )
            token = f"error-empty-message-{turn.turn_id}"
            self.calls.append(token)
            return token

        entry: Dict[str, Any] = {
            "task_id": f"chat:{turn.turn_id}",
            "description": msg_clipped,
            "target_files": [],
            "priority": _DEFAULT_CHAT_BACKLOG_PRIORITY,
            "repo": "jarvis",
            "status": "pending",
            # Provenance markers — let downstream surfaces (sensor,
            # REPL, audit) filter chat-originated entries.
            "source": "chat_repl",
            "session_id": str(turn.session_id),
            "turn_id": str(turn.turn_id),
            "submitted_timestamp_unix": time.time(),
        }

        backlog_path = _backlog_path(self._project_root)
        ok = _append_to_backlog_json(backlog_path, entry)
        if not ok:
            token = f"error-append-failed-{turn.turn_id}"
            self.calls.append(token)
            logger.warning(
                "[BacklogChatExecutor] turn=%s append to %s failed",
                turn.turn_id, backlog_path,
            )
            return token

        task_id = entry["task_id"]
        self.calls.append(task_id)
        logger.info(
            "[BacklogChatExecutor] queued task_id=%s session=%s "
            "msg_chars=%d (BacklogSensor will pick up on next scan)",
            task_id, turn.session_id, len(msg_clipped),
        )
        return task_id

    # ---- delegated to fallback (subsequent PRs swap these out) ----

    def spawn_subagent(self, message: str, turn: ChatTurn) -> str:
        return self._fallback.spawn_subagent(message, turn)

    def query_claude(
        self,
        message: str,
        turn: ChatTurn,
        recent_turns: List[ChatTurn],
    ) -> str:
        return self._fallback.query_claude(message, turn, recent_turns)

    def attach_context(
        self,
        message: str,
        turn: ChatTurn,
        target_turn: ChatTurn,
    ) -> str:
        return self._fallback.attach_context(message, turn, target_turn)


# ---------------------------------------------------------------------------
# Factory wiring (sits one layer above build_chat_repl_dispatcher)
# ---------------------------------------------------------------------------


def build_chat_repl_dispatcher_with_backlog(
    *,
    submit_now: Optional[Callable[[str, "ChatTurn"], Optional[str]]] = None,
    project_root: Optional[Path] = None,
    orchestrator: Optional[ConversationOrchestrator] = None,
    fallback_executor: Optional[ChatActionExecutor] = None,
    default_session_id: str = DEFAULT_SESSION_ID,
) -> Optional[ChatReplDispatcher]:
    """Build a chat dispatcher with the BacklogChatActionExecutor
    wired in when ``JARVIS_CHAT_EXECUTOR_BACKLOG_ENABLED`` is truthy.

    When the env flag is off (default until graduation) OR
    ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` is off (hot-reverts the
    entire /chat surface), this factory falls back to the
    pre-existing `build_chat_repl_dispatcher` behavior — which
    returns ``None`` when /chat is disabled and a
    `LoggingChatActionExecutor`-wired dispatcher otherwise.

    project_root is required to locate `.jarvis/backlog.json`. When
    omitted, defaults to `Path.cwd()` (matches the convention used
    by the auto-proposed REPL + the BacklogSensor).
    """
    if not is_enabled():
        # Pass through to the legacy factory — it handles the master
        # /chat enable/disable for us.
        return build_chat_repl_dispatcher(
            orchestrator=orchestrator,
            executor=fallback_executor,
            default_session_id=default_session_id,
        )

    root = project_root if project_root is not None else Path.cwd()
    wired_executor: ChatActionExecutor = BacklogChatActionExecutor(
        project_root=root,
        fallback=fallback_executor or LoggingChatActionExecutor(),
        # None here is the honest default: with no submitter the goal is
        # filed, and the reply SAYS it was filed. The harness supplies the
        # real one, so the immediate path turns on where intake exists rather
        # than being assumed everywhere.
        submit_now=submit_now,
    )
    return build_chat_repl_dispatcher(
        orchestrator=orchestrator,
        executor=wired_executor,
        default_session_id=default_session_id,
    )


__all__ = [
    "BacklogChatActionExecutor",
    "set_operator_dispatcher",
    "get_operator_dispatcher",
    "MAX_BACKLOG_DESCRIPTION_CHARS",
    "build_chat_repl_dispatcher_with_backlog",
    "is_enabled",
]

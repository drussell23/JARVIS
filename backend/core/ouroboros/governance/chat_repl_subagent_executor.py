"""P3 P2 Slice 4 deferred follow-up — concrete SubagentChatActionExecutor.

PR 2 of 3 in the operator-authorized chat-executor mini-arc. PR 1
shipped `BacklogChatActionExecutor` (FS write to `.jarvis/backlog.json`).
This PR ships the second concrete executor — `spawn_subagent` against
the read-only EXPLORE subagent infrastructure.

## Design choice: enqueue-and-return-ticket (not synchronous run)

The Protocol method `spawn_subagent(message, turn) -> str` is **sync**.
Synchronously running an `AgenticExploreSubagent` would block the
`/chat` REPL on a multi-second exploration (default 120s timeout). Bad
UX. Instead this executor follows PR 1's pattern: serialize the
request as a JSONL ticket entry to `.jarvis/chat_subagent_queue.jsonl`
and return the ticket id immediately. A future sweeper (out of scope
this PR — tracked as a follow-up) actually invokes the subagent and
writes the result back. The operator can `/chat history` to see the
ticket id and check status.

This keeps the authority surface tiny and identical in shape to
PR 1: ONE file write, no subprocess, no network, no env mutation.

## Per-method composition (PR 1 pattern preserved)

The other three Protocol methods (dispatch_backlog / query_claude /
attach_context) delegate to a fallback executor (defaults to
`LoggingChatActionExecutor`). The new factory
`build_chat_repl_dispatcher_with_subagent()` chains through PR 1's
backlog factory so an operator with BOTH `JARVIS_CHAT_EXECUTOR_BACKLOG_
ENABLED` AND `JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED` on gets:

    Subagent(fallback=Backlog(fallback=Logging))

— each method routes to the right concrete executor; the rest fall
through to the safe-default Logging.

## Authority surface

  * Writes EXACTLY one file: `<project_root>/.jarvis/chat_subagent_queue.jsonl`.
  * No subprocess, no network, no env mutation, no other FS writes.
  * Bounded message length (`MAX_SUBAGENT_GOAL_CHARS = 512` — tighter
    than backlog's 1024 because subagent goals are interpreted as the
    `goal` field of a `SubagentRequest` which has its own per-token
    budget downstream).
  * Per-call dedup: ticket_id is `subagent:{turn_id}` so re-emission
    is idempotent at the future-sweeper layer.
  * Default-off: `JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED` (default false
    until graduation).
  * Master flag JARVIS_CONVERSATIONAL_MODE_ENABLED (graduated true)
    still gates the entire `/chat` surface.

## What this PR does NOT do

  * Does NOT actually run the subagent. The ticket lands in the queue;
    a future ChatSubagentSweeper (separate PR) reads the queue, builds
    a SubagentContext, dispatches via `agentic_subagent.AgenticExploreSubagent`,
    and writes the result back. This split keeps the chat-side
    authority surface deterministic and unit-testable without dragging
    in the full subagent dispatch stack.
  * Does NOT mutate any pre-existing subagent dispatch path. The
    queue file is a new artifact owned by this executor + its future
    sweeper; no existing module reads it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
    is_enabled as _backlog_is_enabled,
    build_chat_repl_dispatcher_with_backlog,
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


# Cap on the goal text persisted in the ticket. Tighter than backlog's
# 1024 because the goal feeds a SubagentRequest's `goal` field which
# has downstream per-token budget pressure.
MAX_SUBAGENT_GOAL_CHARS: int = 512

# Queue file location relative to project_root. New file dedicated to
# chat-driven subagent tickets — does not pollute the existing
# subagent dispatch artifacts.
_QUEUE_FILENAME: str = "chat_subagent_queue.jsonl"
_JARVIS_DIR: str = ".jarvis"

# Default subagent type for chat-driven dispatches. EXPLORE is the
# only type guaranteed read-only by the Phase 1 contract; chat
# operators cannot escalate to PLAN / REVIEW / GENERAL via this
# surface (those require orchestrator-driven invocation per
# subagent_contracts.py:454-478 design notes).
_DEFAULT_SUBAGENT_TYPE: str = "explore"

# Schema version stamped into every ticket so a future sweeper can
# pin a parser version + reject incompatibly-shaped rows.
TICKET_SCHEMA_VERSION: int = 1


def is_enabled() -> bool:
    """Master flag — ``JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED`` (default
    false until graduation).

    When off, the factory falls back to `build_chat_repl_dispatcher_with_
    backlog` (which itself honors PR 1's backlog flag, then falls
    back to `LoggingChatActionExecutor`)."""
    return os.environ.get(
        "JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _queue_path(project_root: Path) -> Path:
    return Path(project_root) / _JARVIS_DIR / _QUEUE_FILENAME


def _append_ticket(path: Path, ticket: Dict[str, Any]) -> bool:
    """Append a single ticket as one JSONL line. NEVER raises —
    returns False on persist failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[ChatSubagentExecutor] mkdir failed for %s: %s",
            path.parent, exc,
        )
        return False
    try:
        line = json.dumps(ticket, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[ChatSubagentExecutor] ticket serialization failed: %s",
            exc,
        )
        return False
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except OSError as exc:
        logger.warning(
            "[ChatSubagentExecutor] append failed: %s", exc,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Concrete executor
# ---------------------------------------------------------------------------


class SubagentChatActionExecutor:
    """Concrete ChatActionExecutor that wires `spawn_subagent` against
    a JSONL queue for the future ChatSubagentSweeper to consume.

    Design: enqueue-and-return-ticket. The Protocol method is sync; a
    real subagent run is multi-second async. Persisting a ticket and
    returning the id keeps the chat REPL responsive while preserving
    every operator-authorization gate downstream (the sweeper runs
    under the same JARVIS_SUBAGENT_DISPATCH_ENABLED master flag the
    rest of the subagent infrastructure uses).

    Other three Protocol methods (dispatch_backlog / query_claude /
    attach_context) delegate to ``self._fallback`` (defaults to
    `LoggingChatActionExecutor`). The factory composition lets PR 1's
    backlog executor sit in the fallback slot when both flags are on.
    """

    def __init__(
        self,
        project_root: Path,
        fallback: Optional[ChatActionExecutor] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._fallback: ChatActionExecutor = (
            fallback or LoggingChatActionExecutor()
        )
        # Audit list — tests + operator inspection.
        self.calls: List[str] = []

    def spawn_subagent(self, message: str, turn: ChatTurn) -> str:
        """Persist a subagent ticket. Returns the ticket id (or an
        ``error-...`` token on failure)."""
        goal = (message or "")[:MAX_SUBAGENT_GOAL_CHARS]
        if not goal.strip():
            token = f"error-empty-goal-{turn.turn_id}"
            self.calls.append(token)
            logger.warning(
                "[ChatSubagentExecutor] turn=%s empty goal — refusing "
                "to enqueue empty subagent ticket",
                turn.turn_id,
            )
            return token

        ticket: Dict[str, Any] = {
            "schema_version": TICKET_SCHEMA_VERSION,
            "ticket_id": f"subagent:{turn.turn_id}",
            "subagent_type": _DEFAULT_SUBAGENT_TYPE,
            "goal": goal,
            # Empty by default; future sweeper may resolve target_files
            # by parsing the goal or via a separate operator step.
            "target_files": [],
            "scope_paths": [],
            # Provenance markers for the sweeper + audit.
            "source": "chat_repl",
            "session_id": str(turn.session_id),
            "turn_id": str(turn.turn_id),
            "submitted_timestamp_unix": time.time(),
            # Status starts as "pending"; future sweeper transitions to
            # "running" / "completed" / "failed" / "expired".
            "status": "pending",
        }

        path = _queue_path(self._project_root)
        ok = _append_ticket(path, ticket)
        if not ok:
            token = f"error-enqueue-failed-{turn.turn_id}"
            self.calls.append(token)
            return token

        ticket_id = ticket["ticket_id"]
        self.calls.append(ticket_id)
        logger.info(
            "[ChatSubagentExecutor] queued ticket_id=%s session=%s "
            "goal_chars=%d (sweeper will dispatch on next cycle)",
            ticket_id, turn.session_id, len(goal),
        )
        return ticket_id

    # ---- delegated to fallback (backlog already concrete from PR 1;
    # claude lands in PR 3) ----

    def dispatch_backlog(self, message: str, turn: ChatTurn) -> str:
        return self._fallback.dispatch_backlog(message, turn)

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
# Factory wiring (chains through PR 1's backlog factory)
# ---------------------------------------------------------------------------


def build_chat_repl_dispatcher_with_subagent(
    *,
    project_root: Optional[Path] = None,
    orchestrator: Optional[ConversationOrchestrator] = None,
    fallback_executor: Optional[ChatActionExecutor] = None,
    default_session_id: str = DEFAULT_SESSION_ID,
) -> Optional[ChatReplDispatcher]:
    """Build a chat dispatcher with the SubagentChatActionExecutor
    wired in when ``JARVIS_CHAT_EXECUTOR_SUBAGENT_ENABLED`` is truthy.

    Composition rules:
      * If subagent flag OFF: pass through to PR 1's
        `build_chat_repl_dispatcher_with_backlog` factory (which
        handles backlog flag + master flag).
      * If subagent flag ON + backlog flag ON:
        Subagent(fallback=Backlog(fallback=Logging)) — both concrete
        executors active for their respective methods.
      * If subagent flag ON + backlog flag OFF:
        Subagent(fallback=Logging) — only spawn_subagent is concrete.
      * If `JARVIS_CONVERSATIONAL_MODE_ENABLED` (master) OFF: returns
        ``None`` regardless of either per-executor flag.

    project_root defaults to `Path.cwd()` (matches PR 1)."""
    if not is_enabled():
        # Pass through to PR 1's factory — it handles backlog + master.
        return build_chat_repl_dispatcher_with_backlog(
            project_root=project_root,
            orchestrator=orchestrator,
            fallback_executor=fallback_executor,
            default_session_id=default_session_id,
        )

    root = project_root if project_root is not None else Path.cwd()

    # Resolve the fallback chain. If backlog flag is also on, the
    # subagent executor's fallback becomes a Backlog(fallback=Logging)
    # so dispatch_backlog still hits real backlog.json + spawn_subagent
    # hits the new queue.
    fb: ChatActionExecutor
    if fallback_executor is not None:
        fb = fallback_executor
    elif _backlog_is_enabled():
        # Lazy import to avoid pulling the backlog executor when the
        # caller has supplied an explicit fallback.
        from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
            BacklogChatActionExecutor,
        )
        fb = BacklogChatActionExecutor(project_root=root)
    else:
        fb = LoggingChatActionExecutor()

    wired_executor: ChatActionExecutor = SubagentChatActionExecutor(
        project_root=root,
        fallback=fb,
    )
    # Note: we go to build_chat_repl_dispatcher (not _with_backlog)
    # because the subagent executor already composes the backlog
    # logic into its fallback slot. Going through _with_backlog would
    # double-wrap.
    return build_chat_repl_dispatcher(
        orchestrator=orchestrator,
        executor=wired_executor,
        default_session_id=default_session_id,
    )


__all__ = [
    "MAX_SUBAGENT_GOAL_CHARS",
    "SubagentChatActionExecutor",
    "TICKET_SCHEMA_VERSION",
    "build_chat_repl_dispatcher_with_subagent",
    "is_enabled",
]

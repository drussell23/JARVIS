"""P3 P2 Slice 4 deferred follow-up — concrete ClaudeChatActionExecutor.

PR 3 of 3 in the operator-authorized chat-executor mini-arc. **CLOSES
the entire mini-arc** + closes the third (final) deferred follow-up
from Phase 3 P2 Slice 4 graduation.

Wires `ChatActionExecutor.query_claude` against an injectable
`ClaudeQueryProvider` Protocol so a chat operator can ask
"/chat why is X happening?" and get a real conversational answer
back from Claude (instead of the safe-default LoggingExecutor's
synthetic `logged-claude-{turn_id}` placeholder).

This is the **highest-authority** of the three concrete chat
executors (PR 1 wrote a JSON file; PR 2 enqueued a JSONL ticket;
this PR actually spends tokens on the LLM API). The cage:

  * Per-call cost cap (DEFAULT_COST_CAP_PER_CALL_USD = $0.05 —
    matches AdversarialReviewer's per-call budget per PRD Phase 5
    P5).
  * Cumulative per-instance session budget cap
    (DEFAULT_SESSION_BUDGET_USD = $1.00 — generous but bounded).
  * Bounded prompt length (MAX_QUERY_CHARS = 1024).
  * Bounded context history (MAX_RECENT_TURNS_INCLUDED = 5).
  * Bounded response length (MAX_RESPONSE_CHARS = 4096).
  * No auto-retry (one-shot per Protocol semantics — operator can
    manually re-issue if Claude errors).
  * Provider injection: production wires a real Anthropic client
    via `AnthropicClaudeQueryProvider`; tests inject a fake; default
    is `_NullClaudeQueryProvider` (returns a sentinel — no API
    call, no cost) so a misconfigured factory cannot accidentally
    hit the API.
  * Persistent audit ledger at `.jarvis/chat_claude_audit.jsonl`
    captures every query attempt (success / cost-cap / provider
    error / session-budget-exhausted) for post-hoc cost
    accountability.

## Per-method composition (PR 1+2 pattern preserved)

The other 3 Protocol methods (dispatch_backlog / spawn_subagent /
attach_context) delegate to a fallback executor. The new factory
`build_chat_repl_dispatcher_with_claude()` chains through PR 2's
subagent factory so an operator with all three flags on gets:

    Claude(fallback=Subagent(fallback=Backlog(fallback=Logging)))

— each method routes to the right concrete executor.

## Authority surface (binding)

  * Calls EXACTLY one external API: the injected ClaudeQueryProvider.
  * Writes EXACTLY one new file: `.jarvis/chat_claude_audit.jsonl`.
  * No subprocess, no network (other than via the injected
    provider), no env mutation, no other FS writes.
  * Cost-bounded: per-call AND per-session caps; both can be
    overridden per-instance for tests / operator-tuning, but the
    defaults are conservative.
  * Default-off: `JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED` (default
    false until graduation).
  * Even with the master flag on, factory requires an explicit
    `claude_provider` kwarg OR falls through to the NullProvider
    (which returns a sentinel + does NOT spend money). No
    accidental API hits possible from misconfiguration.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from backend.core.ouroboros.governance.chat_repl_dispatcher import (
    ChatActionExecutor,
    ChatReplDispatcher,
    DEFAULT_SESSION_ID,
    LoggingChatActionExecutor,
    build_chat_repl_dispatcher,
)
from backend.core.ouroboros.governance.chat_repl_subagent_executor import (
    is_enabled as _subagent_is_enabled,
    build_chat_repl_dispatcher_with_subagent,
)
from backend.core.ouroboros.governance.conversation_orchestrator import (
    ChatTurn,
    ConversationOrchestrator,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Per-call cost cap. Mirrors the AdversarialReviewer's $0.05/op default
# from PRD Phase 5 P5 — same operator-budget mental model.
DEFAULT_COST_CAP_PER_CALL_USD: float = 0.05

# Cumulative per-instance session budget. Generous but bounded; an
# operator hitting this is either testing aggressively or has a
# misconfigured retry loop.
DEFAULT_SESSION_BUDGET_USD: float = 1.0

# Hard prompt-length cap. Defends against multi-MB chat-paste
# blowing the per-call cost cap.
MAX_QUERY_CHARS: int = 1024

# Bound the number of recent turns included as conversational
# context. Keeps token usage predictable.
MAX_RECENT_TURNS_INCLUDED: int = 5

# Cap on each recent-turn fragment included in the prompt.
MAX_RECENT_TURN_FRAGMENT_CHARS: int = 240

# Max characters in the response surfaced back to the operator.
MAX_RESPONSE_CHARS: int = 4_096

# Default max_tokens passed to the provider. The provider is free to
# enforce a tighter cap; this is the upper bound the executor will
# request.
DEFAULT_MAX_TOKENS_PER_QUERY: int = 1024

# Schema version stamped into every audit row.
AUDIT_SCHEMA_VERSION: int = 1

# Audit file location relative to project_root.
_AUDIT_FILENAME: str = "chat_claude_audit.jsonl"
_JARVIS_DIR: str = ".jarvis"


def is_enabled() -> bool:
    """Master flag — ``JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED`` (default
    false until graduation).

    When off, the factory falls back to PR 2's subagent factory
    (which itself chains through PR 1's backlog factory + the
    LoggingChatActionExecutor safe-default)."""
    return os.environ.get(
        "JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _audit_path(project_root: Path) -> Path:
    return Path(project_root) / _JARVIS_DIR / _AUDIT_FILENAME


def _append_audit(path: Path, row: Dict[str, Any]) -> bool:
    """Append one audit row as JSONL. NEVER raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[ClaudeChatExecutor] audit mkdir failed: %s", exc,
        )
        return False
    try:
        line = json.dumps(row, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[ClaudeChatExecutor] audit serialization failed: %s", exc,
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
            "[ClaudeChatExecutor] audit append failed: %s", exc,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Provider Protocol + safe-default null implementation
# ---------------------------------------------------------------------------


class ClaudeQueryProvider(Protocol):
    """Sync interface the executor calls per chat query.

    Production wires `AnthropicClaudeQueryProvider` (wraps the real
    Anthropic SDK). Tests inject fakes. Default is
    `_NullClaudeQueryProvider` (no API call, returns sentinel).

    The executor never retries — one call per chat turn. The
    provider is free to internally batch / cache / etc, but the
    executor treats each query as independent."""

    def query(self, prompt: str, max_tokens: int = ...) -> str: ...


class _NullClaudeQueryProvider:
    """Default safe-fallback when the master flag is on but no
    real provider was supplied. Returns a sentinel string; does
    NOT call the API; does NOT spend money."""

    SENTINEL_RESPONSE: str = (
        "[Claude provider not wired — chat query was accepted but "
        "no LLM was contacted. Set JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED "
        "and pass a real claude_provider to the factory.]"
    )

    def query(
        self,
        prompt: str,
        max_tokens: int = DEFAULT_MAX_TOKENS_PER_QUERY,
    ) -> str:
        # Read prompt for completeness (ensures deterministic length
        # under malicious inputs) but ignore the content.
        _ = len(prompt or "")
        _ = int(max_tokens)
        return self.SENTINEL_RESPONSE


# ---------------------------------------------------------------------------
# Concrete executor
# ---------------------------------------------------------------------------


class ClaudeChatActionExecutor:
    """Concrete ChatActionExecutor that wires `query_claude` against
    an injectable `ClaudeQueryProvider`.

    Authority surface: cost (token spend). Cage: per-call cost cap +
    cumulative per-instance session cap + bounded prompt + bounded
    context + no auto-retry + one-shot.

    Other 3 Protocol methods delegate to ``self._fallback`` (defaults
    to `LoggingChatActionExecutor`). The factory composes a
    Subagent(Backlog(Logging)) chain when those flags are also on.
    """

    def __init__(
        self,
        project_root: Path,
        provider: Optional[ClaudeQueryProvider] = None,
        fallback: Optional[ChatActionExecutor] = None,
        cost_cap_per_call_usd: float = DEFAULT_COST_CAP_PER_CALL_USD,
        session_budget_usd: float = DEFAULT_SESSION_BUDGET_USD,
        max_tokens: int = DEFAULT_MAX_TOKENS_PER_QUERY,
    ) -> None:
        self._project_root = Path(project_root)
        self._provider: ClaudeQueryProvider = (
            provider or _NullClaudeQueryProvider()
        )
        self._fallback: ChatActionExecutor = (
            fallback or LoggingChatActionExecutor()
        )
        self._cost_cap_per_call_usd = float(cost_cap_per_call_usd)
        self._session_budget_usd = float(session_budget_usd)
        self._max_tokens = int(max_tokens)
        # Cumulative spend (best-effort estimate — uses the per-call
        # cap as a conservative upper bound per call since the
        # provider doesn't return token counts here).
        self._cumulative_cost_usd: float = 0.0
        # Audit list — tests + operator inspection.
        self.calls: List[str] = []

    @property
    def cumulative_cost_usd(self) -> float:
        """Best-effort estimated cumulative spend for this instance.
        Conservative: assumes every successful query costs the
        per-call cap. Used to enforce the session budget gate."""
        return self._cumulative_cost_usd

    def query_claude(
        self,
        message: str,
        turn: ChatTurn,
        recent_turns: List[ChatTurn],
    ) -> str:
        """Send `message` (with bounded `recent_turns` context) to
        the injected provider. Returns the response text on success
        or an `error-...` token on bounded-failure paths."""
        msg_clipped = (message or "")[:MAX_QUERY_CHARS]
        if not msg_clipped.strip():
            token = f"error-empty-message-{turn.turn_id}"
            self.calls.append(token)
            self._audit("empty_message", turn, "", "", 0.0)
            logger.warning(
                "[ClaudeChatExecutor] turn=%s empty message — refusing",
                turn.turn_id,
            )
            return token

        # Session-budget gate (cumulative).
        if self._cumulative_cost_usd >= self._session_budget_usd:
            token = f"error-session-budget-exhausted-{turn.turn_id}"
            self.calls.append(token)
            self._audit(
                "session_budget_exhausted", turn, msg_clipped, "",
                self._cumulative_cost_usd,
            )
            logger.warning(
                "[ClaudeChatExecutor] turn=%s session budget "
                "exhausted (%.4f >= %.4f) — refusing",
                turn.turn_id, self._cumulative_cost_usd,
                self._session_budget_usd,
            )
            return token

        # Per-call cost cap (refuse to send if a single call's worst-
        # case cost would push us over the session budget).
        if (
            self._cumulative_cost_usd + self._cost_cap_per_call_usd
            > self._session_budget_usd
        ):
            token = f"error-call-would-exceed-budget-{turn.turn_id}"
            self.calls.append(token)
            self._audit(
                "call_would_exceed_budget", turn, msg_clipped, "",
                self._cumulative_cost_usd,
            )
            logger.warning(
                "[ClaudeChatExecutor] turn=%s next call would exceed "
                "session budget (%.4f + %.4f > %.4f) — refusing",
                turn.turn_id, self._cumulative_cost_usd,
                self._cost_cap_per_call_usd, self._session_budget_usd,
            )
            return token

        prompt = self._build_prompt(msg_clipped, recent_turns)

        # One-shot — no auto-retry.
        try:
            raw_response = self._provider.query(
                prompt, max_tokens=self._max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            token = (
                f"error-provider-{type(exc).__name__}-{turn.turn_id}"
            )
            self.calls.append(token)
            self._audit(
                "provider_error", turn, msg_clipped,
                f"{type(exc).__name__}: {exc}", 0.0,
            )
            logger.warning(
                "[ClaudeChatExecutor] turn=%s provider raised: %s",
                turn.turn_id, exc,
            )
            return token

        if not isinstance(raw_response, str):
            token = f"error-provider-non-string-{turn.turn_id}"
            self.calls.append(token)
            self._audit(
                "provider_non_string", turn, msg_clipped,
                f"got {type(raw_response).__name__}", 0.0,
            )
            return token

        # Conservative spend accounting: assume per-call cap was hit.
        self._cumulative_cost_usd += self._cost_cap_per_call_usd

        response_clipped = raw_response[:MAX_RESPONSE_CHARS]
        # The "call result" the dispatcher returns + tests inspect is
        # the response text itself (operators want to see Claude's
        # actual reply in the chat pane).
        self.calls.append(response_clipped)
        self._audit(
            "ok", turn, msg_clipped, response_clipped,
            self._cumulative_cost_usd,
        )
        logger.info(
            "[ClaudeChatExecutor] turn=%s session=%s ok "
            "prompt_chars=%d response_chars=%d cumulative_cost=%.4f",
            turn.turn_id, turn.session_id,
            len(prompt), len(response_clipped),
            self._cumulative_cost_usd,
        )
        return response_clipped

    # ---- delegated to fallback ----

    def dispatch_backlog(self, message: str, turn: ChatTurn) -> str:
        return self._fallback.dispatch_backlog(message, turn)

    def spawn_subagent(self, message: str, turn: ChatTurn) -> str:
        return self._fallback.spawn_subagent(message, turn)

    def attach_context(
        self,
        message: str,
        turn: ChatTurn,
        target_turn: ChatTurn,
    ) -> str:
        return self._fallback.attach_context(message, turn, target_turn)

    # ---- internals ----

    def _build_prompt(
        self,
        message: str,
        recent_turns: List[ChatTurn],
    ) -> str:
        """Build a minimal conversational prompt: trimmed recent
        history + current operator message. Bounded by
        MAX_RECENT_TURNS_INCLUDED + MAX_RECENT_TURN_FRAGMENT_CHARS."""
        lines: List[str] = []
        ctx_turns = list(recent_turns or [])[-MAX_RECENT_TURNS_INCLUDED:]
        if ctx_turns:
            lines.append("[chat context]")
            for t in ctx_turns:
                op_msg = (t.operator_message or "")[
                    :MAX_RECENT_TURN_FRAGMENT_CHARS
                ]
                lines.append(f"operator: {op_msg}")
                if t.response_text:
                    asst = t.response_text[:MAX_RECENT_TURN_FRAGMENT_CHARS]
                    lines.append(f"assistant: {asst}")
            lines.append("")
        lines.append("[current message]")
        lines.append(f"operator: {message}")
        lines.append("")
        lines.append(
            f"Answer the operator concisely (<= "
            f"{MAX_RESPONSE_CHARS} chars). No code blocks unless "
            f"explicitly requested."
        )
        return "\n".join(lines)

    def _audit(
        self,
        outcome: str,
        turn: ChatTurn,
        prompt: str,
        response: str,
        cumulative_cost: float,
    ) -> None:
        """Append one audit row. Best-effort; never raises."""
        row: Dict[str, Any] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "outcome": outcome,
            "turn_id": str(turn.turn_id),
            "session_id": str(turn.session_id),
            "submitted_timestamp_unix": time.time(),
            "prompt_chars": len(prompt),
            "response_chars": len(response),
            "cumulative_cost_usd": round(cumulative_cost, 6),
            "source": "chat_repl",
        }
        _append_audit(_audit_path(self._project_root), row)


# ---------------------------------------------------------------------------
# Factory wiring (chains through PR 2's subagent factory)
# ---------------------------------------------------------------------------


def build_chat_repl_dispatcher_with_claude(
    *,
    project_root: Optional[Path] = None,
    claude_provider: Optional[ClaudeQueryProvider] = None,
    orchestrator: Optional[ConversationOrchestrator] = None,
    fallback_executor: Optional[ChatActionExecutor] = None,
    default_session_id: str = DEFAULT_SESSION_ID,
    cost_cap_per_call_usd: float = DEFAULT_COST_CAP_PER_CALL_USD,
    session_budget_usd: float = DEFAULT_SESSION_BUDGET_USD,
) -> Optional[ChatReplDispatcher]:
    """Build a chat dispatcher with the ClaudeChatActionExecutor
    wired in when ``JARVIS_CHAT_EXECUTOR_CLAUDE_ENABLED`` is truthy.

    Composition rules (chains through PR 2's subagent factory):
      * Claude flag OFF: pass through to PR 2's
        `build_chat_repl_dispatcher_with_subagent` (which itself
        handles subagent + backlog + master flags).
      * Claude flag ON + subagent flag ON:
        Claude(fallback=Subagent(fallback=Backlog(fallback=Logging)))
        if backlog flag also on, else
        Claude(fallback=Subagent(fallback=Logging)).
      * Claude flag ON + subagent flag OFF + backlog flag ON:
        Claude(fallback=Backlog(fallback=Logging)).
      * Claude flag ON + everything else off:
        Claude(fallback=Logging).
      * Chat master OFF: returns None regardless.

    `claude_provider`: required for real LLM calls. When omitted
    AND the Claude flag is on, falls back to `_NullClaudeQueryProvider`
    (returns sentinel + does NOT spend money) — prevents
    accidental API hits from misconfiguration.
    """
    if not is_enabled():
        # Pass through to PR 2's factory.
        return build_chat_repl_dispatcher_with_subagent(
            project_root=project_root,
            orchestrator=orchestrator,
            fallback_executor=fallback_executor,
            default_session_id=default_session_id,
        )

    root = project_root if project_root is not None else Path.cwd()

    # Resolve the fallback chain. If subagent flag is on, build the
    # subagent-wrapped chain; else fall back to backlog (or logging)
    # via the legacy backlog factory's executor selection.
    fb: ChatActionExecutor
    if fallback_executor is not None:
        fb = fallback_executor
    else:
        # Build the subagent-or-backlog-or-logging chain by reusing
        # PR 2's executor-selection logic. Easiest: ask PR 2's
        # factory for a dispatcher, then steal its executor. The
        # factory will return None only when chat master is off,
        # which we already passed here (master gate happens INSIDE
        # build_chat_repl_dispatcher_with_subagent at the chat layer).
        if _subagent_is_enabled():
            from backend.core.ouroboros.governance.chat_repl_subagent_executor import (
                SubagentChatActionExecutor,
            )
            from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
                is_enabled as _backlog_is_enabled,
                BacklogChatActionExecutor,
            )
            if _backlog_is_enabled():
                fb = SubagentChatActionExecutor(
                    project_root=root,
                    fallback=BacklogChatActionExecutor(project_root=root),
                )
            else:
                fb = SubagentChatActionExecutor(project_root=root)
        else:
            from backend.core.ouroboros.governance.chat_repl_backlog_executor import (
                is_enabled as _backlog_is_enabled,
                BacklogChatActionExecutor,
            )
            if _backlog_is_enabled():
                fb = BacklogChatActionExecutor(project_root=root)
            else:
                fb = LoggingChatActionExecutor()

    wired_executor: ChatActionExecutor = ClaudeChatActionExecutor(
        project_root=root,
        provider=claude_provider,
        fallback=fb,
        cost_cap_per_call_usd=cost_cap_per_call_usd,
        session_budget_usd=session_budget_usd,
    )
    return build_chat_repl_dispatcher(
        orchestrator=orchestrator,
        executor=wired_executor,
        default_session_id=default_session_id,
    )


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "ClaudeChatActionExecutor",
    "ClaudeQueryProvider",
    "DEFAULT_COST_CAP_PER_CALL_USD",
    "DEFAULT_MAX_TOKENS_PER_QUERY",
    "DEFAULT_SESSION_BUDGET_USD",
    "MAX_QUERY_CHARS",
    "MAX_RECENT_TURNS_INCLUDED",
    "MAX_RECENT_TURN_FRAGMENT_CHARS",
    "MAX_RESPONSE_CHARS",
    "build_chat_repl_dispatcher_with_claude",
    "is_enabled",
]

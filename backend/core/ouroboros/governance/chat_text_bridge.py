"""Heuristic Intent Multiplexer — the REPL text plane's missing bridge.

Closes the wired-but-inert gap where the complete conversational stack
(``intent_classifier`` + ``chat_repl_dispatcher`` + the executor triplet)
had ZERO callers on the ``ov`` REPL path: bare operator text fell through
``_handle_repl_command`` to a debug log. This module supplies ONLY the
missing I/O bridge — classification, routing, session state, decision
rendering, and executor side-effects all stay in their canonical modules
(DRY mandate: zero routing logic is duplicated here).

Three pieces:

1. **Code-shape pre-filter** (:func:`code_shape_signals` +
   :func:`weighted_classify`) — pure, deterministic evidence the base
   classifier cannot express: multiline fenced blocks and AST-parseable
   payloads re-weight the verdict toward the TASK plane
   (``ACTION_REQUEST``) over the CHAT plane (``EXPLANATION``). The base
   :func:`intent_classifier.classify` remains the single classification
   authority; this layer only re-weights its verdict on code evidence,
   and the re-weighted verdict rides the additive ``verdict_override``
   seam on the canonical dispatch path.

2. **CancellationToken** — an asyncio-native, thread-safe abort signal.
   The REPL's existing ``KeyboardInterrupt`` surface (prompt_toolkit
   raises it INTO the input loop on Ctrl+C) triggers the token; no
   process-level ``signal.signal`` handler is registered — the harness's
   Ticket-B SIGINT handlers (partial-summary writes) stay untouched, per
   the watchdog-isolation discipline.

3. **ChatTextMultiplexer** — non-blocking submit: each turn runs the
   sync dispatcher off-loop via ``asyncio.to_thread`` and RACES it
   against the token (``asyncio.wait FIRST_COMPLETED``). Token fires →
   the turn task is cancelled and the operator is back at the prompt
   immediately; a late executor result is discarded on arrival.
   Cooperative boundary (stated honestly): a sync provider mid-HTTP-call
   cannot be hard-aborted — the wrapping task completes cancelled at
   once, the worker thread drains in the background, and its result is
   dropped. A cancel-aware ``ClaudeQueryProvider`` can tighten this
   without touching the bridge.

Authority invariant: presentation-plane only. Never imports orchestrator
/ iron_gate / policy_engine / risk_engine. The default executor chain is
the graduated safe-default (``LoggingChatActionExecutor`` unless the
per-leg executor flags are armed) — mounting this bridge grants ZERO new
mutation authority.

Master flag: ``JARVIS_CHAT_TEXT_BRIDGE_ENABLED`` (default **true** —
presentation-layer bridge over an already-graduated default-true chat
master; the side-effecting executors keep their own default-FALSE
flags per §33.1).
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Callable, Optional, Set, Tuple

from backend.core.ouroboros.governance.intent_classifier import (
    ChatIntent,
    IntentClassification,
    classify,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")

_BRIDGE_ENV_VAR = "JARVIS_CHAT_TEXT_BRIDGE_ENABLED"


def bridge_enabled() -> bool:
    """Master gate — default ON (pure presentation bridge; executor
    authority is governed by the per-leg flags). NEVER raises."""
    return os.environ.get(_BRIDGE_ENV_VAR, "1").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Code-shape pre-filter — pure evidence functions
# ---------------------------------------------------------------------------


_FENCE_BLOCK = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)

SIGNAL_FENCED = "fenced_code_block"
SIGNAL_AST = "ast_parseable_body"
BOOST_REASON = "code_shape_boost"


def _looks_substantive(tree: "ast.Module") -> bool:
    """A parse is code evidence only when it contains something a prose
    sentence cannot: a def/class/import/assignment/compound statement,
    or 2+ statements. Guards against ``"status"`` (a bare Name) or a
    one-word message parsing as a trivial expression."""
    if len(tree.body) >= 2:
        return True
    for node in ast.walk(tree):
        if isinstance(node, (
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
            ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
            ast.AugAssign, ast.For, ast.While, ast.If, ast.With,
            ast.Try, ast.Return, ast.Call,
        )):
            return True
    return False


def _ast_parses(src: str) -> bool:
    """True iff *src* is a non-trivial AST-parseable body. NEVER raises."""
    try:
        cleaned = textwrap.dedent(src).strip()
        if not cleaned or "\n" not in cleaned and len(cleaned) < 12:
            # Single short token — never code evidence on its own.
            return False
        tree = ast.parse(cleaned)
        return bool(tree.body) and _looks_substantive(tree)
    except (SyntaxError, ValueError, RecursionError):
        return False
    except Exception:  # noqa: BLE001 — evidence must never break dispatch
        return False


def code_shape_signals(text: str) -> Tuple[str, ...]:
    """Fired code-evidence signal names for *text*. Pure; NEVER raises.

    * ``fenced_code_block`` — a ``` fence is present.
    * ``ast_parseable_body`` — a fence interior OR the raw payload
      parses as substantive Python (see :func:`_looks_substantive`).
    """
    try:
        t = str(text or "")
        if not t.strip():
            return ()
        signals = []
        if "```" in t:
            signals.append(SIGNAL_FENCED)
        candidates = [m.group(1) for m in _FENCE_BLOCK.finditer(t)] or [t]
        for cand in candidates:
            if _ast_parses(cand):
                signals.append(SIGNAL_AST)
                break
        return tuple(signals)
    except Exception:  # noqa: BLE001
        return ()


def weighted_classify(text: str) -> IntentClassification:
    """The heuristic pre-filter: base classifier verdict, re-weighted
    toward the TASK plane on code-shape evidence. Pure; NEVER raises
    (degrades to the base verdict).

    Re-weight rules (deterministic, auditable via ``reasons``):
      * ``EXPLANATION`` (chat) + any code signal → ``ACTION_REQUEST``.
        A chat-shaped message carrying real code is a work order.
      * ``CONTEXT_PASTE`` + ``ast_parseable_body`` → ``ACTION_REQUEST``.
        Runnable code handed to an engineering organism is a task; a
        paste that is only a stack trace / log fence (NOT parseable)
        keeps the canonical paste-attach semantics — diagnostic context
        belongs to the previous turn.
      * ``ACTION_REQUEST`` + signals → confidence reinforcement only.
      * ``EXPLORATION`` is already on the task plane — untouched.
    """
    base = classify(text)
    try:
        signals = code_shape_signals(text)
        if not signals:
            return base
        boosted_conf = min(1.0, max(
            base.confidence, 0.55 + 0.15 * len(signals),
        ))
        reasons = tuple(base.reasons) + signals + (BOOST_REASON,)
        if base.intent == ChatIntent.EXPLANATION:
            return IntentClassification(
                intent=ChatIntent.ACTION_REQUEST,
                confidence=boosted_conf,
                reasons=reasons,
                truncated=base.truncated,
            )
        if base.intent == ChatIntent.CONTEXT_PASTE and SIGNAL_AST in signals:
            return IntentClassification(
                intent=ChatIntent.ACTION_REQUEST,
                confidence=boosted_conf,
                reasons=reasons,
                truncated=base.truncated,
            )
        if base.intent == ChatIntent.ACTION_REQUEST:
            return IntentClassification(
                intent=base.intent,
                confidence=boosted_conf,
                reasons=reasons,
                truncated=base.truncated,
            )
        return base
    except Exception:  # noqa: BLE001
        return base


# ---------------------------------------------------------------------------
# CancellationToken — asyncio-native, thread-safe abort signal
# ---------------------------------------------------------------------------


class CancellationToken:
    """One resettable abort signal shared by the REPL surface and the
    multiplexer. ``trigger()`` is safe from any thread (marshalled via
    ``call_soon_threadsafe`` when a loop is bound); NEVER raises."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def trigger(self) -> None:
        try:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is not loop:
                    loop.call_soon_threadsafe(self._event.set)
                    return
            self._event.set()
        except Exception:  # noqa: BLE001
            logger.debug("[ChatTextBridge] token trigger degraded", exc_info=True)

    def reset(self) -> None:
        try:
            self._event.clear()
        except Exception:  # noqa: BLE001
            pass

    async def wait(self) -> None:
        await self._event.wait()


# ---------------------------------------------------------------------------
# ChatTextMultiplexer — non-blocking turn execution with graceful abort
# ---------------------------------------------------------------------------


class ChatTextMultiplexer:
    """Async multiplexer between the REPL text plane and the canonical
    :class:`ChatReplDispatcher`.

    ``submit()`` is O(1) and never blocks the REPL loop: the sync
    dispatcher (whose executors may make blocking LLM calls) runs via
    ``asyncio.to_thread``, raced against the cancellation token. Every
    spawned task is tracked and self-discarding — ``drain()`` proves the
    loop clean at shutdown.
    """

    _CANCEL_LINE = "[chat] ^C -- generation abandoned, back to prompt"

    def __init__(
        self,
        dispatcher: Any,
        *,
        print_sink: Optional[Callable[[str], None]] = None,
        session_id: str = "repl",
        token: Optional[CancellationToken] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._sink = print_sink or (lambda _s: None)
        self._session_id = session_id
        self._token = token or CancellationToken()
        self._tasks: Set[asyncio.Task] = set()

    # ---- introspection ----

    @property
    def token(self) -> CancellationToken:
        return self._token

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    # ---- ingress ----

    def submit(self, text: str) -> Optional[asyncio.Task]:
        """Spawn one conversational turn. Non-blocking; returns the
        tracked task, or ``None`` (empty input / no running loop).
        NEVER raises."""
        try:
            if not text or not str(text).strip():
                return None
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[ChatTextBridge] submit without running loop — drop")
            return None
        except Exception:  # noqa: BLE001
            return None
        self._token.bind_loop(loop)
        task = loop.create_task(self._run(str(text)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_active(self) -> None:
        """The REPL's Ctrl+C surface. Thread-safe; NEVER raises."""
        self._token.trigger()

    async def drain(self, timeout: float = 5.0) -> None:
        """Cancel + await every tracked task (shutdown hygiene: no
        orphaned tasks may outlive the bridge). NEVER raises."""
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    # ---- turn execution ----

    async def _run(self, text: str) -> Optional[Any]:
        verdict = weighted_classify(text)
        work = asyncio.ensure_future(asyncio.to_thread(
            self._dispatcher.handle,
            text,
            self._session_id,
            verdict_override=verdict,
        ))
        token_wait = asyncio.ensure_future(self._token.wait())
        try:
            done, _pending = await asyncio.wait(
                {work, token_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if token_wait in done and work not in done:
                # Graceful abort: cancel the turn, drop the late result,
                # hand the prompt back. The token resets so the NEXT
                # turn starts clean.
                work.cancel()
                try:
                    await work
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._token.reset()
                self._safe_print(self._CANCEL_LINE)
                return None
            result = await work
            rendered = getattr(result, "rendered_text", None)
            if rendered:
                self._safe_print(str(rendered))
            return result
        except asyncio.CancelledError:
            # drain()/shutdown cancelled us — propagate after tidying.
            work.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — REPL must survive anything
            logger.warning("[ChatTextBridge] turn failed: %s", exc)
            self._safe_print(f"[chat] turn failed: {exc}")
            return None
        finally:
            if not token_wait.done():
                token_wait.cancel()
                try:
                    await token_wait
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def _safe_print(self, line: str) -> None:
        try:
            self._sink(line)
        except Exception:  # noqa: BLE001
            logger.debug("[ChatTextBridge] print sink degraded", exc_info=True)


# ---------------------------------------------------------------------------
# Factory — composes the canonical top-of-chain dispatcher factory
# ---------------------------------------------------------------------------


def build_chat_text_multiplexer(
    *,
    project_root: Optional[Path] = None,
    print_sink: Optional[Callable[[str], None]] = None,
    session_id: str = "repl",
    dispatcher: Optional[Any] = None,
) -> Optional[ChatTextMultiplexer]:
    """Build the bridge over the canonical executor chain.

    Returns ``None`` when the bridge master OR the chat master
    (``JARVIS_CONVERSATIONAL_MODE_ENABLED``, inside the chained factory)
    is off — callers keep the legacy debug-log fall-through. The
    dispatcher comes from ``build_chat_repl_dispatcher_with_claude``,
    the TOP of the existing executor chain (Claude → Subagent → Backlog
    → Logging, each leg behind its own flag) — zero chain logic is
    re-implemented here. NEVER raises."""
    try:
        if not bridge_enabled():
            return None
        d = dispatcher
        if d is None:
            from backend.core.ouroboros.governance.chat_repl_claude_executor import (  # noqa: E501
                build_chat_repl_dispatcher_with_claude,
            )
            d = build_chat_repl_dispatcher_with_claude(
                project_root=project_root,
            )
        if d is None:
            return None
        return ChatTextMultiplexer(
            d, print_sink=print_sink, session_id=session_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ChatTextBridge] factory degraded", exc_info=True)
        return None


__all__ = [
    "BOOST_REASON",
    "CancellationToken",
    "ChatTextMultiplexer",
    "SIGNAL_AST",
    "SIGNAL_FENCED",
    "bridge_enabled",
    "build_chat_text_multiplexer",
    "code_shape_signals",
    "weighted_classify",
]

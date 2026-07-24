"""Operator Prompt Bridge — attached cockpits ANSWER the organism's questions.

The gap this closes (cockpit-completeness audit, 2026-07-23): interactive
gates (`Iron Gate [Y/n]`, endorse prompts) awaited ``prompt_async`` on the
DAEMON's local stdin only. An attached ``ov`` operator saw the mirrored
question but their typed reply routed to the REPL chat surface — the
pending prompt was unreachable. HITL must be an available override from
EVERY surface the operator actually watches.

Design — one single-slot pending-prompt registry, raced not replaced:

* A prompt site calls :meth:`OperatorPromptBridge.begin` to arm the slot
  and receives an ``asyncio.Future``; it races that future against its
  local ``prompt_async`` — FIRST answer wins, loser is cancelled. The
  local terminal loses nothing; the cockpit gains everything.
* The harness's attach-input handler calls :meth:`resolve` BEFORE the
  REPL dispatcher: while a prompt is pending, the next operator line from
  ANY attached terminal is the answer, not chat. With no pending prompt,
  ``resolve`` declines instantly and input flows to the REPL untouched.
* Single-slot by design: the FSM serializes interactive gates; a second
  ``begin`` supersedes (cancels) the first rather than queueing —
  superseded prompts fall back to their local surface.

Zero authority (carries text, decides nothing); asyncio-native;
NEVER raises anywhere. Master: ``JARVIS_OPERATOR_PROMPT_BRIDGE_ENABLED``
(default on).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.OperatorPromptBridge")

_TRUTHY = ("1", "true", "yes", "on")


def prompt_bridge_enabled() -> bool:
    """Master gate — default ON. Re-read at call time. NEVER raises."""
    return os.environ.get(
        "JARVIS_OPERATOR_PROMPT_BRIDGE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


class OperatorPromptBridge:
    """Single-slot pending-prompt registry (see module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[str, asyncio.Future]] = None

    # -- prompt side ----------------------------------------------------

    def begin(self, prompt_id: str) -> Optional[asyncio.Future]:
        """Arm the slot for ``prompt_id`` and return the answer future
        (resolved with the operator's raw text). A prior pending prompt
        is superseded (its future cancelled — it falls back to its local
        surface). Returns None when the bridge is disabled or no running
        loop exists. NEVER raises."""
        if not prompt_bridge_enabled():
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        try:
            fut: asyncio.Future = loop.create_future()
            with self._lock:
                prev = self._pending
                self._pending = (str(prompt_id), fut)
            if prev is not None and not prev[1].done():
                prev[1].cancel()
            return fut
        except Exception:  # noqa: BLE001
            return None

    def end(self, fut: Optional[asyncio.Future]) -> None:
        """Disarm the slot IF it still holds ``fut`` (a superseding
        prompt keeps its own slot). Always safe to call. NEVER raises."""
        try:
            with self._lock:
                if self._pending is not None and self._pending[1] is fut:
                    self._pending = None
            if fut is not None and not fut.done():
                fut.cancel()
        except Exception:  # noqa: BLE001
            pass

    # -- input side (harness attach handler) ----------------------------

    @property
    def waiting(self) -> bool:
        try:
            with self._lock:
                p = self._pending
            return p is not None and not p[1].done()
        except Exception:  # noqa: BLE001
            return False

    def resolve(self, text: Any) -> bool:
        """Offer one operator line to the pending prompt. True when the
        line was CONSUMED as the answer (caller must not route it to the
        REPL); False when no prompt is pending. Thread-safe: marshals to
        the future's loop. NEVER raises."""
        try:
            with self._lock:
                p = self._pending
                if p is None or p[1].done():
                    return False
                self._pending = None
            _pid, fut = p
            answer = str(text if text is not None else "").strip()

            def _set() -> None:
                if not fut.done():
                    fut.set_result(answer)

            loop = getattr(fut, "get_loop", lambda: None)()
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(_set)
            else:
                _set()
            logger.info(
                "[OperatorPromptBridge] prompt %s answered via attach",
                _pid,
            )
            return True
        except Exception:  # noqa: BLE001
            return False


_BRIDGE: Optional[OperatorPromptBridge] = None
_BRIDGE_LOCK = threading.Lock()


def get_operator_prompt_bridge() -> OperatorPromptBridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = OperatorPromptBridge()
        return _BRIDGE


def reset_bridge_for_tests() -> None:
    global _BRIDGE
    with _BRIDGE_LOCK:
        _BRIDGE = None

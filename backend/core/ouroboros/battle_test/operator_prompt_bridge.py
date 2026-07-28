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
  REPL dispatcher: while a prompt is pending, an operator line that IS a
  verdict answers it. With no pending prompt — or with a line that is not
  a verdict — ``resolve`` declines and the text flows to the REPL untouched.

  **This was originally "the next line, whatever it is."** That is a live
  hazard once the organism raises gates asynchronously, because the operator
  is typing a goal, not answering a question they may not have noticed:

    - ``go fix the flaky test``  → first token ``go`` → **APPROVED** an
      unrelated APPROVAL_REQUIRED op, and the goal never reached the REPL.
    - ``stop the doc_staleness storm`` → **REJECTED** one.
    - ``let's build the streaming next`` → not a verdict, yet still consumed:
      silently dropped on the floor while the gate re-armed.

  So a verdict must now be UNAMBIGUOUS: the whole line, one word, in the
  shared vocabulary. A sentence is a goal. See :func:`is_bare_verdict`.
* Single-slot by design: the FSM serializes interactive gates; a second
  ``begin`` supersedes (cancels) the first rather than queueing —
  superseded prompts fall back to their local surface.
* Because the slot can be superseded between a prompt being SHOWN and being
  ANSWERED, :meth:`resolve` accepts an optional ``prompt_id`` and refuses to
  answer a different one. Without that check a deferred "y" — meant for the
  gate the operator read — would land on whichever op happens to hold the
  slot now. Approving the wrong op is the worst outcome this file can have.

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


def strict_verdicts_enabled() -> bool:
    """Default ON. Off restores "any line answers", which is unsafe once
    gates arrive asynchronously — kept only as a rollback."""
    return os.environ.get(
        "JARVIS_PROMPT_BRIDGE_STRICT_VERDICTS", "1",
    ).strip().lower() in _TRUTHY


def is_bare_verdict(text: Any) -> Optional[bool]:
    """``True`` approve, ``False`` reject, ``None`` "not an answer".

    The VOCABULARY is `approval_narrator.interpret_answer` — one definition
    of what "yes" means, shared with the local terminal path. What is added
    here is strictness about SHAPE: the whole line must be a single word.

    That distinction is the entire safety property. `interpret_answer` reads
    the first token, which is right at an explicit ``[y/n]`` prompt where the
    operator is deliberately answering, and wrong for an unprompted line
    typed by someone composing a goal — where ``go fix the flaky test``
    would otherwise approve an op they never looked at.

    Imported lazily: governance already imports this module, so a top-level
    import would close the cycle. Falls back to a local vocabulary if that
    import fails, because the bridge must keep working regardless.
    """
    try:
        raw = str(text if text is not None else "").strip()
        if not raw:
            return None
        if strict_verdicts_enabled() and len(raw.split()) != 1:
            # A sentence is a goal, not a verdict. Punctuation is tolerated
            # below ("y." / "no!") — an extra WORD is what disqualifies.
            return None
        token = raw.split()[0].strip(".,!;:'\"").lower()
        if not token:
            return None
        try:
            from backend.core.ouroboros.governance.approval_narrator import (
                interpret_answer,
            )
            return interpret_answer(token)
        except Exception:  # noqa: BLE001
            if token in ("y", "yes", "approve", "ok", "go", "ship", "accept"):
                return True
            if token in ("n", "no", "reject", "stop", "deny", "cancel",
                         "abort"):
                return False
            return None
    except Exception:  # noqa: BLE001
        return None


def prompt_bridge_enabled() -> bool:
    """Master gate — default ON. Re-read at call time. NEVER raises."""
    return os.environ.get(
        "JARVIS_OPERATOR_PROMPT_BRIDGE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


#: Set once at harness boot to the attach server's publishers. Injected
#: rather than imported because the server is an instance the harness owns —
#: the same reason `markup_mirror` and `set_operator_dispatcher` are wired
#: this way. Absent (unit tests, no cockpit), announcing is a silent no-op
#: and the bridge behaves exactly as it always has.
_ANNOUNCE: Optional[Any] = None
_ANNOUNCE_DONE: Optional[Any] = None


def set_prompt_publisher(
    announce: Optional[Any], resolved: Optional[Any] = None,
) -> None:
    """Install the cockpit announce sinks. ``None`` disarms them."""
    global _ANNOUNCE, _ANNOUNCE_DONE
    _ANNOUNCE, _ANNOUNCE_DONE = announce, resolved


def _announce(prompt_id: str, text: str, risk: str, timeout_s: float) -> None:
    fn = _ANNOUNCE
    if fn is None:
        return
    try:
        fn(prompt_id, text, risk=risk, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 — a gate must never fail on telemetry
        logger.debug("[OperatorPromptBridge] announce degraded", exc_info=True)


def _announce_done(prompt_id: str) -> None:
    fn = _ANNOUNCE_DONE
    if fn is None or not prompt_id:
        return
    try:
        fn(prompt_id)
    except Exception:  # noqa: BLE001
        logger.debug("[OperatorPromptBridge] resolve-announce degraded",
                     exc_info=True)


class OperatorPromptBridge:
    """Single-slot pending-prompt registry (see module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[str, asyncio.Future]] = None

    # -- prompt side ----------------------------------------------------

    def begin(
        self,
        prompt_id: str,
        *,
        text: str = "",
        risk: str = "",
        timeout_s: float = 0.0,
    ) -> Optional[asyncio.Future]:
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
                # A superseded gate is closed as far as cockpits are
                # concerned; leaving it queued would offer the operator a
                # question nothing is listening to.
                _announce_done(prev[0])
            # Announced HERE because arming the slot is exactly what makes a
            # gate answerable from a cockpit. Announcing at the call sites
            # instead would mean every future gate has to remember to, and
            # the one that forgets is invisible — the wired-but-inert defect
            # this codebase keeps finding.
            _announce(str(prompt_id), text, risk, float(timeout_s or 0.0))
            return fut
        except Exception:  # noqa: BLE001
            return None

    def end(self, fut: Optional[asyncio.Future]) -> None:
        """Disarm the slot IF it still holds ``fut`` (a superseding
        prompt keeps its own slot). Always safe to call. NEVER raises."""
        try:
            closed = None
            with self._lock:
                if self._pending is not None and self._pending[1] is fut:
                    closed = self._pending[0]
                    self._pending = None
            if fut is not None and not fut.done():
                fut.cancel()
            if closed:
                _announce_done(closed)
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

    @property
    def pending_id(self) -> Optional[str]:
        """Which prompt currently holds the slot, or None."""
        try:
            with self._lock:
                p = self._pending
            return p[0] if p is not None and not p[1].done() else None
        except Exception:  # noqa: BLE001
            return None

    def resolve(self, text: Any, prompt_id: Optional[str] = None) -> bool:
        """Offer one operator line to the pending prompt.

        True when the line was CONSUMED as the answer (caller must not route
        it to the REPL). False when no prompt is pending, when the line is
        not a verdict, or when *prompt_id* names a prompt that no longer
        holds the slot.

        Returning False for a non-verdict is the load-bearing part: the line
        then reaches the REPL, which is where the operator meant it to go.
        Consuming it — the original behaviour — dropped a typed goal on the
        floor while the gate silently re-armed.

        *prompt_id*, when given, must match the armed prompt. A deferred
        answer can be sitting in a cockpit's queue while the slot moves on to
        a different op, and landing "y" on the wrong gate is the worst thing
        this class could do. Callers with no id (the local terminal, which
        cannot be stale) keep the old behaviour.

        Thread-safe: marshals to the future's loop. NEVER raises.
        """
        try:
            answer = str(text if text is not None else "").strip()
            if strict_verdicts_enabled() and is_bare_verdict(answer) is None:
                # Not an answer — leave the gate armed and let the text pass
                # through to the REPL untouched.
                return False
            with self._lock:
                p = self._pending
                if p is None or p[1].done():
                    return False
                if prompt_id is not None and p[0] != str(prompt_id):
                    logger.info(
                        "[OperatorPromptBridge] declined stale answer for %s "
                        "(slot holds %s)", prompt_id, p[0],
                    )
                    return False
                self._pending = None
            _pid, fut = p

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
            _announce_done(_pid)
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

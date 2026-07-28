"""A gate never interrupts a half-typed line.

The organism raises `APPROVAL_REQUIRED` gates on its own schedule. The
operator types on theirs. Those two clocks are independent, so sooner or
later a gate arrives in the middle of a sentence — and the moment it does,
whatever happens next has to be something the operator chose.

The hazard is not a stolen cursor
---------------------------------
Nothing in the cockpit calls `set_focus`, so there is no focus to steal. The
real race was on the DAEMON, and it was worse: `_on_input` offered every
attached line to the pending gate BEFORE the REPL, and the gate consumed
whatever it got. Typing ``go fix the flaky test`` while a gate was open
approved an op the operator had not read, and the goal never reached the
REPL. That is fixed at the source in `operator_prompt_bridge` — a verdict now
has to be one bare word.

This module is the other half: making sure the operator KNOWS a question is
open, without the question seizing the screen mid-thought.

The rule
--------
A gate arriving while the buffer holds text does not display. It is queued,
and a badge appears in the toolbar::

    [ ⚠ 1 pending approval · ctrl-p ]

It surfaces when the operator is ready — explicitly with ``Ctrl+P``, or
implicitly the moment their buffer goes empty (they submitted or cleared it).
Either way the gate appears in a lull, and an Enter meant for their own line
can never land on someone else's ``[y/n]``.

Deferral makes staleness possible
---------------------------------
A queued gate is a gate nobody has answered yet, and the organism does not
wait forever — `await_decision` stamps EXPIRED on its own timeout. So a
queued prompt carries its deadline and is dropped on pop rather than shown:
presenting a dead gate as actionable would let the operator believe they
approved something they did not. Same reason the bridge refuses an answer
whose `prompt_id` no longer holds the slot — deferral is exactly the window
in which the slot can move on.

No authority: this decides WHEN a question is visible, never what the answer
is. Master: ``JARVIS_FOCUS_SHIELD_ENABLED`` (default on). Off = the previous
behaviour, where a gate simply scrolled past in the deck.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger("Ouroboros.FocusShield")

__all__ = [
    "PendingPrompt",
    "FocusShield",
    "focus_shield_enabled",
    "parse_prompt_frame",
]

#: A cockpit is not a ticket queue. Past this, the OLDEST pending gates are
#: dropped — they are the ones closest to expiring anyway, and an operator
#: who has ignored eight approvals is not helped by a ninth.
_MAX_PENDING = 8

#: Used only when a frame carries no deadline of its own. Matches the
#: `JARVIS_REVIEW_TIMEOUT_S` default so a queued gate and the op it belongs
#: to disagree as little as possible.
_DEFAULT_TIMEOUT_S = 300.0


def focus_shield_enabled() -> bool:
    """Default ON. Off, gates render inline exactly as they did before."""
    return os.environ.get(
        "JARVIS_FOCUS_SHIELD_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class PendingPrompt:
    """One deferred question, and the deadline it is living under."""

    __slots__ = ("prompt_id", "text", "risk", "timeout_s", "received_at")

    def __init__(
        self,
        prompt_id: str,
        text: str = "",
        risk: str = "",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        received_at: float = 0.0,
    ) -> None:
        self.prompt_id = str(prompt_id or "")
        self.text = str(text or "")
        self.risk = str(risk or "")
        try:
            self.timeout_s = float(timeout_s)
        except Exception:  # noqa: BLE001
            self.timeout_s = _DEFAULT_TIMEOUT_S
        self.received_at = float(received_at)

    def seconds_left(self, now: float) -> float:
        if self.timeout_s <= 0:
            # No deadline declared — never auto-expire. Dropping a gate the
            # daemon intends to hold open would be the shield inventing an
            # expiry the organism never agreed to.
            return float("inf")
        return self.timeout_s - (now - self.received_at)

    def expired(self, now: float) -> bool:
        return self.seconds_left(now) <= 0

    @property
    def ref(self) -> str:
        """The op's TAIL. UUIDv7 is time-ordered, so ops raised in the same
        session share their leading bytes and differ only at the end."""
        parts = [p for p in self.prompt_id.replace(":", "-").split("-") if p]
        return "-".join(parts[-2:]) if len(parts) >= 3 else (
            self.prompt_id or "op"
        )

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<PendingPrompt {self.prompt_id!r} risk={self.risk!r}>"


def parse_prompt_frame(frame: Any) -> Optional[PendingPrompt]:
    """Build a PendingPrompt from a wire frame. None if it is not one.

    Tolerant by design: a frame from a newer daemon must not crash an older
    cockpit, and a prompt with no id is unanswerable — the bridge would have
    nothing to match — so it is refused rather than shown.
    """
    try:
        if not isinstance(frame, dict):
            return None
        prompt_id = str(frame.get("prompt_id", "") or "").strip()
        if not prompt_id:
            return None
        return PendingPrompt(
            prompt_id=prompt_id,
            text=str(frame.get("text", "") or ""),
            risk=str(frame.get("risk", "") or ""),
            timeout_s=frame.get("timeout_s", _DEFAULT_TIMEOUT_S),
        )
    except Exception:  # noqa: BLE001
        return None


class FocusShield:
    """Decides when a pending question is allowed on screen."""

    def __init__(
        self,
        *,
        show: Optional[Callable[[PendingPrompt], None]] = None,
        notify: Optional[Callable[[], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        max_pending: int = _MAX_PENDING,
    ) -> None:
        # INJECTED sinks and clock: the FSM must be provable with no
        # Application, no socket and no wall-clock dependence. The last
        # focus-adjacent rule that read live global state returned the wrong
        # answer under test and looked correct.
        self._show = show
        self._notify = notify
        self._clock = clock or time.monotonic
        self._queue: Deque[PendingPrompt] = deque(maxlen=max(1, max_pending))
        self._showing: Optional[PendingPrompt] = None
        self.dropped_expired = 0
        self.dropped_overflow = 0

    # -- arrival -----------------------------------------------------------

    def offer(self, prompt: Any, *, composing: bool) -> str:
        """Take delivery of a gate. Returns ``shown`` / ``queued`` / ``""``.

        *composing* is the whole decision: it is the caller's answer to "does
        the operator have text in their buffer right now". Passed in rather
        than read from a global app, so the rule is testable and so the
        cockpit and the plain client can answer it their own way.
        """
        try:
            pending = (
                prompt if isinstance(prompt, PendingPrompt)
                else parse_prompt_frame(prompt)
            )
            if pending is None:
                return ""
            if pending.received_at <= 0:
                pending.received_at = self._clock()

            if not focus_shield_enabled():
                # Shield off: straight to the surface, as before.
                self._present(pending)
                return "shown"

            # Re-arming sends the same id again (the bridge re-arms whenever
            # a line turns out not to be a verdict). Refresh rather than
            # stack, or one unanswered gate becomes a queue of itself.
            self._forget(pending.prompt_id)

            if composing or self._showing is not None:
                # Either the operator is mid-line, or a gate already holds
                # the overlay. Both mean "not now".
                if len(self._queue) == self._queue.maxlen:
                    self.dropped_overflow += 1
                self._queue.append(pending)
                self._ping()
                return "queued"

            self._present(pending)
            return "shown"
        except Exception:  # noqa: BLE001 — a gate must never crash a cockpit
            logger.debug("[FocusShield] offer degraded", exc_info=True)
            return ""

    # -- release -----------------------------------------------------------

    def pop(self) -> Optional[PendingPrompt]:
        """Surface the next live gate, or None.

        Expired entries are discarded on the way out, never shown. A queued
        gate whose deadline passed has already been decided by the organism
        — presenting it would invite an answer that changes nothing while
        reading as though it did.
        """
        try:
            if self._showing is not None:
                # One question at a time; the rest keep waiting.
                return None
            now = self._clock()
            while self._queue:
                candidate = self._queue.popleft()
                if candidate.expired(now):
                    self.dropped_expired += 1
                    logger.info(
                        "[FocusShield] dropped expired prompt %s",
                        candidate.prompt_id,
                    )
                    continue
                self._present(candidate)
                self._ping()
                return candidate
            self._ping()
            return None
        except Exception:  # noqa: BLE001
            logger.debug("[FocusShield] pop degraded", exc_info=True)
            return None

    def note_buffer(self, text: Any) -> Optional[PendingPrompt]:
        """Call whenever the input buffer changes. Pops once it is empty.

        This is the implicit half of the rule: the operator does not have to
        learn a key. They send their line, the buffer empties, and the
        question they were shielded from appears in the lull that follows.
        """
        try:
            if str(text or "").strip():
                return None
            return self.pop()
        except Exception:  # noqa: BLE001
            return None

    def dismiss(self, prompt_id: Any = None) -> None:
        """The showing gate was answered, expired, or decided elsewhere."""
        try:
            if prompt_id is None or (
                self._showing is not None
                and self._showing.prompt_id == str(prompt_id)
            ):
                self._showing = None
            self._forget(prompt_id)
            self._ping()
        except Exception:  # noqa: BLE001
            pass

    # -- state -------------------------------------------------------------

    @property
    def showing(self) -> Optional[PendingPrompt]:
        return self._showing

    @property
    def pending_count(self) -> int:
        """Live gates waiting. Expired ones are not counted — a badge that
        advertises a dead approval is worse than no badge."""
        try:
            now = self._clock()
            return sum(1 for p in self._queue if not p.expired(now))
        except Exception:  # noqa: BLE001
            return len(self._queue)

    def badge(self) -> str:
        """Toolbar fragment, or "" when there is nothing waiting."""
        try:
            count = self.pending_count
            if count <= 0:
                return ""
            noun = "approval" if count == 1 else "approvals"
            return f"⚠ {count} pending {noun} · ctrl-p"
        except Exception:  # noqa: BLE001
            return ""

    def snapshot(self) -> List[Dict[str, Any]]:
        """Queue contents for `/gates`-style inspection. Read-only."""
        try:
            now = self._clock()
            return [
                {
                    "prompt_id": p.prompt_id,
                    "ref": p.ref,
                    "risk": p.risk,
                    "seconds_left": round(p.seconds_left(now), 1),
                }
                for p in self._queue
            ]
        except Exception:  # noqa: BLE001
            return []

    # -- internals ---------------------------------------------------------

    def _present(self, prompt: PendingPrompt) -> None:
        self._showing = prompt
        if self._show is not None:
            try:
                self._show(prompt)
            except Exception:  # noqa: BLE001
                logger.debug("[FocusShield] show sink raised", exc_info=True)

    def _forget(self, prompt_id: Any) -> None:
        if prompt_id is None:
            return
        key = str(prompt_id)
        for existing in [p for p in self._queue if p.prompt_id == key]:
            try:
                self._queue.remove(existing)
            except ValueError:
                pass

    def _ping(self) -> None:
        if self._notify is not None:
            try:
                self._notify()
            except Exception:  # noqa: BLE001
                pass

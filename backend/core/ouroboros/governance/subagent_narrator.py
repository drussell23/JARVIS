"""Subagent work, visible while it happens.

`SubagentOrchestrator` already emits a lifecycle event for every dispatch —
`CommSink.emit_spawn` / `emit_result`, documented as "§7 Absolute
observability: every dispatch emits a spawn event". Those events reach
CommProtocol as HEARTBEAT frames with `phase="subagent_spawn"`.

Nothing rendered them. Grep for `subagent_spawn` across every cockpit surface
returns zero consumers, so an op that recruited four subagents and ran them in
parallel looked, from the operator's chair, exactly like an op that had
stalled. A live producer with no renderer — the same shape as the `/` palette
that was wired into a layout `ov` never mounted.

    ⏺ Explore(find every caller of _normalize)
      ⎿ 3 files · 41 refs · 2.1s
    ⏺ Review(candidate patch)
      ⎿ 2 findings · 4.7s

A DECORATOR, not a replacement
-------------------------------
`SubagentOrchestrator` takes ONE `comm` sink. Narrating by swapping it would
cost the CommProtocol spine that carries these events to the ledger, the
observability API and the SSE stream. Wrapping keeps both: the wrapped sink
sees every event unchanged, and narration is added beside it.

That also makes the failure modes independent. A narration fault cannot break
observability, and a CommProtocol fault cannot blank the cockpit — each is
caught where it happens.

Renders through the mirror seam, not to a console
--------------------------------------------------
Lines go to the same `markup_mirror` chokepoint every op-chrome line uses, so
they reach an attached cockpit by the route that is already proven to work,
and they inherit its escaping rules. This module never touches a Console: a
narrator that printed directly would render on the daemon's own terminal —
detached, unwatched — which is precisely the bug that hid verb output.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.SubagentNarrator")

__all__ = ["SubagentNarrationSink", "narration_enabled", "render_spawn",
           "render_result"]

#: How a subagent kind is SPOKEN. The orchestrator's type token is a routing
#: identifier ("EXPLORE"); this is the word an operator reads. Keyed off the
#: existing token, so a new subagent kind means a new entry here rather than a
#: renderer that has to learn a new concept.
_KIND_VOICE = {
    "explore": "Explore",
    "review": "Review",
    "plan": "Plan",
    "general": "Agent",
}

#: A goal is a sentence; a chrome line is a line. Clipped at the composer
#: rather than the emitter so the ledger keeps the full text.
_GOAL_MAX = 72


def narration_enabled() -> bool:
    """Master switch. Default ON — this closes an observability gap, and a
    dark default would leave it closed."""
    return os.environ.get(
        "JARVIS_SUBAGENT_NARRATION_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _voice(subagent_type: Any) -> str:
    token = str(getattr(subagent_type, "value", subagent_type) or "").lower()
    return _KIND_VOICE.get(token, token.title() or "Agent")


def _clip(text: str, limit: int = _GOAL_MAX) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def render_spawn(subagent_type: Any, goal: str) -> str:
    """``⏺ Explore(goal)`` — §04: ⏺ opens a primary action."""
    return f"⏺ {_voice(subagent_type)}({_clip(goal)})"


def render_result(result: Any, elapsed_s: Optional[float] = None) -> str:
    """``⎿ 3 files · 41 refs · 2.1s`` — §04: ⎿ is subordinate to the line above.

    Summarises rather than dumps. The full result is already in the ledger;
    what belongs on screen is enough to know whether it worked and what it
    cost — the same judgement that moved classifier internals off the chat
    reply.
    """
    bits = []
    try:
        ok = getattr(result, "ok", None)
        if ok is False:
            reason = _clip(str(getattr(result, "error", "") or "failed"), 48)
            bits.append(f"failed · {reason}")
        else:
            for attr, unit in (("files_touched", "files"),
                               ("findings", "findings"),
                               ("steps", "steps")):
                value = getattr(result, attr, None)
                if isinstance(value, (list, tuple, set)):
                    value = len(value)
                # Non-zero only. "0 findings" on an EXPLORE reports a
                # metric that belongs to REVIEW and reads as a result rather
                # than an absence — the same noise as a blank help column
                # filled for the sake of being filled.
                if isinstance(value, int) and value > 0:
                    bits.append(f"{value} {unit}")
            summary = getattr(result, "summary", "") or ""
            if not bits and summary:
                bits.append(_clip(str(summary), 48))
    except Exception:  # noqa: BLE001
        pass
    if isinstance(elapsed_s, (int, float)) and elapsed_s >= 0:
        bits.append(f"{elapsed_s:.1f}s")
    return f"  ⎿ {' · '.join(bits) if bits else 'done'}"


class SubagentNarrationSink:
    """Wraps a CommSink and narrates each event to the cockpit.

    Every method delegates FIRST and narrates second, so the wrapped sink
    cannot be starved by a rendering fault — observability is the contract
    that must not break, and narration is the addition.
    """

    def __init__(
        self,
        inner: Any = None,
        emit_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._inner = inner
        # INJECTED: this module must be testable with no SerpentFlow, no
        # bridge and no daemon. Absent a sink, narration is silently skipped
        # and delegation still happens.
        self._emit = emit_line
        self._started: Dict[str, float] = {}

    # -- narration ---------------------------------------------------------

    def _say(self, line: str) -> None:
        try:
            if not line or not narration_enabled():
                return
            sink = self._emit
            if sink is None:
                return
            sink(line)
        except Exception:  # noqa: BLE001 — never break a dispatch to draw it
            logger.debug("[SubagentNarrator] emit degraded", exc_info=True)

    # -- CommSink ----------------------------------------------------------

    def emit_spawn(
        self,
        parent_op_id: str,
        subagent_id: str,
        subagent_type: Any,
        goal: str,
    ) -> None:
        try:
            if self._inner is not None:
                self._inner.emit_spawn(
                    parent_op_id, subagent_id, subagent_type, goal,
                )
        except Exception:  # noqa: BLE001
            logger.debug("[SubagentNarrator] inner emit_spawn failed",
                         exc_info=True)
        try:
            # Monotonic: the elapsed figure must not jump if the wall clock
            # is adjusted mid-dispatch.
            self._started[str(subagent_id)] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        self._say(render_spawn(subagent_type, goal))

    def emit_result(
        self,
        parent_op_id: str,
        subagent_id: str,
        result: Any,
    ) -> None:
        try:
            if self._inner is not None:
                self._inner.emit_result(parent_op_id, subagent_id, result)
        except Exception:  # noqa: BLE001
            logger.debug("[SubagentNarrator] inner emit_result failed",
                         exc_info=True)
        elapsed: Optional[float] = None
        try:
            started = self._started.pop(str(subagent_id), None)
            if started is not None:
                elapsed = time.monotonic() - started
        except Exception:  # noqa: BLE001
            elapsed = None
        self._say(render_result(result, elapsed))

    # -- passthrough -------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward anything else to the wrapped sink.

        The CommSink protocol is structural and may grow. A decorator that
        implemented only the two methods it knew about would silently drop a
        third the day one is added.
        """
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

"""Who is working right now — the cockpit's agent view.

O+V dispatches subagents constantly: EXPLORE mapping callers, REVIEW trying
to refute a fix, a swarm sharding a large file. Every one of them announces
itself as a LINE in the deck (`op_subagent_spawn`) and another line when it
returns. Lines scroll. Thirty seconds later the operator has no idea whether
three agents are still running or all of them finished, and the only way to
find out is to scroll back and mentally diff spawns against results.

A line is an event. A roster is a STATE, and "who is working right now" is a
state question::

    ❯ ⏺ main
      ◯ Explore  Map ov completion architecture

Derived, not tracked separately
-------------------------------
Nothing new reports into this. It is fed from the two seams that already
exist — the spawn and result renderers — because a second reporting path
would eventually disagree with the deck about what happened, and then the
roster becomes a thing you have to check against the transcript.

Finishing is not guaranteed
---------------------------
An agent can die without returning: a provider timeout, a killed worker, a
daemon restart mid-flight. A roster that only removes entries on a result
would show ghosts forever, and a ghost is worse than an omission because it
implies work is still happening.

So a running entry has a deadline. Past it, it is reaped as `unknown` —
stated as unknown rather than quietly deleted, since "we lost track of this"
and "this finished" are different facts and only one of them is good news.

Selection survives the roster changing
--------------------------------------
The operator can be pointing at row 3 when an agent two rows above them
finishes. Selection is therefore held by AGENT ID, not by index: an index
would silently move the cursor onto a different agent at the exact moment
they press Enter.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.AgentRoster")

__all__ = ["AgentEntry", "AgentRoster", "agent_view_enabled", "format_duration"]

#: Rows drawn before the roster collapses to a count. A cockpit footer is a
#: glance, not a process table.
_MAX_ROWS = 8

#: Longest a running agent may go unheard from before it is presumed lost.
_STALE_S = 900.0


def agent_view_enabled() -> bool:
    """Default ON. Off, subagent lines still render in the deck as before."""
    return os.environ.get(
        "JARVIS_AGENT_VIEW_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _stale_after_s() -> float:
    try:
        return max(30.0, float(os.environ.get("JARVIS_AGENT_STALE_S", "")
                               or _STALE_S))
    except (TypeError, ValueError):
        return _STALE_S


def format_duration(seconds: float) -> str:
    """``5m 39s`` — the shape an operator reads at a glance.

    Sub-minute work keeps its seconds; hours drop them, because at that scale
    nobody is counting and the extra token only costs width.
    """
    try:
        total = max(0, int(round(float(seconds))))
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60:02d}s"
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    except (TypeError, ValueError):
        return "0s"


class AgentEntry:
    """One dispatched agent, and what is known about it."""

    __slots__ = ("agent_id", "kind", "goal", "state", "started", "finished",
                 "detail")

    def __init__(self, agent_id: str, kind: str = "", goal: str = "",
                 started: float = 0.0) -> None:
        self.agent_id = str(agent_id or "")
        self.kind = str(kind or "agent")
        self.goal = str(goal or "")
        #: running | finished | failed | unknown
        self.state = "running"
        self.started = float(started)
        self.finished: Optional[float] = None
        self.detail = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    def elapsed(self, now: float) -> float:
        end = self.finished if self.finished is not None else now
        return max(0.0, end - self.started)

    @property
    def glyph(self) -> str:
        # ⏺ filled = this session; ◯ hollow = delegated work happening
        # elsewhere. Failure and loss are NOT the same mark: one reported
        # back, the other never did.
        return {"running": "◯", "finished": "⏺",
                "failed": "✗", "unknown": "?"}.get(self.state, "◯")

    def label(self, width: int = 48) -> str:
        goal = " ".join(self.goal.split())
        if len(goal) > width:
            goal = goal[: width - 1].rstrip() + "…"
        return goal

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AgentEntry {self.agent_id!r} {self.kind} {self.state}>"


class AgentRoster:
    """Live agents, ordered oldest-first, with a selection the operator owns."""

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        max_rows: int = _MAX_ROWS,
    ) -> None:
        self._clock = clock or time.monotonic
        self._entries: Dict[str, AgentEntry] = {}
        self._order: List[str] = []
        self._max = max(1, int(max_rows))
        #: Held by ID, never by index — see the module docstring.
        self._selected: Optional[str] = None
        self.reaped = 0

    # -- the feed ----------------------------------------------------------

    def spawn(self, agent_id: str, kind: str = "", goal: str = "") -> None:
        """An agent was dispatched. NEVER raises."""
        try:
            key = str(agent_id or "").strip()
            if not key:
                return
            if key in self._entries:
                # A re-spawn of the same id is a retry, not a second agent.
                # Restarting its clock is more truthful than showing an
                # elapsed time that spans an attempt which already ended.
                self._entries[key].started = self._clock()
                self._entries[key].state = "running"
                return
            self._entries[key] = AgentEntry(key, kind, goal, self._clock())
            self._order.append(key)
            self._evict()
        except Exception:  # noqa: BLE001
            logger.debug("[AgentRoster] spawn degraded", exc_info=True)

    def finish(self, agent_id: str, state: str = "finished",
               detail: str = "") -> Optional[AgentEntry]:
        """An agent returned. Returns the entry, so the caller can announce
        it — the notice and the roster then cannot disagree about duration."""
        try:
            entry = self._entries.get(str(agent_id or "").strip())
            if entry is None or not entry.running:
                return None
            entry.state = state if state in ("finished", "failed") else "finished"
            entry.finished = self._clock()
            entry.detail = str(detail or "")
            return entry
        except Exception:  # noqa: BLE001
            return None

    def reap(self) -> int:
        """Presume lost anything running past its deadline.

        Marked `unknown`, not deleted: "we lost track of this" and "this
        finished" are different facts, and only one of them is good news.
        """
        try:
            now, limit, count = self._clock(), _stale_after_s(), 0
            for entry in self._entries.values():
                if entry.running and (now - entry.started) > limit:
                    entry.state = "unknown"
                    entry.finished = now
                    count += 1
            self.reaped += count
            return count
        except Exception:  # noqa: BLE001
            return 0

    # -- state -------------------------------------------------------------

    @property
    def entries(self) -> List[AgentEntry]:
        return [self._entries[k] for k in self._order if k in self._entries]

    @property
    def running_count(self) -> int:
        return sum(1 for e in self.entries if e.running)

    def finished_notice(self, entry: Optional[AgentEntry]) -> str:
        """``⏺ Agent "Map ov completion architecture" finished · 5m 39s``

        Quotes the GOAL rather than the id, because that is what the operator
        asked for and what they will recognise; the id means nothing to them.
        """
        try:
            if entry is None:
                return ""
            verb = {"finished": "finished", "failed": "failed",
                    "unknown": "lost"}.get(entry.state, "finished")
            took = format_duration(entry.elapsed(self._clock()))
            goal = entry.label(60) or entry.kind
            tail = f" · {entry.detail}" if entry.detail else ""
            return f'⏺ Agent "{goal}" {verb} · {took}{tail}'
        except Exception:  # noqa: BLE001
            return ""

    # -- selection ---------------------------------------------------------

    @property
    def selected(self) -> Optional[AgentEntry]:
        if self._selected is None:
            return None
        return self._entries.get(self._selected)

    def select(self, offset: int) -> Optional[AgentEntry]:
        """Move the cursor. Row 0 is `main` — the operator's own session,
        which is always present and cannot be dispatched or reaped."""
        try:
            rows = self.entries
            if not rows:
                self._selected = None
                return None
            ids = [None] + [e.agent_id for e in rows]   # None == main
            try:
                index = ids.index(self._selected)
            except ValueError:
                index = 0
            index = max(0, min(len(ids) - 1, index + int(offset)))
            self._selected = ids[index]
            return self.selected
        except Exception:  # noqa: BLE001
            return None

    # -- render ------------------------------------------------------------

    def render(self) -> List[str]:
        """Footer rows, or [] when nothing has been dispatched.

        An empty roster renders NOTHING — not "0 agents". A cockpit that
        always shows a section for work that is not happening spends a row
        saying nothing, every session, forever.
        """
        try:
            if not agent_view_enabled():
                return []
            self.reap()
            rows = self.entries
            if not rows:
                return []
            lines = ["  ↑/↓ to select · Enter to view", ""]
            cursor = "❯" if self._selected is None else " "
            lines.append(f"{cursor} ⏺ main")
            shown = rows[-self._max:]
            for entry in shown:
                mark = "❯" if self._selected == entry.agent_id else " "
                label = entry.label()
                took = ""
                if not entry.running:
                    took = f"  {format_duration(entry.elapsed(self._clock()))}"
                lines.append(
                    f"{mark} {entry.glyph} {entry.kind}  {label}{took}".rstrip()
                )
            hidden = len(rows) - len(shown)
            if hidden > 0:
                lines.append(f"    … {hidden} more")
            return lines
        except Exception:  # noqa: BLE001
            logger.debug("[AgentRoster] render degraded", exc_info=True)
            return []

    # -- internals ---------------------------------------------------------

    def _evict(self) -> None:
        # Drop the oldest FINISHED entry first. A running agent is never
        # evicted for age: it is the one thing on this list that is still
        # true.
        limit = self._max * 3
        while len(self._order) > limit:
            for key in list(self._order):
                entry = self._entries.get(key)
                if entry is None or not entry.running:
                    self._order.remove(key)
                    self._entries.pop(key, None)
                    break
            else:
                return


_ROSTER: Optional[AgentRoster] = None


def get_agent_roster() -> AgentRoster:
    """The process-wide roster.

    A module singleton for the same reason the plan checklist is one: the
    producer (SerpentFlow, at each spawn) and the consumer (the cockpit
    footer) sit in different layers with no handle to each other.
    """
    global _ROSTER
    if _ROSTER is None:
        _ROSTER = AgentRoster()
    return _ROSTER


def reset_roster_for_tests() -> None:
    global _ROSTER
    _ROSTER = None

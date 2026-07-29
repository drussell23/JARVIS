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

The roster lives in one process and is READ in another
------------------------------------------------------
This is the constraint that shapes everything below. The producer is the
daemon — `SerpentFlow` calls :meth:`AgentRoster.spawn` as each subagent is
dispatched. The consumer is a cockpit, which under ``ov attach`` is a
DIFFERENT PROCESS with its own empty module singleton. Mounting a render call
in the client would draw an empty roster forever, and it would look exactly
like a system that never dispatches agents.

So the module is split the way ``attach_heartbeat`` is split — two pure
halves over one schema:

* :meth:`AgentRoster.snapshot` — the daemon serialises its live state.
* :func:`render_roster` — a PURE function over that snapshot, which the
  in-process cockpit and the remote client both call.

:meth:`AgentRoster.render` is a thin delegation to the same function, so
there is exactly one place that decides what a roster looks like.

Elapsed, never a timestamp
--------------------------
A snapshot carries ``elapsed_s`` per agent, never ``started``.
``time.monotonic()`` is an arbitrary per-process origin: shipping one across
the bridge and subtracting it from the reader's clock yields a duration that
is wrong by however long the two processes have been alive, and wrong in a
way that looks plausible. Durations are computed where the clock lives.

Between frames the reader advances running agents by the snapshot's own age,
so seconds tick smoothly at 1 Hz instead of stepping.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.AgentRoster")

__all__ = [
    "ROSTER_SCHEMA_VERSION", "AgentEntry", "AgentRoster", "agent_view_enabled",
    "format_duration", "render_roster", "roster_hint",
]

#: Additive schema. A reader that does not know a field ignores it; a reader
#: that expects one it does not get renders without it. Bumped only if a
#: field's MEANING changes, never for an addition.
ROSTER_SCHEMA_VERSION = "roster.v1"

#: Rows drawn before the roster collapses to a count. A cockpit footer is a
#: glance, not a process table.
_MAX_ROWS = 8

#: Longest a running agent may go unheard from before it is presumed lost.
_STALE_S = 900.0

#: Narrowest goal column worth printing. Below this a label is all ellipsis
#: and the row costs a line to say nothing.
_MIN_LABEL = 18

#: The roster's own keys, declared to the ONE registry that knows what this
#: cockpit binds. Registered rather than printed so `/keys` can list them and
#: the rendered hint has a single source — the defect the registry exists to
#: prevent is a footer that advertises a key nothing binds.
_ROSTER_ACTIONS = (("↑/↓", "select"), ("Enter", "view"))


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


def _max_rows() -> int:
    """Rows drawn before collapsing to a count. Re-read at call time, so a
    resized cockpit or a changed preference takes effect without a restart."""
    try:
        return max(1, min(64, int(os.environ.get(
            "JARVIS_AGENT_VIEW_ROWS", "") or _MAX_ROWS)))
    except (TypeError, ValueError):
        return _MAX_ROWS


def roster_wire_rows() -> int:
    """Rows a SNAPSHOT carries, as distinct from rows a cockpit DRAWS.

    These are different questions and conflating them caps the wrong one. The
    producer cannot know how tall its readers are — a 60-row terminal and a
    24-row one attach to the same daemon — so if it serialises only what the
    smallest could draw, the roomy client is silently truncated by a peer's
    screen. It therefore ships a generous window and lets each reader fold to
    its own budget.

    Generous, not unbounded: this rides a 1 Hz frame, so the window is what
    stops a 400-worker swarm from putting 400 rows on the wire every second
    for a reader that can draw twenty.
    """
    try:
        return max(1, min(128, int(os.environ.get(
            "JARVIS_AGENT_VIEW_WIRE_ROWS", "") or 24)))
    except (TypeError, ValueError):
        return 24


def _screen_share() -> float:
    """Fraction of the terminal the roster may take before it folds.

    A share, not a row count, because the constraint is proportional: eleven
    rows is a footer on a 60-row terminal and half the cockpit on a 24-row
    one, and the operator opened the cockpit to watch the DECK. Tunable, but
    clamped — a roster permitted the whole screen is a process table, and this
    is a glance.
    """
    try:
        return max(0.05, min(0.75, float(os.environ.get(
            "JARVIS_AGENT_VIEW_SCREEN_SHARE", "") or 0.35)))
    except (TypeError, ValueError):
        return 0.35


def roster_line_budget(term_rows: Optional[int]) -> Optional[int]:
    """Terminal rows the roster may occupy, or None when height is unknown.

    None rather than a guess: :func:`render_roster` folds only when it is
    given a budget, so an unknown height degrades to "show the snapshot's own
    window" — the behaviour before there was a budget at all — instead of
    folding against a number nobody measured.
    """
    try:
        if not term_rows or int(term_rows) <= 0:
            return None
        return max(_CHROME_ROWS + 2, int(int(term_rows) * _screen_share()))
    except (TypeError, ValueError):
        return None


def _register_keys() -> None:
    """Publish the roster's keys to the canonical registry. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.keybinding_registry import (
            register_keybinding,
        )
        for key, action in _ROSTER_ACTIONS:
            register_keybinding(
                key=key, action=action,
                source_file="battle_test/agent_roster.py",
            )
    except Exception:  # noqa: BLE001 — an unregistered key still works
        logger.debug("[AgentRoster] key registration degraded", exc_info=True)


def roster_hint() -> str:
    """``↑/↓ to select · Enter to view`` — composed from the registered keys.

    Read back from the declaration rather than written twice, so a rebind
    cannot leave the cockpit advertising a key that no longer does anything.
    """
    try:
        return " · ".join(f"{k} to {a}" for k, a in _ROSTER_ACTIONS)
    except Exception:  # noqa: BLE001
        return ""


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
                 "detail", "op_id", "parent_id", "worktree")

    def __init__(self, agent_id: str, kind: str = "", goal: str = "",
                 started: float = 0.0, op_id: str = "",
                 parent_id: str = "", worktree: str = "") -> None:
        self.agent_id = str(agent_id or "")
        self.kind = str(kind or "agent")
        self.goal = str(goal or "")
        #: running | finished | failed | unknown
        self.state = "running"
        self.started = float(started)
        self.finished: Optional[float] = None
        self.detail = ""
        # The organism's agents form a GRAPH — an op fans out to units,
        # a unit spawns subagents, each may hold its own worktree. The
        # roster modelled a flat list, so it could show WHO was working
        # and never what they were working ON, under whom, or in which
        # isolated tree. Those three facts are the difference between a
        # list of names and a picture of the work.
        self.op_id = str(op_id or "")
        self.parent_id = str(parent_id or "")
        #: The isolated tree this agent mutates, when it has one. L3
        #: promises isolation; an operator cannot verify a promise they
        #: cannot see.
        self.worktree = str(worktree or "")

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

    def as_row(self, now: float) -> Dict[str, Any]:
        """This agent as a transport-safe dict.

        ``elapsed_s``, never ``started`` — see the module docstring. The goal
        is carried UNCLIPPED: how much of it fits is the reader's question,
        because the reader is the one who knows how wide its terminal is.
        """
        row = {
            "id": self.agent_id,
            "kind": self.kind,
            "goal": self.goal,
            "state": self.state,
            "elapsed_s": round(self.elapsed(now), 1),
        }
        # Structural fields ride only when they are KNOWN. An absent key
        # is "this producer did not say", which a reader can render as
        # flat; a present-but-empty one would claim the agent has no
        # parent, and a false claim about the graph is worse than none.
        if self.op_id:
            row["op_id"] = self.op_id
        if self.parent_id:
            row["parent_id"] = self.parent_id
        if self.worktree:
            row["worktree"] = self.worktree
        return row

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AgentEntry {self.agent_id!r} {self.kind} {self.state}>"


class AgentRoster:
    """Live agents, ordered oldest-first, with a selection the operator owns."""

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        max_rows: Optional[int] = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._entries: Dict[str, AgentEntry] = {}
        self._order: List[str] = []
        #: ``None`` = follow the env at call time, so an operator who widens
        #: the window mid-session gets it. An explicit int PINS the window,
        #: which is what a test wants and what an embedding surface with a
        #: fixed budget of rows wants.
        self._max: Optional[int] = (
            max(1, int(max_rows)) if max_rows else None
        )
        #: Held by ID, never by index — see the module docstring.
        self._selected: Optional[str] = None
        self.reaped = 0
        _register_keys()

    @property
    def _window(self) -> int:
        return self._max if self._max is not None else _max_rows()

    # -- the feed ----------------------------------------------------------

    def spawn(self, agent_id: str, kind: str = "", goal: str = "",
              op_id: str = "", parent_id: str = "",
              worktree: str = "") -> None:
        """An agent was dispatched. NEVER raises.

        The structural arguments are OPTIONAL and default to empty, so
        every existing caller keeps working unchanged and a producer that
        knows more can say more. That is deliberate: this roster had
        exactly ONE producer for a long time, and the way to fix that is
        to make joining cheap, not to demand every caller learn a schema.
        """
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
            self._entries[key] = AgentEntry(
                key, kind, goal, self._clock(),
                op_id=op_id, parent_id=parent_id, worktree=worktree)
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

    def snapshot(self, *, max_rows: Optional[int] = None) -> Dict[str, Any]:
        """The roster as a transport-safe dict — the daemon's half.

        REAPS first, because the process holding the entries is the only one
        that can honestly decide an agent has gone quiet. A reader that reaped
        on its own would be guessing from a frame that is already a second
        old, and would eventually mark an agent lost that the daemon can see
        is running.

        Bounded to the visible window plus a ``hidden`` count. The whole point
        of the cap is that a 200-worker swarm must not turn a 1 Hz frame into
        a 200-row payload — the operator cannot read 200 rows either way, so
        the truthful summary is cheaper AND better.
        """
        try:
            if not agent_view_enabled():
                return {"schema_version": ROSTER_SCHEMA_VERSION, "rows": [],
                        "total": 0, "running": 0, "hidden": 0}
            self.reap()
            now = self._clock()
            rows = self.entries
            window = int(max_rows) if max_rows else self._window
            shown = rows[-window:] if window > 0 else []
            return {
                "schema_version": ROSTER_SCHEMA_VERSION,
                "rows": [e.as_row(now) for e in shown],
                "total": len(rows),
                "running": sum(1 for e in rows if e.running),
                "hidden": max(0, len(rows) - len(shown)),
            }
        except Exception:  # noqa: BLE001
            logger.debug("[AgentRoster] snapshot degraded", exc_info=True)
            return {"schema_version": ROSTER_SCHEMA_VERSION, "rows": [],
                    "total": 0, "running": 0, "hidden": 0}

    def render(self, *, width: Optional[int] = None) -> List[str]:
        """Footer rows for the IN-PROCESS cockpit.

        A thin delegation to :func:`render_roster` over this roster's own
        snapshot. Local and remote readers therefore cannot drift into two
        opinions about what a roster looks like — which they would, because
        the two would be edited months apart.
        """
        return render_roster(
            self.snapshot(), selected=self._selected, width=width,
        )

    # -- internals ---------------------------------------------------------

    def _evict(self) -> None:
        # Drop the oldest FINISHED entry first. A running agent is never
        # evicted for age: it is the one thing on this list that is still
        # true.
        limit = self._window * 3
        while len(self._order) > limit:
            for key in list(self._order):
                entry = self._entries.get(key)
                if entry is None or not entry.running:
                    self._order.remove(key)
                    self._entries.pop(key, None)
                    break
            else:
                return


# ---------------------------------------------------------------------------
# The reader's half — pure, stdlib-only, shared by both processes
# ---------------------------------------------------------------------------


#: state → mark. Failure and loss are NOT the same: one reported back, the
#: other never did. Defined once here because the reader may be holding a
#: snapshot from a process whose AgentEntry class it never imported.
_GLYPHS = {"running": "◯", "finished": "⏺", "failed": "✗", "unknown": "?"}


def _clip(text: Any, width: int) -> str:
    goal = " ".join(str(text or "").split())
    if width > 0 and len(goal) > width:
        return goal[: width - 1].rstrip() + "…"
    return goal


#: Rows the roster spends on itself. Just `main` now — the hint and its
#: blank line were two rows charged on every render to teach two keys once,
#: and they live in the keybinding registry where `?` and `/keys` find them.
_CHROME_ROWS = 1


def order_by_lineage(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Depth-first order with a ``depth`` stamped on each row. Pure.

    The organism's agents form a graph — an op fans out to units, a unit
    spawns subagents. Rendered flat, twelve agents are twelve names; ordered
    by lineage they are three ops with their work underneath, which is the
    question an operator actually has.

    Roots are anything whose parent is absent or unknown, so an orphan (a
    child whose parent already finished and was reaped) still renders at
    top level rather than vanishing. A cycle — which should be impossible
    and therefore will happen — is broken by a visited set, not by trusting
    the data. NEVER raises: a roster that cannot be sorted still lists.
    """
    try:
        by_id = {str(r.get("id") or ""): r for r in rows if r.get("id")}
        kids: Dict[str, List[Dict[str, Any]]] = {}
        roots: List[Dict[str, Any]] = []
        for r in rows:
            parent = str(r.get("parent_id") or "")
            if parent and parent in by_id:
                kids.setdefault(parent, []).append(r)
            else:
                roots.append(r)
        out: List[Dict[str, Any]] = []
        seen = set()

        def _walk(row: Dict[str, Any], depth: int) -> None:
            rid = str(row.get("id") or "")
            if rid in seen or depth > 6:
                return
            seen.add(rid)
            row = dict(row)
            row["depth"] = depth
            out.append(row)
            for child in kids.get(rid, ()):
                _walk(child, depth + 1)

        for root in roots:
            _walk(root, 0)
        # Anything unreachable from a root (a cycle) still gets listed —
        # losing an agent from the view is worse than losing its indent.
        for r in rows:
            if str(r.get("id") or "") not in seen:
                r = dict(r)
                r["depth"] = 0
                out.append(r)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[AgentRoster] lineage ordering degraded", exc_info=True)
        try:
            return list(rows or ())
        except Exception:  # noqa: BLE001
            return []


def render_roster(
    snapshot: Optional[Dict[str, Any]],
    *,
    selected: Optional[str] = None,
    age_s: float = 0.0,
    width: Optional[int] = None,
    max_lines: Optional[int] = None,
) -> List[str]:
    """Footer rows for a roster snapshot, from any process. NEVER raises.

    An empty roster renders NOTHING — not "0 agents". A cockpit that always
    shows a section for work that is not happening spends a row saying
    nothing, every session, forever.

    ``age_s`` is how long ago the snapshot was taken. Running agents advance
    by it, so a 1 Hz frame still shows seconds ticking; finished ones do not,
    because their duration is settled and inventing motion in it would be a
    lie the reader has no way to check.

    ``width`` is the reader's terminal, so the goal column is sized where the
    terminal is known rather than guessed at the producer. A snapshot rendered
    into an 80-column client and a 200-column one differ in what they clip,
    and neither producer could have known which.

    ``max_lines`` is how many TERMINAL ROWS the roster may occupy — a
    different question from how many agents the snapshot holds, and the one a
    30-row cockpit running a 40-worker swarm actually needs answered. Rows
    beyond the budget are folded into the "… N more" count rather than
    silently dropped, because a roster that quietly shows six of forty is
    worse than one that says so.
    """
    try:
        if not agent_view_enabled() or not isinstance(snapshot, dict):
            return []
        rows = [r for r in (snapshot.get("rows") or ()) if isinstance(r, dict)]
        if not rows:
            return []
        # ORDERED BY LINEAGE before anything is measured or drawn: a flat
        # list of twelve agents is twelve names; the same twelve ordered by
        # who spawned whom is three ops with their work underneath. The
        # indent is the only thing that turns a roster into a picture.
        rows = order_by_lineage(rows)
        age = max(0.0, float(age_s or 0.0))
        # Fold to fit BEFORE anything is formatted: the count line has to
        # include what the height budget dropped, and it cannot if the drop
        # happens after the total was written.
        folded = 0
        if max_lines is not None:
            # Budget the "… N more" row up front. Reserving it only when
            # something overflows costs a row exactly when the overflow is
            # one — and then the reservation causes a second overflow.
            budget = int(max_lines) - _CHROME_ROWS - 1
            if budget < 1:
                return []
            if len(rows) > budget:
                folded = len(rows) - budget
                rows = rows[-budget:]
        # Chrome is `❯ ◯ Kind  ` plus a duration column. Whatever is left is
        # the goal's, floored so a narrow terminal drops the goal entirely
        # rather than printing three characters and an ellipsis.
        cols = int(width) if width and int(width) > 0 else 80
        kind_w = max((len(str(r.get("kind") or "")) for r in rows), default=8)
        room = cols - (kind_w + 16)
        label_w = room if room >= _MIN_LABEL else 0

        # NO permanent header.
        #
        # The hint and its blank line cost two rows every time an agent runs,
        # forever, to teach two keys once. Claude Code's task area is the
        # rows themselves; the keys live in `?`. A surface that re-teaches
        # itself on every render is spending the operator's screen on their
        # first minute of using it.
        #
        # `main` stays, because it is the row the cursor rests on and the
        # thing every other row is a child of.
        lines = []
        lines.append(f"{'❯' if selected is None else ' '} ⏺ main")
        for row in rows:
            state = str(row.get("state") or "running")
            mark = "❯" if selected and selected == row.get("id") else " "
            glyph = _GLYPHS.get(state, "◯")
            elapsed = float(row.get("elapsed_s") or 0.0)
            if state == "running":
                elapsed += age
            took = f"  {format_duration(elapsed)}"
            # Depth eats into the GOAL column, not the terminal width: an
            # indent that pushed the line wider would wrap on exactly the
            # deep rows it is meant to clarify.
            depth = max(0, min(4, int(row.get("depth") or 0)))
            indent = "  " * depth
            label = (_clip(row.get("goal"), max(8, label_w - len(indent)))
                     if label_w else "")
            body = f"{indent}{row.get('kind') or 'agent'}  {label}".rstrip()
            # The isolated tree this agent mutates. L3 PROMISES isolation;
            # an operator cannot verify a promise they cannot see.
            wt = str(row.get("worktree") or "")
            if wt:
                body = f"{body}  ⧉{_clip(wt, 18)}"
            lines.append(f"{mark} {glyph} {body}{took}".rstrip())
        # What the PRODUCER withheld plus what the height budget folded. Two
        # separate elisions, one honest number — reporting either alone would
        # undercount, and the operator reads this to decide whether they are
        # looking at all of it.
        hidden = int(snapshot.get("hidden") or 0) + folded
        if hidden > 0:
            lines.append(f"    … {hidden} more")
        return lines
    except Exception:  # noqa: BLE001
        logger.debug("[AgentRoster] render degraded", exc_info=True)
        return []


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

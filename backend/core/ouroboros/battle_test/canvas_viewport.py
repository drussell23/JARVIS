"""Scrollback for a cockpit that owns the whole screen.

Taking the alternate screen is what makes `ov` feel like an instrument rather
than a command that scrolled past — but smcup **disables the terminal's native
scrollback**. The primary buffer stops receiving output the moment the cockpit
mounts, so an operator scrolling up to re-read what the organism did an hour
ago finds nothing there.

That is why full-screen was defaulted OFF in #70171, and it is the actual
problem to solve. Turning the flag back on without this would not deliver a
full-screen cockpit; it would delete the history and call it a feature.

So the canvas stops being a tail and becomes a VIEWPORT: a window onto a much
longer history, with an offset the operator moves. `_visible_lines` used to
return ``snap[-budget:]`` — the last screenful, always. Now it returns the
screenful the operator is looking at.

Following, and the one rule that matters
----------------------------------------
At the bottom (offset 0) the view FOLLOWS: new telemetry appears as it
arrives, which is what a live cockpit should do when nobody is reading
history.

Scrolled up, it does **not** follow. The organism emits continuously, and a
view that drifted on every incoming line would make reading anything older
physically impossible — the text would slide out from under the reader
several times a second.

The offset is measured from the bottom, so it names a distance from the
NEWEST line — and every append moves the newest line. To keep the same
absolute text under the reader, the offset is advanced by exactly the number
of lines appended since the last frame.

That count has to come from the ring's monotonic ``push_count``, not from the
change in length, and this is the part that is easy to get wrong. Once the
ring is saturated every append also drops a line off the front, so the length
stops changing while the content keeps moving. A length-delta compensation is
therefore correct while the ring is filling and silently stops working the
moment it is full — the regime a long session spends all its time in.

Both errors were live here before they were caught: first `PgUp` walked the
view from line 489 to 529 as telemetry arrived, and the length-delta fix that
followed still walked it from 180 to 220 once the ring hit its cap.

Leaving history is explicit (`End`, or scrolling back down). Nothing the
organism does can steal the view.

No authority, no rendering: this decides WHICH slice is visible and nothing
else. It holds no lines, imports no prompt_toolkit, and is therefore provable
without a terminal — the property that the last focus-adjacent rule in this
codebase failed to have.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CanvasViewport")

__all__ = ["CanvasViewport", "scrollback_enabled", "canvas_history_lines",
           "scroll_speed", "install_scroll_bindings"]

#: History the canvas keeps once it is the ONLY history there is. The old 500
#: was sized for a tail beneath a terminal that still had its own scrollback;
#: in the alternate screen that assumption is gone. ~20k lines is minutes of
#: dense telemetry and a few MB at most — cheap next to losing the session.
_DEFAULT_HISTORY_LINES = 20000


def scrollback_enabled() -> bool:
    """Default ON. Off pins the canvas to the tail, as it behaved before."""
    return os.environ.get(
        "JARVIS_CANVAS_SCROLLBACK_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def scroll_speed() -> float:
    """Lines moved per wheel notch.

    Terminals disagree about wheel events: some send one per physical notch,
    others amplify already. Nothing can detect which, so this is a knob rather
    than a constant. 3 matches vim's default and is the value that feels right
    in terminals that do NOT amplify.
    """
    try:
        value = float(os.environ.get("JARVIS_SCROLL_SPEED", "3"))
        return min(20.0, max(0.25, value))
    except (TypeError, ValueError):
        return 3.0


def canvas_history_lines() -> int:
    """How many lines the canvas retains.

    Honours the pre-existing ``JARVIS_BIPARTITE_CANVAS_MAX_LINES`` so an
    operator who already tuned it is not overridden — reusing the knob that
    exists rather than introducing a second one that silently wins.
    """
    for name in ("JARVIS_CANVAS_HISTORY_LINES",
                 "JARVIS_BIPARTITE_CANVAS_MAX_LINES"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                return max(64, int(raw))
            except (TypeError, ValueError):
                continue
    return _DEFAULT_HISTORY_LINES


def auto_scroll_enabled() -> bool:
    """``JARVIS_AUTO_SCROLL`` (default true). NEVER raises.

    Claude Code's own escape hatch: "To turn auto-follow off entirely so the
    view stays where you leave it, open `/config` and set Auto-scroll to
    off." Off, the view never moves on its own and every arriving line is
    held — which is what an operator reading a long trace while the organism
    keeps working actually wants.
    """
    return os.environ.get(
        "JARVIS_AUTO_SCROLL", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class CanvasViewport:
    """Which slice of the history is on screen, and how the operator moves it.

    ``offset`` counts lines from the BOTTOM: 0 is live/following, larger means
    further back. Measuring from the bottom rather than the top is what lets
    the view hold still while the organism appends — a top-anchored index
    would drift under the reader as the ring rotated.
    """

    def __init__(self) -> None:
        self._offset = 0
        #: Last monotonic append count seen by `window`. None until the first
        #: frame, so the first render cannot mistake pre-existing history for
        #: lines that arrived while the operator was reading.
        self._last_appended: Optional[int] = None
        #: Set when a scroll is attempted past the oldest retained line, so the
        #: UI can say "this is everything that is kept" rather than appearing
        #: to freeze. Truncation the operator cannot see reads as a bug.
        self.hit_top = False
        #: Lines that arrived while the operator was reading history. Shown
        #: as a count rather than left implicit — "there is newer output" is
        #: the fact that decides whether they want to jump back to live.
        self.new_since_paused = 0
        #: Auto-follow held OFF explicitly, independent of the offset.
        #:
        #: Until this existed, "pinned to the tail" and "showing the newest
        #: line" were ONE state with one variable — `following` was derived
        #: from `_offset <= 0` — so there was no honest way to stop following
        #: without also moving the view. The transcript viewer documented
        #: that limitation rather than faking a pin, and this is the field
        #: that removes it: a reader can freeze the page they are on at the
        #: live tail, and arriving output holds still instead of scrolling
        #: the sentence they are mid-way through off the screen.
        #:
        #: Claude Code states the same property — "scrolling up pauses
        #: auto-follow so new output doesn't pull you back to the bottom" —
        #: and separately allows turning follow off entirely. Scrolling still
        #: pauses implicitly via the offset; this is the explicit half.
        self._paused = False
        #: Has this operator scrolled yet, this session?
        #:
        #: The alternate screen takes the terminal's OWN scrollback, and
        #: nothing replaces the muscle memory it took with it. So the tail
        #: teaches the key — ONCE. A surface that re-teaches itself on every
        #: render spends the operator's screen on their first minute of using
        #: it, forever.
        self.taught = False

    # -- state -------------------------------------------------------------

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def following(self) -> bool:
        """True when new output will pull the view along.

        BOTH conditions, because they are genuinely different reasons not to
        follow: an operator who scrolled up (`_offset > 0`) and one who froze
        the page they are on (`_paused`). Deriving this from the offset alone
        is what made a pin-without-moving impossible.
        """
        return self._offset <= 0 and not self._paused

    @property
    def paused(self) -> bool:
        """Auto-follow explicitly held, regardless of position."""
        return self._paused

    def pause(self) -> bool:
        """Freeze the view where it is. True if this changed anything.

        Deliberately does NOT move the offset. Freezing by scrolling would
        discard the line the operator was reading when they asked — which is
        the whole reason the derived version could not do this.
        """
        changed = not self._paused
        self._paused = True
        return changed

    def resume(self) -> bool:
        """Follow again. True if this changed anything.

        Does not move either: a caller that wants the tail calls
        `to_bottom`, and one that wants to keep reading where it is while
        letting output resume behind it gets exactly that.
        """
        changed = self._paused
        self._paused = False
        return changed

    def mark_taught(self) -> None:
        """The operator has found the scrollback; stop offering the key."""
        self.taught = True

    def reset(self) -> None:
        self._offset = 0
        self.hit_top = False
        self.new_since_paused = 0
        # Returning to live means FOLLOWING again. Leaving the flag set would
        # give an operator who jumped to the bottom a view pinned at the
        # bottom that never updates — the most confusing possible state,
        # because it looks exactly like a working tail on a dead organism.
        self._paused = False

    # -- movement ----------------------------------------------------------

    def scroll(self, lines: int, *, total: int, budget: int) -> bool:
        """Move by *lines* (negative = toward history). True if it moved.

        Clamped at both ends against the CURRENT total, so a scroll issued
        while the ring is short cannot strand the viewport in empty space
        above the first line.
        """
        try:
            budget = max(1, int(budget))
            total = max(0, int(total))
            ceiling = max(0, total - budget)
            want = self._offset - int(lines)
            new = max(0, min(ceiling, want))
            self.taught = True
            self.hit_top = want > ceiling and ceiling > 0
            if new == self._offset:
                return False
            self._offset = new
            return True
        except Exception:  # noqa: BLE001 — scrolling must never break a repaint
            logger.debug("[CanvasViewport] scroll degraded", exc_info=True)
            return False

    def page(self, direction: int, *, total: int, budget: int) -> bool:
        """One screenful. ``direction`` +1 is PgUp (older), -1 is PgDn.

        Overlaps by one line so nothing falls between consecutive pages.

        Note the sign flip into `scroll`, which takes NEGATIVE for older.
        The first cut passed the direction straight through, so `PgUp` moved
        toward the tail — where the viewport already was — and the key read
        as dead. It was only visible because the test asserted on the lines
        rather than on the return value.
        """
        # HALF a screen, not a full one. A full page leaves nothing on
        # screen to anchor against, so the reader loses their place at every
        # press; half keeps the previous context in view.
        step = max(1, int(budget) // 2)
        return self.scroll(-step if direction >= 0 else step,
                           total=total, budget=budget)

    def to_top(self, *, total: int, budget: int) -> bool:
        return self.scroll(-max(0, int(total)), total=total, budget=budget)

    def to_bottom(self) -> bool:
        """Return to live. The one move that is always available.

        "Already at offset 0" is NOT "already following" now that a pause can
        hold the tail without moving it. The early return used to be correct
        because the two were the same fact; with an explicit flag it left a
        reader who paused AT the tail permanently un-following — and every
        surface gated on `is_scrolled_back` then behaved as though they were
        still in history after they had left the viewer.
        """
        if self._offset == 0 and not self._paused:
            return False
        self.reset()
        return True

    # -- the slice ---------------------------------------------------------

    def window(
        self, lines: Sequence[str], budget: int, appended: Optional[int] = None,
    ) -> Tuple[List[str], int, int]:
        """``(visible, hidden_above, hidden_below)`` for this frame.

        Re-clamps against the live total on every call rather than trusting
        the stored offset: the ring drops old lines as it rotates, so an
        offset that was valid when the operator scrolled can point past the
        start a second later. Clamping HERE means the ring and the viewport
        cannot disagree about what exists.
        """
        try:
            budget = max(1, int(budget))
            snap = list(lines or ())
            total = len(snap)

            # Hold the view still while the organism appends. Only while
            # scrolled — at the tail, following is the whole point.
            #
            # `appended` is the ring's monotonic push count. Falling back to
            # the length is strictly worse (it goes blind once the ring is
            # saturated) and exists only so the class stays usable against a
            # plain list in tests.
            marker = total if appended is None else int(appended)
            previous, self._last_appended = self._last_appended, marker
            held = self._offset > 0 or self._paused or not auto_scroll_enabled()
            if held and previous is not None and marker > previous:
                arrived = marker - previous
                self._offset += arrived
                self.new_since_paused += arrived

            if total <= budget:
                # Everything fits; there is no history to be inside of.
                # `_paused` is NOT cleared here: a short transcript that grows
                # past the budget while the operator is reading must stay
                # held, and clearing the flag on a frame that happened to fit
                # would silently resume follow underneath them.
                self._offset = 0
                return snap, 0, 0
            ceiling = total - budget
            if self._offset > ceiling:
                self._offset = ceiling
            end = total - self._offset
            start = max(0, end - budget)
            return snap[start:end], start, total - end
        except Exception:  # noqa: BLE001
            logger.debug("[CanvasViewport] window degraded", exc_info=True)
            tail = list(lines or ())[-max(1, int(budget)):]
            return tail, 0, 0

    def tail_hint(self, hidden_above: int) -> str:
        """One line at the LIVE TAIL saying history exists, and how to reach it.

        The counterpart to :meth:`status`, which only ever renders while
        SCROLLED — it tells a reader how to get back, and nothing told them
        they could leave. In the alternate screen that is not a missing
        convenience: the terminal's own scrollback is gone, so if the
        operator does not know this key the history is unreachable.

        Names ``shift+↑``, not PgUp. A MacBook keyboard has no PageUp — it is
        ``Fn+↑`` — so a hint naming it sends exactly the operator who most
        needs help to a key they cannot press. Shift+arrow is bound to the
        same handler and every keyboard can send it.

        Shown only while there IS history above and only until the operator
        scrolls once. NEVER raises.
        """
        try:
            if self.taught or hidden_above <= 0:
                return ""
            return f"↑ {hidden_above} above · shift+↑ to scroll"
        except Exception:  # noqa: BLE001
            return ""

    def status(self, hidden_above: int, hidden_below: int) -> str:
        """One line telling the operator they are not looking at "now".

        Rendered only while scrolled: a permanent indicator would be chrome
        that costs a row and says nothing 99% of the time. Naming the key is
        deliberate — a reader who cannot find their way back to live has been
        trapped by the feature, not helped by it.
        """
        try:
            if hidden_below <= 0:
                return ""
            older = f"↑ {hidden_above} older" if hidden_above > 0 else "↑ top"
            if self.hit_top and hidden_above <= 0:
                older = "↑ oldest kept"
            # What ARRIVED while they were reading is the fact that decides
            # whether to jump back; the raw "lines below" count includes
            # everything they scrolled past and answers a different question.
            fresh = (f"{self.new_since_paused} new" if self.new_since_paused
                     else f"{hidden_below} newer")
            return f"{older} · {fresh} below · End to follow"
        except Exception:  # noqa: BLE001
            return ""


def install_scroll_bindings(
    kb: Any,
    viewport: CanvasViewport,
    metrics: Any,
    invalidate: Optional[Any] = None,
) -> bool:
    """Bind the movement keys every pager already taught operators.

    *metrics* is a callable returning ``(total, budget)`` — injected rather
    than reached for, so the bindings work against whatever surface owns the
    canvas and stay testable without one.

    NEVER raises: a cockpit that starts without scroll keys is far better
    than one that does not start.
    """
    try:
        if kb is None or not scrollback_enabled():
            return False

        def _move(fn: Any) -> Any:
            def _handler(event: Any) -> None:
                try:
                    total, budget = metrics()
                    if fn(total, budget) and invalidate is not None:
                        invalidate()
                except Exception:  # noqa: BLE001
                    pass
            return _handler

        kb.add("pageup")(_move(
            lambda t, b: viewport.page(1, total=t, budget=b)))
        kb.add("pagedown")(_move(
            lambda t, b: viewport.page(-1, total=t, budget=b)))
        # The MOUSE WHEEL. prompt_toolkit turns wheel events into these two
        # keys once `mouse_support` is on, so the wheel and the keyboard move
        # the same viewport through the same clamping — no second scroll path
        # that can disagree about where the bottom is.
        from prompt_toolkit.keys import Keys

        def _notch(direction: int) -> Any:
            def _fn(t: int, b: int) -> bool:
                step = max(1, int(round(scroll_speed())))
                return viewport.scroll(direction * step, total=t, budget=b)
            return _fn

        kb.add(Keys.ScrollUp)(_move(_notch(-1)))
        kb.add(Keys.ScrollDown)(_move(_notch(1)))
        kb.add("s-up")(_move(_notch(-1)))
        kb.add("s-down")(_move(_notch(1)))

        # Home/End AND their Ctrl variants. Both, deliberately: Ctrl+End is
        # the conventional jump-to-latest, and a MacBook keyboard cannot send
        # it at all (Ctrl+Fn+→ does not reach the app), so binding only that
        # would leave this machine with no way back to live.
        for key in ("end", "c-end"):
            kb.add(key)(_move(lambda _t, _b: viewport.to_bottom()))
        for key in ("home", "c-home"):
            kb.add(key)(_move(
                lambda t, b: viewport.to_top(total=t, budget=b)))
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[CanvasViewport] bindings degraded", exc_info=True)
        return False

"""The cockpit owns the screen, so it has to own the history too.

Taking the alternate screen disables the terminal's native scrollback. That
is why full-screen was defaulted off in #70171 — Zone 1 rendered
``snap[-budget:]``, the last screenful, with nothing reachable above it, so
claiming the screen deleted the session.

These pin the viewport that removes the objection, and in particular the two
bugs it took to get "the view holds still" actually working. Both were live,
and both looked fine from the outside.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.canvas_viewport import (
    CanvasViewport, canvas_history_lines, scrollback_enabled,
)


def _lines(n: int, start: int = 0) -> list:
    return [f"line {i}" for i in range(start, start + n)]


# --------------------------------------------------------------------------
# following
# --------------------------------------------------------------------------

def test_it_follows_the_tail_by_default() -> None:
    """A live cockpit should show now, when nobody is reading history."""
    v = CanvasViewport()
    visible, above, below = v.window(_lines(100), 10)
    assert v.following is True
    assert visible[-1] == "line 99"
    assert below == 0 and above == 90


def test_short_history_is_shown_whole() -> None:
    v = CanvasViewport()
    visible, above, below = v.window(_lines(3), 10)
    assert visible == ["line 0", "line 1", "line 2"]
    assert (above, below) == (0, 0)


# --------------------------------------------------------------------------
# movement
# --------------------------------------------------------------------------

def test_pageup_moves_toward_history() -> None:
    """The direction bug: `page(1)` first computed `offset - step`, which
    moves toward the TAIL — where the viewport already was — so PgUp was a
    dead key. Asserting on the returned lines alone missed it, because the
    lines were correct for a viewport that had not moved."""
    v = CanvasViewport()
    v.window(_lines(500), 11)
    assert v.page(1, total=500, budget=11) is True
    assert v.offset > 0
    assert v.following is False


def test_end_always_returns_to_live() -> None:
    v = CanvasViewport()
    v.window(_lines(500), 11)
    v.page(1, total=500, budget=11)
    assert v.to_bottom() is True
    assert v.following is True
    visible, _a, below = v.window(_lines(500), 11)
    assert visible[-1] == "line 499" and below == 0


def test_it_cannot_scroll_above_the_oldest_line() -> None:
    v = CanvasViewport()
    v.to_top(total=50, budget=10)
    visible, above, _b = v.window(_lines(50), 10)
    assert above == 0
    assert visible[0] == "line 0"


def test_scrolling_down_at_the_tail_is_a_no_op() -> None:
    v = CanvasViewport()
    assert v.scroll(5, total=100, budget=10) is False
    assert v.following is True


# --------------------------------------------------------------------------
# the rule that matters: the view holds still
# --------------------------------------------------------------------------

def test_the_view_holds_still_while_the_ring_grows() -> None:
    """Bug #1. The organism emits continuously. A view that drifted on every
    incoming line would make reading anything older impossible — the text
    slides out from under the reader several times a second."""
    v = CanvasViewport()
    history = _lines(500)
    v.window(history, 11)
    v.page(1, total=500, budget=11)
    before, _a, _b = v.window(history, 11, appended=500)

    history += _lines(140, start=500)          # telemetry keeps arriving
    after, _a, _b = v.window(history, 11, appended=640)

    assert after == before, "the view drifted while the operator was reading"


def test_the_view_holds_still_once_the_ring_is_SATURATED() -> None:
    """Bug #2, and the one that mattered more.

    The first fix compensated by the change in LENGTH. That works while the
    ring is filling and silently stops the moment it is full: every append
    then also drops a line off the front, so the length freezes while the
    content keeps moving — and a long session spends all its time in that
    regime. The view walked from line 180 to 220 with the length-delta fix
    in place. Compensation has to come from the monotonic append count.
    """
    v = CanvasViewport()
    cap, budget = 200, 11
    history = _lines(cap)
    v.window(history, budget, appended=cap)
    v.page(1, total=cap, budget=budget)
    before, _a, _b = v.window(history, budget, appended=cap)

    for i in range(140):                        # saturated: push == drop
        history = history[1:] + [f"line {cap + i}"]
    assert len(history) == cap, "the ring must be saturated for this to test"
    after, _a, _b = v.window(history, budget, appended=cap + 140)

    assert after == before, "bottom-anchoring drifted once the ring was full"


def test_following_is_NOT_frozen_by_the_compensation() -> None:
    """The compensation must apply only while scrolled — at the tail,
    following is the entire point of a live cockpit."""
    v = CanvasViewport()
    v.window(_lines(100), 10, appended=100)
    visible, _a, _b = v.window(_lines(140), 10, appended=140)
    assert visible[-1] == "line 139"
    assert v.following is True


def test_the_first_frame_is_not_mistaken_for_new_lines() -> None:
    """`_last_appended` starts None so pre-existing history cannot be read as
    having arrived while the operator was reading."""
    v = CanvasViewport()
    v.scroll(-5, total=500, budget=10)
    visible, _a, _b = v.window(_lines(500), 10, appended=500)
    assert visible[-1] == "line 494"


# --------------------------------------------------------------------------
# telling the operator where they are
# --------------------------------------------------------------------------

def test_no_status_while_following() -> None:
    """Permanent chrome would cost a row to say nothing 99% of the time."""
    v = CanvasViewport()
    _v, above, below = v.window(_lines(100), 10)
    assert v.status(above, below) == ""


def test_the_status_names_the_way_back() -> None:
    """A reader who cannot find their way back to live has been trapped by
    the feature rather than helped by it."""
    v = CanvasViewport()
    v.window(_lines(500), 11)
    v.page(1, total=500, budget=11)
    _vis, above, below = v.window(_lines(500), 11)
    status = v.status(above, below)
    assert "End" in status
    assert str(below) in status


def test_re_clamping_survives_a_ring_that_shrank() -> None:
    """The ring drops lines as it rotates, so an offset valid a moment ago
    can point past the start. Clamping in `window` is what keeps the ring and
    the viewport from disagreeing about what exists."""
    v = CanvasViewport()
    v.scroll(-400, total=500, budget=10)
    visible, above, _b = v.window(_lines(20), 10)
    assert len(visible) == 10
    assert above == 0


@pytest.mark.parametrize("junk", [None, "", 0, -5])
def test_it_never_raises_on_junk(junk) -> None:
    v = CanvasViewport()
    assert isinstance(v.window(_lines(20), junk or 1), tuple)
    assert isinstance(v.scroll(1, total=junk or 0, budget=junk or 1), bool)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def test_history_is_sized_for_a_session_not_a_tail() -> None:
    assert canvas_history_lines() >= 10000


def test_the_pre_existing_knob_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who already tuned the canvas must not be silently
    overridden by a second knob that means the same thing."""
    monkeypatch.setenv("JARVIS_BIPARTITE_CANVAS_MAX_LINES", "750")
    assert canvas_history_lines() == 750


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CANVAS_SCROLLBACK_ENABLED", "0")
    assert scrollback_enabled() is False

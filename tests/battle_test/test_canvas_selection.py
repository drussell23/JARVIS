"""Selecting text in the transcript, which has no selection model to borrow.

prompt_toolkit gives selection to BUFFERS, which is why selecting inside the
ov prompt always worked and needed no code. The canvas is a
`FormattedTextControl`: styled runs with no cursor, no anchor, and no notion
that two of its characters might be "between" each other.

The failures pinned here are the ones a naive implementation ships:

  * the single-row case written as the multi-row case with the middle
    removed — which slices row N from `start` to end AND from start to
    `end`, copying the row twice;
  * a backwards drag (up and to the left) selecting nothing;
  * an inclusive end, so a one-cell drag selects two cells;
  * a drag treated as a click on release, expanding whatever it began on;
  * a highlight emitted per CHARACTER rather than per run, handing every
    downstream renderer a screenful of one-character fragments.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import canvas_selection as S

ROWS = [
    "│ ⏺ Read(backend/x.py)            │",
    "│ ⎿ collected 41 items            │",
    "│ ⎿ 3 failed, 38 passed           │",
]


@pytest.fixture(autouse=True)
def _clean():
    S.reset_selection_for_tests()
    yield
    S.reset_selection_for_tests()


class TestGeometry:
    def test_the_end_is_half_open(self):
        """A one-cell drag selects one cell. An inclusive end selects two."""
        sel = S.Selection((1, 2), (1, 3))
        assert sel.contains(1, 2) and not sel.contains(1, 3)

    def test_a_press_with_no_drag_is_not_a_selection(self):
        """Load-bearing: a plain click must still reach click-to-expand
        rather than being swallowed as a zero-width selection."""
        assert S.Selection((2, 2)).empty is True
        assert S.Selection((2, 2)).contains(2, 2) is False

    def test_a_backwards_drag_selects(self):
        """Dragging up and to the left is the same selection dragged the
        other way. Storing normalised would lose the direction; not ordering
        on read selects nothing."""
        back = S.Selection((3, 8), (1, 2))
        assert back.ordered() == ((1, 2), (3, 8))
        assert back.contains(2, 0) and back.contains(1, 5)
        assert not back.contains(0, 9)

    def test_the_first_and_last_rows_are_partial(self):
        sel = S.Selection((0, 5), (2, 3))
        assert not sel.contains(0, 4) and sel.contains(0, 5)
        assert sel.contains(1, 0)                    # middle row: all of it
        assert sel.contains(2, 2) and not sel.contains(2, 3)

    def test_rows_outside_are_excluded(self):
        sel = S.Selection((1, 0), (1, 5))
        assert not sel.contains(0, 2) and not sel.contains(2, 2)


class TestExtraction:
    def test_a_single_row_is_not_the_row_twice(self):
        """THE off-by-a-whole-line bug: writing the one-row case as the
        multi-row case with the middle removed slices `[start:]` and
        `[:end]` of the SAME row."""
        got = S.extract_text(ROWS, S.Selection((0, 4), (0, 21)))
        assert got == "Read(backend/x.py"
        assert got.count("Read") == 1

    def test_a_multi_row_selection_spans_correctly(self):
        got = S.extract_text(ROWS, S.Selection((0, 4), (2, 24))).splitlines()
        assert len(got) == 3
        assert got[0].startswith("Read(")
        assert got[1] == ROWS[1].rstrip()             # whole middle row
        assert got[2].endswith("passed")

    def test_trailing_padding_is_trimmed(self):
        """The canvas pads every row to the panel width; without trimming,
        every copied line carries a tail of spaces the operator cannot see."""
        got = S.extract_text(ROWS, S.Selection((1, 2), (1, 40)))
        assert got == got.rstrip()

    def test_a_selection_past_the_end_clamps(self):
        """A drag released below the last row selects to the END of the
        content rather than raising or truncating.

        The last line keeps its border, and that is the contract rather than
        an oversight: coordinates are RENDERED cells, so selecting to
        end-of-row selects what is on that row — which is what a terminal's
        own selection does and what CC documents its selection as capturing,
        "the hard-wrapped terminal rendering rather than the source text".
        """
        got = S.extract_text(ROWS, S.Selection((0, 4), (99, 3)))
        assert got.splitlines()[-1] == ROWS[-1].rstrip()
        assert len(got.splitlines()) == len(ROWS)

    def test_an_empty_selection_yields_nothing(self):
        assert S.extract_text(ROWS, S.Selection((1, 2))) == ""
        assert S.extract_text(ROWS, None) == ""

    def test_ansi_is_stripped_from_what_is_copied(self):
        rows = ["\x1b[2m⎿ collected 41 items\x1b[0m"]
        got = S.extract_text(rows, S.Selection((0, 0), (0, 40)))
        assert "\x1b" not in got and "collected 41 items" in got

    def test_garbage_never_raises(self):
        for bad in (None, [], [None, 3]):
            S.extract_text(bad, S.Selection((0, 0), (1, 1)))


class TestHighlight:
    def test_it_splits_a_run_at_the_boundary(self):
        out = S.apply_selection([("class:a", "hello")],
                                S.Selection((0, 1), (0, 4)))
        assert out == [("class:a", "h"),
                       ("class:a reverse", "ell"),
                       ("class:a", "o")]

    def test_it_emits_runs_not_characters(self):
        """A screenful of one-character fragments is a pathological input
        for every downstream renderer."""
        out = S.apply_selection([("", "x" * 40)], S.Selection((0, 5), (0, 35)))
        assert len(out) == 3

    def test_newlines_advance_the_row_and_reset_the_column(self):
        out = S.apply_selection([("", "ab\ncd")], S.Selection((1, 0), (1, 1)))
        text = "".join(t for _s, t in out)
        assert text == "ab\ncd"
        assert any("reverse" in s for s, _t in out)
        selected = "".join(t for s, t in out if "reverse" in s)
        assert selected == "c"

    def test_nothing_selected_costs_nothing(self):
        """Returns the input untouched, so an idle canvas walks no chars."""
        frags = [("class:a", "hello world")]
        assert S.apply_selection(frags, None) == frags
        assert S.apply_selection(frags, S.Selection((0, 2))) == frags

    def test_the_style_is_configurable_and_theme_neutral(self, monkeypatch):
        """A hardcoded highlight colour is the one thing certain to collide
        with an operator-chosen theme."""
        assert S.selection_style() == "reverse"
        monkeypatch.setenv("JARVIS_SELECTION_STYLE", "bg:#333333")
        out = S.apply_selection([("", "abc")], S.Selection((0, 0), (0, 2)))
        assert any("bg:#333333" in s for s, _t in out)

    def test_extra_fragment_members_survive(self):
        """Fragments may carry a third element (a mouse handler). Dropping
        it would silently unbind whatever it was."""
        handler = object()
        out = S.apply_selection([("", "abc", handler)],
                                S.Selection((0, 0), (0, 2)))
        assert all(len(f) == 3 and f[2] is handler for f in out)

    def test_malformed_fragments_pass_through(self):
        out = S.apply_selection([("only-one",), ("", "ab")],
                                S.Selection((0, 0), (0, 1)))
        assert out[0] == ("only-one",)

    def test_the_kill_switch_leaves_fragments_alone(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CANVAS_SELECTION_ENABLED", "0")
        frags = [("", "abc")]
        assert S.apply_selection(frags, S.Selection((0, 0), (0, 2))) == frags


class TestTheDragGesture:
    def _wire(self, monkeypatch):
        """`monkeypatch`, NOT a bare assignment.

        The first cut set `C.copy_text` directly, which is a permanent module
        mutation that leaked into `test_clipboard_write.py` in the same
        session and failed four of its tests. A fake that outlives its test
        is indistinguishable from a broken module.
        """
        from backend.core.ouroboros.battle_test import canvas_mouse as M
        from backend.core.ouroboros.battle_test import clipboard_write as C

        copied, notes, submitted = [], [], []
        monkeypatch.setattr(
            C, "copy_text", lambda t, **k: copied.append(t) or "pbcopy")
        control = type("_C", (), {})()
        M.install_canvas_mouse(control, lambda: ROWS, submitted.append,
                               notify=notes.append)
        return control, copied, notes, submitted

    def _ev(self, y, x, kind):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import (
            MouseButton, MouseEvent, MouseEventType,
        )
        return MouseEvent(Point(x=x, y=y), getattr(MouseEventType, kind),
                          MouseButton.LEFT, frozenset())

    def test_a_drag_copies_and_does_not_expand(self, monkeypatch):
        """A drag that began on a line offering `/expand` must not expand it
        on release — the operator was selecting, not clicking."""
        control, copied, notes, submitted = self._wire(monkeypatch)
        control.mouse_handler(self._ev(1, 2, "MOUSE_DOWN"))
        control.mouse_handler(self._ev(1, 10, "MOUSE_MOVE"))
        control.mouse_handler(self._ev(2, 8, "MOUSE_UP"))
        assert copied and submitted == []
        assert notes and "copied" in notes[0]

    def test_a_click_still_expands(self, monkeypatch):
        control, copied, _notes, submitted = self._wire(monkeypatch)
        row = next(i for i, r in enumerate(ROWS) if "/expand" in r) \
            if any("/expand" in r for r in ROWS) else None
        rows = ROWS + ["⎿ 41 parked · /expand t-3"]
        from backend.core.ouroboros.battle_test import canvas_mouse as M
        control = type("_C", (), {})()
        M.install_canvas_mouse(control, lambda: rows, submitted.append)
        control.mouse_handler(self._ev(3, 5, "MOUSE_DOWN"))
        control.mouse_handler(self._ev(3, 5, "MOUSE_UP"))
        assert submitted == ["/expand t-3"]

    def test_the_selection_is_cleared_after_the_copy(self, monkeypatch):
        """A highlight that outlives the gesture asserts the operator still
        has something selected — and the next click would extend it."""
        control, _copied, _notes, _submitted = self._wire(monkeypatch)
        control.mouse_handler(self._ev(0, 2, "MOUSE_DOWN"))
        control.mouse_handler(self._ev(0, 9, "MOUSE_UP"))
        assert S.current_selection() is None

    def test_the_wheel_is_still_not_consumed(self, monkeypatch):
        control, _c, _n, _s = self._wire(monkeypatch)
        assert control.mouse_handler(
            self._ev(0, 0, "SCROLL_DOWN")) is NotImplemented

    def test_copy_on_select_off_still_selects(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CANVAS_COPY_ON_SELECT", "0")
        control, copied, _notes, submitted = self._wire(monkeypatch)
        control.mouse_handler(self._ev(0, 2, "MOUSE_DOWN"))
        control.mouse_handler(self._ev(0, 9, "MOUSE_UP"))
        assert copied == [] and submitted == []

    def test_a_failed_copy_says_so(self, monkeypatch):
        from backend.core.ouroboros.battle_test import clipboard_write as C

        control, _copied, notes, _submitted = self._wire(monkeypatch)
        monkeypatch.setattr(C, "copy_text", lambda t, **k: None)
        control.mouse_handler(self._ev(0, 2, "MOUSE_DOWN"))
        control.mouse_handler(self._ev(0, 9, "MOUSE_UP"))
        assert notes and "nothing copied" in notes[0]

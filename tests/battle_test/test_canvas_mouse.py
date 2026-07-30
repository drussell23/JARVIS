"""Clicking a transcript line that says it has more to show.

`mouse_support` was already on — the wheel scrolled — but the cockpit had
ZERO `MouseEventType` handlers, so every click fell on the floor. Measured
against CC's documented surface that was the largest single gap: 1 of 13.

Two failures here would read as normal operation, and both are pinned:

  * a click resolving one row off, because the row was DERIVED (logical
    lines + anchor padding + panel border - scroll offset) instead of read
    off the rendered output — reported by an operator as "it expanded the
    wrong thing";
  * a handler that returns None for events it does not consume, silently
    taking the mouse WHEEL away — trading the one mouse capability the
    cockpit already had for the one being added.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import canvas_mouse as M


def _ev(y, kind):
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import (
        MouseButton, MouseEvent, MouseEventType,
    )
    return MouseEvent(Point(x=4, y=y), getattr(MouseEventType, kind),
                      MouseButton.LEFT, frozenset())


class TestRefResolution:
    def test_a_line_offering_expand_is_clickable(self):
        assert M.ref_in_line("  ⎿ 41 lines parked · /expand t-3") == "t-3"

    def test_a_bare_ref_is_clickable(self):
        """The moltbook feed appends `n-4` with no verb."""
        assert M.ref_in_line("🐍 @cassandra: the soak refused  n-4") == "n-4"

    def test_the_offer_wins_over_a_mentioned_ref(self):
        """A line carrying both is resolved by what it OFFERS, not by
        whichever artifact happens to appear first in it."""
        line = "⎿ derived from d-9 · /expand t-3"
        assert M.ref_in_line(line) == "t-3"

    def test_prose_is_not_a_ref(self):
        """THE false-positive that would make ordinary sentences clickable."""
        for prose in (
            "a well-known b-tree traversal",
            "see the x-ray of the call graph",
            "rated a-1 by the advisor",          # no digit boundary abuse
        ):
            got = M.ref_in_line(prose)
            assert got is None or "-" in got, prose
        assert M.ref_in_line("a well-known b-tree traversal") is None

    def test_a_line_with_nothing_to_show_is_not_clickable(self):
        """CC: 'Only messages that have more to show are clickable.'"""
        assert M.ref_in_line("⏺ Read(backend/x.py)") is None
        assert M.ref_in_line("╭───── canvas ─────╮") is None
        assert M.ref_in_line("") is None

    def test_ansi_is_stripped_before_matching(self):
        assert M.ref_in_line("\x1b[2m  ⎿ /expand t-7\x1b[0m") == "t-7"

    def test_every_expand_ref_family_resolves(self):
        """`/expand` dispatches t-/d-/o-/n-/p-/q-/b-. None of that is
        reimplemented here, so all of it works or none of it does."""
        for prefix in ("t", "d", "o", "n", "p", "q", "b"):
            assert M.ref_in_line(f"⎿ more · /expand {prefix}-12") == f"{prefix}-12"

    def test_garbage_never_raises(self):
        for bad in (None, 42, [], object()):
            assert M.ref_in_line(bad) is None


class TestRowIndexing:
    ROWS = [
        "╭──────────── canvas ────────────╮",
        "│ ⏺ Read(backend/x.py)           │",
        "│ ⎿ 41 lines parked · /expand t-3│",
        "╰────────────────────────────────╯",
    ]

    def test_it_resolves_the_row_it_was_given(self):
        assert M.ref_at_row(self.ROWS, 2) == "t-3"
        assert M.ref_at_row(self.ROWS, 1) is None

    def test_border_rows_carry_no_ref(self):
        """Why no offset arithmetic is needed: the panel's own rows simply
        do not resolve, so the border costs nothing to account for."""
        assert M.ref_at_row(self.ROWS, 0) is None
        assert M.ref_at_row(self.ROWS, 3) is None

    def test_out_of_range_is_none_not_an_error(self):
        """A click can land during a resize, between the render that sized
        the frame and the one that filled it."""
        for row in (99, -1, None, "x"):
            assert M.ref_at_row(self.ROWS, row) is None

    def test_clickable_rows_is_answerable_without_a_terminal(self):
        assert M.clickable_rows(self.ROWS) == [2]

    def test_resolve_click_builds_the_verb(self):
        assert M.resolve_click(self.ROWS, 2) == "/expand t-3"
        assert M.resolve_click(self.ROWS, 0) is None


class TestTheHandler:
    def _wire(self, rows):
        submitted = []
        control = type("_C", (), {})()
        assert M.install_canvas_mouse(control, lambda: rows, submitted.append)
        return control, submitted

    def test_a_click_submits_the_verb(self):
        control, submitted = self._wire(["⎿ /expand t-3"])
        control.mouse_handler(_ev(0, "MOUSE_UP"))
        assert submitted == ["/expand t-3"]

    def test_the_wheel_is_not_consumed(self):
        """THE regression this guard exists for. Returning None here would
        take scrolling away — the one mouse capability already working."""
        control, submitted = self._wire(["⎿ /expand t-3"])
        for kind in ("SCROLL_UP", "SCROLL_DOWN"):
            assert control.mouse_handler(_ev(0, kind)) is NotImplemented
        assert submitted == []

    def test_a_press_is_not_a_click(self):
        """Acting on MOUSE_DOWN fires while the operator is still deciding —
        including at the start of a drag they meant as a selection."""
        control, submitted = self._wire(["⎿ /expand t-3"])
        assert control.mouse_handler(_ev(0, "MOUSE_DOWN")) is NotImplemented
        assert submitted == []

    def test_a_click_on_a_plain_line_is_not_consumed(self):
        control, submitted = self._wire(["⏺ Read(x.py)"])
        assert control.mouse_handler(_ev(0, "MOUSE_UP")) is NotImplemented
        assert submitted == []

    def test_a_failing_rows_source_never_breaks_the_frame(self):
        submitted = []
        control = type("_C", (), {})()

        def _boom():
            raise RuntimeError("canvas mid-resize")

        M.install_canvas_mouse(control, _boom, submitted.append)
        assert control.mouse_handler(_ev(0, "MOUSE_UP")) is NotImplemented

    def test_a_failing_submit_never_breaks_the_frame(self):
        control = type("_C", (), {})()

        def _boom(_line):
            raise RuntimeError("dispatch gone")

        M.install_canvas_mouse(control, lambda: ["⎿ /expand t-3"], _boom)
        control.mouse_handler(_ev(0, "MOUSE_UP"))          # no raise

    def test_an_existing_handler_is_chained_not_replaced(self):
        seen = []
        control = type("_C", (), {})()
        control.mouse_handler = lambda e: seen.append(e) or "prior"
        M.install_canvas_mouse(control, lambda: ["plain"], lambda _l: None)
        assert control.mouse_handler(_ev(0, "MOUSE_UP")) == "prior"
        assert len(seen) == 1

    def test_the_kill_switch_keeps_the_wheel(self, monkeypatch):
        """Distinct from JARVIS_DISABLE_MOUSE, which costs scrolling too —
        the same split CC draws between DISABLE_MOUSE_CLICKS and
        DISABLE_MOUSE."""
        monkeypatch.setenv("JARVIS_CANVAS_MOUSE_ENABLED", "0")
        control = type("_C", (), {})()
        assert M.install_canvas_mouse(control, lambda: [], lambda _l: None) is False
        assert not hasattr(control, "mouse_handler")


class TestOnTheRealApplication:
    def _app(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
        submitted = []
        mux = BipartiteLayout(width=80, height=14)
        for line in ("⏺ Read(x.py)", "⎿ 41 lines parked · /expand t-3"):
            mux.emit("line", {"text": line})
        app = build_bipartite_application(mux, on_accept=submitted.append)
        control = next(c for c in app.layout.find_all_controls()
                       if type(c).__name__ == "_MeasuredCanvasControl")
        return mux, control, submitted

    def test_the_canvas_control_has_a_handler(self):
        _mux, control, _submitted = self._app()
        assert callable(getattr(control, "mouse_handler", None))

    def test_a_click_on_the_rendered_ref_row_expands(self):
        """Indexed against RENDERED output, so the panel border and the
        anchor padding need no arithmetic — row Y is whatever is on row Y."""
        mux, control, submitted = self._app()
        rows = mux.render_canvas_ansi().splitlines()
        target = next(i for i, r in enumerate(rows) if "t-3" in r)
        control.mouse_handler(_ev(target, "MOUSE_UP"))
        assert submitted == ["/expand t-3"]

    def test_a_click_routes_through_on_accept(self):
        """A click IS the verb: the same callable a typed line goes through,
        so no surface grows a second dispatch path to drift."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout

        src = inspect.getsource(bipartite_layout.build_bipartite_application)
        tree = ast.parse(src.lstrip())
        assert any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "install_canvas_mouse"
            for n in ast.walk(tree)
        )
        assert "on_accept(line)" in src

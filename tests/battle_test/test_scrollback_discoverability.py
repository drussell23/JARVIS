"""The scrollback worked; nobody could find it.

`ov demo live` takes the alternate screen, which removes the terminal's OWN
scrollback. PgUp, the wheel, Shift+arrows, Home and End were all bound and
all functional — and the operator reported they "can't scroll" and were
zooming out with Cmd+- to read the deck.

Two reasons, both real:

  * nothing at the live tail said history existed or how to reach it. The
    existing `status()` only renders while SCROLLED — it tells a reader how
    to get BACK, and nothing told them they could leave.
  * the key everyone names is PgUp, and a MacBook keyboard cannot send it.
    It is `Fn+↑`, so a hint naming PageUp sends exactly the operator who most
    needs help to a key they cannot press.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.bipartite_layout import BipartiteLayout
from backend.core.ouroboros.battle_test.canvas_viewport import CanvasViewport


def _tail(mux) -> str:
    from rich.text import Text
    return Text.from_markup(mux._visible_lines()[-1]).plain


def _deck(lines: int = 60, height: int = 12) -> BipartiteLayout:
    mux = BipartiteLayout(width=70, height=height)
    for i in range(lines):
        mux.push_raw(f"line {i:03d}")
    return mux


class TestTheTailSaysHistoryExists:
    def test_it_offers_the_key_when_there_is_something_above(self):
        assert "to scroll" in _tail(_deck())

    def test_it_counts_what_is_up_there(self):
        """"There is more" is the fact; how much decides whether to look."""
        assert "49 above" in _tail(_deck(60, 12))

    def test_a_deck_that_FITS_says_nothing(self):
        """Nothing has scrolled off, so there is nothing to teach."""
        assert "to scroll" not in _tail(_deck(4, 12))


class TestItNamesAKeyEveryKeyboardCanSEND:
    def test_it_does_NOT_name_PageUp(self):
        """A MacBook has no PageUp — it is `Fn+↑`. Naming it sends the
        operator who most needs help to a key they cannot press."""
        hint = _tail(_deck())
        assert "pageup" not in hint.lower()
        assert "page up" not in hint.lower()

    def test_it_names_shift_arrow(self):
        assert "shift+↑" in _tail(_deck())

    def test_that_key_is_actually_BOUND(self):
        """A hint naming a key nothing binds is worse than no hint."""
        from prompt_toolkit.key_binding import KeyBindings
        from backend.core.ouroboros.battle_test.canvas_viewport import (
            install_scroll_bindings,
        )
        kb = KeyBindings()
        vp = CanvasViewport()
        install_scroll_bindings(kb, vp, lambda: (200, 20), lambda: None)
        keys = {str(getattr(k, "value", k))
                for b in kb.bindings for k in b.keys}
        assert "s-up" in keys and "s-down" in keys


class TestItTeachesOnce:
    def test_scrolling_retires_the_hint(self):
        """A surface that re-teaches itself on every render spends the
        operator's screen on their first minute, forever."""
        mux = _deck()
        assert "to scroll" in _tail(mux)
        total, budget = mux.scroll_metrics()
        mux._viewport.scroll(-3, total=total, budget=budget)
        mux._viewport.to_bottom()
        assert "to scroll" not in _tail(mux)

    def test_returning_to_live_does_not_un_teach(self):
        vp = CanvasViewport()
        vp.scroll(-3, total=200, budget=20)
        vp.to_bottom()
        assert vp.taught
        assert vp.tail_hint(50) == ""

    def test_paging_counts_as_learning(self):
        vp = CanvasViewport()
        vp.page(1, total=200, budget=20)
        assert vp.taught

    def test_an_explicit_mark_works_for_surfaces_that_scroll_elsewhere(self):
        vp = CanvasViewport()
        vp.mark_taught()
        assert vp.tail_hint(50) == ""


class TestItCostsTheSameRowAsTheStatus:
    def test_the_hint_replaces_a_line_rather_than_adding_one(self):
        """It buys its row from the deck, exactly as `status` does — the
        canvas cannot grow to make room for its own chrome."""
        mux = _deck()
        assert len(mux._visible_lines()) <= mux._line_budget()

    def test_scrolled_and_tail_render_the_same_height(self):
        mux = _deck()
        at_tail = len(mux._visible_lines())
        total, budget = mux.scroll_metrics()
        mux._viewport.scroll(-3, total=total, budget=budget)
        assert len(mux._visible_lines()) == at_tail


class TestNeverRaises:
    @pytest.mark.parametrize("above", [None, -1, 0, "many"])
    def test_junk_degrades(self, above):
        assert isinstance(CanvasViewport().tail_hint(above), str)

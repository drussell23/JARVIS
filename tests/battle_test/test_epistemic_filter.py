"""Navigating the transcript by how the organism KNOWS what it said.

Claude Code's viewer jumps between search matches and prompts. It cannot jump
between CLAIMS, because nothing in it records which sentences were observed
and which were asserted. O+V marks every one at the `_op_line` chokepoint and
had never navigated by it — this is the consumer.

Two failures here read as normal operation, and both were found by running it
rather than reading it:

  * classifying only the RENDERED window, so every claim found is already on
    screen and `c` moves nowhere;
  * deriving each press's start from the newest VISIBLE line, so after
    landing on a claim the search finds the same one again and `C` sticks.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import epistemic_filter as E
from backend.core.ouroboros.ui.provenance import Provenance, annotate


def _line(text, prov=None):
    return annotate(text, prov) if prov is not None else text


class TestClassification:
    @pytest.mark.parametrize("prov", [
        Provenance.STATED, Provenance.MODELED,
        Provenance.SYNTHETIC, Provenance.UNKNOWN,
    ])
    def test_every_marked_rung_is_recognised(self, prov):
        assert E.provenance_of_line(_line("x", prov)) is prov

    def test_a_clean_line_is_NOT_reported_as_observed(self):
        """THE honesty point. `provenance` renders OBSERVED and DERIVED with
        no mark — marks are the EXCEPTION surface — so from the text alone
        the two are indistinguishable. Reporting the stronger would be the
        confident-and-wrong this vocabulary exists to prevent, and UNKNOWN is
        already taken by a different fact: asked and unanswerable."""
        assert E.provenance_of_line("41 tests passed") is None
        assert E.provenance_of_line(_line("41 tests passed",
                                          Provenance.OBSERVED)) is None
        assert E.provenance_of_line(_line("a count", Provenance.DERIVED)) is None

    def test_unknown_is_a_mark_not_an_absence(self):
        """`‹unverified›` is the LOUDEST mark there is; it must never be
        confused with a clean line."""
        assert E.provenance_of_line(
            _line("could not resolve", Provenance.UNKNOWN)
        ) is Provenance.UNKNOWN

    def test_the_weaker_wins_when_two_marks_meet(self):
        """`provenance` rules that a chain is exactly as trustworthy as its
        softest link. Scanning weakest-first makes the first hit correct
        without comparing."""
        both = (_line("summary", Provenance.DERIVED)
                + annotate("", Provenance.MODELED)
                + annotate("", Provenance.SYNTHETIC))
        assert E.provenance_of_line(both) is Provenance.SYNTHETIC

    def test_ansi_does_not_hide_a_mark(self):
        marked = "\x1b[2m" + _line("x", Provenance.MODELED) + "\x1b[0m"
        assert E.provenance_of_line(marked) is Provenance.MODELED

    def test_the_mark_table_is_derived_not_transcribed(self):
        """A hardcoded glyph list is how a filter silently stops seeing a
        category when a mark is re-worded."""
        import inspect

        src = inspect.getsource(E._marks)
        assert "mark_for" in src and "Provenance" in src

    def test_garbage_never_raises(self):
        for bad in (None, 42, [], object()):
            assert E.provenance_of_line(bad) is None


class TestWalking:
    ROWS = [
        "observed 0",
        _line("blast radius is 12", Provenance.MODELED),      # 1
        "observed 2",
        _line("cap defaulted to 50", Provenance.SYNTHETIC),    # 3
        "observed 4",
    ]

    def test_claim_rows_finds_only_marked_lines(self):
        assert E.claim_rows(self.ROWS) == [1, 3]

    def test_forward_walk(self):
        assert E.next_claim_row(self.ROWS, 0, +1) == 1
        assert E.next_claim_row(self.ROWS, 1, +1) == 3

    def test_backward_walk(self):
        assert E.next_claim_row(self.ROWS, 4, -1) == 3
        assert E.next_claim_row(self.ROWS, 3, -1) == 1

    def test_it_wraps_like_every_n_and_N(self):
        assert E.next_claim_row(self.ROWS, 3, +1) == 1
        assert E.next_claim_row(self.ROWS, 1, -1) == 3

    def test_wrap_can_be_refused(self):
        assert E.next_claim_row(self.ROWS, 3, +1, wrap=False) is None

    def test_no_claims_is_None_not_an_arbitrary_row(self):
        """A reader who presses `c` in a session with no claims should be
        told that, not moved somewhere and left to wonder."""
        assert E.next_claim_row(["a", "b"], 0, +1) is None

    def test_a_bad_cursor_still_walks(self):
        assert E.next_claim_row(self.ROWS, None, +1) == 1
        assert E.next_claim_row(self.ROWS, "x", -1) == 3


class TestSummary:
    def test_it_counts_weakest_first(self):
        rows = [_line("a", Provenance.MODELED),
                _line("b", Provenance.UNKNOWN),
                _line("c", Provenance.MODELED)]
        assert E.summarise(rows) == "1 unknown · 2 modeled"

    def test_a_clean_transcript_renders_nothing(self):
        """No claims is the GOOD case and does not need a badge saying so."""
        assert E.summarise(["observed", "derived"]) == ""


class TestTheViewerWalk:
    """End to end against a real canvas — where both design errors showed."""

    def _wired(self):
        from prompt_toolkit.key_binding import KeyBindings

        from backend.core.ouroboros.battle_test import transcript_mode as tm
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, set_active_canvas,
        )
        kb = KeyBindings()
        tm.install_transcript_mode_bindings(kb)
        table = {" ".join(str(k).replace("Keys.", "") for k in b.keys):
                 b.handler for b in kb.bindings}
        mux = BipartiteLayout(width=100, height=16)
        for i in range(40):
            mux.emit("line", {"text": f"observed {i}"})
        mux.push_raw(_line("blast radius is 12", Provenance.MODELED))   # 40
        for i in range(40):
            mux.emit("line", {"text": f"observed {40 + i}"})
        mux.push_raw(_line("cap defaulted to 50", Provenance.SYNTHETIC))  # 81
        for i in range(20):
            mux.emit("line", {"text": f"observed {81 + i}"})
        set_active_canvas(mux)
        mux.render_canvas_ansi()
        tm.reset_transcript_mode_for_tests()
        tm.enter_transcript_mode()
        return tm, mux, table

    def teardown_method(self):
        from backend.core.ouroboros.battle_test import transcript_mode as tm
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            set_active_canvas,
        )
        tm.reset_transcript_mode_for_tests()
        set_active_canvas(None)

    def test_it_searches_the_whole_ring_not_the_visible_window(self):
        """THE first design error: classifying only what is DRAWN finds
        claims already on screen, so the key moves nowhere and looks broken.
        The point is reaching the claim forty lines back."""
        tm, mux, table = self._wired()
        assert mux._viewport.offset == 0
        table["C"](None)
        assert mux._viewport.offset > 0, "the walk never left the tail"

    def test_repeated_presses_advance(self):
        """THE second: deriving each press's start from the newest VISIBLE
        line re-finds the claim just landed on, so `C` sticks."""
        tm, mux, table = self._wired()
        seen = []
        for _ in range(3):
            table["C"](None)
            seen.append(tm._CLAIM_CURSOR[0])
        assert seen == [81, 40, 81], seen

    def test_forward_and_backward_are_symmetric(self):
        tm, mux, table = self._wired()
        table["C"](None)
        table["C"](None)
        assert tm._CLAIM_CURSOR[0] == 40
        table["c"](None)
        assert tm._CLAIM_CURSOR[0] == 81

    def test_the_cursor_resets_with_the_mode(self):
        tm, _mux, table = self._wired()
        table["C"](None)
        assert tm._CLAIM_CURSOR[0] is not None
        tm.exit_transcript_mode()
        assert tm._CLAIM_CURSOR[0] is None

    def test_a_session_with_no_claims_says_so(self):
        """A jump key that silently does nothing is indistinguishable from a
        broken one."""
        from prompt_toolkit.key_binding import KeyBindings

        from backend.core.ouroboros.battle_test import transcript_mode as tm
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, set_active_canvas,
        )
        kb = KeyBindings()
        tm.install_transcript_mode_bindings(kb)
        table = {" ".join(str(k).replace("Keys.", "") for k in b.keys):
                 b.handler for b in kb.bindings}
        mux = BipartiteLayout(width=100, height=16)
        for i in range(30):
            mux.emit("line", {"text": f"observed {i}"})
        set_active_canvas(mux)
        mux.render_canvas_ansi()
        tm.reset_transcript_mode_for_tests()
        tm.enter_transcript_mode()
        table["c"](None)
        tail = list(mux._buffer.snapshot())[-1]
        assert "no unobserved claims" in tail
        assert "· line" not in tail, (
            "the notice went through `emit`, whose null renderer prints the "
            "event TYPE — it must use `push_raw`"
        )

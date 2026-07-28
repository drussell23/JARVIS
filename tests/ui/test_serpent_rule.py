"""The serpent runs the hairlines, chasing the prey.

The boot crest already tells this story on a raster. Two hairlines are also a
closed path — left→right along the top, right→left along the bottom — so the
same creature lives here on the same laws.

These tests pin the properties that make it safe to redraw on every
keystroke: it is a pure function of (t, width), it costs nothing when idle,
and it degrades to the hairline it decorates rather than raising into a
repaint.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import serpent_rule as sr


def _text(row, width, t, **kw):
    return "".join(txt for _s, txt in sr.rule_fragments(row, width, t, **kw))


class TestThePathIsAClosedLoop:
    def test_the_top_runs_left_to_right(self):
        assert sr.cell_at(0, 64) == (sr.TOP, 0)
        assert sr.cell_at(63, 64) == (sr.TOP, 63)

    def test_the_bottom_runs_RIGHT_TO_LEFT(self):
        """A serpent that reappeared at the left edge each time would read as
        two animations, not one creature going round."""
        assert sr.cell_at(64, 64) == (sr.BOTTOM, 63)
        assert sr.cell_at(127, 64) == (sr.BOTTOM, 0)

    def test_it_wraps(self):
        assert sr.cell_at(128, 64) == sr.cell_at(0, 64)
        assert sr.cell_at(-1, 64) == sr.cell_at(127, 64)

    def test_the_circuit_is_both_rules(self):
        assert sr.path_length(64) == 128

    def test_every_cell_is_reachable_exactly_once(self):
        seen = [sr.cell_at(i, 40) for i in range(sr.path_length(40))]
        assert len(set(seen)) == len(seen) == 80


class TestTheChase:
    def test_the_gap_closes_to_a_bite(self):
        """The ouroboros arc the crest tells, in one dimension."""
        period = sr.chase_period_s()
        gaps = []
        for frac in (0.0, 0.25, 0.5, 0.75, 0.98):
            f = sr.frame(frac * period, 64)
            gaps.append((f.prey - f.head) % f.length)
        assert gaps == sorted(gaps, reverse=True), gaps
        assert sr.frame(period * 0.999, 64).biting

    def test_the_prey_stays_on_screen_with_its_pursuer(self):
        """A full-lap lead on a narrow terminal would put them adjacent —
        indistinguishable from a bite that never happened."""
        for width in (12, 24, 80, 200):
            f = sr.frame(0.0, width)
            gap = (f.prey - f.head) % f.length
            assert gap <= f.length // 3 + 1, (width, gap)

    def test_the_body_trails_BEHIND_the_head(self):
        f = sr.frame(1.0, 64)
        assert f.body[0] == (f.head - 1) % f.length
        assert len(set(f.body)) == len(f.body)

    def test_the_head_advances_with_time(self):
        a, b = sr.frame(0.0, 64), sr.frame(1.0, 64)
        assert a.head != b.head


class TestItIsAPureFunctionOfTimeAndWidth:
    def test_two_readers_at_one_instant_agree(self):
        """Why the spinner could be consumed by three surfaces with none of
        them owning it — and it matters more here, redrawn per keystroke."""
        assert sr.frame(3.7, 64) == sr.frame(3.7, 64)
        assert _text(sr.TOP, 64, 3.7) == _text(sr.TOP, 64, 3.7)

    def test_resize_needs_no_invalidation(self):
        """The path length is derived at render time, so a SIGWINCH
        mid-chase simply produces a frame for the new geometry."""
        assert sr.frame(2.0, 40).width == 40
        assert sr.frame(2.0, 200).width == 200
        assert len(_text(sr.TOP, 40, 2.0)) == 40
        assert len(_text(sr.TOP, 200, 2.0)) == 200

    def test_no_state_leaks_between_calls(self):
        first = _text(sr.TOP, 64, 1.0)
        _ = [_text(sr.BOTTOM, 99, t) for t in range(20)]
        assert _text(sr.TOP, 64, 1.0) == first


class TestItKnowsItIsDecoration:
    def test_idle_renders_the_PLAIN_hairline(self):
        """A border that moves forever teaches the operator to stop seeing
        the border, and framing the caret is the one thing it has to do."""
        plain = _text(sr.TOP, 64, 3.0, active=False)
        assert plain == "─" * 64

    def test_idle_costs_exactly_one_fragment(self):
        assert len(sr.rule_fragments(sr.TOP, 64, 3.0, active=False)) == 1

    def test_a_narrow_terminal_does_not_animate(self):
        """Too short to read as motion — the serpent would appear to
        teleport rather than travel."""
        assert sr.frame(1.0, 8) is None
        assert _text(sr.TOP, 8, 1.0) == "─" * 8

    def test_the_master_flag_silences_it(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SERPENT_RULE_ENABLED", "0")
        assert sr.frame(1.0, 64) is None
        assert _text(sr.TOP, 64, 1.0) == "─" * 64


class TestTheLineKeepsItsLength:
    @pytest.mark.parametrize("width", [12, 13, 40, 79, 80, 81, 200])
    @pytest.mark.parametrize("t", [0.0, 1.3, 5.9, 60.0])
    def test_the_rule_is_always_exactly_width(self, width, t):
        """The length of this line is the frame around the operator's caret.
        Every mark is one cell for exactly this reason — the identity emoji
        is double-width on most terminals and single on some."""
        for row in (sr.TOP, sr.BOTTOM):
            assert len(_text(row, width, t)) == width

    def test_no_multi_cell_glyphs(self):
        rule, head, body, prey = sr._glyphs(unicode_ok=True)
        for g in (rule, head, body, prey):
            assert len(g) == 1


class TestDegradation:
    def test_ascii_keeps_the_geometry(self):
        assert len(_text(sr.TOP, 64, 1.0, unicode_ok=False)) == 64
        assert "─" not in _text(sr.TOP, 64, 1.0, unicode_ok=False)

    @pytest.mark.parametrize("bad", [None, -5, "wide", 0])
    def test_junk_width_never_raises(self, bad):
        assert isinstance(sr.rule_fragments(sr.TOP, bad, 1.0), list)
        assert sr.frame(1.0, bad) is None

    def test_junk_time_never_raises(self):
        for t in (None, "now", float("nan")):
            assert isinstance(sr.rule_fragments(sr.TOP, 64, t), list)

    def test_a_dead_palette_still_draws_the_rule(self, monkeypatch):
        monkeypatch.setattr(sr, "_palette", lambda: (_ for _ in ()).throw(
            RuntimeError("theme down")))
        assert isinstance(sr.rule_fragments(sr.TOP, 64, 1.0), list)


class TestItSharesTheCrestsPalette:
    def test_the_prey_wears_the_crests_colours(self):
        """The creature on the hairline and the creature on the boot screen
        must be visibly the same animal."""
        from backend.core.ouroboros.ui.crest import _EYE_RGB, _V_TOP_RGB
        _rule, _serpent, core, edge = sr._palette()
        assert core == _EYE_RGB and edge == _V_TOP_RGB

    def test_the_bite_flashes_the_prey_to_its_CORE(self):
        period = sr.chase_period_s()
        biting = sr.frame(period * 0.999, 64)
        assert biting.biting
        row, _x = sr.cell_at(biting.prey, 64)
        styles = [st for st, _t in sr.rule_fragments(row, 64, period * 0.999)]
        assert any("bold" in st for st in styles)

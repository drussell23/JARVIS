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


class TestSmoothnessIsStructural:
    """The jitter was a BEAT FREQUENCY, not a tuning problem.

    Speed used to be 18 cells/second against a 0.1s repaint — 1.8 cells per
    frame. Cells are integers, so the head actually advanced
    1, 2, 2, 2, 2, 1, 2, 2, 2, 2, 1 … and that irregular 1 every fifth frame
    is what an eye reads as stutter. No fraction fixes it; only an integer
    does.
    """

    def _steps(self, interval, width=88, frames=40):
        prev, out = None, []
        for i in range(frames):
            head = sr.frame(i * interval, width, interval=interval).head
            if prev is not None:
                out.append((head - prev) % sr.path_length(width))
            prev = head
        return out

    @pytest.mark.parametrize("interval", [0.05, 0.1, 0.2, 0.5])
    def test_the_head_advances_by_a_CONSTANT_number_of_cells(self, interval):
        """At ANY repaint rate — including one this module never sees.

        Compared with a tolerance because the positions are FLOATS now: the
        step is 0.6 and IEEE gives 0.5999999999999979. Exact equality would
        fail on representation noise while the property — a constant step,
        which is what the eye reads — holds perfectly.
        """
        steps = self._steps(interval)
        assert max(steps) - min(steps) < 1e-6, (
            f"{interval}s → uneven {sorted(set(steps))}"
        )

    def test_the_step_is_exactly_the_configured_cells_per_frame(self):
        assert all(abs(x - sr.cells_per_frame()) < 1e-6
                   for x in self._steps(0.1))

    def test_speed_is_DERIVED_from_the_frame_rate(self):
        """Expressed as whole cells per frame, so seconds fall out. A rate
        chosen independently of the repaint period is the bug."""
        assert sr.speed_cells_s(0.1) == sr.cells_per_frame() / 0.1
        assert sr.speed_cells_s(0.5) == sr.cells_per_frame() / 0.5

    def test_a_faster_setting_stays_smooth(self, monkeypatch):
        """Two cells per frame is twice the speed and still uniform —
        the property survives the knob."""
        monkeypatch.setenv("JARVIS_SERPENT_RULE_CELLS_PER_FRAME", "3")
        assert all(abs(x - 3.0) < 1e-6 for x in self._steps(0.1))

    def test_the_cockpit_hands_over_its_OWN_repaint_period(self):
        """A literal in two places is how the animation and the Application
        silently disagree about the frame rate."""
        import inspect
        from backend.core.ouroboros.battle_test import bipartite_layout as bl
        src = inspect.getsource(bl.build_bipartite_application)
        assert "interval=_REFRESH_INTERVAL_S" in src
        assert "refresh_interval=_REFRESH_INTERVAL_S" in src


class TestTheChaseIsLegible:
    def test_both_marks_fit_in_one_glance(self):
        """A third of the circuit was 58 cells on an 88-column terminal —
        more than half a rule, so the two were rarely on the same line and
        the chase read as two unrelated marks."""
        for width in (40, 88, 200):
            f = sr.frame(0.0, width)
            gap = (f.prey - f.head) % f.length
            assert gap <= sr.lead_cells(), (width, gap)

    def test_the_lead_is_in_CELLS_so_it_means_one_thing_everywhere(self):
        wide = sr.frame(0.0, 200)
        narrow = sr.frame(0.0, 88)
        assert ((wide.prey - wide.head) % wide.length
                == (narrow.prey - narrow.head) % narrow.length)

    def test_a_narrow_path_cannot_be_handed_a_longer_lead_than_it_has(self):
        f = sr.frame(0.0, 12)
        assert (f.prey - f.head) % f.length <= f.length // 4 + 1

    def test_the_gap_closes_monotonically_to_the_bite(self):
        period = sr.chase_period_s()
        gaps = []
        for i in range(30):
            f = sr.frame(period * i / 30.0, 88)
            gaps.append((f.prey - f.head) % f.length)
        assert gaps == sorted(gaps, reverse=True), gaps


class TestSubCellCoverage:
    """The smoothness a cell grid cannot give.

    An integer step removed the beat but not the steppiness: one whole
    character every 100 ms is a teleport of a character's width. A cell is
    the atom of POSITION, so the rest had to come from INTENSITY — the law
    the crest already uses, where "dimmed edges read as native terminal
    anti-aliasing".
    """

    @pytest.fixture(autouse=True)
    def _truecolor(self, monkeypatch):
        from backend.core.ouroboros.ui import theme
        monkeypatch.setattr(theme, "_active_tier_cache",
                            theme.ColorTier.TRUECOLOR)
        yield

    def test_a_mark_between_cells_paints_BOTH(self):
        cover = dict(sr._coverage(12.4, 64))
        assert set(cover) == {12, 13}
        assert abs(cover[12] - 0.6) < 1e-6
        assert abs(cover[13] - 0.4) < 1e-6

    def test_coverage_always_sums_to_one_mark(self):
        for pos in (0.0, 0.5, 7.25, 63.9, 127.99):
            assert abs(sum(c for _i, c in sr._coverage(pos, 64)) - 1.0) < 1e-6

    def test_a_mark_on_a_boundary_stays_CRISP(self):
        """An integer position must not smear across two cells — otherwise
        every mark is permanently half-lit."""
        assert sr._coverage(9.0, 64) == ((9, 1.0),)

    def test_coverage_wraps_the_circuit(self):
        cover = dict(sr._coverage(sr.path_length(64) - 0.5, 64))
        assert 0 in cover, "the last cell did not blend into the first"

    def test_the_blend_goes_toward_the_RULE_not_black(self):
        """A partially covered cell is a hairline with some serpent on it.
        Fading to black would punch a hole in the line this decorates."""
        rule, serpent = (163, 113, 247), (94, 224, 106)
        faint = sr._blend(rule, serpent, 0.1)
        assert all(abs(f - r) < 25 for f, r in zip(faint, rule))
        assert sum(faint) > sum(c // 2 for c in rule), "faded toward black"

    def test_full_coverage_is_the_mark_itself(self):
        assert sr._blend((163, 113, 247), (94, 224, 106), 1.0) == (94, 224, 106)

    def test_the_head_outranks_its_own_tail_on_a_shared_cell(self):
        """Brightest coverage owns the glyph, so a body segment cannot erase
        the head on the frame they overlap."""
        f = sr.frame(0.0, 64)
        head_cells = {c for c, _ in sr._coverage(f.head, 64)}
        row, x = sr.cell_at(int(f.head), 64)
        frags = sr.rule_fragments(row, 64, 0.0)
        assert head_cells and any("bold" in st for st, _t in frags)

    def test_intermediate_positions_produce_DIFFERENT_renders(self):
        """The proof that motion is continuous: two sub-cell positions the
        old integer stepper would have rendered identically now differ."""
        a = sr.rule_fragments(sr.TOP, 64, 0.0)
        b = sr.rule_fragments(sr.TOP, 64, 0.05)   # half a frame
        assert a != b


class TestSubCellDegradation:
    def test_a_16_colour_terminal_SNAPS_instead_of_smearing(self, monkeypatch):
        """Sixteen colours quantise an interpolated green-purple to whichever
        it is nearer, so the "smooth" render would flicker between rule and
        serpent — worse than a clean step, and worse only on that terminal.
        """
        from backend.core.ouroboros.ui import theme
        monkeypatch.setattr(theme, "_active_tier_cache",
                            theme.ColorTier.STANDARD)
        cover = sr._coverage(12.4, 64)
        assert cover == ((12, 1.0),), cover

    def test_it_snaps_to_the_NEAREST_cell(self, monkeypatch):
        from backend.core.ouroboros.ui import theme
        monkeypatch.setattr(theme, "_active_tier_cache",
                            theme.ColorTier.STANDARD)
        assert sr._coverage(12.6, 64) == ((13, 1.0),)

    def test_the_line_keeps_its_length_either_way(self, monkeypatch):
        from backend.core.ouroboros.ui import theme
        for tier in (theme.ColorTier.TRUECOLOR, theme.ColorTier.STANDARD,
                     theme.ColorTier.NONE):
            monkeypatch.setattr(theme, "_active_tier_cache", tier)
            for row in (sr.TOP, sr.BOTTOM):
                text = "".join(t for _s, t in
                               sr.rule_fragments(row, 64, 1.3))
                assert len(text) == 64, (tier, len(text))

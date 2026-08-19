"""A boot past its estimate must SAY so, not fall silent.

The wait line pinned at ``97%  session open  89s`` and an operator asked
whether it had hung. It had not — the machine was loaded — but nothing on the
line could have told them apart, and that is the defect.

Two mechanisms combined to hide it:

1. ``_projected_total_s`` returned ``max(elapsed, projected)``. The clamp is
   right for a countdown (an ETA may never point into the past) and fatal for
   an overrun test, because once a boot runs long the clamped value tracks
   ``elapsed`` and the two are equal by construction. The function that
   computed the evidence destroyed it.
2. ``render`` reported the overrun by ABSENCE: ``remaining`` clamped to 0, the
   ``~Ns left`` token stopped being appended, and nobody can see a token that
   is not there.

Paired with the deliberate 97% ceiling — itself correct, since a bar at 100%
while nothing happens claims a finish that has not occurred — a slow boot and
a dead one rendered identically.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli import boot_progress as bp


def _progress(expected, stage_times):
    p = bp.Progress(stages=bp.DEFAULT_STAGES, expected_s=expected)
    for t in stage_times:
        p._reached += 1
        p._stage_times.append(float(t))
    return p


class TestTheClampNoLongerHidesTheEvidence:
    def test_the_eta_projection_still_never_points_into_the_past(self):
        """The clamp is KEPT — it is what stops "0s left" that never arrives."""
        p = _progress(None, [4, 9, 14])
        assert p._projected_total_s(500.0) == 500.0

    def test_the_raw_projection_is_unclamped_so_overrun_is_detectable(self):
        p = _progress(None, [4, 9, 14])
        raw = p._raw_projection()
        assert raw is not None and raw < 500.0, (
            "a clamped basis makes every long boot look exactly on time"
        )

    def test_overrun_is_measured_against_the_raw_basis(self):
        p = _progress(None, [4, 9, 14])
        raw = p._raw_projection()
        over = p.overrun_s(raw * 4)
        assert over is not None and over > 0.0


class TestYouCannotBeLateWithoutADeadline:
    def test_no_stages_and_no_history_yields_no_overrun(self):
        p = _progress(None, [])
        assert p.overrun_s(9999.0) is None, (
            "an unmeasured boot cannot be late; claiming otherwise invents a "
            "deadline the operator never set"
        )

    def test_a_line_with_no_prediction_shows_only_the_clock(self):
        p = _progress(None, [])
        out = p.render(89.0)
        assert "over" not in out and "left" not in out
        assert "89s" in out


class TestToleranceIsProportionalNotAbsolute:
    """A 2s boot taking 3s is noise; a 90s boot taking 135s is a fact."""

    def test_a_short_prediction_is_not_tripped_by_jitter(self):
        p = _progress(2.0, [])
        assert p.overrun_s(4.0) is None, (
            "the grace FLOOR must absorb scheduler jitter on a fast boot"
        )

    def test_a_long_prediction_reports_a_real_overshoot(self):
        p = _progress(90.0, [])
        assert p.overrun_s(135.0) is not None

    def test_the_proportional_band_scales_with_the_prediction(self):
        """The SAME absolute overshoot means different things at different
        scales, which is the entire argument for a proportional band.

        50s late on a 10s prediction is a boot that has gone wrong. 50s late
        on a 400s prediction is inside normal variance. A fixed grace would
        have to pick one of those and be wrong about the other.
        """
        assert _progress(10.0, []).overrun_s(60.0) is not None
        assert _progress(400.0, []).overrun_s(450.0) is None

    @pytest.mark.parametrize("env,value,elapsed,expect_over", [
        ("JARVIS_OV_BOOT_OVERRUN_TOLERANCE", "0.0", 44.0, True),
        ("JARVIS_OV_BOOT_OVERRUN_TOLERANCE", "5.0", 200.0, False),
        ("JARVIS_OV_BOOT_OVERRUN_GRACE_S", "600", 200.0, False),
    ])
    def test_both_knobs_are_operator_tunable(self, monkeypatch, env, value,
                                             elapsed, expect_over):
        """The right patience on a cold laptop is not the right patience in
        CI, so neither bound is a literal."""
        monkeypatch.setenv(env, value)
        p = _progress(40.0, [])
        assert (p.overrun_s(elapsed) is not None) is expect_over

    def test_a_malformed_override_degrades_to_the_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_BOOT_OVERRUN_TOLERANCE", "not-a-number")
        monkeypatch.setenv("JARVIS_OV_BOOT_OVERRUN_GRACE_S", "")
        assert bp.overrun_tolerance() == 0.25
        assert bp.overrun_grace_s() == 3.0


class TestTheLineNamesWhatItIsWaitingFor:
    def test_awaiting_is_the_next_stage_not_the_last_reached(self):
        p = _progress(40.0, [4, 9, 14])
        assert p.stage_label == bp.DEFAULT_STAGES[2].label
        assert p.awaiting_label == bp.DEFAULT_STAGES[3].label, (
            "during an overrun the reached stage answers a question nobody "
            "is asking"
        )

    def test_nothing_is_awaited_once_every_stage_arrived(self):
        p = _progress(40.0, [1] * len(bp.DEFAULT_STAGES))
        assert p.awaiting_label == ""

    def test_the_overrun_line_carries_both_the_overshoot_and_the_blocker(self):
        p = _progress(40.0, [4, 9, 14])
        out = p.render(89.0)
        assert "over" in out
        assert f"waiting on {bp.DEFAULT_STAGES[3].label}" in out
        assert "left" not in out, "a countdown and an overrun are exclusive"


class TestTheHealthyPathIsUnchanged:
    def test_an_on_time_boot_still_counts_down(self):
        p = _progress(40.0, [4, 9, 14])
        out = p.render(20.0)
        assert "~" in out and "left" in out
        assert "over" not in out

    def test_the_ceiling_still_reserves_the_last_percent(self):
        """The 97% pin is CORRECT and must survive: a bar at 100% while
        nothing happens claims a finish that has not occurred."""
        p = _progress(1.0, [0.1] * len(bp.DEFAULT_STAGES))
        assert p.fraction(1000.0) <= bp.progress_ceiling()

    def test_the_bar_never_regresses(self):
        p = _progress(40.0, [4, 9, 14])
        seen = [p.fraction(t) for t in (10.0, 40.0, 200.0, 20.0, 5.0)]
        assert all(b >= a for a, b in zip(seen, seen[1:]))

    def test_render_never_raises_on_a_hostile_progress(self):
        p = bp.Progress(stages=(), expected_s=-1.0)
        assert isinstance(p.render(float("nan")), str)
        assert isinstance(p.render(-5.0), str)


class TestOnePrecedenceRule:
    def test_fraction_and_render_share_one_horizon(self):
        """Both spelled `expected_s or _projected_total_s(...)` separately.
        Two copies of a precedence rule is two rules, and the drift is found
        by an operator watching a bar disagree with its own ETA."""
        import ast
        import inspect
        import textwrap
        for fn in (bp.Progress.fraction, bp.Progress.render):
            src = textwrap.dedent(inspect.getsource(fn))
            called = {
                n.func.attr for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            assert "_eta_horizon" in called, f"{fn.__name__} re-derives it"

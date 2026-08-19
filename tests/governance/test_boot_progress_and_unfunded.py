"""A wait that says what it is doing, and a ceiling that stops posing as a balance."""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.core.ouroboros.cli import boot_progress as bp
from backend.core.ouroboros.governance import capability_state as cs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_OV_BOOT_HISTORY_PATH", str(tmp_path / "h.json"))
    for k in ("JARVIS_OV_BOOT_PROGRESS_ENABLED", "JARVIS_OV_BOOT_PROGRESS_CEILING"):
        monkeypatch.delenv(k, raising=False)
    cs.reset_for_tests()
    yield
    cs.reset_for_tests()


class TestProgressNeverLies:
    def test_no_history_and_no_markers_shows_no_percentage(self):
        """The honest rendering of "I don't know how far along this is" is no
        number, not a guess dressed as one."""
        p = bp.Progress(expected_s=None)
        out = p.render(4.0)
        assert "%" not in out and "4s" in out

    def test_it_never_regresses(self):
        """A bar that goes backwards destroys the only thing it is for."""
        p = bp.Progress(expected_s=30.0)
        p.observe_log("[AegisPreflight] x\n[CredentialBootstrap] y\n"
                      "[AegisDaemon] serving on x\nStatusLineBuilder registered",
                      now=20.0)
        high = p.fraction(25.0)
        assert p.fraction(1.0) >= high
        assert p.fraction(0.0) >= high

    def test_it_never_reaches_100_before_the_socket_is_live(self):
        """The last few percent belong to the event that actually matters."""
        p = bp.Progress(expected_s=10.0)
        for st in bp.DEFAULT_STAGES:
            p.observe_log(st.marker, now=1.0)
        assert p.fraction(9999.0) <= bp.progress_ceiling() < 1.0

    def test_evidence_outranks_the_estimate(self):
        """A confirmed stage sets a FLOOR the clock cannot argue below."""
        p = bp.Progress(expected_s=600.0)      # estimate says ~0%
        p.observe_log("[AegisPreflight] x\n[CredentialBootstrap] y\n"
                      "[AegisDaemon] serving on x", now=1.0)
        assert p.fraction(2.0) > 0.15

    def test_the_estimate_cannot_claim_an_unconfirmed_stage(self):
        """Interpolation may move the bar between milestones, never past the
        next one that has not happened."""
        p = bp.Progress(expected_s=10.0)
        p.observe_log("[AegisPreflight] x", now=1.0)
        total = sum(s.weight for s in bp.DEFAULT_STAGES)
        nxt = sum(s.weight for s in bp.DEFAULT_STAGES[:2]) / total
        assert p.fraction(9999.0) <= nxt + 1e-9

    def test_unmatched_markers_degrade_to_elapsed_only(self):
        """The marker table couples to log text. A format change must cost the
        bar, never its correctness."""
        p = bp.Progress(expected_s=None)
        p.observe_log("nothing recognisable here", now=5.0)
        assert "%" not in p.render(5.0)

    def test_a_hostile_log_never_raises(self):
        p = bp.Progress(expected_s=10.0)
        for bad in ("", "\x00\x00", "x" * 100000):
            p.observe_log(bad, now=1.0)
            assert isinstance(p.render(1.0), str)


class TestBootHistory:
    def test_only_successful_boots_are_recorded(self):
        """A failed boot has no duration; folding one in would teach the
        estimator that boots take as long as the operator's patience."""
        h = os.path.join(tempfile.mkdtemp(), "h.json")
        for bad in (-1.0, 0.0, 99999.0):
            bp.record_boot_duration(bad, path=h)
        assert bp.observed_boot_durations(h) == []

    def test_the_expectation_needs_evidence(self):
        h = os.path.join(tempfile.mkdtemp(), "h.json")
        bp.record_boot_duration(10.0, path=h)
        bp.record_boot_duration(12.0, path=h)
        assert bp.expected_boot_s(h) is None      # 2 samples is not a median
        bp.record_boot_duration(11.0, path=h)
        assert bp.expected_boot_s(h) == 11.0

    def test_history_is_bounded(self, monkeypatch):
        monkeypatch.setenv("JARVIS_OV_BOOT_HISTORY_MAX", "5")
        h = os.path.join(tempfile.mkdtemp(), "h.json")
        for i in range(30):
            bp.record_boot_duration(float(i + 1), path=h)
        assert len(bp.observed_boot_durations(h)) == 5

    def test_a_corrupt_history_never_raises(self):
        h = os.path.join(tempfile.mkdtemp(), "h.json")
        open(h, "w").write("{not json")
        assert bp.observed_boot_durations(h) == []
        assert bp.expected_boot_s(h) is None


class TestUnfundedNamesTheRemedy:
    """NOTE on the fixture: these use the real `monkeypatch` FIXTURE, never a
    bare `pytest.MonkeyPatch()`. A directly-constructed one is never undone,
    so the class-level patches to `_read_lanes` / `_read_ops` / `_read_remote`
    survived the test and leaked into every file that ran afterwards —
    producing failures that appeared only when the suite ran together and
    vanished when a file ran alone."""

    def _ev(self, monkeypatch, remote):
        e = cs.CapabilityEvaluator()
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                            staticmethod(lambda: (True, "doubleword", True)))
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_ops",
                            staticmethod(lambda: (0, 0, 0, True)))
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_remote",
                            staticmethod(lambda: (remote, "http://h:1", True)))
        return e.evaluate()

    def test_out_of_credit_is_unfunded_not_merely_blocked(self, monkeypatch):
        """"blocked" sends the operator to a log to find out why. "unfunded"
        names the remedy in the word itself — and money is the one blocker the
        organism can never clear on its own."""
        r = self._ev(monkeypatch, "absent")
        assert r.state is cs.Capability.UNFUNDED
        assert r.badge == "unfunded"

    def test_the_reason_states_what_to_do(self, monkeypatch):
        r = self._ev(monkeypatch, "absent")
        assert "add credits" in r.reason.lower()

    def test_unfunded_still_stops_dispatch(self):
        # no evaluator needed — this asks the enum directly
        """A friendlier word must not become a lesser state."""
        assert cs.Capability.UNFUNDED.is_blocking
        assert not cs.Capability.UNFUNDED.can_work

    def test_a_serving_local_lane_is_degraded_not_unfunded(self, monkeypatch):
        r = self._ev(monkeypatch, "serving")
        assert r.state is cs.Capability.DEGRADED
        assert not r.is_funding_issue


class TestOneLineNotMany:
    """The indicator must REDRAW, not accumulate.

    Observed: four identical rows appended during a single boot —

        ⎿ [██████·······]  56%  session open   0s
        ⎿ [██████·······]  56%  session open   5s
        ⎿ [██████·······]  56%  session open  10s
        ⎿ [██████·······]  56%  session open  16s

    Two separate defects in one picture: it appended instead of redrawing,
    and the percentage was frozen between stages.
    """

    def _tty_render(self, ticks):
        import io as _io
        import sys as _sys

        class _FakeTTY(_io.StringIO):
            def isatty(self):
                return True

        from backend.core.ouroboros.cli.thin_client import _mk_tick
        real = _sys.__stdout__
        _sys.__stdout__ = _FakeTTY()
        try:
            said = []
            tick = _mk_tick(said.append)
            for e in ticks:
                tick(e)
            return said, _sys.__stdout__.getvalue()
        finally:
            _sys.__stdout__ = real

    def test_a_tty_redraws_one_line(self):
        said, buf = self._tty_render([0.0, 0.3, 0.6, 0.9])
        assert said == []                 # nothing appended
        assert buf.count("\r") >= 3       # redrawn in place
        assert "\n" not in buf            # exactly one line

    def test_a_non_tty_keeps_appending(self):
        """A carriage return in a pipe or a log file is an unreadable smear.
        The wait must not become less legible in the transcript to become
        prettier on screen."""
        import sys as _sys
        from backend.core.ouroboros.cli.thin_client import _mk_tick
        real = _sys.__stdout__
        _sys.__stdout__ = None            # no real stdout -> not interactive
        try:
            said = []
            tick = _mk_tick(said.append)
            for e in (0.0, 6.0, 12.0):
                tick(e)
        finally:
            _sys.__stdout__ = real
        assert len(said) >= 2
        assert all("\r" not in s for s in said)

    def test_the_tty_check_uses_the_unpatched_stream(self):
        """`prompt_toolkit.patch_stdout` swaps sys.stdout for a non-TTY proxy,
        so `sys.stdout.isatty()` returns False on a real terminal — which is
        exactly why the indicator appended. The codebase already solved this
        once for the live status line; a second TTY check would be the bug a
        third time."""
        import inspect
        from backend.core.ouroboros.cli import thin_client
        src = inspect.getsource(thin_client._mk_tick)
        assert "real_stdout_isatty" in src


class TestTheBarMovesOnAFirstBoot:
    def test_progress_advances_between_stages_without_history(self):
        """56% frozen across four ticks: with no cross-boot history there was
        no horizon, so the bar could only move when a marker landed."""
        p = bp.Progress(expected_s=None)
        log = ""
        for t, line in ((2.0, "[AegisPreflight] a"),
                        (5.0, "[CredentialBootstrap] b"),
                        (9.0, "[AegisDaemon] serving on x")):
            log += line + "\n"
            p.observe_log(log, now=t)
        at_stage = p.fraction(9.0)
        later = p.fraction(12.0)
        assert later is not None and at_stage is not None
        assert later > at_stage           # it MOVED with no new marker

    def test_one_arrival_is_enough_because_the_start_is_known(self):
        """An earlier version measured the rate BETWEEN arrivals and so
        returned nothing when several markers landed in the same poll — the
        common case on a fast boot, since the tail is read every quarter
        second. Anchored on t=0, one arrival is a rate: two stages by second
        four is two seconds a stage."""
        p = bp.Progress(expected_s=None)
        assert p._projected_total_s(1.0) is None      # nothing reached yet
        p.observe_log("[AegisPreflight] a", now=2.0)
        assert p._projected_total_s(3.0) is not None

    def test_simultaneous_arrivals_still_project(self):
        """Two markers in ONE observation share a timestamp; the interval
        form produced zero span and gave up."""
        p = bp.Progress(expected_s=None)
        p.observe_log("[AegisPreflight] a\n[CredentialBootstrap] b", now=4.0)
        assert p._projected_total_s(5.0) is not None

    def test_an_overrunning_boot_never_promises_a_past_finish(self):
        """An ETA of "0s left" that keeps not arriving is worse than none."""
        p = bp.Progress(expected_s=None)
        p.observe_log("[AegisPreflight] a", now=1.0)
        assert p._projected_total_s(500.0) >= 500.0

    def test_projection_still_cannot_claim_an_unconfirmed_stage(self):
        """Interpolation earns motion, never milestones."""
        p = bp.Progress(expected_s=None)
        p.observe_log("[AegisPreflight] a\n[CredentialBootstrap] b", now=5.0)
        total = sum(s.weight for s in bp.DEFAULT_STAGES)
        nxt = sum(s.weight for s in bp.DEFAULT_STAGES[:3]) / total
        assert p.fraction(9999.0) <= nxt + 1e-9

    def test_cross_boot_history_still_wins_when_present(self):
        """More samples, and it already knows how this machine behaves.

        Asserts the INTENT — that the horizon lifted the bar above the pure
        evidence floor — rather than a magic number. The first version
        asserted >= 0.4 and failed at 0.375, which is the CORRECT value:
        clamped at the next unconfirmed stage, exactly as designed. The test
        was wrong, not the clamp.
        """
        p = bp.Progress(expected_s=20.0)
        p.observe_log("[AegisPreflight] a\n[CredentialBootstrap] b", now=1.0)
        assert p._projected_total_s(2.0) is not None   # both available
        total = sum(s.weight for s in bp.DEFAULT_STAGES)
        evidence_floor = sum(s.weight for s in bp.DEFAULT_STAGES[:2]) / total
        next_bound = sum(s.weight for s in bp.DEFAULT_STAGES[:3]) / total
        got = p.fraction(10.0)
        assert evidence_floor < got <= next_bound

    def test_an_eta_appears_without_any_history(self):
        p = bp.Progress(expected_s=None)
        p.observe_log("[AegisPreflight] a\n[CredentialBootstrap] b", now=4.0)
        assert "left" in p.render(6.0)

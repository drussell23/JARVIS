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
    def _ev(self, monkeypatch, remote):
        e = cs.CapabilityEvaluator()
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_lanes",
                            staticmethod(lambda: (True, "doubleword", True)))
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_ops",
                            staticmethod(lambda: (0, 0, 0, True)))
        monkeypatch.setattr(cs.CapabilityEvaluator, "_read_remote",
                            staticmethod(lambda: (remote, "http://h:1", True)))
        return e.evaluate()

    def test_out_of_credit_is_unfunded_not_merely_blocked(self):
        """"blocked" sends the operator to a log to find out why. "unfunded"
        names the remedy in the word itself — and money is the one blocker the
        organism can never clear on its own."""
        r = self._ev(pytest.MonkeyPatch(), "absent")
        assert r.state is cs.Capability.UNFUNDED
        assert r.badge == "unfunded"

    def test_the_reason_states_what_to_do(self):
        r = self._ev(pytest.MonkeyPatch(), "absent")
        assert "add credits" in r.reason.lower()

    def test_unfunded_still_stops_dispatch(self):
        """A friendlier word must not become a lesser state."""
        assert cs.Capability.UNFUNDED.is_blocking
        assert not cs.Capability.UNFUNDED.can_work

    def test_a_serving_local_lane_is_degraded_not_unfunded(self):
        r = self._ev(pytest.MonkeyPatch(), "serving")
        assert r.state is cs.Capability.DEGRADED
        assert not r.is_funding_issue

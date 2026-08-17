"""A level says how much is left. It cannot say what is being destroyed.

WHAT WAS MEASURED, ON THIS HOST
-------------------------------
The canonical memory gate reported ``warn`` — 22.8% free, well clear of its
own thresholds — at the same moment that:

  * the machine was swapping IN at 3.6 MiB/s (later 25.7 MiB/s), and
  * ``coreaudiod`` had logged a real overload four minutes earlier, with
    ``safety_violation: 1`` and ``multi_cycle_io_page_faults_duration:
    3141689`` — page faults taken INSIDE the real-time IO cycle.

`local_model_admission`'s docstring had described that exact failure since it
was written ("a swap storm during model load is exactly the condition that
turns a dropped buffer into a severed sentence"). It was PROSE. Nothing read
it, and the ladder would have admitted a model load into it.

These tests pin the two dimensions that were missing — a RATE and a VICTIM —
and the composition that turns them into the existing admit/prune/defer
ladder rather than a second decision path.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance import audio_contention_probe as acp
from backend.core.ouroboros.governance import local_model_admission as lma


#: A real record, copied from `/usr/bin/log show` on this machine.
REAL_OVERLOAD = (
    '2026-08-17 11:35:46.195 Df coreaudiod[413:edf] '
    '[com.apple.audioanalytics:carc] Sending message. { reporterID=683329, '
    'category=IO, type=error, message=["io_frame_counter": Optional(1024), '
    '"is_recovering": Optional(0), "overload_type": Optional(Overload), '
    '"multi_cycle_io_page_faults_duration": Optional(3141689), '
    '"safety_violation": Optional(1), "scheduler_latency": Optional(31083), '
    '"deadline": Optional(2151), "lateness": Optional(271)] }'
)


class TestTheAudioProbeReadsTheRealRecord:

    def test_it_extracts_the_causal_fields(self):
        """A parser that always returned zero would pass a quiet-machine
        test. This is the record it must actually understand."""
        r = acp._parse(REAL_OVERLOAD, 120.0)
        assert r.ok and r.overloads == 1
        assert r.safety_violations == 1
        assert r.worst_lateness == 271
        assert r.page_fault_ns == 3141689
        assert r.contended is True
        assert r.paging_implicated is True

    def test_an_overload_without_page_faults_is_not_paging_implicated(self):
        """Audio can overload for reasons a local model cannot cause — a USB
        interface, a hostile plugin. Refusing local inference for those would
        be a guard punishing the innocent; only mid-cycle page faults are
        evidence that MEMORY did it."""
        clean = REAL_OVERLOAD.replace(
            '"multi_cycle_io_page_faults_duration": Optional(3141689)',
            '"multi_cycle_io_page_faults_duration": Optional(0)')
        r = acp._parse(clean, 120.0)
        assert r.contended is True
        assert r.paging_implicated is False

    def test_a_quiet_window_is_measured_not_unknown(self):
        r = acp._parse("", 120.0)
        assert r.ok is True and r.overloads == 0 and r.contended is False

    def test_unknown_is_never_read_as_healthy(self):
        """`ok=False` must not satisfy `contended` OR its negation by
        accident — an unmeasured probe means the memory dimension rules
        alone, not that audio is fine."""
        unknown = acp.AudioContention(ok=False, error="unanswered")
        assert unknown.contended is False
        assert unknown.paging_implicated is False
        assert unknown.ok is False

    def test_disabled_returns_unknown_not_healthy(self, monkeypatch):
        monkeypatch.setenv("JARVIS_AUDIO_CONTENTION_PROBE_ENABLED", "0")
        acp.reset_for_tests()
        r = acp.probe()
        assert r.ok is False and r.error == "disabled"


class TestThePagingRateSurvivesRapidPolling:

    def test_a_rate_needs_two_samples(self):
        s = lma._PagingSampler()
        assert s.rate_bps() is None, "a rate was invented from one sample"

    def test_it_holds_the_last_good_rate_between_samples(self, monkeypatch):
        """THE BUG THIS PINS. The first version recomputed on every call, so
        a caller sampling twice a millisecond apart measured a near-zero
        delta over a near-zero interval and got ~0 B/s — the guard silently
        DISARMED itself under exactly the rapid-polling pattern an admission
        check produces. Observed live: 27.4 MiB/s on one read, `admit` on the
        very next call."""
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_MIN_INTERVAL_S", "5")
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_RATE_TTL_S", "60")
        s = lma._PagingSampler()

        counter = {"v": 0}
        monkeypatch.setattr(s, "_read_swapin_bytes",
                            staticmethod(lambda: counter["v"]))
        assert s.rate_bps() is None            # baseline
        time.sleep(0.05)
        counter["v"] = 10 * 1024 * 1024
        # Still inside min_interval -> no NEW rate, and none cached yet.
        assert s.rate_bps() is None

        # Force a measurable interval, then read a real rate.
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_MIN_INTERVAL_S", "0.2")
        time.sleep(0.25)
        first = s.rate_bps()
        assert first is not None and first > 0

        # Immediately re-poll: must serve the held rate, NOT collapse to 0.
        again = s.rate_bps()
        assert again == pytest.approx(first), (
            f"rapid re-poll collapsed the rate {first} -> {again}; the guard "
            f"disarms itself under admission-check polling")

    def test_a_stale_held_rate_expires_to_unknown_not_zero(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_MIN_INTERVAL_S", "60")
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_RATE_TTL_S", "0.05")
        s = lma._PagingSampler()
        monkeypatch.setattr(s, "_read_swapin_bytes", staticmethod(lambda: 1))
        s.rate_bps()
        time.sleep(0.1)
        assert s.rate_bps() is None, "a stale rate was reported as fact"

    def test_a_counter_reset_is_unknown_not_negative(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_MIN_INTERVAL_S", "0.01")
        s = lma._PagingSampler()
        vals = iter([10_000_000, 5])           # reboot / wrap
        monkeypatch.setattr(s, "_read_swapin_bytes",
                            staticmethod(lambda: next(vals)))
        s.rate_bps()
        time.sleep(0.05)
        assert s.rate_bps() is None


class TestTheEscalationUsesTheExistingLadder:

    def test_it_climbs_the_ladder_and_saturates(self):
        assert lma._escalate("ok", 1) == "warn"
        assert lma._escalate("warn", 1) == "high"
        assert lma._escalate("warn", 2) == "critical"
        assert lma._escalate("high", 1) == "critical"
        assert lma._escalate("critical", 1) == "critical"

    def test_an_unreadable_level_is_never_escalated(self):
        """`_read_host_pressure` returns 'unknown' when its probe fails.
        Escalating from a level nobody could read would let one broken
        probe manufacture a refusal out of nothing — the fail-toward-OK
        posture this module already keeps."""
        assert lma._escalate("unknown", 3) == "unknown"

    def test_zero_rungs_is_identity(self):
        assert lma._escalate("warn", 0) == "warn"

    def test_the_gate_can_be_switched_off_to_restore_the_old_ladder(
            self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_CONTENTION_GATE_ENABLED", "0")
        rungs, reasons, evidence = lma._read_contention()
        assert rungs == 0 and reasons == []
        assert evidence == {"enabled": False}

    def test_each_signal_contributes_one_rung(self, monkeypatch):
        """Strictest-wins composition, matching this module's own stated
        discipline across its accelerator and host bounds."""
        monkeypatch.setenv("JARVIS_LOCAL_CONTENTION_GATE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_RATE_BPS", "1000")
        monkeypatch.setattr(lma._paging_sampler, "rate_bps",
                            lambda: 50_000.0)          # over threshold
        monkeypatch.setattr(
            acp, "probe",
            lambda force=False: acp._parse(REAL_OVERLOAD, 120.0))
        rungs, reasons, _ = lma._read_contention()
        assert rungs == 2, f"expected paging+audio = 2 rungs, got {rungs}: {reasons}"

    def test_quiet_machine_escalates_nothing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LOCAL_CONTENTION_GATE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_LOCAL_PAGING_RATE_BPS", "1000000")
        monkeypatch.setattr(lma._paging_sampler, "rate_bps", lambda: 10.0)
        monkeypatch.setattr(acp, "probe",
                            lambda force=False: acp._parse("", 120.0))
        rungs, reasons, _ = lma._read_contention()
        assert rungs == 0 and reasons == []

    def test_unmeasurable_signals_never_escalate(self, monkeypatch):
        """Unknown is not contention. A probe that cannot answer must not
        become a permanent refusal — that is how a safety guard turns into
        the reason a tier is dark forever."""
        monkeypatch.setenv("JARVIS_LOCAL_CONTENTION_GATE_ENABLED", "1")
        monkeypatch.setattr(lma._paging_sampler, "rate_bps", lambda: None)
        monkeypatch.setattr(
            acp, "probe",
            lambda force=False: acp.AudioContention(ok=False, error="x"))
        rungs, _, _ = lma._read_contention()
        assert rungs == 0

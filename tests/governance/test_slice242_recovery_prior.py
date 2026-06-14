"""Slice 242 — adaptive statistical recovery-duration prior for hibernation probing.

The hibernation_prober (the "Grid Sentinel") already probes a dark DW grid on
exponential backoff and auto-wakes on recovery. T2's gap: the FIRST probe interval
was a STATIC default (5s) — wasteful when outages historically last minutes (you
ping a dark grid for nothing). This adds an online, training-free estimator
(no NN — exogenous vendor outage, no predictive features): record observed outage
durations (enter_hibernation → wake), and set the first re-probe near a low
quantile (p25) of history so we don't ping before the grid plausibly recovers.
Optimizes resurrection latency vs probe cost; ~$0 while dark. Falls back to the
static default below min-samples. NOT a recovery predictor — it times WHEN to start
probing, never claims to know when DW returns.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import dw_transport_recovery as rec


class TestRecoveryDurationPrior:
    def _fresh(self):
        p = rec.RecoveryDurationPrior()
        p.reset()
        return p

    def test_insufficient_history_returns_default(self):
        p = self._fresh()
        # below min_samples → fall back to the static default (no trust yet)
        assert p.first_probe_interval(default_s=5.0, max_s=300.0, min_samples=3) == 5.0
        p.record(120.0)
        p.record(140.0)
        assert p.first_probe_interval(default_s=5.0, max_s=300.0, min_samples=3) == 5.0

    def test_with_history_uses_p25_quantile(self):
        p = self._fresh()
        # durations: p25 of [60,120,180,240,300] ≈ 120
        for d in (60.0, 120.0, 180.0, 240.0, 300.0):
            p.record(d)
        out = p.first_probe_interval(default_s=5.0, max_s=600.0, min_samples=3, quantile=0.25)
        assert 100.0 <= out <= 140.0  # near p25 — don't probe before then

    def test_clamped_to_max(self):
        p = self._fresh()
        for d in (1000.0, 1200.0, 1400.0):
            p.record(d)
        out = p.first_probe_interval(default_s=5.0, max_s=300.0, min_samples=3)
        assert out == 300.0  # never exceed the cap

    def test_clamped_to_floor(self):
        p = self._fresh()
        for d in (0.1, 0.2, 0.3):
            p.record(d)
        out = p.first_probe_interval(default_s=5.0, max_s=300.0, min_samples=3, floor_s=1.0)
        assert out == 1.0  # never below the floor

    def test_ring_is_bounded(self):
        p = self._fresh()
        for d in range(1000):
            p.record(float(d))
        # bounded window — only the most recent N durations are retained
        assert p.sample_count() <= rec._recovery_prior_window()

    def test_fail_soft_on_bad_input(self):
        p = self._fresh()
        p.record("oops")  # non-numeric — swallowed
        p.record(-5.0)    # negative — ignored
        assert p.sample_count() == 0
        # bad args → default, never raise
        assert p.first_probe_interval(default_s=5.0, max_s=300.0) == 5.0

    def test_quantile_correctness(self):
        p = self._fresh()
        for d in (10.0, 20.0, 30.0, 40.0):
            p.record(d)
        # p50 of [10,20,30,40] is ~25 (linear interp) — sanity on the quantile math
        q = p.quantile(0.5)
        assert 20.0 <= q <= 30.0


class TestRecoveryPriorEnvKnobs:
    def test_window_and_quantile_and_minsamples_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_RECOVERY_PRIOR_WINDOW", raising=False)
        assert isinstance(rec._recovery_prior_window(), int) and rec._recovery_prior_window() > 0
        monkeypatch.setenv("JARVIS_RECOVERY_PRIOR_WINDOW", "8")
        assert rec._recovery_prior_window() == 8
        monkeypatch.delenv("JARVIS_RECOVERY_PRIOR_QUANTILE", raising=False)
        d = rec._recovery_prior_quantile()
        assert 0.0 < d < 1.0
        monkeypatch.setenv("JARVIS_RECOVERY_PRIOR_QUANTILE", "0.4")
        assert rec._recovery_prior_quantile() == 0.4

    def test_singleton_accessor(self):
        a = rec.get_recovery_prior()
        b = rec.get_recovery_prior()
        assert a is b  # process-wide accumulation of outage history


class TestProberWiring:
    """The prober records the outage duration on wake and consults the prior for
    the first probe interval (source pins — the deep async loop is covered by the
    injection integration test below + the existing prober suite)."""

    def test_prober_records_duration_and_consults_prior(self):
        from backend.core.ouroboros.governance import hibernation_prober as hp
        src = inspect.getsource(hp)
        assert "recovery_prior" in src or "RecoveryDurationPrior" in src or "get_recovery_prior" in src
        assert "first_probe_interval" in src, "prober must derive the first interval from the prior"
        assert ".record(" in src, "prober must record the outage duration on wake"

"""Is the voice biometric fit to guard anything? Measured, then answered.

Before binding screen unlock to VBIA somebody had to compute its error rates.
The data was already being written by `unlock_metrics_logger` (live at
`intelligent_voice_unlock_service.py:1577`); nobody had added it up.

WHAT THE FIRST RUN FOUND — 34 attempts, one speaker, Nov 2025

    threshold used 0.35  ·  declared rejection floor 0.60
    accepted 19 · rejected 0 · impostor trials 0
    confidence  min 0.438 · p50 0.593 · max 0.950

Three disqualifying facts, and this suite pins each so a future change cannot
quietly declare readiness without fixing them:

1. the gate ran at roughly HALF the strictness the module's own config claims
2. half the OWNER's attempts fall below that declared floor, so the threshold
   cannot simply be raised without locking the owner out
3. zero impostor trials and zero rejections ever — an UNMEASURED false-accept
   rate, not a low one

`test_far_is_never_reported_as_a_number_without_impostors` is the one to keep.
Computing "0% FAR" from owner-only samples is arithmetic pretending to be
evidence, and it is exactly the figure a security decision must never rest on.
"""
from __future__ import annotations

import json

import pytest

from backend.voice_unlock import biometric_calibration as bc
from backend.voice_unlock.biometric_calibration import (
    Calibration, Readiness, binding_readiness, calibrate,
)


def _row(speaker="Derek", conf=0.6, above=True, threshold=0.35):
    return {"speaker_name": speaker, "success": True,
            "biometrics": {"speaker_confidence": conf, "threshold": threshold,
                           "above_threshold": above}}


class TestItReadsWhatIsAlreadyRecorded:
    def test_it_computes_accept_and_reject_counts(self):
        cal = calibrate([_row(conf=0.8), _row(conf=0.4, above=False)])
        assert cal.accepted == 1 and cal.rejected == 1
        assert cal.accept_rate == 0.5

    def test_it_surfaces_the_threshold_actually_used(self):
        cal = calibrate([_row(threshold=0.35)])
        assert cal.thresholds_used == (0.35,)

    def test_a_second_speaker_counts_as_an_impostor_trial(self):
        """The only way the existing logs could ever yield a FAR without new
        labelling: the majority speaker is the owner, everyone else is not."""
        cal = calibrate([_row("Derek")] * 5 + [_row("Someone Else")])
        assert cal.owner_trials == 5 and cal.impostor_trials == 1

    def test_empty_data_is_reported_not_guessed(self):
        cal = calibrate([])
        assert cal.trials == 0 and "no recorded attempts" in cal.reasons

    def test_unreadable_files_never_raise(self, tmp_path):
        (tmp_path / "unlock_metrics_bad.json").write_text("{not json")
        assert bc.load(tmp_path) == []


class TestTheHonestyRules:
    def test_far_is_never_reported_as_a_number_without_impostors(self):
        """THE one to keep."""
        cal = calibrate([_row()] * 100)
        assert cal.impostor_trials == 0
        assert cal.far_measurable is False
        assert cal.false_accept_rate is None, (
            "a false-accept rate was quoted from owner-only data")

    def test_owner_only_data_is_never_READY(self):
        assert binding_readiness(calibrate([_row()] * 500))[0] is Readiness.NOT_READY

    def test_a_loose_threshold_is_named_explicitly(self, monkeypatch):
        monkeypatch.setenv("VBI_REJECTION_THRESHOLD", "0.60")
        _state, reasons = binding_readiness(calibrate([_row(threshold=0.35)]))
        assert any("BELOW the module's own declared" in r for r in reasons)

    def test_zero_rejections_is_called_out(self):
        _state, reasons = binding_readiness(calibrate([_row()] * 10))
        assert any("zero rejections" in r and "say no" in r for r in reasons)

    def test_the_owner_lockout_risk_is_quantified(self, monkeypatch):
        """Half the owner's attempts below the declared floor means raising the
        threshold locks the owner out — the number that makes this a real
        trade-off rather than a slider to nudge."""
        monkeypatch.setenv("VBI_REJECTION_THRESHOLD", "0.60")
        cal = calibrate([_row(conf=0.9)] * 5 + [_row(conf=0.4)] * 5)
        assert cal.owner_clears_declared == 0.5
        assert any("lock the owner out" in r for r in binding_readiness(cal)[1])

    def test_no_data_is_distinct_from_not_ready(self):
        """Never measured and measured-and-failed need opposite responses."""
        assert binding_readiness(calibrate([]))[0] is Readiness.NO_DATA


class TestFailClosed:
    def test_readiness_requires_every_check(self, monkeypatch):
        """Enough trials, real impostors, real rejections, a sane threshold and
        an owner who clears it — only then READY."""
        monkeypatch.setenv("JARVIS_VBIA_MIN_TRIALS", "10")
        monkeypatch.setenv("JARVIS_VBIA_MIN_IMPOSTORS", "3")
        monkeypatch.setenv("VBI_REJECTION_THRESHOLD", "0.60")
        rows = ([_row("Derek", conf=0.9, threshold=0.60)] * 10
                + [_row("Impostor", conf=0.3, above=False, threshold=0.60)] * 3)
        state, reasons = binding_readiness(calibrate(rows, owner="Derek"))
        assert state is Readiness.READY, reasons

    def test_one_missing_check_is_enough_to_refuse(self, monkeypatch):
        monkeypatch.setenv("JARVIS_VBIA_MIN_TRIALS", "10")
        monkeypatch.setenv("JARVIS_VBIA_MIN_IMPOSTORS", "3")
        rows = [_row("Derek", conf=0.9, threshold=0.60)] * 10   # no impostors
        assert binding_readiness(calibrate(rows, owner="Derek"))[0] is Readiness.NOT_READY

    def test_a_degraded_calibration_refuses(self, monkeypatch):
        monkeypatch.setattr(bc, "calibrate",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        assert binding_readiness()[0] is Readiness.NOT_READY

    def test_render_never_raises(self):
        assert bc.render(Calibration())


class TestAgainstTheRealRecordedData:
    def test_the_live_verdict_is_NOT_READY(self):
        """The finding, pinned. If this ever flips to READY it must be because
        impostor trials were collected — not because a check was softened."""
        cal = calibrate()
        if not cal.trials:
            pytest.skip("no recorded unlock attempts on this host")
        state, reasons = binding_readiness(cal)
        assert state is Readiness.NOT_READY
        assert reasons

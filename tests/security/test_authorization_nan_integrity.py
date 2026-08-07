"""
A NaN may never reach, or survive in, anything that authorises.

Four defects, found while closing the acoustic stage:

1. ``_compute_acoustic_match_sync`` emitted NaN into the fusion on every
   verification once the enrolled formants were NULLed as impossible.
   ``abs(NaN - x)`` is NaN, ``exp(-NaN)`` is NaN, ``np.average`` of a list
   containing NaN is NaN.

2. It did not corrupt the verdict, and the reason is worse than if it had:
   ``_bayesian_verification_sync`` computed a full posterior and then
   overwrote it with ``final_auth_score`` on the next line. The channel was
   SEVERED, not safe. A guard installed there would have guarded nothing.

3. The real authorization tensor is ``_owner_aware_antispoof_fusion_sync``,
   where a NaN would compare False against every ``>=`` and fall through to
   whichever else-branch it reached — a verdict decided by control flow.

4. ``_update_speaker_model_sync`` folds features into EMAs over PERSISTENT
   state, on the SUCCESS path. One NaN makes ``speaker_model.pitch_mean`` NaN
   forever; an EMA cannot recover. The honest extractors introduced in #70427
   made that reachable for the first time.

And one calibration defect: the jitter/shimmer caps were clinical constants
(head-mounted mic, treated room) applied to a laptop array. The operator's own
VERIFIED sample scored jitter:0.27 shimmer:0.33 against them.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from backend.voice.advanced_biometric_verification import (
    AcousticAssessment,
    AdvancedBiometricVerifier,
    PhysicsConstraints,
)
from backend.voice.biological_bounds import (
    UNMEASURED,
    BiologicalBoundsValidator,
    PerturbationTolerance,
    fold_measurement,
    renormalized_weighted_mean,
)


class _Features:
    def __init__(self, **kw):
        self.embedding = kw.get("embedding", np.zeros(192, dtype=np.float32))
        self.pitch_mean = kw.get("pitch_mean", 120.0)
        self.pitch_std = kw.get("pitch_std", 30.0)
        self.formant_f1 = kw.get("formant_f1", 520.0)
        self.formant_f2 = kw.get("formant_f2", 1480.0)
        self.formant_f3 = kw.get("formant_f3", 2500.0)
        self.spectral_centroid = kw.get("spectral_centroid", 1600.0)
        self.speaking_rate = kw.get("speaking_rate", 150.0)
        self.harmonic_to_noise_ratio = kw.get("hnr", 18.0)
        self.jitter = kw.get("jitter", 0.01)
        self.shimmer = kw.get("shimmer", 0.04)


class _Model:
    def __init__(self, **kw):
        self.pitch_std = kw.get("pitch_std", 20.0)
        self.pitch_mean = kw.get("pitch_mean", 120.0)
        self.acoustic_weights = kw.get("acoustic_weights", [0.4, 0.3, 0.2, 0.1])
        self.is_primary_owner = kw.get("is_primary_owner", True)
        self.owner_strong_threshold = kw.get("owner_strong_threshold", 0.65)
        self.spoof_override_limit = kw.get("spoof_override_limit", 0.90)
        self.decision_threshold = kw.get("decision_threshold", 0.4885)
        self.last_fusion_debug = None
        self.embedding_samples = []
        self.feature_samples = []
        self.covariance_matrix = np.eye(3)


def _verifier() -> AdvancedBiometricVerifier:
    v = AdvancedBiometricVerifier.__new__(AdvancedBiometricVerifier)
    v.physics = PhysicsConstraints()
    return v


# ─────────────────────────────────────────────────────────────────────────
# 1. The neutral prior — what "drop the term" means
# ─────────────────────────────────────────────────────────────────────────

def test_renormalising_is_neither_penalty_nor_inflation():
    """
    Dropping a term and renormalising leaves the result exactly where the
    surviving evidence puts it. Substituting a constant does not.
    """
    survivors = [(0.9, 0.4), (0.9, 0.3)]
    assert renormalized_weighted_mean(survivors) == pytest.approx(0.9)

    # A 0.5 stand-in for the absent term would drag a strong result down...
    with_neutral_half = renormalized_weighted_mean(survivors + [(0.5, 0.3)])
    assert with_neutral_half < 0.9
    # ...and a 1.0 stand-in would inflate a weak one.
    weak = [(0.2, 0.4), (0.2, 0.3)]
    assert renormalized_weighted_mean(weak + [(1.0, 0.3)]) > 0.2


def test_nothing_measurable_yields_unmeasured_not_a_middle_value():
    """The caller must drop its own term in turn, not inherit a fabricated 0.5."""
    assert math.isnan(renormalized_weighted_mean([]))
    assert math.isnan(renormalized_weighted_mean([(UNMEASURED, 0.4)]))
    assert math.isnan(renormalized_weighted_mean([(0.9, 0.0)]))


# ─────────────────────────────────────────────────────────────────────────
# 2. The acoustic stage
# ─────────────────────────────────────────────────────────────────────────

def test_acoustic_emits_no_nan_when_the_enrolled_side_was_nulled():
    """
    The live 2026-08-07 shape: the sanitiser NULLed the enrolled formants and
    rate, so those comparisons cannot be made.
    """
    assessment = _verifier()._compute_acoustic_match_sync(
        _Features(),
        _Features(formant_f1=UNMEASURED, formant_f2=UNMEASURED,
                  formant_f3=UNMEASURED, speaking_rate=UNMEASURED),
        _Model())
    assert not math.isnan(assessment.score), assessment.describe()
    assert "formants" not in assessment.components
    assert "rate" not in assessment.components
    assert assessment.has_evidence, "pitch and spectral were measurable on both sides"


def test_acoustic_requires_both_sides_measured():
    """
    Comparing a live formant against an absent enrolled one is not a weak
    match — it is not a comparison.
    """
    assessment = _verifier()._compute_acoustic_match_sync(
        _Features(formant_f1=520.0),
        _Features(formant_f1=UNMEASURED, formant_f2=UNMEASURED, formant_f3=UNMEASURED),
        _Model())
    assert "formants" not in assessment.components
    assert any("formants" in a for a in assessment.abstained)


def test_acoustic_with_no_evidence_at_all_reports_absence_not_a_score():
    assessment = _verifier()._compute_acoustic_match_sync(
        _Features(), _Features(pitch_mean=UNMEASURED, formant_f1=UNMEASURED,
                               formant_f2=UNMEASURED, formant_f3=UNMEASURED,
                               spectral_centroid=UNMEASURED, speaking_rate=UNMEASURED),
        _Model())
    assert assessment.components == {}
    assert math.isnan(assessment.score)
    assert assessment.has_evidence is False


def test_acoustic_weights_renormalise_over_survivors():
    """
    With only pitch surviving, the score IS the pitch score — not the pitch
    score diluted by three absent components.
    """
    assessment = _verifier()._compute_acoustic_match_sync(
        _Features(pitch_mean=120.0),
        _Features(pitch_mean=120.0, formant_f1=UNMEASURED, formant_f2=UNMEASURED,
                  formant_f3=UNMEASURED, spectral_centroid=UNMEASURED,
                  speaking_rate=UNMEASURED),
        _Model())
    assert assessment.components == {"pitch": pytest.approx(1.0)}
    assert assessment.score == pytest.approx(1.0)


def test_a_real_mismatch_still_scores_low():
    """Abstention must not be indistinguishable from switching the stage off."""
    assessment = _verifier()._compute_acoustic_match_sync(
        _Features(pitch_mean=110.0, formant_f1=500.0, formant_f2=1400.0,
                  formant_f3=2400.0, spectral_centroid=1500.0, speaking_rate=140.0),
        _Features(pitch_mean=260.0, formant_f1=900.0, formant_f2=2600.0,
                  formant_f3=3400.0, spectral_centroid=4000.0, speaking_rate=260.0),
        _Model())
    assert assessment.has_evidence is True
    assert assessment.score < 0.3, assessment.describe()


# ─────────────────────────────────────────────────────────────────────────
# 3. The authorization tensor — the guard on the LIVE path
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("owner_match_score", float("nan")),
    ("owner_match_score", float("inf")),
    ("spoof_prob", float("nan")),
])
def test_non_finite_fusion_input_fails_closed(field, value):
    """
    A NaN compares False against every threshold, so an unguarded one picks a
    verdict by control flow. It must deny, loudly.
    """
    kwargs = dict(owner_match_score=0.9, spoof_prob=0.1)
    kwargs[field] = value
    score, decision, debug = _verifier()._owner_aware_antispoof_fusion_sync(
        is_owner=True, speaker_model=_Model(), embedding_sim=0.9,
        acoustic_score=0.8, physics_score=0.9, **kwargs)
    assert decision == "deny"
    assert score == 0.0
    assert debug["rule_applied"] == "nan_input_fail_closed"
    assert debug["authorization_anomaly"]["kind"] == "non_finite_fusion_input"
    assert field in debug["authorization_anomaly"]["fields"]


def test_a_corrupt_threshold_on_the_model_also_fails_closed():
    """The thresholds are inputs too — a NaN one makes every comparison False."""
    _, decision, debug = _verifier()._owner_aware_antispoof_fusion_sync(
        owner_match_score=0.9, spoof_prob=0.1, is_owner=True,
        speaker_model=_Model(decision_threshold=float("nan")),
        embedding_sim=0.9, acoustic_score=0.8, physics_score=0.9)
    assert decision == "deny"
    assert "decision_threshold" in debug["authorization_anomaly"]["fields"]


def test_a_nan_acoustic_score_does_not_block_a_valid_authorization():
    """
    acoustic_score is RECORDED, not decisive. A NaN there must be visible in
    the debug block and must not deny a speaker the embedding matched.
    """
    score, decision, debug = _verifier()._owner_aware_antispoof_fusion_sync(
        owner_match_score=0.92, spoof_prob=0.05, is_owner=True,
        speaker_model=_Model(), embedding_sim=0.92,
        acoustic_score=UNMEASURED, physics_score=UNMEASURED)
    assert decision == "allow"
    assert math.isfinite(score)
    assert debug["acoustic_is_evidence"] is False
    assert debug["physics_is_evidence"] is False
    assert "authorization_anomaly" not in debug


def test_a_genuine_owner_still_authorises():
    """The guards must not have broken the happy path."""
    score, decision, _ = _verifier()._owner_aware_antispoof_fusion_sync(
        owner_match_score=0.90, spoof_prob=0.02, is_owner=True,
        speaker_model=_Model(), embedding_sim=0.90,
        acoustic_score=0.85, physics_score=0.95)
    assert decision == "allow"
    assert score >= 0.4885


def test_an_extreme_spoof_still_denies():
    _, decision, debug = _verifier()._owner_aware_antispoof_fusion_sync(
        owner_match_score=0.95, spoof_prob=0.95, is_owner=True,
        speaker_model=_Model(), embedding_sim=0.95,
        acoustic_score=0.9, physics_score=0.9)
    assert decision == "deny"
    assert debug["rule_applied"] == "extreme_spoof_attack"


# ─────────────────────────────────────────────────────────────────────────
# 4. Persistent state — the EMA that cannot recover
# ─────────────────────────────────────────────────────────────────────────

def test_an_ema_refuses_an_unmeasured_observation():
    """
    ``(1-a)*current + a*NaN`` is NaN, and so is every update after it. One
    unmeasurable sample would corrupt the model permanently.
    """
    assert fold_measurement(120.0, UNMEASURED, 0.1) == 120.0
    assert fold_measurement(120.0, None, 0.1) == 120.0
    assert fold_measurement(120.0, float("inf"), 0.1) == 120.0


def test_an_ema_refuses_an_out_of_band_observation():
    validator = BiologicalBoundsValidator.from_env()
    assert fold_measurement(120.0, 5000.0, 0.1, quantity="pitch_mean_hz",
                            validator=validator) == 120.0


def test_an_ema_folds_a_real_observation_normally():
    assert fold_measurement(100.0, 200.0, 0.1) == pytest.approx(110.0)


def test_an_already_corrupt_running_value_reseeds_from_a_measurement():
    """
    Recovery matters: a model poisoned before this fix existed must be able to
    heal rather than stay NaN forever.
    """
    assert fold_measurement(UNMEASURED, 130.0, 0.1) == 130.0
    assert math.isnan(fold_measurement(UNMEASURED, UNMEASURED, 0.1))


def test_speaker_model_update_never_admits_a_nan_feature_vector():
    """
    A single NaN row makes every entry of the covariance matrix NaN, and that
    matrix feeds Mahalanobis distance for every future verification.
    """
    verifier = _verifier()
    verifier._features_to_vector = lambda f: np.array([1.0, np.nan, 3.0])
    model = _Model()
    original = model.covariance_matrix.copy()

    verifier._update_speaker_model_sync(model, _Features(), confidence=0.9)

    assert model.feature_samples == [], "a NaN sample must not enter the set"
    assert np.array_equal(model.covariance_matrix, original)
    assert math.isfinite(model.pitch_mean)


def test_speaker_model_pitch_survives_an_unmeasured_sample():
    verifier = _verifier()
    verifier._features_to_vector = lambda f: np.array([1.0, 2.0, 3.0])
    model = _Model(pitch_mean=120.0)

    verifier._update_speaker_model_sync(
        model, _Features(pitch_mean=UNMEASURED), confidence=0.9)

    assert model.pitch_mean == 120.0, "the running value must be untouched"
    assert math.isfinite(model.pitch_std)


# ─────────────────────────────────────────────────────────────────────────
# 5. Perturbation tolerance — calibration, not constants
# ─────────────────────────────────────────────────────────────────────────

def test_a_clean_capture_is_still_held_to_the_clinical_figure():
    caps = PerturbationTolerance().caps_for(35.0)
    assert caps.jitter == pytest.approx(0.02)
    assert caps.shimmer == pytest.approx(0.10)


def test_a_noisy_capture_gets_a_proportionally_wider_limit():
    tolerance = PerturbationTolerance()
    clean = tolerance.caps_for(30.0)
    noisy = tolerance.caps_for(12.0)
    assert noisy.jitter > clean.jitter
    assert noisy.scale > 1.0


def test_the_widening_is_bounded():
    """A limit wide enough to pass anything is a check-shaped hole."""
    tolerance = PerturbationTolerance()
    caps = tolerance.caps_for(-50.0)
    assert caps.scale <= tolerance.max_scale


def test_unknown_snr_is_not_treated_as_too_noisy_to_inform():
    """
    "We measured 2 dB" and "we do not know" are different facts. Unknown SNR is
    already compensated by the widened caps; abstaining as well would silently
    disable jitter and shimmer for every caller that passes no audio_quality.
    """
    tolerance = PerturbationTolerance()
    assert tolerance.informative(None) is True
    assert tolerance.informative(2.0) is False
    assert tolerance.informative(20.0) is True


def test_the_operators_own_measured_perturbation_no_longer_tanks_the_score():
    """
    The verified 2026-08-07 sample measured jitter 0.07 / shimmer 0.30 on a
    laptop array and scored 0.27 / 0.33 against clinical caps.
    """
    verifier = _verifier()
    assessment = verifier._check_physics_plausibility_sync(
        _Features(jitter=0.07, shimmer=0.30),
        context={"audio_quality": {"snr_db": 12.0}})
    assert assessment.components["jitter"] > 0.5, assessment.describe()
    assert assessment.components["shimmer"] > 0.5, assessment.describe()


def test_a_genuinely_impossible_perturbation_still_fails_at_any_snr():
    """Scaling the caps must not become "no caps"."""
    verifier = _verifier()
    assessment = verifier._check_physics_plausibility_sync(
        _Features(jitter=0.45, shimmer=0.48),
        context={"audio_quality": {"snr_db": 8.0}})
    assert assessment.components["jitter"] < 0.5, assessment.describe()


def test_sub_floor_snr_abstains_rather_than_scoring_noise():
    verifier = _verifier()
    assessment = verifier._check_physics_plausibility_sync(
        _Features(), context={"audio_quality": {"snr_db": 1.0}})
    assert "jitter" not in assessment.components
    assert any("jitter" in a for a in assessment.abstained)


def test_perturbation_bands_are_env_overridable():
    tolerance = PerturbationTolerance.from_env(
        {"JARVIS_VOICE_PERTURBATION_BASE_JITTER": "0.05"})
    assert tolerance.caps_for(35.0).jitter == pytest.approx(0.05)


def test_a_malformed_perturbation_override_keeps_the_default():
    tolerance = PerturbationTolerance.from_env(
        {"JARVIS_VOICE_PERTURBATION_BASE_JITTER": "wide-open"})
    assert tolerance.caps_for(35.0).jitter == pytest.approx(0.02)

"""
The extractor may not write what no vocal tract can produce.

PR #70426 taught the anti-spoofing stage to abstain on unmeasurable inputs. That
stopped the system reasoning from garbage; it did not stop the garbage being
written, and it left the physics plausibility scorer — the twin stage, in the
same file — carrying the identical defect.

The values below are the ones read out of ``speaker_profiles`` on this machine
on 2026-08-06, kept verbatim so a regression is recognisable as the original
bug rather than as an abstract bounds failure::

    speaking_rate_wpm = 420.0     # 40-300 wpm
    formant_f1_hz     = 42.9      # 150-1200 Hz
    formant_f2_hz     = 80.03     # 500-3500 Hz
    formant_f3_hz     = 102.76    # 1200-4500 Hz
    formant_f4_hz     = 107.35    # 2000-5500 Hz

They came from two extractors that claimed LPC and performed spectral
peak-picking, returning the LOWEST peaks in the spectrum — DC offset, mains hum
and desk rumble — and a hardcoded ``(500, 1500)`` textbook male average when
even that failed.

These tests pin four rules:

  1. an extractor reports UNMEASURED, never a default, when it cannot measure;
  2. a value outside the human band never reaches storage or a verdict;
  3. a scorer excludes an unmeasurable component rather than scoring it low;
  4. none of the above weakens a check against a REAL but implausible voice.

Rule 4 is the one that makes the rest safe. Without it this change would be
indistinguishable from disabling the biometric.
"""

from __future__ import annotations

import math
import sqlite3

import numpy as np
import pytest

from backend.voice.advanced_biometric_verification import (
    AdvancedBiometricVerifier,
    PhysicsConstraints,
    PlausibilityAssessment,
)
from backend.voice.biological_bounds import (
    BOUNDS,
    UNMEASURED,
    BiologicalBoundsValidator,
    MeasuredAggregator,
    canonical_name,
    is_absent,
    is_measured,
)
from backend.voice.formant_estimation import estimate_formants

# Read from the live profile 2026-08-06, kept verbatim.
ENROLLED_RATE_WPM = 420.0
ENROLLED_F1_HZ = 42.9099998474121
ENROLLED_F2_HZ = 80.0299987792969
ENROLLED_F3_HZ = 102.76000213623047
ENROLLED_F4_HZ = 107.3499984741211
ENROLLED_PITCH_HZ = 246.85124206543  # plausible — must SURVIVE
ENROLLED_CENTROID_HZ = 1676.98645019531  # plausible — must SURVIVE


class _Features:
    """Only the attributes the scored stages read."""

    def __init__(self, **kw):
        self.pitch_mean = kw.get("pitch_mean", 120.0)
        self.pitch_std = kw.get("pitch_std", 30.0)
        self.formant_f1 = kw.get("formant_f1", 520.0)
        self.formant_f2 = kw.get("formant_f2", 1480.0)
        self.harmonic_to_noise_ratio = kw.get("hnr", 18.0)
        self.jitter = kw.get("jitter", 0.01)
        self.shimmer = kw.get("shimmer", 0.04)


def _verifier() -> AdvancedBiometricVerifier:
    v = AdvancedBiometricVerifier.__new__(AdvancedBiometricVerifier)
    v.physics = PhysicsConstraints()
    return v


def _synthesize_vowel(f0: float, formants, duration=1.0, sr=16000, bandwidth=80.0):
    """A source-filter vowel with KNOWN formants, to test against truth."""
    from scipy.signal import lfilter

    n = int(duration * sr)
    excitation = np.zeros(n)
    excitation[:: max(1, int(sr / f0))] = 1.0
    signal = excitation
    for freq in formants:
        r = math.exp(-math.pi * bandwidth / sr)
        theta = 2 * math.pi * freq / sr
        signal = lfilter([1.0], [1.0, -2 * r * math.cos(theta), r * r], signal)
    return signal / (np.max(np.abs(signal)) + 1e-9)


# ─────────────────────────────────────────────────────────────────────────
# 1. The shared predicate
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,label", [
    (None, "None"),
    (float("nan"), "NaN"),
    (float("inf"), "infinity"),
    (float("-inf"), "negative infinity"),
    ("500", "a numeric string"),
    (True, "bool True"),
    (False, "bool False"),
    (object(), "an arbitrary object"),
])
def test_absence_forms_are_never_measurements(value, label):
    assert is_absent(value) is True, label
    assert is_measured(value, 0.0, 10_000.0) is False, label


def test_bool_is_excluded_because_float_true_is_one():
    """``float(True)`` is 1.0 — a silent 1 Hz if bool were accepted as numeric."""
    assert is_measured(True, 0.5, 200.0) is False
    assert is_measured(1.0, 0.5, 200.0) is True


def test_physics_constraints_measured_delegates_to_the_shared_predicate():
    """
    One predicate, not two.

    The write side and the read side disagreeing about what counts as a
    measurement is the condition that let 420 wpm be stored and then be
    reasoned from. They must call the same function.
    """
    physics = PhysicsConstraints()
    for value in (None, float("nan"), "500", True, 42.9, 500.0):
        assert physics.measured(value, 150.0, 1200.0) == is_measured(value, 150.0, 1200.0)


def test_bands_have_exactly_one_env_knob_shared_with_physics_constraints():
    """
    ``PhysicsConstraints`` sources its band defaults from the registry, so a
    bound cannot be changed in one place and stay stale in the other.
    """
    physics = PhysicsConstraints()
    assert physics.min_formant_f1_hz == BOUNDS["formant_f1_hz"].low
    assert physics.max_formant_f1_hz == BOUNDS["formant_f1_hz"].high
    assert physics.min_speaking_rate_wpm == BOUNDS["speaking_rate_wpm"].low
    assert physics.max_speaking_rate_wpm == BOUNDS["speaking_rate_wpm"].high
    assert BOUNDS["formant_f1_hz"].min_env == "JARVIS_VOICE_PHYSICS_MIN_FORMANT_F1_HZ"


def test_hnr_measurability_floor_is_not_the_plausibility_floor():
    """
    A 3 dB capture is noisy evidence, not absent evidence.

    Using ``min_hnr_db`` (the plausibility threshold) as the measurability gate
    would make every genuinely noisy recording abstain instead of scoring.
    """
    physics = PhysicsConstraints()
    assert physics.min_hnr_db_measurable < physics.min_hnr_db
    assert physics.measured(3.0, physics.min_hnr_db_measurable, physics.max_hnr_db)


# ─────────────────────────────────────────────────────────────────────────
# 2. The validator
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("quantity,value", [
    ("formant_f1_hz", ENROLLED_F1_HZ),
    ("formant_f2_hz", ENROLLED_F2_HZ),
    ("formant_f3_hz", ENROLLED_F3_HZ),
    ("formant_f4_hz", ENROLLED_F4_HZ),
    ("speaking_rate_wpm", ENROLLED_RATE_WPM),
])
def test_the_live_poisoned_values_are_all_rejected(quantity, value):
    validator = BiologicalBoundsValidator.from_env()
    assert validator.validate(quantity, value) is None
    assert math.isnan(validator.coerce(quantity, value))
    violation = validator.check(quantity, value)
    assert violation is not None
    assert quantity in violation.describe()


@pytest.mark.parametrize("quantity,value", [
    ("pitch_mean_hz", ENROLLED_PITCH_HZ),
    ("spectral_centroid_hz", ENROLLED_CENTROID_HZ),
    ("formant_f1_hz", 520.0),
    ("speaking_rate_wpm", 150.0),
])
def test_plausible_values_survive_untouched(quantity, value):
    """The gate must not be a filter that empties the profile."""
    validator = BiologicalBoundsValidator.from_env()
    assert validator.validate(quantity, value) == value
    assert validator.check(quantity, value) is None


def test_absence_is_never_zero():
    """
    ``coerce`` yields NaN, not 0.0.

    An unset 0.0 formant gives ratio 0.0, trips ``< 0.1`` and lands a confident
    0.5 spoofing penalty. That substitution IS the original defect.
    """
    validator = BiologicalBoundsValidator.from_env()
    coerced = validator.coerce("formant_f1_hz", ENROLLED_F1_HZ)
    assert math.isnan(coerced)
    assert coerced != 0.0


def test_unbounded_quantities_pass_through_rather_than_inventing_a_limit():
    """A quantity with no physiological range gets a finiteness check only."""
    validator = BiologicalBoundsValidator.from_env()
    assert validator.bound("energy_mean") is None
    assert validator.validate("energy_mean", 12345.0) == 12345.0
    assert validator.validate("energy_mean", float("nan")) is None


def test_percent_columns_are_not_aliased_to_the_fraction_band():
    """
    ``jitter_percent`` holds percent from one writer and a fraction from
    another. Aliasing it to the fraction band flagged 1.0 (a textbook healthy 1%
    jitter) as biologically impossible, which would have deleted correct data.
    """
    validator = BiologicalBoundsValidator.from_env()
    assert canonical_name("jitter_percent") == "jitter_percent"
    assert validator.validate("jitter_percent", 1.0) == 1.0      # 1% — real
    assert validator.validate("jitter_percent", 0.01) == 0.01    # fraction — real
    assert validator.validate("jitter_percent", 500.0) is None   # impossible either way


def test_sanitize_nulls_the_impossible_and_keeps_the_key():
    validator = BiologicalBoundsValidator.from_env()
    result = validator.sanitize({
        "formant_f1_hz": ENROLLED_F1_HZ,
        "pitch_mean_hz": ENROLLED_PITCH_HZ,
        "speaker_name": "Derek J. Russell",
    })
    assert result.clean["formant_f1_hz"] is None      # NULL, not deleted
    assert "formant_f1_hz" in result.clean            # key survives for SQL
    assert result.clean["pitch_mean_hz"] == ENROLLED_PITCH_HZ
    assert result.names() == ["formant_f1_hz"]


def test_inverted_env_override_is_refused_rather_than_enforced():
    """A band that rejects everything would read as "every extractor is broken"."""
    validator = BiologicalBoundsValidator.from_env({
        "JARVIS_VOICE_PHYSICS_MIN_FORMANT_F1_HZ": "2000",
        "JARVIS_VOICE_PHYSICS_MAX_FORMANT_F1_HZ": "100",
    })
    assert validator.validate("formant_f1_hz", 520.0) == 520.0


def test_malformed_env_override_keeps_the_default():
    validator = BiologicalBoundsValidator.from_env(
        {"JARVIS_VOICE_PHYSICS_MIN_FORMANT_F1_HZ": "not-a-number"})
    assert validator.bound("formant_f1_hz").low == BOUNDS["formant_f1_hz"].low


# ─────────────────────────────────────────────────────────────────────────
# 2b. The aggregator — enrolment reduces N samples into the stored profile
# ─────────────────────────────────────────────────────────────────────────

def test_one_unmeasurable_sample_does_not_discard_the_others():
    """
    A bare ``np.mean`` over a list holding one NaN returns NaN for the whole
    column, so honest per-sample absence would become total enrolment failure.
    """
    aggregator = MeasuredAggregator(BiologicalBoundsValidator.from_env())
    assert aggregator.mean("formant_f1_hz", [500.0, UNMEASURED, 540.0]) == pytest.approx(520.0)


def test_out_of_band_samples_are_dropped_not_averaged_in():
    """
    Averaging 42.9 Hz in at full weight drags the enrolled value toward a
    number no vocal tract produces.
    """
    aggregator = MeasuredAggregator(BiologicalBoundsValidator.from_env())
    assert aggregator.mean("formant_f1_hz", [ENROLLED_F1_HZ, 500.0, 540.0]) == pytest.approx(520.0)


def test_nothing_measurable_yields_unmeasured_not_zero():
    aggregator = MeasuredAggregator(BiologicalBoundsValidator.from_env())
    assert math.isnan(aggregator.mean("formant_f1_hz", [ENROLLED_F1_HZ, UNMEASURED]))
    assert math.isnan(aggregator.mean("formant_f1_hz", []))


def test_a_single_sample_has_no_standard_deviation():
    """
    One sample gives a spread of 0.0, which reads as "perfectly consistent"
    when it means "there was nothing to compare".
    """
    aggregator = MeasuredAggregator(BiologicalBoundsValidator.from_env())
    assert math.isnan(aggregator.std("formant_f1_hz", [520.0]))
    assert aggregator.std("formant_f1_hz", [500.0, 540.0]) == pytest.approx(20.0)


def test_dynamic_range_refuses_a_zero_floor_instead_of_epsilon_padding():
    """
    ``20*log10(max/(min + 1e-10))`` turned silence in one sample into a ~200 dB
    range reported as this speaker's dynamics.
    """
    aggregator = MeasuredAggregator(BiologicalBoundsValidator.from_env())
    assert math.isnan(aggregator.dynamic_range_db([0.0, 0.5]))
    assert aggregator.dynamic_range_db([0.05, 0.5]) == pytest.approx(20.0, abs=0.1)


# ─────────────────────────────────────────────────────────────────────────
# 3. The formant estimator
# ─────────────────────────────────────────────────────────────────────────

def test_estimator_recovers_known_formants_from_a_synthetic_vowel():
    """
    The check neither previous implementation had.

    Both claimed LPC, and neither was ever run against a signal with a known
    answer — which is how peak-picking survived long enough to write 42.9 Hz.
    """
    truth = [730.0, 1090.0, 2440.0, 3350.0]
    estimated = estimate_formants(_synthesize_vowel(120.0, truth), 16000)
    assert abs(estimated[0] - truth[0]) < 60.0, f"F1 {estimated[0]}"
    assert abs(estimated[1] - truth[1]) < 60.0, f"F2 {estimated[1]}"


@pytest.mark.parametrize("signal,label", [
    (np.zeros(16000), "silence"),
    (np.random.RandomState(1).normal(0, 1, 16000), "white noise"),
    (0.5 + 0.2 * np.sin(2 * np.pi * 60 * np.arange(16000) / 16000), "DC + 60 Hz hum"),
    (np.zeros(10), "too short to frame"),
    (np.full(16000, np.nan), "all NaN"),
])
def test_estimator_reports_unmeasured_rather_than_a_default(signal, label):
    """
    The DC + hum case is the exact signal class that produced 42.9 / 80.0 Hz.
    The old code returned those as formants; this must return nothing at all.
    """
    for value in estimate_formants(signal, 16000):
        assert math.isnan(value), f"{label} produced {value}"


def test_estimator_never_returns_the_textbook_male_average():
    """``(500, 1500)`` was the old failure value. It must not appear from silence."""
    estimated = estimate_formants(np.zeros(16000), 16000)
    assert not any(v in (500.0, 1500.0, 2500.0, 3500.0) for v in estimated)


def test_estimator_always_returns_the_requested_arity():
    """Callers index F1..F4 positionally; a short list would IndexError."""
    assert len(estimate_formants(np.zeros(100), 16000, n_formants=4)) == 4
    assert len(estimate_formants(np.zeros(100), 16000, n_formants=2)) == 2


# ─────────────────────────────────────────────────────────────────────────
# 4. The plausibility scorer — the twin of the anti-spoofing stage
# ─────────────────────────────────────────────────────────────────────────

def test_unmeasured_components_abstain_instead_of_scoring_zero():
    """
    The live 2026-08-06 shape: formants unset.

    Before, ``0.0 / max(0.0, 1.0)`` fell outside the ratio band and scored that
    component 0.5, and an unset HNR scored 0.0 — two of five components pinned
    low by quantities nobody measured, then averaged.
    """
    assessment = _verifier()._check_physics_plausibility_sync(
        _Features(formant_f1=0.0, formant_f2=0.0))
    assert "formants" not in assessment.components
    assert any("formants" in a for a in assessment.abstained)
    assert assessment.evidence_complete is False


def test_no_measurable_component_scores_neutral_not_zero():
    """
    With no evidence the stage must veto nothing.

    Scoring 0.0 would convert a broken feature extractor into a rejection of the
    speaker — the precise defect this change removes.
    """
    assessment = _verifier()._check_physics_plausibility_sync(_Features(
        pitch_mean=float("nan"), formant_f1=float("nan"), formant_f2=float("nan"),
        hnr=float("nan"), jitter=float("nan"), shimmer=float("nan")))
    assert assessment.components == {}
    assert assessment.score == 1.0
    assert assessment.evidence_complete is False


def test_a_fully_measured_real_voice_scores_one():
    assessment = _verifier()._check_physics_plausibility_sync(_Features())
    assert assessment.score == pytest.approx(1.0)
    assert assessment.evidence_complete is True


def test_a_measurable_but_implausible_voice_still_scores_low():
    """
    THE GUARD ON THE WHOLE CHANGE.

    Without this, abstention is indistinguishable from switching the check off.
    Every value here is inside its measurability band and outside its
    plausibility band — real measurements of an impossible voice.
    """
    assessment = _verifier()._check_physics_plausibility_sync(_Features(
        formant_f1=1100.0, formant_f2=1200.0,  # ratio 0.92, outside [0.2, 0.8]
        hnr=2.0,                                # below the plausibility floor
        jitter=0.30, shimmer=0.40))             # far above the perturbation caps
    assert assessment.evidence_complete is True, "these are measurements, not absences"
    assert assessment.score < 0.7, assessment.describe()


def test_abstention_is_recorded_by_name_never_silently():
    """A clean score obtained by not looking is not a clean score."""
    assessment = _verifier()._check_physics_plausibility_sync(
        _Features(hnr=float("nan")))
    assert any("hnr" in a for a in assessment.abstained)
    assert "abstained=[hnr" in assessment.describe()


def test_a_failed_stage_yields_an_abstaining_assessment_not_a_near_pass():
    """The old code substituted a bare 0.8 for a raised exception."""
    assessment = PlausibilityAssessment(abstained=["stage_failed(RuntimeError)"]).finalize()
    assert assessment.score == 1.0
    assert assessment.evidence_complete is False


# ─────────────────────────────────────────────────────────────────────────
# 5. The boot sanitiser
# ─────────────────────────────────────────────────────────────────────────

def _poisoned_db(path):
    connection = sqlite3.connect(str(path))
    connection.execute("""
        CREATE TABLE speaker_profiles (
            speaker_id INTEGER PRIMARY KEY,
            speaker_name TEXT,
            voiceprint_embedding BLOB,
            pitch_mean_hz REAL,
            formant_f1_hz REAL,
            formant_f2_hz REAL,
            speaking_rate_wpm REAL,
            spectral_centroid_hz REAL,
            jitter_percent REAL
        )""")
    connection.execute(
        "INSERT INTO speaker_profiles VALUES (1, 'Derek J. Russell', ?, ?, ?, ?, ?, ?, ?)",
        (np.zeros(192, dtype=np.float32).tobytes(), ENROLLED_PITCH_HZ,
         ENROLLED_F1_HZ, ENROLLED_F2_HZ, ENROLLED_RATE_WPM, ENROLLED_CENTROID_HZ, 1.0),
    )
    connection.commit()
    connection.close()
    return path


@pytest.mark.asyncio
async def test_sanitizer_nulls_impossible_values_and_keeps_the_voiceprint(tmp_path):
    """
    Quarantine, not annihilation.

    The embedding is the only thing that can authorise an unlock and it is not
    corrupt. Deleting the row to remove three bad scalars would leave the
    operator unable to unlock by voice at all.
    """
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    db = _poisoned_db(tmp_path / "profiles.db")
    report = await sanitize_voice_profiles(db)

    assert report.changed is True
    assert report.rows_purged == 0
    assert "Derek J. Russell" in report.flagged_for_reenrollment

    connection = sqlite3.connect(str(db))
    connection.row_factory = sqlite3.Row
    row = dict(connection.execute("SELECT * FROM speaker_profiles").fetchone())
    assert row["formant_f1_hz"] is None
    assert row["formant_f2_hz"] is None
    assert row["speaking_rate_wpm"] is None
    assert row["pitch_mean_hz"] == pytest.approx(ENROLLED_PITCH_HZ)
    assert row["spectral_centroid_hz"] == pytest.approx(ENROLLED_CENTROID_HZ)
    assert row["jitter_percent"] == 1.0, "a real 1% jitter must survive"
    assert row["voiceprint_embedding"] is not None, "the authenticator must survive"
    connection.close()


@pytest.mark.asyncio
async def test_sanitizer_archives_before_it_changes_anything(tmp_path):
    """A sanitiser nobody can second-guess is a sanitiser nobody can audit."""
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    db = _poisoned_db(tmp_path / "profiles.db")
    await sanitize_voice_profiles(db)

    connection = sqlite3.connect(str(db))
    archived = connection.execute(
        "SELECT original_row FROM speaker_profiles_quarantine").fetchone()[0]
    assert "42.9" in archived and "420.0" in archived
    connection.close()


@pytest.mark.asyncio
async def test_sanitizer_is_idempotent(tmp_path):
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    db = _poisoned_db(tmp_path / "profiles.db")
    await sanitize_voice_profiles(db)
    second = await sanitize_voice_profiles(db)
    assert second.changed is False
    assert second.values_nulled == 0


@pytest.mark.asyncio
async def test_sanitizer_leaves_a_clean_profile_completely_alone(tmp_path):
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    db = tmp_path / "clean.db"
    connection = sqlite3.connect(str(db))
    connection.execute("""
        CREATE TABLE speaker_profiles (
            speaker_id INTEGER PRIMARY KEY, speaker_name TEXT,
            voiceprint_embedding BLOB, formant_f1_hz REAL, speaking_rate_wpm REAL)""")
    connection.execute("INSERT INTO speaker_profiles VALUES (1, 'Real', ?, 520.0, 150.0)",
                       (np.zeros(192, dtype=np.float32).tobytes(),))
    connection.commit()
    connection.close()

    report = await sanitize_voice_profiles(db)
    assert report.changed is False
    assert report.flagged_for_reenrollment == []


@pytest.mark.asyncio
async def test_a_missing_database_is_skipped_not_fatal(tmp_path):
    """Hygiene may never take the boot down."""
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    report = await sanitize_voice_profiles(tmp_path / "does-not-exist.db")
    assert report.skipped_reason is not None
    assert report.changed is False


@pytest.mark.asyncio
async def test_a_schema_without_acoustic_columns_is_skipped(tmp_path):
    """An older table costs that column's check, never the sweep."""
    from backend.voice.voice_profile_sanitizer import sanitize_voice_profiles

    db = tmp_path / "old.db"
    connection = sqlite3.connect(str(db))
    connection.execute(
        "CREATE TABLE speaker_profiles (speaker_id INTEGER PRIMARY KEY, speaker_name TEXT)")
    connection.commit()
    connection.close()

    report = await sanitize_voice_profiles(db)
    assert report.skipped_reason == "no acoustic columns in this schema"

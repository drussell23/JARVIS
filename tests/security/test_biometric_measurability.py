"""
An unmeasurable feature is not evidence of an attack.

On 2026-08-06 the operator's own voice was refused by his own profile::

    ⚠️  Spoofing indicators detected:
        [('unnatural_formants', 0.5), ('inconsistent_rate', 0.2)]
    [CapabilityRouter] 'unlock_screen' NOT authorised by VoiceConsentProvider
        (That didn't sound like you, so I've left it locked.)

Neither indicator was a measurement. From ``speaker_profiles`` on that machine:

    speaking_rate_wpm = 420.0      # no human sustains 420 wpm; speech is 110-180
    formant_f1_hz     = 42.9       # F1 is ~500 Hz
    formant_f2_hz     = 80.03      # F2 is ~1500 Hz

``unnatural_formants`` fires when ``f1/max(f2,1)`` leaves [0.1, 0.9] — and an
unset 0.0 formant gives 0.0, which trips it. ``inconsistent_rate`` differences
the test rate against an enrolled 420, which no real speaker can come within
100 wpm of. Both produced confident findings about a voice nothing had
successfully measured, 0.7 of penalty, a spoofing score of 0.3, and a rejection.

This is the same defect the rest of this arc kept producing at other layers: a
fault presented as a verdict. These tests pin the rule that an indicator may
only fire on an input that was actually measured.
"""

from __future__ import annotations

import pytest

from backend.voice.advanced_biometric_verification import (
    AdvancedBiometricVerifier,
    PhysicsConstraints,
    SpoofingAssessment,
)

# The values read out of the live profile, kept verbatim.
ENROLLED_RATE_WPM = 420.0
ENROLLED_F1_HZ = 42.9
ENROLLED_F2_HZ = 80.03
REAL_RATE_WPM = 150.0


class _Features:
    """Only the attributes the detector reads."""

    def __init__(self, **kw):
        self.pitch_std = kw.get("pitch_std", 32.56)
        self.formant_f1 = kw.get("formant_f1", 500.0)
        self.formant_f2 = kw.get("formant_f2", 1500.0)
        self.harmonic_to_noise_ratio = kw.get("hnr", 15.0)
        self.speaking_rate = kw.get("speaking_rate", REAL_RATE_WPM)


def _detector(**env) -> AdvancedBiometricVerifier:
    """A verifier with only the anti-spoofing surface wired."""
    v = AdvancedBiometricVerifier.__new__(AdvancedBiometricVerifier)
    v.physics = PhysicsConstraints.from_env(env or None)
    v._replay_max_snr_db = 50.0
    v._replay_min_noise = 0.001
    v._replay_max_quality = 0.95
    v._synthesis_min_pitch_std = 5.0
    v._synthesis_max_hnr_db = 40.0
    v._conversion_max_rate_diff = 100.0
    return v


# ── The measured rejection ───────────────────────────────────────────────────
def test_the_operators_own_profile_no_longer_reads_as_synthetic():
    """
    The regression, with the exact numbers that caused it.

    Before: score 0.3 from two fabricated indicators. After: both abstain,
    nothing fires, and the abstention is recorded by name.
    """
    result = _detector()._detect_spoofing_sync(
        _Features(formant_f1=ENROLLED_F1_HZ, formant_f2=ENROLLED_F2_HZ),
        _Features(speaking_rate=ENROLLED_RATE_WPM),
        None,
    )

    fired = {name for name, _ in result.indicators}
    assert "unnatural_formants" not in fired
    assert "inconsistent_rate" not in fired
    assert result.score == 1.0, f"penalised on unmeasurable inputs: {result.describe()}"
    assert len(result.abstained) == 2, result.describe()
    assert not result.evidence_complete


def test_an_abstention_is_never_silent():
    """
    A check that did not run is a hole in the evidence.

    A detector that quietly skips work is as dishonest as one that invents
    findings — the operator gets the same clean score either way and no means
    of telling them apart.
    """
    result = _detector()._detect_spoofing_sync(
        _Features(formant_f1=0.0, formant_f2=0.0), _Features(), None
    )
    assert result.abstained
    assert "unnatural_formants" in result.describe()
    assert "abstained" in result.describe()


@pytest.mark.parametrize(
    "value,label",
    [(0.0, "unset default"), (42.9, "off by an order of magnitude"),
     (float("nan"), "NaN"), (float("inf"), "infinite"), (None, "absent"),
     ("500", "string"), (True, "bool")],
)
def test_unmeasurable_inputs_never_count_as_measured(value, label):
    """
    ``True`` is in this list deliberately: ``bool`` is a subclass of ``int``,
    so a naive numeric check accepts it and ``float(True) == 1.0`` silently
    becomes 1 Hz.
    """
    p = PhysicsConstraints.from_env({})
    assert p.measured(value, p.min_formant_f1_hz, p.max_formant_f1_hz) is False, label


# ── The gate must not blind the detector ─────────────────────────────────────
def test_a_real_spoofing_signal_still_fires():
    """
    The control. Abstention must be narrow — when inputs ARE measurable and
    genuinely suspicious, the indicator must still fire, or this fix has
    disabled anti-spoofing instead of correcting it.
    """
    result = _detector()._detect_spoofing_sync(
        # Measurable formants, but a physically implausible F1/F2 relationship.
        _Features(formant_f1=1100.0, formant_f2=1200.0),
        _Features(),
        None,
    )
    assert ("unnatural_formants", 0.5) in result.indicators
    assert result.score < 1.0


def test_a_real_rate_mismatch_still_fires_when_both_sides_are_real():
    """A genuine voice-conversion signal survives the gate."""
    result = _detector()._detect_spoofing_sync(
        _Features(speaking_rate=60.0), _Features(speaking_rate=280.0), None
    )
    assert ("inconsistent_rate", 0.2) in result.indicators


def test_low_pitch_variation_still_fires():
    """Synthesised speech with a flat pitch is measurable and must be caught."""
    result = _detector()._detect_spoofing_sync(
        _Features(pitch_std=1.0), _Features(), None
    )
    assert ("low_pitch_variation", 0.4) in result.indicators


def test_penalties_still_accumulate():
    """Several real findings must still compound into a low score."""
    result = _detector()._detect_spoofing_sync(
        _Features(pitch_std=1.0, formant_f1=1100.0, formant_f2=1200.0),
        _Features(),
        None,
    )
    assert result.score == pytest.approx(1.0 - 0.4 - 0.5)
    assert result.evidence_complete, "nothing should have abstained here"


# ── Quality shapes seen in the field ─────────────────────────────────────────
def test_string_quality_abstains_rather_than_being_ignored():
    """
    The field showed ``Invalid quality score type: <class 'str'>``.

    A string is not a number, so it is not evidence either way — but the
    replay check silently not running is exactly what must be visible.
    """
    result = _detector()._detect_spoofing_sync(
        _Features(), _Features(), {"audio_quality": "excellent"}
    )
    assert any("perfect_quality" in a for a in result.abstained)


def test_numeric_quality_still_trips_replay_detection():
    result = _detector()._detect_spoofing_sync(
        _Features(), _Features(), {"audio_quality": 0.99}
    )
    assert ("perfect_quality", 0.2) in result.indicators


def test_dict_quality_with_missing_members_abstains_per_member():
    """Two independent checks; one absent member may not suppress the other."""
    result = _detector()._detect_spoofing_sync(
        _Features(), _Features(), {"audio_quality": {"snr_db": 60}}
    )
    assert ("perfect_quality", 0.3) in result.indicators
    assert any("no_background" in a for a in result.abstained)


# ── Configuration ────────────────────────────────────────────────────────────
def test_bands_are_environment_tunable():
    """A band proving too tight in the field must be widenable without a patch."""
    p = PhysicsConstraints.from_env({"JARVIS_VOICE_PHYSICS_MAX_SPEAKING_RATE_WPM": "500"})
    assert p.max_speaking_rate_wpm == 500.0
    assert p.measured(ENROLLED_RATE_WPM, p.min_speaking_rate_wpm, p.max_speaking_rate_wpm)


@pytest.mark.parametrize("bad", ["", "abc", "not-a-number", "1e"])
def test_malformed_override_keeps_the_default(bad):
    """A garbage bound must never reach a security decision."""
    default = PhysicsConstraints()
    p = PhysicsConstraints.from_env({"JARVIS_VOICE_PHYSICS_MAX_SPEAKING_RATE_WPM": bad})
    assert p.max_speaking_rate_wpm == default.max_speaking_rate_wpm


def test_assessment_defaults_are_innocent():
    """Anti-spoofing disabled must contribute no penalty and claim no evidence."""
    a = SpoofingAssessment()
    assert a.score == 1.0
    assert a.evidence_complete

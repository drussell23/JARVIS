"""
A fault on the consent path must not be reported as a refusal.

Measured 2026-08-06 13:12:23, on a real unlock attempt::

    EcapaFacade error: registry required for first facade creation
        - falling back to local engine
    No speaker match found. Primary user: None, Best confidence: 0.00%
    'unlock_screen' NOT authorised (That didn't sound like you, so I've left it locked.)

``Best confidence: 0.00%`` is not a low score -- it is the absence of a score.
Nothing was compared, because the fallback engine held no voiceprints. The
operator was told his voice did not match, when his voice was never checked.

The two outcomes demand opposite responses: speak again, or repair enrollment.
These tests pin the distinction so it cannot collapse back into one value, which
is what `voice_identity.py` already warns about seventeen lines above the site
where it happened again.
"""

from __future__ import annotations

import pytest

from backend.hud.voice_identity import Verdict, VoiceIdentity


class _Service:
    """Stands in for SpeakerVerificationService's capability probe."""

    def __init__(self, capable: bool, reason: str):
        self._capable = capable
        self._reason = reason

    def verification_capability(self, speaker_name=None):
        return (self._capable, self._reason)


def test_no_loaded_voiceprint_is_not_enrolled_not_rejected():
    """
    The exact 2026-08-06 failure.

    The engine held no profiles, so no comparison occurred. Saying "that didn't
    sound like you" sends the operator to speak more clearly -- the one action
    that cannot possibly help.
    """
    identity = VoiceIdentity(service=_Service(False, "no_profiles_loaded"))

    capable, reason = identity._comparison_capability("Derek J. Russell")

    assert capable is False
    assert reason == "no_profiles_loaded"


def test_a_real_comparison_still_reports_rejection():
    """
    The fix must not turn every refusal into a fault.

    If a voiceprint IS loaded and the audio does not match, that is a genuine
    REJECTED -- and reporting it as "I can't check who's speaking" would be the
    same dishonesty pointing the other way.
    """
    identity = VoiceIdentity(service=_Service(True, "ready"))

    capable, reason = identity._comparison_capability("Derek J. Russell")

    assert capable is True
    assert reason == "ready"


def test_probe_absent_preserves_previous_behaviour():
    """
    A service too old to answer must not acquire a new excuse.

    Failing toward "a comparison happened" is deliberate: claiming a fault we
    cannot evidence would let a genuine mismatch masquerade as a broken
    verifier.
    """
    class _Old:
        pass

    identity = VoiceIdentity(service=_Old())
    capable, reason = identity._comparison_capability("Derek J. Russell")

    assert capable is True
    assert reason == "capability_probe_unavailable"


def test_probe_that_throws_does_not_decide():
    """A probe must never determine consent by raising."""

    class _Exploding:
        def verification_capability(self, speaker_name=None):
            raise RuntimeError("boom")

    identity = VoiceIdentity(service=_Exploding())
    capable, reason = identity._comparison_capability("Derek J. Russell")

    assert capable is True
    assert reason.startswith("capability_probe_failed:RuntimeError")


@pytest.mark.parametrize(
    "verdict",
    [Verdict.NOT_ENROLLED, Verdict.UNAVAILABLE, Verdict.REJECTED,
     Verdict.NOT_READY, Verdict.NO_AUDIO],
)
def test_every_negative_verdict_still_fails_closed(verdict):
    """
    Honesty must not have cost strictness.

    Distinguishing a fault from a refusal changes only what the operator is
    TOLD. Exactly one verdict approves; an unverifiable voice is not consent.
    """
    assert verdict.approves is False
    assert Verdict.VERIFIED.approves is True


def test_each_negative_verdict_says_something_different():
    """
    Distinct verdicts with identical wording would be the collapse in prose
    rather than in code -- the operator would still not know which happened.
    """
    from backend.hud.voice_identity import _SPOKEN

    spoken = [_SPOKEN[v.value] for v in
              (Verdict.NOT_ENROLLED, Verdict.NOT_READY,
               Verdict.NO_AUDIO, Verdict.REJECTED, Verdict.UNAVAILABLE)]

    assert len(set(spoken)) == len(spoken), "two verdicts share a sentence"
    # The one that misfired on 2026-08-06 must remain specifically about
    # sounding wrong, so that hearing it is evidence a comparison happened.
    assert "sound like you" in _SPOKEN[Verdict.REJECTED.value]
    assert "sound like you" not in _SPOKEN[Verdict.NOT_ENROLLED.value]
    assert "sound like you" not in _SPOKEN[Verdict.UNAVAILABLE.value]

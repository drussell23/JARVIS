"""The audio path, and the authority that works through a locked screen.

Touch ID cannot authorise `unlock_screen` — there is no surface to draw a
dialog on while the screen is locked. A microphone works through one. These
pin the wire that carries the evidence and the state machine that judges it.
"""
from __future__ import annotations

import base64
import math
import struct

import pytest

from backend.hud.utterance_audio import (
    FORMAT_WAV16K, UtteranceHolder, reset_utterance_holder,
)
from backend.hud.voice_identity import (
    Readiness, Verdict, VoiceIdentity, reset_voice_identity,
)
from backend.system_control.capability_router import _approved, _verdict_reason


def _wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(12000 * math.sin(i / 12))) for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture(autouse=True)
def _clean():
    reset_utterance_holder()
    reset_voice_identity()
    yield
    reset_utterance_holder()
    reset_voice_identity()


# ── The wire ────────────────────────────────────────────────────────────────

def test_a_valid_utterance_is_held():
    h = UtteranceHolder()
    assert h.deposit(_b64(_wav()), FORMAT_WAV16K)
    u = h.claim()
    assert u and u.audio.startswith(b"RIFF") and len(u.digest) == 8


def test_claiming_is_destructive():
    """One sentence answers one question. Handing a verifier the same sample
    twice is the shape of a replay."""
    h = UtteranceHolder()
    h.deposit(_b64(_wav()), FORMAT_WAV16K)
    assert h.claim() is not None
    assert h.claim() is None


@pytest.mark.parametrize("payload,fmt,why", [
    ("!!!not base64!!!", FORMAT_WAV16K, "undecodable"),
    (None, FORMAT_WAV16K, "empty"),
    ("", FORMAT_WAV16K, "empty"),
])
def test_malformed_audio_is_refused(payload, fmt, why):
    assert UtteranceHolder().deposit(payload, fmt) is False


def test_a_payload_that_is_not_a_wav_is_refused():
    """Validated on deposit, not at verification time — the worst moment to
    discover the evidence is unusable is when you need it."""
    assert UtteranceHolder().deposit(_b64(b"\x00" * 4096), FORMAT_WAV16K) is False


def test_an_unknown_format_is_refused():
    assert UtteranceHolder().deposit(_b64(_wav()), "mp3") is False


def test_an_oversized_utterance_is_refused(monkeypatch):
    monkeypatch.setenv("JARVIS_UTTERANCE_MAX_BYTES", "2048")
    assert UtteranceHolder().deposit(_b64(_wav(seconds=2.0)), FORMAT_WAV16K) is False


def test_a_stale_utterance_is_not_returned(monkeypatch):
    monkeypatch.setenv("JARVIS_UTTERANCE_TTL_S", "1")
    h = UtteranceHolder()
    h.deposit(_b64(_wav()), FORMAT_WAV16K)
    held = h._held
    held.captured_at -= 60          # age it past the TTL
    assert h.claim() is None


def test_bytes_never_reach_a_log():
    """`repr` is what lands in a log line. It must describe the sample without
    containing any of it."""
    h = UtteranceHolder()
    h.deposit(_b64(_wav()), FORMAT_WAV16K)
    text = repr(h.claim())
    assert "RIFF" not in text and "wav16k_b64" in text


# ── The judgement ───────────────────────────────────────────────────────────

class _Service:
    def __init__(self, profiles, verified=True, confidence=0.9):
        self.speaker_profiles = profiles
        self._v, self._c = verified, confidence

    async def verify_speaker(self, audio, name=None):
        return {"verified": self._v, "confidence": self._c, "speaker_name": name}


@pytest.mark.asyncio
async def test_no_audio_is_its_own_answer():
    r = await VoiceIdentity(service=_Service({"Derek": {}})).identify(None)
    assert r.verdict == Verdict.NO_AUDIO.value and not r.approves


@pytest.mark.asyncio
async def test_an_unloaded_service_says_not_ready_not_not_enrolled():
    """The correction that matters most here.

    Profiles load from `intelligence.learning_database`, so an empty local
    embeddings directory says NOTHING about whether a voiceprint exists.
    Answering NOT_ENROLLED before the service has loaded would tell an
    operator their voice was never enrolled when nothing had looked — and send
    them off to re-record 59 samples they already have.
    """
    r = await VoiceIdentity().identify(b"RIFFxxxx")
    assert r.verdict == Verdict.NOT_READY.value
    assert "unknown" in r.detail


@pytest.mark.asyncio
async def test_a_loaded_service_with_no_profile_is_a_fact():
    vi = VoiceIdentity(service=_Service({}))
    vi._enrolled_cache = ""               # the store answered, and found none
    r = await vi.identify(b"RIFFxxxx")
    assert r.verdict == Verdict.NOT_ENROLLED.value


def test_unknown_enrollment_is_none_not_empty():
    """None and "" are different claims: 'nothing has looked' vs 'it looked and
    found nobody'.

    A service with an EMPTY profiles dict is ALSO None — it has not looked
    either; its load failed. Only a populated dict, or a completed database
    lookup, is a statement about the world."""
    assert VoiceIdentity().enrolled_speaker() is None
    assert VoiceIdentity(service=_Service({})).enrolled_speaker() is None
    assert VoiceIdentity(service=_Service({"Derek": {}})).enrolled_speaker() == "Derek"

    settled = VoiceIdentity(service=_Service({}))
    settled._enrolled_cache = ""          # the store answered: genuinely nobody
    assert settled.enrolled_speaker() == ""


@pytest.mark.asyncio
async def test_the_owner_is_verified():
    r = await VoiceIdentity(service=_Service({"Derek": {}})).identify(b"RIFFxxxx")
    assert r.verdict == Verdict.VERIFIED.value and r.approves


@pytest.mark.asyncio
async def test_a_rejection_stays_a_rejection():
    svc = _Service({"Derek": {}}, verified=False, confidence=0.2)
    r = await VoiceIdentity(service=svc).identify(b"RIFFxxxx")
    assert r.verdict == Verdict.REJECTED.value and not r.approves


@pytest.mark.asyncio
async def test_the_confidence_floor_can_only_tighten():
    """The service's own adaptive threshold is never lowered here. This refuses
    to accept a `verified: true` that arrived with a confidence no one would
    consider convincing for unlocking a computer."""
    svc = _Service({"Derek": {}}, verified=True, confidence=0.40)
    r = await VoiceIdentity(service=svc).identify(b"RIFFxxxx")
    assert r.verdict == Verdict.REJECTED.value
    assert "below floor" in r.detail


@pytest.mark.asyncio
async def test_a_raising_service_is_never_consent():
    class Boom:
        speaker_profiles = {"Derek": {}}

        async def verify_speaker(self, *a, **k):
            raise RuntimeError("model exploded")

    r = await VoiceIdentity(service=Boom()).identify(b"RIFFxxxx")
    assert r.verdict == Verdict.UNAVAILABLE.value and not r.approves


@pytest.mark.asyncio
async def test_disabling_the_authority_denies_rather_than_allows(monkeypatch):
    """There is deliberately no setting that turns a verified unlock into an
    unverified one."""
    monkeypatch.setenv("JARVIS_VOICE_AUTHORITY_ENABLED", "false")
    r = await VoiceIdentity(service=_Service({"Derek": {}})).identify(b"RIFFxxxx")
    assert not r.approves


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [
    Verdict.NOT_ENROLLED, Verdict.NOT_READY, Verdict.NO_AUDIO,
    Verdict.REJECTED, Verdict.UNAVAILABLE,
])
async def test_every_non_verified_verdict_fails_closed(verdict):
    assert verdict.approves is False
    assert Verdict.VERIFIED.approves is True


# ── The router speaks its language ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_router_reads_the_verdict_without_a_translation_table():
    """`approves` IS the decision. Matching "verified" against a list of
    spellings of "yes" in the router would put one decision in two places."""
    ok = await VoiceIdentity(service=_Service({"Derek": {}})).identify(b"RIFFxxxx")
    no = await VoiceIdentity(service=_Service({})).identify(b"RIFFxxxx")
    assert _approved(ok) is True
    assert _approved(no) is False


@pytest.mark.asyncio
async def test_the_operator_hears_a_sentence_not_a_state_name():
    vi = VoiceIdentity(service=_Service({}))
    vi._enrolled_cache = ""               # the store answered: nobody enrolled
    r = await vi.identify(b"RIFFxxxx")
    spoken = _verdict_reason(r)
    assert "voiceprint" in spoken.lower()
    assert "not_enrolled" not in spoken


def test_readiness_reports_disabled_honestly(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_AUTHORITY_ENABLED", "false")
    assert VoiceIdentity().readiness is Readiness.DISABLED


# ── The 2026-08-03 01:41 log, defect by defect ──────────────────────────────

@pytest.mark.asyncio
async def test_unknown_enrollment_is_never_reported_as_not_enrolled():
    """Measured 01:41:27, with 272 samples sitting in CloudSQL.

    The enrollment lookup timed out and returned None, `if not owner` treated
    that identically to "", and JARVIS told its owner "I don't have a
    voiceprint for you on this Mac yet" — suggesting he enroll a voice he
    enrolled last October. Three states were designed so this could not
    happen, then two were collapsed by one falsy check.
    """
    # The measured shape: the ECAPA facade came up ("falling back to local
    # engine") so the service is present and answers verifications, but it
    # exposes no loaded profile dict, and the database lookup behind it timed
    # out — so enrollment is genuinely UNKNOWN.
    class _NoProfilesLoaded:
        async def verify_speaker(self, audio, name=None):
            return {"verified": True, "confidence": 0.9}

    vi = VoiceIdentity(service=_NoProfilesLoaded())
    vi._enrolled_cache = None                     # the lookup timed out
    r = await vi.identify(b"RIFFxxxx")
    assert r.verdict == Verdict.NOT_READY.value
    assert "UNKNOWN" in r.detail
    assert "voiceprint" not in r.spoken_reason.lower()


@pytest.mark.asyncio
async def test_a_reachable_store_with_no_profile_still_says_not_enrolled():
    """The other side of the same coin — "" must keep meaning nobody."""
    vi = VoiceIdentity(service=_Service({}))
    vi._enrolled_cache = ""
    r = await vi.identify(b"RIFFxxxx")
    assert r.verdict == Verdict.NOT_ENROLLED.value


def test_an_empty_profile_dict_is_not_a_statement_that_nobody_is_enrolled():
    """Live 2026-08-03 08:47. `EcapaFacade error ... falling back to local
    engine` + `CloudSQL init timed out after 10.0s` left a LIVE service whose
    `speaker_profiles` was `{}` — and this reported "I don't have a voiceprint
    for you on this Mac yet" while "Derek J. Russell / 272 samples" sat in the
    local SQLite one query away.

    Third instance of one defect: an empty container produced by a FAILED
    load, read as a positive statement of absence.
    """
    class FailedLoad:
        speaker_profiles = {}
        async def verify_speaker(self, a, n=None):
            return {"verified": True, "confidence": 0.9}

    vi = VoiceIdentity(service=FailedLoad())
    assert vi.enrolled_speaker() is None, "empty != nobody"

    class Loaded:
        speaker_profiles = {"Derek J. Russell": {}}
        async def verify_speaker(self, a, n=None):
            return {"verified": True, "confidence": 0.9}

    assert VoiceIdentity(service=Loaded()).enrolled_speaker() == "Derek J. Russell"


@pytest.mark.asyncio
async def test_an_unloaded_service_says_not_ready_so_a_retry_can_succeed():
    """NOT_ENROLLED is terminal advice ("go enroll"); NOT_READY invites the
    retry that actually works once the database answers."""
    class FailedLoad:
        speaker_profiles = {}
        async def verify_speaker(self, a, n=None):
            return {"verified": True, "confidence": 0.9}

    r = await VoiceIdentity(service=FailedLoad()).identify(b"RIFFxxxx")
    assert r.verdict == Verdict.NOT_READY.value
    assert "UNKNOWN" in r.detail


@pytest.mark.asyncio
async def test_boot_warm_does_not_load_the_model():
    """Measured from an unstarvable OS thread: awaiting
    `SpeakerVerificationService.initialize()` produced a 13.82s loop
    round-trip lag, matching the 14.33s stall in the live boot log. It is
    `async def` whose body never yields.

    It cannot be pushed to a worker loop either — it creates `asyncio.Lock`
    and `asyncio.Queue`, which would then be bound to a dying thread's loop.
    So boot resolves ENROLLMENT only; the model loads lazily on first need.
    """
    import ast
    import inspect
    # Parse it, and strip the docstring — this docstring EXPLAINS why
    # start_warming is absent, so a substring check reads its own prose as
    # evidence against itself.
    fn = ast.parse(inspect.getsource(VoiceIdentity.warm).lstrip()).body[0]
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    called = {n.func.attr for n in ast.walk(ast.Module(body=body, type_ignores=[]))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "refresh_enrollment" in called
    assert "start_warming" not in called, "boot must not load the speaker model"


@pytest.mark.asyncio
async def test_the_model_still_warms_on_first_need():
    """Lazily, not never. The first verification that needs it kicks the load
    and answers NOT_READY so the operator can retry."""
    import inspect
    assert "start_warming" in inspect.getsource(VoiceIdentity.identify)

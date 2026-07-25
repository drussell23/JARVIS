"""Karen's voice, and proof that synthesis actually happened.

Two defects, one symptom — the operator hears nothing.

PERSONA. `MacOSVoice._select_best_voice()` prefers British voices and had no
idea which agent it spoke for, so every process that mounted TTS got JARVIS's
voice. The `ov` audio host logged it on boot:

    RealTimeVoiceCommunicator initialized with voice: Daniel

Karen's cockpit, in JARVIS's voice. Karen is en_AU — her system prompt calls
her "a terse, senior Australian engineer", and the voice is installed.

ZERO-BYTE. `say -o file` can fail and still leave the file behind. afplay
plays a 0-byte AIFF happily and exits 0, so the failure is SILENCE THAT
REPORTS SUCCESS: no exception, no log, nothing to read.
"""

from __future__ import annotations

import os

import pytest

from backend.voice.agent_persona import (
    AgentPersona,
    active_persona,
    bind_persona,
    resolve_profile,
)
from backend.voice.macos_voice import (
    TTSGenerationError,
    _min_synthesis_bytes,
    _validate_synthesized_audio,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("JARVIS_AGENT_PERSONA", "JARVIS_VOICE_KAREN",
              "JARVIS_VOICE_JARVIS", "JARVIS_TTS_MIN_BYTES"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# (1) MANDATE: AgentPersona.KAREN resolves to Karen's voice profile
# ---------------------------------------------------------------------------


def test_karen_resolves_to_the_karen_voice_profile():
    """(1) THE MANDATE."""
    profile = resolve_profile(AgentPersona.KAREN)
    if profile is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert profile.persona is AgentPersona.KAREN
    assert profile.voice == "Karen", f"Karen resolved to {profile.voice!r}"
    assert "-v" in profile.as_say_args()
    assert "Karen" in profile.as_say_args()


def test_jarvis_still_resolves_to_daniel():
    """The other agent must not be collateral damage — JARVIS keeps his voice."""
    profile = resolve_profile(AgentPersona.JARVIS)
    if profile is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert profile.voice == "Daniel"


def test_the_two_personas_do_not_share_a_voice():
    k, j = resolve_profile(AgentPersona.KAREN), resolve_profile(AgentPersona.JARVIS)
    if k is None or j is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert k.voice != j.voice, "a multi-agent ecosystem speaking in one voice"


def test_binding_a_persona_reaches_the_synthesizer(monkeypatch):
    """The whole point of binding: MacOSVoice must pick it up WITHOUT being
    passed an argument, because the sites that got missed are exactly where
    the wrong voice survived."""
    from backend.voice.macos_voice import MacOSVoice

    bind_persona(AgentPersona.KAREN)
    assert active_persona() is AgentPersona.KAREN
    voice = MacOSVoice()
    if not voice.voices:
        pytest.skip("no macOS voices available")
    assert voice.primary_voice == "Karen"


def test_an_operator_override_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Tessa")
    p = resolve_profile(AgentPersona.KAREN)
    if p is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert p.voice == "Tessa" and p.source == "operator_override"


def test_an_uninstalled_override_falls_back_rather_than_failing(monkeypatch):
    """A typo must not silence her — an unusable voice name makes `say`
    fail, which is the 0-byte case this suite also defends."""
    monkeypatch.setenv("JARVIS_VOICE_KAREN", "NoSuchVoiceExists")
    p = resolve_profile(AgentPersona.KAREN)
    if p is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert p.voice != "NoSuchVoiceExists"
    assert p.source != "operator_override"


def test_preferences_are_ordered_lists_not_single_names():
    """A machine without Karen installed must get a deliberate second choice,
    not whatever the generic British-first selector happened to pick."""
    from backend.voice.agent_persona import _PREFERENCES

    for persona, prefs in _PREFERENCES.items():
        assert len(prefs) >= 2, f"{persona} has no fallback"


def test_an_unknown_persona_returns_none_rather_than_guessing():
    assert resolve_profile("nobody") is None
    assert resolve_profile(None) is None
    assert AgentPersona.coerce("garbage") is None


def test_resolution_never_raises(monkeypatch):
    import backend.voice.agent_persona as ap

    monkeypatch.setattr(ap, "installed_voices", lambda **_k: (_ for _ in ()).throw(OSError))
    assert ap.resolve_profile(AgentPersona.KAREN) is None


# ---------------------------------------------------------------------------
# (2) MANDATE: a 0-byte aiff raises and afplay never runs
# ---------------------------------------------------------------------------


def test_a_zero_byte_aiff_raises_instead_of_playing(tmp_path):
    """(2) THE MANDATE. afplay would play this as silence and exit 0."""
    f = tmp_path / "empty.aiff"
    f.write_bytes(b"")
    with pytest.raises(TTSGenerationError) as exc:
        _validate_synthesized_audio(str(f), "hello karen", 0)
    assert "0-byte" in str(exc.value)


def test_a_header_only_file_raises(tmp_path):
    """An AIFF header is ~50 bytes. Present but empty of speech."""
    f = tmp_path / "runt.aiff"
    f.write_bytes(b"FORM" + b"\0" * 60)
    with pytest.raises(TTSGenerationError):
        _validate_synthesized_audio(str(f), "hello karen", 0)


def test_a_missing_file_raises(tmp_path):
    with pytest.raises(TTSGenerationError):
        _validate_synthesized_audio(str(tmp_path / "absent.aiff"), "x", 1)


def test_real_audio_passes(tmp_path):
    """Positive control — without it every assertion above could pass simply
    because the validator always raises."""
    f = tmp_path / "ok.aiff"
    f.write_bytes(b"FORM" + b"\0" * 40000)
    _validate_synthesized_audio(str(f), "hello karen", 0)


def test_the_floor_is_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_TTS_MIN_BYTES", "50")
    f = _min_synthesis_bytes()
    assert f == 50


def test_the_error_names_the_size_and_the_return_code(tmp_path):
    """"TTS failed" sends the next person back to the same subprocess this
    check exists to see into."""
    f = tmp_path / "runt.aiff"
    f.write_bytes(b"x" * 10)
    with pytest.raises(TTSGenerationError) as exc:
        _validate_synthesized_audio(str(f), "hi", 42)
    msg = str(exc.value)
    assert "10B" in msg and "42" in msg


async def test_afplay_never_runs_for_an_empty_synthesis(tmp_path, monkeypatch):
    """(2) END TO END: the validator sits BETWEEN say and afplay, so a failed
    synthesis must never reach the player."""
    import subprocess as sp

    played = []
    real_popen = sp.Popen

    empty = tmp_path / "empty.aiff"
    empty.write_bytes(b"")

    def _fake_popen(argv, **kw):
        if argv and argv[0] == "afplay":
            played.append(argv)
            raise AssertionError("afplay ran on a 0-byte synthesis")
        return real_popen(["true"], **kw)

    monkeypatch.setattr(sp, "Popen", _fake_popen)

    with pytest.raises(TTSGenerationError):
        _validate_synthesized_audio(str(empty), "hello karen", 0)
        sp.Popen(["afplay", str(empty)])      # unreachable if the guard works

    assert played == [], "afplay was invoked despite failed synthesis"


def test_the_guard_sits_before_afplay_in_the_source():
    """Structural pin: ordering is the whole contract. Validating AFTER the
    player starts would log a failure the operator already heard as silence."""
    from pathlib import Path

    src = Path("backend/voice/macos_voice.py").read_text(encoding="utf-8")
    assert "_validate_synthesized_audio(" in src
    guard = src.index("_validate_synthesized_audio(_temp_path")
    play = src.index('"afplay"')
    assert guard < play, "the zero-byte guard runs after afplay"


def test_the_host_binds_karen():
    """Wiring pin — a persona nobody binds is a persona nobody uses."""
    from pathlib import Path

    src = Path("backend/audio/audio_plane_host.py").read_text(encoding="utf-8")
    assert "bind_persona(AgentPersona.KAREN)" in src
    # `await wire_conversation_pipeline(` is the CALL; the bare name also
    # appears in the module docstring, and comparing against prose would make
    # this pin a check on documentation.
    assert src.index("bind_persona") < src.index("await wire_conversation_pipeline("), (
        "persona bound after the pipeline — TTS resolves its voice at "
        "construction, so the singleton would already hold JARVIS's"
    )


# ---------------------------------------------------------------------------
# The SECOND hardcoding site
# ---------------------------------------------------------------------------
#
# Binding the persona fixed MacOSVoice and not RealTimeVoiceCommunicator,
# which pinned "Daniel" in _primary_voice AND in all seven VoiceModeConfigs.
# It kept announcing JARVIS's voice inside Karen's cockpit:
#   RealTimeVoiceCommunicator initialized with voice: Daniel


def test_the_voice_communicator_follows_the_bound_persona(monkeypatch):
    from backend.agi_os.realtime_voice_communicator import _persona_voice_or

    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "karen")
    assert _persona_voice_or("Daniel") == "Karen"


def test_an_unbound_process_keeps_its_existing_default(monkeypatch):
    """A process that never declared an identity must behave exactly as before
    — the fix may not change JARVIS."""
    from backend.agi_os.realtime_voice_communicator import _persona_voice_or

    monkeypatch.delenv("JARVIS_AGENT_PERSONA", raising=False)
    assert _persona_voice_or("Daniel") == "Daniel"


def test_no_mode_config_pins_a_voice_name():
    """Rate and pause are per-MODE character; the voice is per-AGENT identity.
    Conflating them meant seven configs each overrode the persona."""
    from pathlib import Path

    src = Path(
        "backend/agi_os/realtime_voice_communicator.py",
    ).read_text(encoding="utf-8")
    assert 'VoiceModeConfig(rate=175, voice="Daniel"' not in src
    assert "voice=self._primary_voice" in src

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


def test_karen_resolves_to_her_own_profile_not_jarvis(monkeypatch):
    """(1) THE MANDATE — that KAREN resolves to KAREN's profile.

    The head of her preference chain became SYSTEM_DEFAULT on 2026-07-25 at
    the operator's request, so the assertion is about the PERSONA being
    honoured, not about one voice name. Pinning the name here would make the
    test the very thing the sentinel exists to avoid.

    With the sentinel removed, the named chain must still resolve to Karen."""
    profile = resolve_profile(AgentPersona.KAREN)
    if profile is None:
        pytest.skip("no macOS voice inventory in this environment")
    assert profile.persona is AgentPersona.KAREN

    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Karen")
    named = resolve_profile(AgentPersona.KAREN)
    assert named.voice == "Karen"
    assert named.as_say_args()[:2] == ["-v", "Karen"]


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

    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Karen")   # pin a NAME to observe
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


def test_resolution_never_raises_even_with_no_inventory(monkeypatch):
    """A dead `say -v ?` must not propagate. KAREN still resolves — the system
    default needs no inventory, which is precisely why it is a safer head of
    the chain than any name."""
    import backend.voice.agent_persona as ap

    monkeypatch.setattr(
        ap, "installed_voices", lambda **_k: (_ for _ in ()).throw(OSError),
    )
    p = ap.resolve_profile(AgentPersona.KAREN)
    assert p is not None and p.is_system_default

    # A persona whose chain is all NAMES has nothing to verify against and
    # correctly declines rather than guessing.
    assert ap.resolve_profile(AgentPersona.JARVIS) is None


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
    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Karen")
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


def test_voice_discovery_does_not_override_the_persona(monkeypatch):
    """THE THIRD SITE. `_discover_voices()` re-ran a Daniel-first selection
    during __init__ and clobbered the persona voice assigned moments earlier —
    two selectors writing one attribute, last writer wins, and the last writer
    knew nothing about agents.

    Discovery answers "what is available"; the persona answers "who is
    speaking". The second outranks the first."""
    from backend.agi_os.realtime_voice_communicator import RealTimeVoiceCommunicator

    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "karen")
    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Karen")
    c = RealTimeVoiceCommunicator()
    if not c._available_voices:
        pytest.skip("no macOS voices available")
    assert c._primary_voice == "Karen", (
        f"discovery overrode the persona: got {c._primary_voice!r}"
    )


def test_every_mode_config_speaks_as_the_persona(monkeypatch):
    """Seven configs each pinned a voice; one surviving pin is a mode that
    silently reverts to the wrong agent mid-conversation."""
    from backend.agi_os.realtime_voice_communicator import RealTimeVoiceCommunicator

    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "karen")
    monkeypatch.setenv("JARVIS_VOICE_KAREN", "Karen")
    c = RealTimeVoiceCommunicator()
    if not c._available_voices:
        pytest.skip("no macOS voices available")
    voices = {cfg.voice for cfg in c._mode_configs.values()}
    assert voices == {"Karen"}, f"modes disagree on who is speaking: {voices}"


def test_an_unbound_process_still_discovers_daniel(monkeypatch):
    """The fallback must survive: a process with no persona keeps the
    behaviour it had before any of this existed."""
    from backend.agi_os.realtime_voice_communicator import RealTimeVoiceCommunicator

    monkeypatch.delenv("JARVIS_AGENT_PERSONA", raising=False)
    c = RealTimeVoiceCommunicator()
    if not c._available_voices:
        pytest.skip("no macOS voices available")
    assert c._primary_voice == "Daniel"


# ---------------------------------------------------------------------------
# The system-default voice — operator's explicit choice for O+V
# ---------------------------------------------------------------------------
#
# "whatever voice that was, I want to use that instead of Karen's voice" — the
# voice heard was `say` with no -v, i.e. whatever macOS is configured to use.
# Asking for "the system voice" is a STABLE request; pinning the name it
# currently resolves to is right until the operator changes the setting, and
# silently wrong afterwards.


def test_karen_now_defers_to_the_system_default():
    from backend.voice.agent_persona import SYSTEM_DEFAULT

    p = resolve_profile(AgentPersona.KAREN)
    assert p is not None
    assert p.voice == SYSTEM_DEFAULT and p.is_system_default


def test_the_system_default_omits_dash_v_entirely():
    """Expressed by OMITTING -v, never by resolving a name — that is the whole
    difference between following the setting and pinning it."""
    p = resolve_profile(AgentPersona.KAREN)
    args = p.as_say_args()
    assert "-v" not in args, f"a voice name was pinned: {args}"
    assert "-r" in args


def test_a_named_persona_still_passes_dash_v():
    """The sentinel must not turn every voice into the default."""
    p = resolve_profile(AgentPersona.JARVIS)
    if p is None or p.is_system_default:
        pytest.skip("no named voice resolvable here")
    assert p.as_say_args()[:2] == ["-v", p.voice]


@pytest.mark.parametrize("alias", ["system", "default", "SYSTEM", " Default "])
def test_operator_can_ask_for_the_system_voice_by_alias(monkeypatch, alias):
    monkeypatch.setenv("JARVIS_VOICE_JARVIS", alias)
    p = resolve_profile(AgentPersona.JARVIS)
    assert p is not None and p.is_system_default


def test_both_communicator_say_sites_honour_the_sentinel():
    """Two call sites build the same command; one un-migrated site is a mode
    that silently reverts to a pinned voice."""
    from pathlib import Path

    from backend.agi_os.realtime_voice_communicator import _voice_args

    assert _voice_args("system") == []
    assert _voice_args("Daniel") == ["-v", "Daniel"]

    src = Path(
        "backend/agi_os/realtime_voice_communicator.py",
    ).read_text(encoding="utf-8")
    assert "'-v', config.voice" not in src, "a say site still pins the voice"


def test_macos_voice_builds_the_command_without_dash_v(monkeypatch):
    from backend.voice.macos_voice import _is_system_default_voice

    assert _is_system_default_voice("system") is True
    assert _is_system_default_voice("Karen") is False

    from pathlib import Path
    src = Path("backend/voice/macos_voice.py").read_text(encoding="utf-8")
    assert "'-v', voice_config['voice']" not in src


# ---------------------------------------------------------------------------
# Identity — she must know that her own name means HER
# ---------------------------------------------------------------------------
#
# The conversation prompt was hardcoded "You are JARVIS", so in Karen's
# cockpit the model believed it was a different agent. Hearing "Hello Karen"
# it reasoned the operator was addressing SOMEONE ELSE:
#
#   "if you're expecting a real Karen to respond, that's not me …
#    So, Karen, or whoever you are, what would you like to do today?"
#
# An assistant asking the operator to identify a third party who was in fact
# itself. Four voice sites had been made persona-aware; the prompt had not.
# She SOUNDED like Karen and THOUGHT she was JARVIS.


def test_the_prompt_establishes_her_own_name():
    from backend.voice.agent_persona import system_prompt_for

    prompt = system_prompt_for(AgentPersona.KAREN)
    assert prompt and prompt.startswith("You are Karen"), prompt


def test_the_prompt_says_her_name_means_her():
    """THE REGRESSION. Without this the model treats its own name as a third
    party in the room."""
    from backend.voice.agent_persona import system_prompt_for

    prompt = system_prompt_for(AgentPersona.KAREN).lower()
    assert "addressing you" in prompt
    assert "third party" in prompt or "someone else" in prompt


def test_the_prompt_names_the_operator_when_known(monkeypatch):
    monkeypatch.setenv("JARVIS_OPERATOR_NAME", "Derek")
    from backend.voice.agent_persona import system_prompt_for

    assert "Derek" in system_prompt_for(AgentPersona.KAREN)


def test_the_operator_name_is_resolved_not_invented(monkeypatch):
    """An assistant that invents a name for the person in front of it is
    worse than one that simply does not use it."""
    import backend.voice.agent_persona as ap

    monkeypatch.delenv("JARVIS_OPERATOR_NAME", raising=False)
    monkeypatch.setattr(ap.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError))
    assert ap.operator_name() == ""
    prompt = ap.system_prompt_for(AgentPersona.KAREN)
    assert prompt and "addressing YOU" in prompt   # still unambiguous


def test_jarvis_keeps_his_own_identity():
    from backend.voice.agent_persona import system_prompt_for

    prompt = system_prompt_for(AgentPersona.JARVIS)
    assert prompt.startswith("You are JARVIS")
    assert "Karen" not in prompt


def test_an_explicit_operator_prompt_still_wins(monkeypatch):
    """Someone who wrote their own prompt has said something more specific
    than any default this can compose."""
    monkeypatch.setenv("JARVIS_CONV_SYSTEM_PROMPT", "You are a pirate.")
    from backend.voice.agent_persona import system_prompt_for

    assert system_prompt_for(AgentPersona.KAREN) == "You are a pirate."


def test_an_unknown_persona_yields_no_prompt():
    """None means 'keep the caller's prompt' — the same contract voices use,
    so an unknown persona degrades to existing behaviour rather than an
    invented identity."""
    from backend.voice.agent_persona import system_prompt_for

    assert system_prompt_for("nobody") is None
    assert system_prompt_for(None) is None


def test_the_pipeline_takes_its_identity_from_the_persona(monkeypatch):
    """Wiring pin: voice and prompt must come from ONE place or they drift —
    which is precisely how she came to sound like Karen and think she was
    JARVIS."""
    import backend.audio.conversation_pipeline as cp

    monkeypatch.delenv("JARVIS_CONV_SYSTEM_PROMPT", raising=False)
    monkeypatch.setenv("JARVIS_AGENT_PERSONA", "karen")
    assert (cp._persona_system_prompt() or "").startswith("You are Karen")

    monkeypatch.delenv("JARVIS_AGENT_PERSONA", raising=False)
    assert cp._persona_system_prompt() is None

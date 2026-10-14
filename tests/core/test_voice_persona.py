"""JARVIS has one voice: every engine renders the persona or stays silent."""
from __future__ import annotations

from backend.core.voice_persona import Persona, VoiceOption, choose, persona

DANIEL = Persona("Daniel", "en-gb", "male")
WINDOWS = [VoiceOption("david-id", "Microsoft David Desktop - English (United States)", "en-US", "Male"),
           VoiceOption("zira-id", "Microsoft Zira Desktop - English (United States)", "en-US", "Female")]
EDGE = [VoiceOption("en-GB-LibbyNeural", "Libby", "en-GB", "Female"),
        VoiceOption("en-GB-RyanNeural", "Ryan", "en-GB", "Male"),
        VoiceOption("en-US-GuyNeural", "Guy", "en-US", "Male")]
MAC = [VoiceOption("Daniel", "Daniel", "en_GB", ""), VoiceOption("Samantha", "Samantha", "en_US", "")]


def test_the_declared_voice_wins_where_it_exists():
    assert choose(MAC, DANIEL).id == "Daniel"


def test_elsewhere_the_closest_voice_by_locale_and_gender():
    assert choose(EDGE, DANIEL).id == "en-GB-RyanNeural"


def test_an_engine_without_such_a_voice_offers_none_not_the_first_installed():
    assert choose(WINDOWS, DANIEL) is None


def test_no_declared_locale_means_exact_name_only():
    assert choose(EDGE, Persona("Daniel")) is None


def test_persona_reads_its_declaration(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_NAME", "Daniel")
    monkeypatch.setenv("JARVIS_VOICE_LOCALE", "en_GB")
    monkeypatch.setenv("JARVIS_VOICE_GENDER", "M")
    assert persona() == Persona("Daniel", "en-gb", "male")


class _FakeVoice:
    def __init__(self, id, name, languages, gender):
        self.id, self.name, self.languages, self.gender = id, name, languages, gender


class _FakeEngine:
    def __init__(self, voices):
        self._voices, self.props, self.stopped = voices, {}, False

    def getProperty(self, key):
        return self._voices if key == "voices" else self.props.get(key)

    def setProperty(self, key, value):
        self.props[key] = value

    def stop(self):
        self.stopped = True


def _fake_pyttsx3(monkeypatch, voices):
    import sys
    import types
    made = []

    def init():
        made.append(_FakeEngine(voices))
        return made[-1]

    monkeypatch.setitem(sys.modules, "pyttsx3", types.SimpleNamespace(init=init))
    return made


def _declare_daniel(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_NAME", "Daniel")
    monkeypatch.setenv("JARVIS_VOICE_LOCALE", "en-GB")
    monkeypatch.setenv("JARVIS_VOICE_GENDER", "male")
    monkeypatch.delenv("JARVIS_ENFORCE_CANONICAL_VOICE", raising=False)


def test_sapi_without_the_persona_is_declined_not_left_speaking_as_david(monkeypatch):
    from backend.core.voice_persona import pyttsx3_persona_engine
    _declare_daniel(monkeypatch)
    made = _fake_pyttsx3(monkeypatch, [_FakeVoice("david", "Microsoft David", ["en-US"], "Male")])
    engine, options, why = pyttsx3_persona_engine()
    assert engine is None and "Daniel" in why
    assert made[0].stopped and [o.id for o in options] == ["david"]


def test_a_rendering_voice_is_set_on_the_engine(monkeypatch):
    from backend.core.voice_persona import pyttsx3_persona_engine
    _declare_daniel(monkeypatch)
    _fake_pyttsx3(monkeypatch, [_FakeVoice("david", "Microsoft David", ["en-US"], "Male"),
                                _FakeVoice("george", "Microsoft George", ["en-GB"], "Male")])
    engine, _options, why = pyttsx3_persona_engine()
    assert engine is not None and why == ""
    assert engine.props["voice"] == "george"


def test_enforcement_off_keeps_the_engine(monkeypatch):
    from backend.core.voice_persona import pyttsx3_persona_engine
    _declare_daniel(monkeypatch)
    monkeypatch.setenv("JARVIS_ENFORCE_CANONICAL_VOICE", "false")
    _fake_pyttsx3(monkeypatch, [_FakeVoice("david", "Microsoft David", ["en-US"], "Male")])
    engine, _options, _why = pyttsx3_persona_engine()
    assert engine is not None and "voice" not in engine.props

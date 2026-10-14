"""safe_say speaks in the voice it was asked for, or not at all.

v304.0 fell back through Daniel, Samantha, Alex, Fred and then the first
installed voice, so a missing voice became a different person. Resolution now
follows backend/core/voice_persona: the exact name, else (for JARVIS's own
voice) the persona's locale and gender, else nothing.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core import voice_persona
from backend.core.supervisor import unified_voice_orchestrator as uvo
from backend.core.voice_persona import VoiceOption


def _catalogue(monkeypatch, *voices):
    monkeypatch.setattr(voice_persona, "say_voice_options",
                        lambda: [VoiceOption(id=n, name=n, locale=loc) for n, loc in voices])


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(uvo, "_validated_voices", {})
    monkeypatch.setenv("JARVIS_VOICE_NAME", "Daniel")
    monkeypatch.setenv("JARVIS_VOICE_LOCALE", "en-GB")
    monkeypatch.setenv("JARVIS_VOICE_GENDER", "male")
    monkeypatch.delenv("JARVIS_ENFORCE_CANONICAL_VOICE", raising=False)


def _resolve(name):
    return asyncio.run(uvo._resolve_voice(name))


def test_the_requested_voice_when_installed(monkeypatch):
    _catalogue(monkeypatch, ("Daniel", "en_GB"), ("Samantha", "en_US"))
    assert _resolve("Daniel") == "Daniel"


def test_never_another_person_when_it_is_missing(monkeypatch):
    _catalogue(monkeypatch, ("Samantha", "en_US"), ("Alex", "en_US"), ("Fred", "en_US"))
    assert _resolve("Daniel") == ""


def test_another_callers_voice_is_matched_by_name_only(monkeypatch):
    _catalogue(monkeypatch, ("Daniel", "en_GB"), ("Karen", "en_AU"))
    assert _resolve("Karen") == "Karen"
    assert _resolve("Moira") == ""


def test_with_enforcement_off_say_is_left_to_choose(monkeypatch):
    monkeypatch.setenv("JARVIS_ENFORCE_CANONICAL_VOICE", "false")
    _catalogue(monkeypatch, ("Samantha", "en_US"))
    assert _resolve("Daniel") == "Daniel"

"""The page that plays JARVIS's voice may be talked over; the operator sets how.

2026-10-14: the web app aborted recognition whenever JARVIS spoke, so the
user could not interrupt -- and the host's barge-in detectors were wired to
macOS playback the web app never used. The page now keeps listening and
judges what it hears against what JARVIS is saying; the thresholds it judges
by are served from one place (backend/core/barge_in_policy).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.barge_in_policy import barge_in_policy

_SETTINGS = ("JARVIS_BARGE_IN_ENABLED", "JARVIS_BARGE_IN_MIN_WORDS",
             "JARVIS_BARGE_IN_ECHO_OVERLAP", "JARVIS_BARGEIN_COOLDOWN_MS")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in _SETTINGS:
        monkeypatch.delenv(name, raising=False)


def test_by_default_the_user_can_interrupt_with_one_word():
    policy = barge_in_policy()
    assert policy.enabled and policy.min_words == 1
    assert 0 < policy.echo_overlap <= 1 and policy.cooldown_ms >= 0


def test_the_operator_sets_it_and_it_applies_without_a_restart(monkeypatch):
    monkeypatch.setenv("JARVIS_BARGE_IN_ENABLED", "off")
    monkeypatch.setenv("JARVIS_BARGE_IN_MIN_WORDS", "3")
    monkeypatch.setenv("JARVIS_BARGE_IN_ECHO_OVERLAP", "0.8")
    monkeypatch.setenv("JARVIS_BARGEIN_COOLDOWN_MS", "0")
    assert barge_in_policy().as_dict() == {
        "enabled": False, "min_words": 3, "echo_overlap": 0.8, "cooldown_ms": 0}


@pytest.mark.parametrize("name,raw", [
    ("JARVIS_BARGE_IN_ENABLED", "maybe"),
    ("JARVIS_BARGE_IN_MIN_WORDS", "0"),
    ("JARVIS_BARGE_IN_MIN_WORDS", "two"),
    ("JARVIS_BARGE_IN_ECHO_OVERLAP", "1.5"),
    ("JARVIS_BARGE_IN_ECHO_OVERLAP", "0"),
    ("JARVIS_BARGEIN_COOLDOWN_MS", "-1"),
])
def test_a_typo_is_the_default_never_a_broken_listener(monkeypatch, name, raw):
    default = barge_in_policy()
    monkeypatch.setenv(name, raw)
    assert barge_in_policy() == default


def test_the_page_reads_the_policy_from_the_backend(monkeypatch):
    from backend import main
    monkeypatch.setenv("JARVIS_BARGE_IN_MIN_WORDS", "2")
    served = asyncio.run(main.audio_barge_in())
    assert served["min_words"] == 2 and set(served) == {
        "enabled", "min_words", "echo_overlap", "cooldown_ms"}

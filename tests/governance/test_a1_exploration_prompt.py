"""A1 exploration-forcing system-prompt addendum.

The 32B fails the Iron Gate by emitting zero-shot diffs. This addendum (injected
into the Prime-path system prompt, gated to the A1 soak) teaches it the gate's
contract: >=2 read_file/search_code calls BEFORE any patch. Byte-identical (empty)
when the flag is off.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.providers as providers


def test_addendum_empty_when_disabled(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_EXPLORATION_PROMPT_ENABLED", raising=False)
    assert providers._a1_exploration_addendum() == ""


def test_addendum_present_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_EXPLORATION_PROMPT_ENABLED", "true")
    txt = providers._a1_exploration_addendum()
    assert txt  # non-empty
    # Names the exact exploration tools + the >=2-before-patch floor the gate enforces.
    assert "read_file" in txt and "search_code" in txt
    assert "2" in txt
    assert "patch" in txt.lower()


def test_addendum_floor_tracks_env(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_EXPLORATION_PROMPT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MIN_EXPLORATION_CALLS", "3")
    txt = providers._a1_exploration_addendum()
    assert "3 times" in txt or "least 3" in txt


def test_addendum_fail_soft_on_bad_floor(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_EXPLORATION_PROMPT_ENABLED", "on")
    monkeypatch.setenv("JARVIS_MIN_EXPLORATION_CALLS", "not-a-number")
    txt = providers._a1_exploration_addendum()  # must not raise; floor -> 2
    assert "2" in txt

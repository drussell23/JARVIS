"""PresentationMode: default-SOAK fail-safe resolution (spec §3.5)."""
from __future__ import annotations

from backend.core.ouroboros.ui.presentation_mode import (
    PresentationMode, resolve_presentation_mode, is_cockpit,
)


def test_default_is_soak():
    assert resolve_presentation_mode(env={}) is PresentationMode.SOAK


def test_cockpit_value_resolves():
    env = {"JARVIS_OV_PRESENTATION": "cockpit"}
    assert resolve_presentation_mode(env=env) is PresentationMode.COCKPIT


def test_garbage_fails_safe_to_soak():
    env = {"JARVIS_OV_PRESENTATION": "PARTY_MODE"}
    assert resolve_presentation_mode(env=env) is PresentationMode.SOAK


def test_case_and_whitespace_tolerant():
    env = {"JARVIS_OV_PRESENTATION": "  Cockpit "}
    assert resolve_presentation_mode(env=env) is PresentationMode.COCKPIT


def test_is_cockpit_reads_process_env(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_PRESENTATION", "cockpit")
    assert is_cockpit() is True
    monkeypatch.delenv("JARVIS_OV_PRESENTATION")
    assert is_cockpit() is False

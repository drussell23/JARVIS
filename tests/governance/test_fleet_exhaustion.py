"""Sovereign Fleet-Wide Hibernation Matrix tests (2026-06-20)."""
from __future__ import annotations

from backend.core.ouroboros.governance import fleet_exhaustion as FE


def test_failsafe_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", raising=False)
    assert FE.failsafe_enabled() is True


def test_failsafe_off(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", "0")
    assert FE.failsafe_enabled() is False


def test_deepsleep_default_and_clamp(monkeypatch):
    monkeypatch.delenv("JARVIS_FLEET_DEEPSLEEP_S", raising=False)
    assert FE.deepsleep_seconds() == 2700.0
    monkeypatch.setenv("JARVIS_FLEET_DEEPSLEEP_S", "5")     # below floor
    assert FE.deepsleep_seconds() == 60.0
    monkeypatch.setenv("JARVIS_FLEET_DEEPSLEEP_S", "999999")  # above ceiling
    assert FE.deepsleep_seconds() == 4 * 3600.0
    monkeypatch.setenv("JARVIS_FLEET_DEEPSLEEP_S", "garbage")
    assert FE.deepsleep_seconds() == 2700.0


def test_disabled_never_reports_exhausted(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", "0")
    monkeypatch.setattr(FE, "_candidate_models", lambda r: ("m1", "m2"))
    monkeypatch.setattr(FE, "_is_quarantined", lambda m: True)
    assert FE.fleet_exhausted() is False


def test_fail_open_on_empty_candidate_list(monkeypatch):
    # No candidates resolved → cannot prove exhaustion → do NOT sleep.
    monkeypatch.setenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", "true")
    monkeypatch.setattr(FE, "_candidate_models", lambda r: ())
    assert FE.fleet_exhausted() is False


def test_exhausted_when_all_quarantined(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", "true")
    monkeypatch.setattr(FE, "_candidate_models", lambda r: ("m1", "m2", "m3"))
    monkeypatch.setattr(FE, "_is_quarantined", lambda m: True)
    assert FE.fleet_exhausted() is True


def test_not_exhausted_when_one_available(monkeypatch):
    monkeypatch.setenv("JARVIS_FLEET_EXHAUSTION_FAILSAFE_ENABLED", "true")
    monkeypatch.setattr(FE, "_candidate_models", lambda r: ("m1", "m2", "m3"))
    monkeypatch.setattr(FE, "_is_quarantined", lambda m: m != "m2")  # m2 available
    assert FE.fleet_exhausted() is False


def test_flush_never_raises(monkeypatch):
    # Even with the substrates absent/raising, flush returns an int, never raises.
    monkeypatch.setattr(FE, "_candidate_models", lambda r: ("m1",))
    n = FE.flush_ephemeral_quarantine()
    assert isinstance(n, int)

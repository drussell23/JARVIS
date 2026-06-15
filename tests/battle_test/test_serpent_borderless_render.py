"""Sovereign Terminal UI — borderless render + sub-flag regression suite."""
import backend.core.ouroboros.battle_test.presentation_restraint as PR


# --------------------------------------------------------------------------- flags
def test_borderless_flag_default_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", raising=False)
    assert PR.borderless_enabled() is True


def test_borderless_flag_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "false")
    assert PR.borderless_enabled() is False


def test_borderless_off_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    monkeypatch.delenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", raising=False)
    assert PR.borderless_enabled() is False     # master gates the sub-flag


def test_pulse_flag_default_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_TUI_PULSE_ENABLED", raising=False)
    assert PR.pulse_enabled() is True


def test_pulse_off_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    monkeypatch.delenv("JARVIS_TUI_PULSE_ENABLED", raising=False)
    assert PR.pulse_enabled() is False

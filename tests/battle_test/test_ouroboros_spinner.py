"""UI Slice 8 — Ouroboros snake-eating-tail spinner.

Pins the contract that:
  * The custom ``"ouroboros"`` spinner is registered in Rich's
    ``SPINNERS`` registry at module import time.
  * Frames are non-empty + finite + single-line.
  * Frame cycle includes the bite glyph (🐍◯) — the moment of
    self-consumption that names the symbol.
  * ``_active_spinner_name()`` returns ``"ouroboros"`` by default
    (graduated default-on) and ``"dots"`` when explicitly disabled.
  * All four hardcoded ``spinner="dots"`` sites in serpent_flow.py
    now consume the helper instead.

Authority Invariant
-------------------
Tests import only from the modules under test + stdlib + Rich.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Registry + frame shape
# -----------------------------------------------------------------------


def test_ouroboros_registered_in_rich_spinners():
    """The spinner must be registered in Rich's SPINNERS dict at
    serpent_flow import time."""
    from rich.spinner import SPINNERS
    # Importing serpent_flow registers the spinner as a side effect.
    import backend.core.ouroboros.battle_test.serpent_flow  # noqa: F401
    assert "ouroboros" in SPINNERS


def test_ouroboros_frames_are_well_formed():
    from rich.spinner import SPINNERS
    import backend.core.ouroboros.battle_test.serpent_flow  # noqa: F401
    spec = SPINNERS["ouroboros"]
    assert "interval" in spec
    assert "frames" in spec
    frames = spec["frames"]
    assert len(frames) >= 5, "expect a real cycle (≥5 frames)"
    # Each frame must be a single-line non-empty string
    for f in frames:
        assert isinstance(f, str)
        assert "\n" not in f, "frame must be single-line"
        assert len(f) > 0


def test_ouroboros_cycle_contains_bite_glyph():
    """The bite frame (🐍◯ — head consuming tail) must appear in the
    cycle. Without it the animation isn't visually communicating
    'eating its own tail' — the entire point of the symbol."""
    from rich.spinner import SPINNERS
    import backend.core.ouroboros.battle_test.serpent_flow  # noqa: F401
    frames = SPINNERS["ouroboros"]["frames"]
    bite_frames = [f for f in frames if "◯" in f]
    assert bite_frames, (
        "expected at least one frame with the bite glyph (◯) — "
        "the head-eating-tail moment"
    )
    # And the snake glyph itself
    assert all("🐍" in f for f in frames), (
        "every frame should contain the snake glyph 🐍"
    )


# -----------------------------------------------------------------------
# § B — Active spinner resolver
# -----------------------------------------------------------------------


def test_active_spinner_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_UI_OUROBOROS_SPINNER", raising=False)
    from backend.core.ouroboros.battle_test.serpent_flow import (
        _active_spinner_name,
    )
    assert _active_spinner_name() == "ouroboros"


def test_active_spinner_explicit_truthy(monkeypatch):
    from backend.core.ouroboros.battle_test.serpent_flow import (
        _active_spinner_name,
    )
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_UI_OUROBOROS_SPINNER", val)
        assert _active_spinner_name() == "ouroboros"


def test_active_spinner_explicit_falsy_falls_back_to_dots(monkeypatch):
    from backend.core.ouroboros.battle_test.serpent_flow import (
        _active_spinner_name,
    )
    for val in ("0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("JARVIS_UI_OUROBOROS_SPINNER", val)
        assert _active_spinner_name() == "dots"


def test_active_spinner_empty_string_treated_as_unset(monkeypatch):
    """Asymmetric env semantics: explicit empty string == unset ==
    graduated default-on."""
    from backend.core.ouroboros.battle_test.serpent_flow import (
        _active_spinner_name,
    )
    monkeypatch.setenv("JARVIS_UI_OUROBOROS_SPINNER", "")
    assert _active_spinner_name() == "ouroboros"


# -----------------------------------------------------------------------
# § C — Bytes pins on integration sites
# -----------------------------------------------------------------------


def test_no_hardcoded_dots_in_status_calls():
    """All four prior ``spinner="dots"`` call sites in serpent_flow.py
    have been replaced with ``spinner=_active_spinner_name()`` so the
    env knob can hot-revert at any of them."""
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert 'spinner="dots"' not in src, (
        "Slice 8 routes spinner construction through "
        "_active_spinner_name(); no hardcoded 'dots' should remain"
    )
    # And the helper is consumed from at least 4 sites (the four
    # prior dots-spinner call sites).
    helper_uses = src.count("spinner=_active_spinner_name()")
    assert helper_uses >= 4, (
        f"expected ≥4 _active_spinner_name() consumers, got {helper_uses}"
    )


def test_start_status_default_resolves_at_call_time():
    """``_start_status`` default param must be ``None`` so the active
    spinner is resolved on each call (lets operators flip the env
    knob live without restarting)."""
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    fn_idx = src.find("    def _start_status(")
    end_idx = src.find("    def _stop_status(", fn_idx)
    body = src[fn_idx:end_idx]
    # Default param is Optional[str] = None
    assert "spinner: Optional[str] = None" in body
    # Resolves via helper when None
    assert "_active_spinner_name()" in body


# -----------------------------------------------------------------------
# § D — Behavioral
# -----------------------------------------------------------------------


def test_spinner_construction_uses_ouroboros_by_default(monkeypatch):
    """Construct an actual rich.Status with the resolved spinner name
    — must not raise (i.e. the spinner is correctly registered)."""
    monkeypatch.delenv("JARVIS_UI_OUROBOROS_SPINNER", raising=False)
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import (
        _active_spinner_name,
    )
    name = _active_spinner_name()
    assert name == "ouroboros"
    # Construction must succeed (Rich looks up the name in SPINNERS)
    console = Console(force_terminal=False)
    status = console.status("test", spinner=name)
    # No exception means the spinner was found in the registry
    assert status is not None


# -----------------------------------------------------------------------
# § E — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_no_orchestrator_imports():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "candidate_generator", "providers", "orchestrator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"

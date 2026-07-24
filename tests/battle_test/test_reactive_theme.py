"""Bulletproof spine for the Reactive Theme Singleton.

Mandated assertions, headless (no real terminal):

  (1) injecting a ``provider_state_changed`` event updates the Theme Singleton's
      active border-color property, and
  (2) that property mutation triggers a layout invalidation WITHOUT requiring a
      full application restart (the registered invalidate hook fires; the same
      singleton object persists).

Plus: state→accent coverage, graceful truecolor→8-bit degradation, idempotent
non-transitions, and the DRY seam (the registry sources its severity styling
from the theme).
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import theme as T
from backend.core.ouroboros.ui.theme import (
    ColorTier,
    UIState,
    get_reactive_theme,
    reset_reactive_theme,
    semantic,
    severity_style,
)


@pytest.fixture(autouse=True)
def _fresh_theme():
    reset_reactive_theme()
    T.reset_active_tier_cache()
    yield
    reset_reactive_theme()


# ---------------------------------------------------------------------------
# (1) provider_state_changed → active border color mutates
# ---------------------------------------------------------------------------


def test_provider_state_event_mutates_border_color():
    th = get_reactive_theme()
    assert th.state == UIState.DORMANT
    dormant_border = th.active_border_style(ColorTier.TRUECOLOR)

    # Inject the broker event — DEGRADED.
    changed = th.on_event("provider_state_changed", {"provider": "doubleword", "state": "DEGRADED"})
    assert changed is True
    assert th.state == UIState.DEGRADED
    degraded_border = th.active_border_style(ColorTier.TRUECOLOR)
    assert degraded_border != dormant_border
    assert degraded_border == semantic("crit", ColorTier.TRUECOLOR)   # red

    # Recovery flips it to the HEALTHY accent.
    th.on_event("provider_state_changed", {"provider": "doubleword", "state": "HEALTHY"})
    assert th.state == UIState.HEALTHY
    assert th.active_border_style(ColorTier.TRUECOLOR) == semantic("cyan", ColorTier.TRUECOLOR)


# ---------------------------------------------------------------------------
# (2) the mutation triggers invalidation WITHOUT an app restart
# ---------------------------------------------------------------------------


def test_mutation_triggers_invalidation_no_restart():
    th = get_reactive_theme()
    hits = {"n": 0}

    # A prompt_toolkit Application would register app.invalidate here — we model
    # it with a counter. The theme holds only this callable, not the app.
    unregister = th.register_invalidate(lambda: hits.__setitem__("n", hits["n"] + 1))
    same_singleton = get_reactive_theme()
    assert same_singleton is th                      # ONE persistent object

    th.on_event("provider_state_changed", {"state": "DEGRADED"})
    assert hits["n"] == 1                             # invalidate fired — in-place redraw
    th.on_event("supervisor_armed", {})              # DEGRADED → ARMED
    assert hits["n"] == 2

    # The singleton was never rebuilt — same object, transitions accumulated.
    assert get_reactive_theme() is th
    assert th.transitions == 2

    # Unregister stops future redraws (decoupling proof).
    unregister()
    th.on_event("soak_chunk_committed", {})          # ARMED → SOAKING
    assert hits["n"] == 2                             # no further hook calls


def test_non_transition_does_not_invalidate():
    th = get_reactive_theme()
    hits = {"n": 0}
    th.register_invalidate(lambda: hits.__setitem__("n", hits["n"] + 1))
    th.on_event("provider_state_changed", {"state": "DEGRADED"})
    assert hits["n"] == 1
    # Same state again — no transition, no redraw (zero wasted work).
    th.on_event("provider_state_changed", {"state": "DEGRADED"})
    assert hits["n"] == 1
    # An unmapped event is ignored entirely.
    assert th.on_event("some_unrelated_event", {"x": 1}) is False
    assert hits["n"] == 1


# ---------------------------------------------------------------------------
# state → accent coverage + graceful degradation
# ---------------------------------------------------------------------------


def test_state_accent_map_full_coverage():
    th = get_reactive_theme()
    expect = {
        "supervisor_armed": UIState.ARMED,
        "awe_soak_launched": UIState.SOAKING,
        "supervisor_disarmed": UIState.DORMANT,
        "soak_run_complete": UIState.HEALTHY,
    }
    for et, st in expect.items():
        th.on_event(et, {})
        assert th.state == st, f"{et} → {st}"
        assert isinstance(th.active_border_style(ColorTier.TRUECOLOR), str)


def test_graceful_terminal_degradation():
    th = get_reactive_theme()
    th.on_event("provider_state_changed", {"state": "DEGRADED"})
    # Truecolor → hex; standard → ANSI name; NONE → stripped ("").
    assert th.active_border_style(ColorTier.TRUECOLOR).startswith("#") or "#" in th.active_border_style(ColorTier.TRUECOLOR)
    assert th.active_border_style(ColorTier.STANDARD) == "red"
    assert th.active_border_style(ColorTier.NONE) == ""
    # severity_style degrades identically and never raises on a bad rank.
    assert severity_style(3, ColorTier.TRUECOLOR).endswith("#F85149")
    assert severity_style(3, ColorTier.STANDARD) == "bold red"
    assert severity_style(3, ColorTier.NONE) == ""
    assert isinstance(severity_style(99, ColorTier.STANDARD), str)   # unknown rank → safe


# ---------------------------------------------------------------------------
# DRY — the registry sources severity styling from the theme (one definition)
# ---------------------------------------------------------------------------


def test_registry_imports_severity_from_theme():
    from backend.core.ouroboros.governance import event_breadcrumb_registry as R
    # The registry's severity→glyph table IS the theme's (same object).
    assert R._GLYPH_BY_SEV is T.SEVERITY_GLYPH
    # And its color derivation routes through the theme's tier resolver.
    assert R._color_for_sev(3) == severity_style(3)

"""§38 Slice 1 (PRD v2.57→v2.58, 2026-05-07) — posture mood-ring
regression spine.

Covers:

  * palette_for_posture pure-function mapping (4 enum values +
    None + defensive on bad inputs)
  * read_current_posture_safe defensive composition of canonical
    posture_repl._default_store
  * format_posture_badge master-flag gating + plain/rich modes
  * status_line _format_posture_badge_token integration (lead
    position)
  * 4 AST pins (master_default_false / authority_asymmetry /
    composes_canonical_store / color_table_exhaustive)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_1(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance import posture_repl
    posture_repl._default_store = None
    yield
    posture_repl._default_store = None


# ---------------------------------------------------------------------------
# Master flag default-FALSE
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.posture_palette import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "on"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.posture_palette import (
        master_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", value,
    )
    assert master_enabled() is True


# ---------------------------------------------------------------------------
# palette_for_posture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "posture_value,expected_color",
    [
        ("EXPLORE", "green"),
        ("CONSOLIDATE", "blue"),
        ("HARDEN", "yellow"),
        ("MAINTAIN", "bright_black"),
    ],
)
def test_palette_for_known_posture_values(
    posture_value, expected_color,
):
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    posture = Posture(posture_value)
    assert palette_for_posture(posture) == expected_color


def test_palette_for_none_returns_dim():
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    assert palette_for_posture(None) == "bright_black"


def test_palette_for_string_value():
    """Accepts string posture values for forward-compat."""
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    assert palette_for_posture("EXPLORE") == "green"
    assert palette_for_posture("HARDEN") == "yellow"


def test_palette_for_unknown_string_returns_dim():
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    assert palette_for_posture("UNKNOWN_POSTURE") == "bright_black"
    assert palette_for_posture("xyz") == "bright_black"


def test_palette_defensive_on_non_string():
    """NEVER raises on bad input types."""
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    for bad in (42, [], {}, object()):
        assert palette_for_posture(bad) == "bright_black"


def test_palette_color_table_exhaustive():
    """Operator binding "no hardcoding" — every Posture enum
    value MUST have a canonical color mapping."""
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        palette_for_posture,
    )
    # All 4 enum values resolve to non-empty color string
    for p in Posture:
        color = palette_for_posture(p)
        assert color
        assert isinstance(color, str)


# ---------------------------------------------------------------------------
# read_current_posture_safe
# ---------------------------------------------------------------------------


def test_read_current_posture_no_store_wired():
    from backend.core.ouroboros.governance.posture_palette import (
        read_current_posture_safe,
    )
    assert read_current_posture_safe() is None


def test_read_current_posture_store_returns_none():
    """Store wired but no current reading → None."""
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture_palette import (
        read_current_posture_safe,
    )

    class _MockEmptyStore:
        def load_current(self):
            return None

    posture_repl._default_store = _MockEmptyStore()
    assert read_current_posture_safe() is None


def test_read_current_posture_returns_posture():
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        read_current_posture_safe,
    )

    class _MockReading:
        posture = Posture.EXPLORE

    class _MockStore:
        def load_current(self):
            return _MockReading()

    posture_repl._default_store = _MockStore()
    assert read_current_posture_safe() == Posture.EXPLORE


def test_read_current_posture_defensive_on_broken_store():
    """Store that raises on load_current → returns None."""
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture_palette import (
        read_current_posture_safe,
    )

    class _BrokenStore:
        def load_current(self):
            raise RuntimeError("simulated")

    posture_repl._default_store = _BrokenStore()
    # NEVER raises — returns None.
    assert read_current_posture_safe() is None


# ---------------------------------------------------------------------------
# format_posture_badge
# ---------------------------------------------------------------------------


def test_format_badge_master_off_returns_empty():
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        format_posture_badge,
    )

    class _R:
        posture = Posture.HARDEN

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    # Master off → empty
    assert format_posture_badge() == ""


def test_format_badge_master_on_no_store_returns_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.posture_palette import (
        format_posture_badge,
    )
    assert format_posture_badge() == ""


def test_format_badge_plain_text(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        format_posture_badge,
    )

    class _R:
        posture = Posture.EXPLORE

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    assert format_posture_badge(plain=True) == "🐍 EXPLORE"


def test_format_badge_rich_markup(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        format_posture_badge,
    )

    class _R:
        posture = Posture.HARDEN

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    assert (
        format_posture_badge(plain=False)
        == "[yellow]🐍 HARDEN[/yellow]"
    )


@pytest.mark.parametrize(
    "posture_value,expected_glyph",
    [
        ("EXPLORE", "🐍 EXPLORE"),
        ("CONSOLIDATE", "🐍 CONSOLIDATE"),
        ("HARDEN", "🐍 HARDEN"),
        ("MAINTAIN", "🐍 MAINTAIN"),
    ],
)
def test_format_badge_for_each_posture(
    monkeypatch, posture_value, expected_glyph,
):
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.posture_palette import (
        format_posture_badge,
    )

    class _R:
        posture = Posture(posture_value)

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    assert format_posture_badge(plain=True) == expected_glyph


# ---------------------------------------------------------------------------
# status_line lead-position integration
# ---------------------------------------------------------------------------


def test_status_line_posture_badge_lead_position(monkeypatch):
    """When master flag on + posture wired, badge appears as
    FIRST token in non-compact render."""
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )

    class _R:
        posture = Posture.EXPLORE

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="standard",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=False)
    # First token is the posture badge.
    assert rendered.startswith("🐍 EXPLORE")


def test_status_line_no_badge_when_master_off(monkeypatch):
    """Master off → badge NOT in render (pre-§38 byte-identical)."""
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", raising=False,
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=False)
    assert "🐍" not in rendered


def test_status_line_compact_mode_omits_badge(monkeypatch):
    """Compact mode preserves minimum-noise behavior — no badge."""
    monkeypatch.setenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_POSTURE_MOOD_RING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance import posture_repl
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.battle_test.status_line import (
        _format_plain, StatusSnapshot,
    )

    class _R:
        posture = Posture.EXPLORE

    class _S:
        def load_current(self):
            return _R()

    posture_repl._default_store = _S()
    snap = StatusSnapshot(
        phase="GENERATE", phase_detail="",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=15, idle_timeout_s=600,
        primary_op_id="op-019d", extra_op_count=0,
        route="standard", provider="dw",
    )
    rendered = _format_plain(snap, compact=True)
    assert "🐍" not in rendered


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def _palette_pins():
    from backend.core.ouroboros.governance.posture_palette import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _palette_source():
    return Path(
        "backend/core/ouroboros/governance/posture_palette.py"
    ).read_text()


def test_pins_register_exactly_4():
    pins = _palette_pins()
    assert len(pins) == 4


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_pin_passes_on_canonical_source(idx):
    pins = _palette_pins()
    src = _palette_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _palette_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _palette_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_store_fires_on_direct_construction():
    pins = _palette_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_store" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance import posture_repl\n"
        "from backend.core.ouroboros.governance.posture_store import PostureStore\n"
        "_default_store = PostureStore('/tmp/x')\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any(
        "PostureStore" in v for v in violations
    )


def test_pin_color_table_fires_on_missing_key():
    pins = _palette_pins()
    pin = next(
        p for p in pins
        if "color_table_exhaustive" in p.invariant_name
    )
    bad_src = (
        "def _build_color_table():\n"
        "    return {\n"
        "        'EXPLORE': 'green',\n"
        "        'CONSOLIDATE': 'blue',\n"
        # Missing HARDEN + MAINTAIN + None
        "    }\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    # Should mention missing keys + missing None.
    assert any(
        "HARDEN" in v or "MAINTAIN" in v or "None" in v
        for v in violations
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.posture_palette import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    assert n == 1
    assert reg.calls[0]["name"] == (
        "JARVIS_POSTURE_MOOD_RING_ENABLED"
    )


def test_register_flags_none_registry():
    from backend.core.ouroboros.governance.posture_palette import (
        register_flags,
    )
    assert register_flags(None) == 0

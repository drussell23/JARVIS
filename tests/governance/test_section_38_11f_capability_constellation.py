"""Section 38.11-F (PRD v2.69 to v2.70, 2026-05-08) -
capability constellation regression spine.

Final §38.11 slice. Closes the §38.11.5a.2 sequence row 6.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11f(monkeypatch):
    for var in (
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED",
        "JARVIS_CONSTELLATION_PANEL_ENABLED",
        "JARVIS_CONSTELLATION_AUTO_REFRESH_ENABLED",
        "JARVIS_CONSTELLATION_REFRESH_INTERVAL_S",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.governance import (
        capability_constellation as c,
    )
    c.reset_cache_for_tests()
    yield
    c.reset_cache_for_tests()


# ----------------------------------------------------------- Master flag


def test_master_default_false():
    from backend.core.ouroboros.governance.capability_constellation import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", value,
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        master_enabled,
    )
    assert master_enabled() is True


def test_subflags_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONSTELLATION_PANEL_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CONSTELLATION_AUTO_REFRESH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        auto_refresh_enabled, panel_enabled,
    )
    assert panel_enabled() is False
    assert auto_refresh_enabled() is False


def test_subflag_panel_default_on_when_master(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        auto_refresh_enabled, panel_enabled,
    )
    assert panel_enabled() is True
    # auto_refresh defaults FALSE — opt-in
    assert auto_refresh_enabled() is False


# ------------------------------------------------ Brightness taxonomy


def test_brightness_taxonomy_5_values():
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness,
    )
    assert {m.name for m in ConstellationBrightness} == {
        "RADIANT", "GLOWING", "DIM", "FAULTING", "DARK",
    }


def test_verdict_to_brightness_pinned_1to1():
    """The brightness map MUST be 1:1 with
    UnifiedGraduationVerdict — adding/removing either
    without parity breaks aggregation."""
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, _verdict_to_brightness,
    )
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (
        UnifiedGraduationVerdict,
    )
    expected = {
        UnifiedGraduationVerdict.READY:
            ConstellationBrightness.RADIANT,
        UnifiedGraduationVerdict.EVIDENCE_GATHERING:
            ConstellationBrightness.GLOWING,
        UnifiedGraduationVerdict.EVIDENCE_INSUFFICIENT:
            ConstellationBrightness.DIM,
        UnifiedGraduationVerdict.EVIDENCE_FAILED:
            ConstellationBrightness.FAULTING,
        UnifiedGraduationVerdict.DISABLED:
            ConstellationBrightness.DARK,
    }
    for verdict, expected_brightness in expected.items():
        assert (
            _verdict_to_brightness(verdict)
            is expected_brightness
        )


def test_verdict_to_brightness_unknown_returns_dark():
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, _verdict_to_brightness,
    )
    assert (
        _verdict_to_brightness("unknown_value")
        is ConstellationBrightness.DARK
    )
    assert (
        _verdict_to_brightness(None)
        is ConstellationBrightness.DARK
    )


# -------------------------------------------- Manifesto principle map


def test_principles_for_known_categories():
    from backend.core.ouroboros.governance.capability_constellation import (
        _principles_for_category,
    )
    # All 8 canonical categories should map to ≥1 principle
    principles_obs = _principles_for_category("observability")
    assert "Absolute observability" in principles_obs[0]

    principles_routing = _principles_for_category("routing")
    assert "Intelligence" in principles_routing[0]

    # Empty / unknown returns empty tuple
    assert _principles_for_category("") == ()
    assert _principles_for_category("nonsense") == ()


def test_all_canonical_categories_have_principles():
    """Every value in flag_registry.Category must have a
    Manifesto principle mapping."""
    from backend.core.ouroboros.governance.capability_constellation import (
        _principles_for_category,
    )
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
    )
    for cat in Category:
        principles = _principles_for_category(cat.value)
        assert len(principles) > 0, (
            f"category {cat.value} has no Manifesto "
            "principle mapping"
        )


# ---------------------------------------------------- Frozen artifacts


def test_constellation_star_to_dict():
    from backend.core.ouroboros.governance.capability_constellation import (
        CAPABILITY_CONSTELLATION_SCHEMA_VERSION,
        ConstellationBrightness, ConstellationStar,
    )
    s = ConstellationStar(
        flag_name="JARVIS_X",
        brightness=ConstellationBrightness.RADIANT,
        graduation_verdict="ready",
        category="safety",
        linked_principles=("6. Neuroplasticity",),
        diagnostic="contract OK",
    )
    d = s.to_dict()
    assert d["flag_name"] == "JARVIS_X"
    assert d["brightness"] == "radiant"
    assert d["category"] == "safety"
    assert d["linked_principles"] == ["6. Neuroplasticity"]
    assert d["schema_version"] == CAPABILITY_CONSTELLATION_SCHEMA_VERSION


def test_constellation_snapshot_to_dict():
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, ConstellationSnapshot,
        ConstellationStar,
    )
    snap = ConstellationSnapshot(
        aggregated_at_unix=1234567890.0,
        stars=(
            ConstellationStar(
                flag_name="A",
                brightness=ConstellationBrightness.RADIANT,
            ),
        ),
        by_brightness={"radiant": 1},
        by_category={"safety": 1},
        elapsed_s=0.1,
    )
    d = snap.to_dict()
    assert d["aggregated_at_unix"] == 1234567890.0
    assert len(d["stars"]) == 1
    assert d["by_brightness"] == {"radiant": 1}


def test_snapshot_stars_by_brightness_filter():
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, ConstellationSnapshot,
        ConstellationStar,
    )
    s_a = ConstellationStar(
        flag_name="A",
        brightness=ConstellationBrightness.RADIANT,
    )
    s_b = ConstellationStar(
        flag_name="B",
        brightness=ConstellationBrightness.DARK,
    )
    snap = ConstellationSnapshot(stars=(s_a, s_b))
    radiant = snap.stars_by_brightness(
        ConstellationBrightness.RADIANT,
    )
    assert len(radiant) == 1
    assert radiant[0].flag_name == "A"


# ----------------------------------------------------- Aggregator


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.capability_constellation import (
        aggregate_constellation,
    )
    snap = aggregate_constellation()
    assert snap.stars == ()


def test_aggregate_master_on_real_sources(monkeypatch):
    """Against real canonical sources — composes
    flag_registry + graduation_dashboard."""
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_FLAG_REGISTRY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        aggregate_constellation,
    )
    snap = aggregate_constellation()
    # Real repo has many flag specs; expect non-empty.
    assert len(snap.stars) > 0
    # Every star has a valid brightness.
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness,
    )
    for s in snap.stars:
        assert isinstance(s.brightness, ConstellationBrightness)


def test_aggregate_caches_snapshot(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        aggregate_constellation, get_cached_snapshot,
    )
    snap = aggregate_constellation()
    cached = get_cached_snapshot()
    assert cached is snap


# ------------------------------------------------------------ Renderer


def test_format_panel_master_off():
    from backend.core.ouroboros.governance.capability_constellation import (
        format_constellation_panel,
    )
    assert format_constellation_panel() == ""


def test_format_panel_no_stars(monkeypatch):
    """master on but snapshot empty (canonical sources
    return nothing in this test scope)."""
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationSnapshot, format_constellation_panel,
    )
    out = format_constellation_panel(
        snapshot=ConstellationSnapshot(),
    )
    assert out == ""


def test_format_panel_renders_groups(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, ConstellationSnapshot,
        ConstellationStar, format_constellation_panel,
    )
    snap = ConstellationSnapshot(
        stars=(
            ConstellationStar(
                flag_name="JARVIS_FOO",
                brightness=ConstellationBrightness.RADIANT,
                category="safety",
                linked_principles=(
                    "6. Threshold-triggered neuroplasticity",
                ),
            ),
            ConstellationStar(
                flag_name="JARVIS_BAR",
                brightness=ConstellationBrightness.DIM,
                category="observability",
                linked_principles=(
                    "7. Absolute observability",
                ),
            ),
        ),
        by_brightness={"radiant": 1, "dim": 1},
        by_category={"safety": 1, "observability": 1},
    )
    out = format_constellation_panel(snapshot=snap)
    assert "Capability constellation" in out
    assert "JARVIS_FOO" in out
    assert "JARVIS_BAR" in out
    assert "safety" in out
    assert "observability" in out
    assert "⭐" in out  # radiant glyph
    assert "·" in out  # dim glyph


def test_format_panel_only_brightness_filter(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.capability_constellation import (
        ConstellationBrightness, ConstellationSnapshot,
        ConstellationStar, format_constellation_panel,
    )
    snap = ConstellationSnapshot(
        stars=(
            ConstellationStar(
                flag_name="A",
                brightness=ConstellationBrightness.RADIANT,
                category="safety",
            ),
            ConstellationStar(
                flag_name="B",
                brightness=ConstellationBrightness.DARK,
                category="safety",
            ),
        ),
        by_brightness={"radiant": 1, "dark": 1},
    )
    out = format_constellation_panel(
        snapshot=snap,
        only_brightness=ConstellationBrightness.RADIANT,
    )
    assert "A" in out
    assert "B" not in out


# ----------------------------------------------- /constellation REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/constellation help")
    assert r.ok is True
    assert "constellation" in r.text.lower()


def test_repl_panel_master_off_blocks():
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/constellation panel")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_panel_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/constellation panel")
    assert r.ok is True


def test_repl_refresh(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/constellation refresh")
    assert r.ok is True
    assert "total stars" in r.text


def test_repl_show_unknown_flag(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command(
        "/constellation show JARVIS_NONEXISTENT_FLAG",
    )
    assert r.ok is False


def test_repl_only_invalid_brightness(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command(
        "/constellation only sparkly",
    )
    assert r.ok is False
    assert "invalid brightness" in r.text.lower()


def test_repl_only_valid_brightness(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command(
        "/constellation only dark",
    )
    assert r.ok is True


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command("/constellation status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAPABILITY_CONSTELLATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.constellation_repl import (
        dispatch_constellation_command,
    )
    r = dispatch_constellation_command(
        "/constellation gibberish",
    )
    assert r.ok is False


# --------------------------------------------------------- AST pins


def _pins():
    from backend.core.ouroboros.governance.capability_constellation import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src():
    return Path(
        "backend/core/ouroboros/governance/"
        "capability_constellation.py"
    ).read_text()


def test_pins_register_5():
    assert len(_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _pins()
    src = _src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired on canonical "
        f"source: {violations}"
    )


def test_pin_master_default_false_fires():
    pin = next(
        p for p in _pins()
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_brightness_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "brightness_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class ConstellationBrightness(str, enum.Enum):\n"
        "    RADIANT = 'radiant'\n"  # missing 4 others
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_dashboard_fires():
    pin = next(
        p for p in _pins()
        if "composes_canonical_graduation_dashboard"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_flag_registry_fires():
    pin = next(
        p for p in _pins()
        if "composes_canonical_flag_registry"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# ------------------------------------------------------- FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.capability_constellation import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 4  # master + 2 sub-flags + 1 tunable


# ---------------------------------------------- Canonical-source smokes


def test_canonical_event_constellation_updated_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED,
        _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED
        == "capability_constellation_updated"
    )
    assert (
        EVENT_TYPE_CAPABILITY_CONSTELLATION_UPDATED
        in _VALID_EVENT_TYPES
    )


def test_canonical_unified_graduation_verdict_5_values_pinned():
    """If UnifiedGraduationVerdict ever gains/loses values,
    this test fires — and the brightness mapping must
    update in lockstep."""
    from backend.core.ouroboros.governance.unified_graduation_dashboard import (
        UnifiedGraduationVerdict,
    )
    assert {m.name for m in UnifiedGraduationVerdict} == {
        "READY", "EVIDENCE_GATHERING",
        "EVIDENCE_INSUFFICIENT", "EVIDENCE_FAILED", "DISABLED",
    }


def test_canonical_flag_registry_category_8_values():
    """If flag_registry.Category gains values, the
    _CATEGORY_PRINCIPLE_MAP must extend in lockstep."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
    )
    assert len(list(Category)) == 8

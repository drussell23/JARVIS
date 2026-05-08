"""§39 Tier-2 (PRD v2.71 to v2.72, 2026-05-08) -
living organism dashboard + cognitive heatmap regression spine.

Two operator-facing surfaces composing canonical sources only:

  * #1 organism dashboard (composes ALL 8 canonical panes)
  * #3 cognitive heatmap (composes activity_radar)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tier2(monkeypatch):
    for var in (
        "JARVIS_COGNITIVE_HEATMAP_ENABLED",
        "JARVIS_COGNITIVE_HEATMAP_BAR_ENABLED",
        "JARVIS_COGNITIVE_HEATMAP_BAR_WIDTH",
        "JARVIS_ORGANISM_DASHBOARD_ENABLED",
        "JARVIS_ORGANISM_DASHBOARD_LAYOUT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ============================================ Surface #3 — heatmap


# ----- Master flag


def test_heatmap_master_default_false():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_heatmap_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_COGNITIVE_HEATMAP_ENABLED", value,
    )
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        master_enabled,
    )
    assert master_enabled() is True


def test_heatmap_subflag_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COGNITIVE_HEATMAP_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        bar_enabled,
    )
    assert bar_enabled() is False


# ----- HeatLevel taxonomy + bucketing


def test_heat_taxonomy_4_values():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatLevel,
    )
    assert {m.name for m in HeatLevel} == {
        "COLD", "COOL", "WARM", "HOT",
    }


@pytest.mark.parametrize(
    "n,expected", [
        (0, "COLD"),
        (1, "COOL"),
        (2, "WARM"),
        (5, "WARM"),
        (6, "HOT"),
        (50, "HOT"),
        (-3, "COLD"),
    ],
)
def test_heat_for_count(n, expected):
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatLevel, _heat_for_count,
    )
    assert _heat_for_count(n) is getattr(HeatLevel, expected)


def test_heat_for_count_invalid():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatLevel, _heat_for_count,
    )
    assert _heat_for_count("nan") is HeatLevel.COLD
    assert _heat_for_count(None) is HeatLevel.COLD


def test_heatlevel_coerce_lenient():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatLevel,
    )
    assert HeatLevel.coerce("hot") is HeatLevel.HOT
    assert HeatLevel.coerce("nonsense") is HeatLevel.COLD


# ----- Frozen artifacts


def test_heatcell_to_dict():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        COGNITIVE_HEATMAP_SCHEMA_VERSION, HeatCell, HeatLevel,
    )
    c = HeatCell(
        category="sensors",
        event_count=5,
        heat_level=HeatLevel.WARM,
        fill_ratio=0.5,
    )
    d = c.to_dict()
    assert d["category"] == "sensors"
    assert d["heat_level"] == "warm"
    assert d["fill_ratio"] == 0.5
    assert d["schema_version"] == COGNITIVE_HEATMAP_SCHEMA_VERSION


def test_heatmap_snapshot_to_dict_and_lookup():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatCell, HeatLevel, HeatmapSnapshot,
    )
    snap = HeatmapSnapshot(
        aggregated_at_unix=1234.0,
        cells=(
            HeatCell(
                category="sensors", event_count=3,
                heat_level=HeatLevel.WARM, fill_ratio=0.6,
            ),
        ),
    )
    d = snap.to_dict()
    assert d["aggregated_at_unix"] == 1234.0
    assert len(d["cells"]) == 1
    assert snap.cell_for_category("sensors") is not None
    assert snap.cell_for_category("nonexistent") is None


# ----- Aggregator


def test_heatmap_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        aggregate_heatmap,
    )
    snap = aggregate_heatmap()
    assert snap.cells == ()


def test_heatmap_aggregate_master_on_real_canonical(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COGNITIVE_HEATMAP_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ACTIVITY_RADAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        aggregate_heatmap,
    )
    from backend.core.ouroboros.governance.activity_radar import (
        ActivityCategory,
    )
    snap = aggregate_heatmap()
    # One cell per canonical ActivityCategory.
    assert len(snap.cells) == len(list(ActivityCategory))
    seen_cats = {c.category for c in snap.cells}
    expected_cats = {c.value for c in ActivityCategory}
    assert seen_cats == expected_cats


# ----- Renderer


def test_heatmap_format_master_off():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        format_heatmap_panel,
    )
    assert format_heatmap_panel() == ""


def test_heatmap_format_with_explicit_snapshot(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_COGNITIVE_HEATMAP_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        HeatCell, HeatLevel, HeatmapSnapshot,
        format_heatmap_panel,
    )
    snap = HeatmapSnapshot(
        total_events=10,
        window_s=60.0,
        cells=(
            HeatCell(
                category="sensors", event_count=6,
                heat_level=HeatLevel.HOT, fill_ratio=0.6,
            ),
            HeatCell(
                category="bridges", event_count=2,
                heat_level=HeatLevel.WARM, fill_ratio=0.2,
            ),
        ),
    )
    out = format_heatmap_panel(snapshot=snap)
    assert "Cognitive heatmap" in out
    assert "sensors" in out
    assert "bridges" in out


# ----- Heatmap AST pins


def _heatmap_pins():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _heatmap_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "cognitive_heatmap.py"
    ).read_text()


def test_heatmap_pins_register_4():
    assert len(_heatmap_pins()) == 4


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_heatmap_pin_passes_canonical(idx):
    pins = _heatmap_pins()
    src = _heatmap_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_heatmap_pin_master_default_false_fires():
    pin = next(
        p for p in _heatmap_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_heatmap_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _heatmap_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_heatmap_pin_taxonomy_fires():
    pin = next(
        p for p in _heatmap_pins()
        if "heat_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class HeatLevel(str, enum.Enum):\n"
        "    COLD = 'cold'\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_heatmap_pin_composes_activity_radar_fires():
    pin = next(
        p for p in _heatmap_pins()
        if "composes_canonical_activity_radar"
        in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_heatmap_register_flags_count():
    from backend.core.ouroboros.governance.cognitive_heatmap import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 3


# ============================================ Surface #1 — dashboard


# ----- Master flag


def test_dashboard_master_default_false():
    from backend.core.ouroboros.governance.organism_dashboard import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_dashboard_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", value,
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        master_enabled,
    )
    assert master_enabled() is True


# ----- DashboardPane taxonomy


def test_pane_taxonomy_8_values():
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane,
    )
    assert {m.name for m in DashboardPane} == {
        "ALIVE", "ACTIVITY_RADAR", "FANOUT",
        "GRADUATION", "POSTURE", "PHASE_RIBBON",
        "HEATMAP", "CONSTELLATION",
    }


def test_pane_coerce_lenient():
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane,
    )
    assert DashboardPane.coerce("alive") is DashboardPane.ALIVE
    assert DashboardPane.coerce("nonsense") is None
    assert DashboardPane.coerce(None) is None


# ----- Composer dispatch completeness


def test_pane_composers_cover_all_panes():
    """Critical regression — every DashboardPane MUST be
    keyed in _PANE_COMPOSERS dispatch dict."""
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, _PANE_COMPOSERS,
    )
    for p in DashboardPane:
        assert p in _PANE_COMPOSERS, (
            f"DashboardPane.{p.name} missing from "
            "_PANE_COMPOSERS dispatch"
        )


# ----- Aggregator


def test_dashboard_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.organism_dashboard import (
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    assert snap.rendered_panes == {}


def test_dashboard_aggregate_master_on_renders_panes(monkeypatch):
    """End-to-end against canonical sources. Some panes
    will be empty (canonical sub-flags not all set), but
    the snapshot must enumerate all 8 panes in the
    `panes` field."""
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    assert len(snap.panes) == 8
    assert snap.layout in ("stacked", "compact")
    assert snap.elapsed_s >= 0.0
    # All panes are valid enum values
    for p in snap.panes:
        assert isinstance(p, DashboardPane)


def test_dashboard_aggregate_with_explicit_panes(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        DashboardPane, aggregate_dashboard,
    )
    snap = aggregate_dashboard(
        panes=(DashboardPane.ALIVE,),
    )
    assert snap.panes == (DashboardPane.ALIVE,)


def test_dashboard_layout_env(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_LAYOUT", "compact",
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    assert snap.layout == "compact"


def test_dashboard_layout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_LAYOUT", "garbage",
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        aggregate_dashboard,
    )
    snap = aggregate_dashboard()
    assert snap.layout == "stacked"  # default


# ----- Renderer


def test_dashboard_format_master_off():
    from backend.core.ouroboros.governance.organism_dashboard import (
        format_organism_dashboard,
    )
    assert format_organism_dashboard() == ""


def test_dashboard_format_master_on_renders(monkeypatch):
    """Compose all canonical surfaces — at least ALIVE
    pane should render (organism_status sub-flags master-on
    by transitive chain)."""
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_HEARTBEAT_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ORGANISM_STATUS_RISK_LIGHT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.organism_dashboard import (
        format_organism_dashboard,
    )
    out = format_organism_dashboard()
    assert "ORGANISM DASHBOARD" in out


# ----- /dashboard REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard help")
    assert r.ok is True


def test_repl_list_bypasses_master(monkeypatch):
    """`list` is a discoverability surface — operator-
    important to enumerate panes even when master off."""
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard list")
    assert r.ok is True
    assert "alive" in r.text
    assert "constellation" in r.text


def test_repl_show_master_off_blocks():
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard show")
    assert r.ok is False


def test_repl_show_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard show")
    assert r.ok is True


def test_repl_pane_unknown(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard pane bogus")
    assert r.ok is False


def test_repl_pane_known(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard pane heatmap")
    assert r.ok is True


def test_repl_compact(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard compact")
    assert r.ok is True


def test_repl_show_with_panes(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command(
        "/dashboard show alive heatmap",
    )
    assert r.ok is True


def test_repl_show_with_unknown_pane_fails(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command(
        "/dashboard show alive sparkly",
    )
    assert r.ok is False


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ORGANISM_DASHBOARD_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.dashboard_repl import (
        dispatch_dashboard_command,
    )
    r = dispatch_dashboard_command("/dashboard gibberish")
    assert r.ok is False


# ----- Dashboard AST pins


def _dashboard_pins():
    from backend.core.ouroboros.governance.organism_dashboard import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _dashboard_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "organism_dashboard.py"
    ).read_text()


def test_dashboard_pins_register_5():
    assert len(_dashboard_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_dashboard_pin_passes_canonical(idx):
    pins = _dashboard_pins()
    src = _dashboard_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_dashboard_pin_master_default_false_fires():
    pin = next(
        p for p in _dashboard_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_dashboard_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _dashboard_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_dashboard_pin_pane_taxonomy_fires():
    pin = next(
        p for p in _dashboard_pins()
        if "pane_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class DashboardPane(str, enum.Enum):\n"
        "    ALIVE = 'alive'\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_dashboard_pin_composes_all_panes_fires():
    pin = next(
        p for p in _dashboard_pins()
        if "composes_all_canonical_panes" in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_dashboard_pin_pane_composer_completeness_fires():
    """Synthetic: drop a pane key from the source — the pin
    must fire."""
    pin = next(
        p for p in _dashboard_pins()
        if "pane_composer_completeness" in p.invariant_name
    )
    bad = "x = 1\n"  # no DashboardPane references at all
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_dashboard_register_flags_count():
    from backend.core.ouroboros.governance.organism_dashboard import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 2


# ----- Canonical-source smokes


def test_canonical_event_dashboard_rendered_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_DASHBOARD_RENDERED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_DASHBOARD_RENDERED == "dashboard_rendered"
    assert EVENT_TYPE_DASHBOARD_RENDERED in _VALID_EVENT_TYPES


def test_canonical_activity_radar_5_categories():
    """Lockstep regression — heatmap maps 1:1 to
    ActivityCategory's 5 values."""
    from backend.core.ouroboros.governance.activity_radar import (
        ActivityCategory,
    )
    assert len(list(ActivityCategory)) == 5

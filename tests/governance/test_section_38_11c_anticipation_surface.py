"""Section 38.11-C (PRD v2.66 to v2.67, 2026-05-08) -
proactive intervention banners + anticipatory pre-fetch
indicator regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11c(monkeypatch):
    for var in (
        "JARVIS_ANTICIPATION_SURFACE_ENABLED",
        "JARVIS_ANTICIPATION_BANNERS_ENABLED",
        "JARVIS_ANTICIPATION_PREFETCH_ENABLED",
        "JARVIS_ANTICIPATION_BANNER_RING_SIZE",
        "JARVIS_ANTICIPATION_PREFETCH_RING_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.governance import (
        anticipation_surface as a,
    )
    a.reset_surface_for_tests()
    yield
    a.reset_surface_for_tests()


# ------------------------------------------------------------- Master flag


def test_master_default_false():
    from backend.core.ouroboros.governance.anticipation_surface import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", value,
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        master_enabled,
    )
    assert master_enabled() is True


def test_subflags_master_off_force_off(monkeypatch):
    """Sub-flags can't be on when master is off."""
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_BANNERS_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_PREFETCH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        banners_enabled, prefetch_enabled,
    )
    assert banners_enabled() is False
    assert prefetch_enabled() is False


def test_subflags_default_on_when_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        banners_enabled, prefetch_enabled,
    )
    assert banners_enabled() is True
    assert prefetch_enabled() is True


# ----------------------------------------------------- Closed taxonomies


def test_banner_kind_taxonomy_4_values():
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind,
    )
    assert {m.name for m in BannerKind} == {
        "SENSOR_INTERVENTION",
        "PROACTIVE_CURIOSITY",
        "CAPABILITY_GAP",
        "OPPORTUNITY",
    }


def test_prefetch_kind_taxonomy_5_values():
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchKind,
    )
    assert {m.name for m in PrefetchKind} == {
        "READ_FILE", "SEARCH_CODE", "GET_CALLERS",
        "GLOB_FILES", "OTHER",
    }


def test_banner_kind_coerce_lenient():
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind,
    )
    assert (
        BannerKind.coerce("opportunity")
        is BannerKind.OPPORTUNITY
    )
    assert (
        BannerKind.coerce("nonsense")
        is BannerKind.SENSOR_INTERVENTION  # default
    )
    assert BannerKind.coerce(None) is BannerKind.SENSOR_INTERVENTION


def test_prefetch_kind_coerce_lenient():
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchKind,
    )
    assert (
        PrefetchKind.coerce("read_file")
        is PrefetchKind.READ_FILE
    )
    assert PrefetchKind.coerce("nonsense") is PrefetchKind.OTHER


# --------------------------------------------------- Versioned artifacts


def test_intervention_banner_event_to_dict():
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, InterventionBannerEvent,
        ANTICIPATION_SURFACE_SCHEMA_VERSION,
    )
    ev = InterventionBannerEvent(
        banner_kind=BannerKind.OPPORTUNITY,
        signal_source="OpportunityMinerSensor",
        summary="found stale TODOs",
        op_id="op-1",
        risk_tier_label="SAFE_AUTO",
    )
    d = ev.to_dict()
    assert d["banner_kind"] == "opportunity"
    assert d["signal_source"] == "OpportunityMinerSensor"
    assert d["summary"] == "found stale TODOs"
    assert d["schema_version"] == ANTICIPATION_SURFACE_SCHEMA_VERSION


def test_prefetch_event_to_dict():
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchEvent, PrefetchKind,
        ANTICIPATION_SURFACE_SCHEMA_VERSION,
    )
    ev = PrefetchEvent(
        op_id="op-1",
        prefetch_kind=PrefetchKind.READ_FILE,
        tool_name="read_file",
        arg_summary="orchestrator.py",
    )
    d = ev.to_dict()
    assert d["prefetch_kind"] == "read_file"
    assert d["tool_name"] == "read_file"
    assert d["schema_version"] == ANTICIPATION_SURFACE_SCHEMA_VERSION


# ---------------------------------------------------------- Surface API


def test_emit_banner_master_off_returns_false():
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, emit_banner,
    )
    assert emit_banner(
        banner_kind=BannerKind.OPPORTUNITY,
        summary="x",
    ) is False


def test_emit_banner_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, emit_banner, get_default_surface,
    )
    ok = emit_banner(
        banner_kind=BannerKind.OPPORTUNITY,
        summary="x",
        signal_source="TestSensor",
    )
    assert ok is True
    banners = get_default_surface().recent_banners(limit=10)
    assert len(banners) == 1
    assert banners[0].banner_kind is BannerKind.OPPORTUNITY


def test_emit_prefetch_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchKind, emit_prefetch, get_default_surface,
    )
    ok = emit_prefetch(
        op_id="op-1",
        prefetch_kind=PrefetchKind.READ_FILE,
        tool_name="read_file",
        arg_summary="foo.py",
    )
    assert ok is True
    prefetches = get_default_surface().recent_prefetches(
        limit=10,
    )
    assert len(prefetches) == 1


def test_subflag_disables_banners(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_BANNERS_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, emit_banner,
    )
    assert emit_banner(
        banner_kind=BannerKind.OPPORTUNITY,
        summary="x",
    ) is False


def test_subflag_disables_prefetch(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_PREFETCH_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchKind, emit_prefetch,
    )
    assert emit_prefetch(
        op_id="op-1",
        prefetch_kind=PrefetchKind.READ_FILE,
    ) is False


def test_ring_bounded_drops_oldest(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_BANNER_RING_SIZE", "5",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, emit_banner, reset_surface_for_tests,
        get_default_surface,
    )
    reset_surface_for_tests()
    for i in range(10):
        emit_banner(
            banner_kind=BannerKind.OPPORTUNITY,
            summary=f"banner-{i}",
        )
    banners = get_default_surface().recent_banners(limit=64)
    assert len(banners) == 5
    # oldest dropped — first survivor is banner-5
    assert banners[0].summary == "banner-5"


def test_singleton(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        get_default_surface,
    )
    s1 = get_default_surface()
    s2 = get_default_surface()
    assert s1 is s2


# ------------------------------------------------------------ Renderers


def test_format_banner_panel_master_off():
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_intervention_banner_panel,
    )
    assert format_intervention_banner_panel() == ""


def test_format_banner_panel_no_banners(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_intervention_banner_panel,
    )
    assert format_intervention_banner_panel() == ""


def test_format_banner_panel_renders_glyphs(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        BannerKind, InterventionBannerEvent,
        format_intervention_banner_panel,
    )
    banners = (
        InterventionBannerEvent(
            banner_kind=BannerKind.OPPORTUNITY,
            summary="found todos",
            signal_source="OppSensor",
            risk_tier_label="SAFE_AUTO",
        ),
        InterventionBannerEvent(
            banner_kind=BannerKind.CAPABILITY_GAP,
            summary="missing telemetry",
        ),
    )
    out = format_intervention_banner_panel(
        banners=banners,
    )
    assert "Recently queued by autonomy" in out
    assert "found todos" in out
    assert "missing telemetry" in out
    assert "💡" in out  # opportunity glyph
    assert "🧩" in out  # capability gap glyph


def test_format_prefetch_master_off():
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_prefetch_indicator,
    )
    assert format_prefetch_indicator() == ""


def test_format_prefetch_renders_glyphs(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        PrefetchEvent, PrefetchKind,
        format_prefetch_indicator,
    )
    prefetches = (
        PrefetchEvent(
            op_id="op-1",
            prefetch_kind=PrefetchKind.READ_FILE,
            tool_name="read_file",
            arg_summary="x.py",
        ),
        PrefetchEvent(
            op_id="op-1",
            prefetch_kind=PrefetchKind.SEARCH_CODE,
            tool_name="search_code",
            arg_summary="foo",
        ),
    )
    out = format_prefetch_indicator(
        prefetches=prefetches,
    )
    assert "Pre-fetching" in out
    assert "read_file x.py" in out
    assert "📄" in out
    assert "🔍" in out


def test_format_anticipation_panel_master_off():
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_anticipation_panel,
    )
    assert format_anticipation_panel() == ""


def test_format_anticipation_panel_master_on_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipation_surface import (
        format_anticipation_panel,
    )
    assert format_anticipation_panel() == ""


# ------------------------------------------------- INTENT prose lookup


def test_lookup_intent_prose_empty_op_id():
    from backend.core.ouroboros.governance.anticipation_surface import (
        lookup_intent_prose,
    )
    assert lookup_intent_prose(op_id="") == ""


def test_lookup_intent_prose_no_frame():
    from backend.core.ouroboros.governance.anticipation_surface import (
        lookup_intent_prose,
    )
    # Real call against canonical channel — should return empty
    # since no INTENT frame exists for this op_id.
    assert lookup_intent_prose(op_id="nonexistent-op") == ""


# ------------------------------------------------------------- /anticipate REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate help")
    assert r.ok is True
    assert "anticipation" in r.text.lower()


def test_repl_panel_master_off_blocks():
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate panel")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_panel_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate panel")
    assert r.ok is True


def test_repl_banners(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate banners 5")
    assert r.ok is True


def test_repl_prefetch(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate prefetch")
    assert r.ok is True


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_ANTICIPATION_SURFACE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.anticipate_repl import (
        dispatch_anticipate_command,
    )
    r = dispatch_anticipate_command("/anticipate gibberish")
    assert r.ok is False


# -------------------------------------------------------------- AST pins


def _pins():
    from backend.core.ouroboros.governance.anticipation_surface import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src():
    return Path(
        "backend/core/ouroboros/governance/"
        "anticipation_surface.py"
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


def test_pin_master_default_false_fires_on_premature_flip():
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


def test_pin_banner_taxonomy_fires_on_missing():
    pin = next(
        p for p in _pins()
        if "banner_kind_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class BannerKind(str, enum.Enum):\n"
        "    SENSOR_INTERVENTION = 'sensor_intervention'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_prefetch_taxonomy_fires_on_missing():
    pin = next(
        p for p in _pins()
        if "prefetch_kind_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class PrefetchKind(str, enum.Enum):\n"
        "    READ_FILE = 'read_file'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_narrative_channel_fires():
    pin = next(
        p for p in _pins()
        if "composes_canonical_narrative_channel"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# --------------------------------------------------------- FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.anticipation_surface import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 5  # master + 2 sub-flags + 2 ring sizes


# ----------------------------------------------- Canonical-source smokes


def test_canonical_narrative_channel_importable():
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind, get_default_channel,
    )
    assert NarrativeKind is not None
    assert callable(get_default_channel)


def test_canonical_event_types_registered():
    """The two new SSE event types MUST be in canonical
    _VALID_EVENT_TYPES frozenset."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_INTERVENTION_BANNER_RAISED,
        EVENT_TYPE_PREFETCH_SCHEDULED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_INTERVENTION_BANNER_RAISED in _VALID_EVENT_TYPES
    assert EVENT_TYPE_PREFETCH_SCHEDULED in _VALID_EVENT_TYPES
    assert (
        EVENT_TYPE_INTERVENTION_BANNER_RAISED
        == "intervention_banner_raised"
    )
    assert (
        EVENT_TYPE_PREFETCH_SCHEDULED == "prefetch_scheduled"
    )

"""§39 Tier-1 (PRD v2.70 to v2.71, 2026-05-08) -
risk-tier ambient tint + phase-flow ribbon regression spine.

Two operator-facing surfaces composing canonical sources only:

  * #2  risk-tier tint  (composes canonical organism_status)
  * #14 phase-flow ribbon (composes canonical pipeline_progress)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tier1(monkeypatch):
    for var in (
        "JARVIS_RISK_TIER_TINT_ENABLED",
        "JARVIS_RISK_TIER_TINT_PROMPT_ENABLED",
        "JARVIS_RISK_TIER_TINT_OUTPUT_ENABLED",
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED",
        "JARVIS_PHASE_FLOW_RIBBON_DENSITY_ENABLED",
        "JARVIS_PHASE_FLOW_RIBBON_ANIMATION_ENABLED",
        "JARVIS_PHASE_FLOW_RIBBON_WINDOW_S",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.governance import (
        phase_flow_ribbon as r,
    )
    r.reset_cache_for_tests()
    yield
    r.reset_cache_for_tests()


# ============================================ Surface #2 — risk-tier tint


# ----- Master flag


def test_tint_master_default_false():
    from backend.core.ouroboros.governance.risk_tier_tint import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_tint_master_truthy(monkeypatch, value):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", value)
    from backend.core.ouroboros.governance.risk_tier_tint import (
        master_enabled,
    )
    assert master_enabled() is True


def test_tint_subflags_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_RISK_TIER_TINT_PROMPT_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_RISK_TIER_TINT_OUTPUT_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.risk_tier_tint import (
        output_tint_enabled, prompt_tint_enabled,
    )
    assert prompt_tint_enabled() is False
    assert output_tint_enabled() is False


def test_tint_subflags_default_on_when_master(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        output_tint_enabled, prompt_tint_enabled,
    )
    assert prompt_tint_enabled() is True
    assert output_tint_enabled() is True


# ----- Canonical extension landed


def test_canonical_rich_color_for_light_accessor():
    """The §39 Tier-1 slice extends canonical organism_status
    with a public rich_color_for_light accessor. ALL 4 risk
    lights must map to a non-empty Rich color."""
    from backend.core.ouroboros.governance.organism_status import (
        RiskTierLight, rich_color_for_light,
    )
    for light in RiskTierLight:
        color = rich_color_for_light(light)
        assert isinstance(color, str)
        assert len(color) > 0


# ----- Tint helpers


def test_apply_ambient_tint_master_off_passthrough():
    from backend.core.ouroboros.governance.risk_tier_tint import (
        apply_ambient_tint,
    )
    assert apply_ambient_tint("hello") == "hello"


def test_apply_ambient_tint_master_on_wraps(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        apply_ambient_tint,
    )
    out = apply_ambient_tint("hello")
    assert out.startswith("[")
    assert "hello" in out
    assert out.endswith("[/]")


def test_apply_ambient_tint_explicit_color(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        apply_ambient_tint,
    )
    out = apply_ambient_tint("hi", color="red")
    assert out == "[red]hi[/]"


def test_apply_ambient_tint_with_style(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        apply_ambient_tint,
    )
    out = apply_ambient_tint("warning", color="yellow", style="bold")
    assert "yellow bold" in out


def test_apply_ambient_tint_none_input(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        apply_ambient_tint,
    )
    assert apply_ambient_tint(None) == ""


def test_tint_prompt_marker_master_off():
    from backend.core.ouroboros.governance.risk_tier_tint import (
        tint_prompt_marker,
    )
    assert tint_prompt_marker() == ""


def test_tint_prompt_marker_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    from backend.core.ouroboros.governance.risk_tier_tint import (
        tint_prompt_marker,
    )
    out = tint_prompt_marker()
    assert "▸" in out


def test_tint_output_subflag_off(monkeypatch):
    """Output tint is sub-flag-gated separately."""
    monkeypatch.setenv("JARVIS_RISK_TIER_TINT_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_RISK_TIER_TINT_OUTPUT_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.risk_tier_tint import (
        tint_output,
    )
    assert tint_output("plain") == "plain"


# ----- AST pins for tint


def _tint_pins():
    from backend.core.ouroboros.governance.risk_tier_tint import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _tint_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "risk_tier_tint.py"
    ).read_text()


def test_tint_pins_register_3():
    assert len(_tint_pins()) == 3


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_tint_pin_passes_canonical(idx):
    pins = _tint_pins()
    src = _tint_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_tint_pin_master_default_false_fires():
    pin = next(
        p for p in _tint_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_tint_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _tint_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_tint_pin_composes_canonical_organism_status_fires():
    pin = next(
        p for p in _tint_pins()
        if "composes_canonical_organism_status"
        in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_tint_register_flags_count():
    from backend.core.ouroboros.governance.risk_tier_tint import (
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


# ============================================ Surface #14 — phase-flow ribbon


# ----- Master flag


def test_ribbon_master_default_false():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_ribbon_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", value,
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        master_enabled,
    )
    assert master_enabled() is True


def test_ribbon_subflags_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_DENSITY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ANIMATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        animation_enabled, density_enabled,
    )
    assert density_enabled() is False
    assert animation_enabled() is False


def test_ribbon_subflags_default_on_when_master(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        animation_enabled, density_enabled,
    )
    assert density_enabled() is True
    assert animation_enabled() is True


# ----- DensityLevel taxonomy


def test_density_taxonomy_5_values():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel,
    )
    assert {m.name for m in DensityLevel} == {
        "IDLE", "LIGHT", "STEADY", "HEAVY", "SATURATED",
    }


@pytest.mark.parametrize(
    "n,expected", [
        (0, "IDLE"),
        (1, "LIGHT"),
        (2, "STEADY"),
        (3, "STEADY"),
        (4, "HEAVY"),
        (7, "HEAVY"),
        (8, "SATURATED"),
        (100, "SATURATED"),
        (-5, "IDLE"),
    ],
)
def test_density_for_count(n, expected):
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, _density_for_count,
    )
    assert (
        _density_for_count(n) is getattr(DensityLevel, expected)
    )


def test_density_for_count_invalid():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, _density_for_count,
    )
    assert _density_for_count("not a number") is DensityLevel.IDLE
    assert _density_for_count(None) is DensityLevel.IDLE


# ----- Frozen artifacts


def test_phase_flow_cell_to_dict():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, PHASE_FLOW_RIBBON_SCHEMA_VERSION,
        PhaseFlowCell,
    )
    c = PhaseFlowCell(
        phase_name="GENERATE",
        forward_flow_index=4,
        charge_count=5,
        density_level=DensityLevel.HEAVY,
        is_active=True,
    )
    d = c.to_dict()
    assert d["phase_name"] == "GENERATE"
    assert d["density_level"] == "heavy"
    assert d["is_active"] is True
    assert d["schema_version"] == PHASE_FLOW_RIBBON_SCHEMA_VERSION


def test_phase_flow_snapshot_to_dict():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, PhaseFlowCell, PhaseFlowSnapshot,
    )
    snap = PhaseFlowSnapshot(
        aggregated_at_unix=1234.0,
        window_s=60,
        cells=(
            PhaseFlowCell(
                phase_name="A", forward_flow_index=0,
                density_level=DensityLevel.IDLE,
            ),
        ),
        active_phase_name="A",
        by_density={"idle": 1},
    )
    d = snap.to_dict()
    assert d["aggregated_at_unix"] == 1234.0
    assert len(d["cells"]) == 1
    assert d["active_phase_name"] == "A"


def test_snapshot_cell_for_phase():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel, PhaseFlowCell, PhaseFlowSnapshot,
    )
    snap = PhaseFlowSnapshot(
        cells=(
            PhaseFlowCell(
                phase_name="GENERATE", forward_flow_index=4,
                density_level=DensityLevel.IDLE,
            ),
        ),
    )
    assert snap.cell_for_phase("GENERATE") is not None
    assert snap.cell_for_phase("NONEXISTENT") is None


# ----- Aggregator


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        aggregate_phase_flow,
    )
    snap = aggregate_phase_flow()
    assert snap.cells == ()


def test_aggregate_master_on_real_canonical_sources(monkeypatch):
    """End-to-end against canonical pipeline_progress.
    Forward-flow tuple is 11 phases."""
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        aggregate_phase_flow,
    )
    snap = aggregate_phase_flow(
        active_phase="GENERATE",
        phase_charges={
            "GENERATE": 5, "VALIDATE": 2, "VERIFY": 8,
        },
    )
    # Canonical 11-phase forward-flow.
    assert len(snap.cells) == 11
    # Active-phase index correct.
    active_cells = [c for c in snap.cells if c.is_active]
    assert len(active_cells) == 1
    assert active_cells[0].phase_name == "GENERATE"
    # Density propagated correctly.
    gen = snap.cell_for_phase("GENERATE")
    assert gen.charge_count == 5
    # density bucket for 5 = HEAVY
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        DensityLevel,
    )
    assert gen.density_level is DensityLevel.HEAVY
    verify = snap.cell_for_phase("VERIFY")
    assert verify.density_level is DensityLevel.SATURATED


def test_aggregate_caches_snapshot(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        aggregate_phase_flow, get_cached_snapshot,
    )
    snap = aggregate_phase_flow()
    cached = get_cached_snapshot()
    assert cached is snap


def test_aggregate_window_clamped(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        aggregate_phase_flow,
    )
    snap = aggregate_phase_flow(window_s=999999)
    assert snap.window_s == 600  # clamped to MAX
    snap2 = aggregate_phase_flow(window_s=1)
    assert snap2.window_s == 5  # clamped to MIN


# ----- Renderer


def test_format_master_off():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        format_phase_flow_ribbon,
    )
    assert format_phase_flow_ribbon() == ""


def test_format_compact_renders_cells(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        format_phase_flow_ribbon,
    )
    out = format_phase_flow_ribbon(
        active_phase="GENERATE",
        phase_charges={"GENERATE": 5},
        compact=True,
    )
    # Compact uses ─ separator
    assert "─" in out
    # Active highlight
    assert "bold green" in out


def test_format_expanded_has_phase_labels(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        format_phase_flow_ribbon,
    )
    out = format_phase_flow_ribbon(
        active_phase="GENERATE",
        compact=False,
    )
    # Phase labels present
    assert "CLASSIFY" in out
    assert "GENERATE" in out
    assert "VERIFY" in out
    assert "COMPLETE" in out


def test_format_no_active_phase(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        format_phase_flow_ribbon,
    )
    # No active_phase; should still render (no highlight)
    out = format_phase_flow_ribbon(compact=True)
    assert out  # non-empty
    # No 'bold green' since no active phase
    assert "bold green" not in out


# ----- /ribbon REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon help")
    assert r.ok is True
    assert "ribbon" in r.text.lower()


def test_repl_show_master_off_blocks():
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon show")
    assert r.ok is False


def test_repl_show_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon show GENERATE")
    assert r.ok is True


def test_repl_expand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon expand GENERATE")
    assert r.ok is True


def test_repl_refresh(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon refresh")
    assert r.ok is True


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_FLOW_RIBBON_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ribbon_repl import (
        dispatch_ribbon_command,
    )
    r = dispatch_ribbon_command("/ribbon gibberish")
    assert r.ok is False


# ----- Ribbon AST pins


def _ribbon_pins():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _ribbon_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "phase_flow_ribbon.py"
    ).read_text()


def test_ribbon_pins_register_5():
    assert len(_ribbon_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_ribbon_pin_passes_canonical(idx):
    pins = _ribbon_pins()
    src = _ribbon_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_ribbon_pin_master_default_false_fires():
    pin = next(
        p for p in _ribbon_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_ribbon_pin_authority_asymmetry_fires():
    pin = next(
        p for p in _ribbon_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_ribbon_pin_density_taxonomy_fires():
    pin = next(
        p for p in _ribbon_pins()
        if "density_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class DensityLevel(str, enum.Enum):\n"
        "    IDLE = 'idle'\n"
    )
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_ribbon_pin_composes_pipeline_progress_fires():
    pin = next(
        p for p in _ribbon_pins()
        if "composes_canonical_pipeline_progress"
        in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_ribbon_pin_animation_frames_fires():
    pin = next(
        p for p in _ribbon_pins()
        if "animation_frames_canonical" in p.invariant_name
    )
    bad = "x = 1\n"
    violations = pin.validate(ast.parse(bad), bad)
    assert violations


def test_ribbon_register_flags_count():
    from backend.core.ouroboros.governance.phase_flow_ribbon import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 4


# ----- Canonical-source smokes


def test_canonical_event_phase_flow_updated_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PHASE_FLOW_UPDATED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_PHASE_FLOW_UPDATED == "phase_flow_updated"
    assert EVENT_TYPE_PHASE_FLOW_UPDATED in _VALID_EVENT_TYPES


def test_canonical_pipeline_progress_forward_flow_11_phases():
    """The §39 Tier-1 #14 ribbon assumes canonical
    forward-flow is 11 phases. This is a lockstep regression."""
    from backend.core.ouroboros.governance.pipeline_progress import (
        forward_flow_length, forward_flow_phases,
    )
    # Force resolution.
    flow = forward_flow_phases()
    assert len(flow) == forward_flow_length()
    assert len(flow) == 11


def test_canonical_organism_status_rich_color_for_light_callable():
    from backend.core.ouroboros.governance.organism_status import (
        rich_color_for_light,
    )
    assert callable(rich_color_for_light)

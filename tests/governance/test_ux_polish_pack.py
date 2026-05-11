"""Regression spine for §40 UX Polish Pack composer substrate.

Covers:

* §33.1 master flag default-FALSE
* Closed 15-value :class:`PolishSubstrate` taxonomy
* :data:`_SUBSTRATE_REGISTRY` covers all 15 enum values exactly
* :func:`is_substrate_in_active_pack` predicate semantics:
  pack-off → False; pack-on + sub-flag unset → True;
  pack-on + sub-flag explicit-false → False (operator veto);
  pack-on + sub-flag explicit-true → True (substrate self-on)
* Every wired substrate's ``master_enabled()`` correctly composes
  the pack predicate (parametrized × 15)
* Every wired substrate's existing AST pins (master_default_false
  + authority_asymmetry) STILL PASS after the wiring edit
* §33.5 frozen :class:`PolishPackReport` projection
* 5 AST pin canonical-source pass + 4 synthetic regressions
* FlagRegistry seed auto-discovered
* Operator-facing renderer
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import ux_polish_pack as pack
from backend.core.ouroboros.governance.ux_polish_pack import (
    PolishPackReport,
    PolishSubstrate,
    PolishSubstrateState,
    UX_POLISH_PACK_SCHEMA_VERSION,
    _ENV_MASTER,
    _SUBSTRATE_REGISTRY,
    composed_substrates,
    format_polish_panel,
    is_substrate_in_active_pack,
    pack_master_enabled,
    polish_status,
)


# Pre-imported here so the test isolation fixture can clear their
# env vars cleanly. Each substrate's master_enabled() is the
# composition target.
from backend.core.ouroboros.governance import (
    activity_radar,
    cognitive_heatmap,
    memory_crystallization,
    op_fanout_tree,
    op_trajectory_predictor,
    organism_dashboard,
    phase_flow_ribbon,
    pipeline_progress,
    polish_bundle,
    posture_palette,
    risk_command_preview,
    risk_tier_tint,
    session_story,
    task_panel_aggregator,
    thinking_progress_aggregator,
)


SUBSTRATE_MODULES = {
    "polish_bundle": polish_bundle,
    "thinking_progress_aggregator": thinking_progress_aggregator,
    "task_panel_aggregator": task_panel_aggregator,
    "posture_palette": posture_palette,
    "pipeline_progress": pipeline_progress,
    "activity_radar": activity_radar,
    "op_fanout_tree": op_fanout_tree,
    "phase_flow_ribbon": phase_flow_ribbon,
    "risk_tier_tint": risk_tier_tint,
    "organism_dashboard": organism_dashboard,
    "cognitive_heatmap": cognitive_heatmap,
    "op_trajectory_predictor": op_trajectory_predictor,
    "risk_command_preview": risk_command_preview,
    "session_story": session_story,
    "memory_crystallization": memory_crystallization,
}


SUBSTRATE_ENV_VARS = {
    "polish_bundle": "JARVIS_POLISH_BUNDLE_ENABLED",
    "thinking_progress_aggregator": "JARVIS_THINKING_PROGRESS_ENABLED",
    "task_panel_aggregator": "JARVIS_TASK_PANEL_ENABLED",
    "posture_palette": "JARVIS_POSTURE_MOOD_RING_ENABLED",
    "pipeline_progress": "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED",
    "activity_radar": "JARVIS_ACTIVITY_RADAR_ENABLED",
    "op_fanout_tree": "JARVIS_OP_FANOUT_TREE_ENABLED",
    "phase_flow_ribbon": "JARVIS_PHASE_FLOW_RIBBON_ENABLED",
    "risk_tier_tint": "JARVIS_RISK_TIER_TINT_ENABLED",
    "organism_dashboard": "JARVIS_ORGANISM_DASHBOARD_ENABLED",
    "cognitive_heatmap": "JARVIS_COGNITIVE_HEATMAP_ENABLED",
    "op_trajectory_predictor": "JARVIS_OP_TRAJECTORY_PREDICTOR_ENABLED",
    "risk_command_preview": "JARVIS_RISK_COMMAND_PREVIEW_ENABLED",
    "session_story": "JARVIS_SESSION_STORY_ENABLED",
    "memory_crystallization": "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED",
}


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    for env in SUBSTRATE_ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    yield


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert pack_master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert pack_master_enabled() is True


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class TestTaxonomy:
    def test_15_values(self):
        assert len({p.value for p in PolishSubstrate}) == 15

    def test_substrate_values(self):
        assert {p.value for p in PolishSubstrate} == set(
            SUBSTRATE_MODULES.keys()
        )

    def test_registry_covers_all_enum_values(self):
        registry_values = {
            d.substrate.value for d in _SUBSTRATE_REGISTRY
        }
        enum_values = {p.value for p in PolishSubstrate}
        assert registry_values == enum_values

    def test_no_duplicate_registry_entries(self):
        names = [d.substrate.value for d in _SUBSTRATE_REGISTRY]
        assert len(names) == len(set(names))

    def test_composed_substrates_returns_tuple(self):
        result = composed_substrates()
        assert isinstance(result, tuple)
        assert len(result) == 15
        assert all(isinstance(s, str) for s in result)


# ---------------------------------------------------------------------------
# Predicate semantics
# ---------------------------------------------------------------------------


class TestPredicate:
    def test_pack_off_returns_false(self):
        # Master env unset → False for any substrate
        assert (
            is_substrate_in_active_pack("polish_bundle") is False
        )

    def test_pack_on_unset_substrate_returns_true(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        assert (
            is_substrate_in_active_pack("polish_bundle") is True
        )

    def test_pack_on_explicit_false_vetoes(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "false")
        assert (
            is_substrate_in_active_pack("polish_bundle") is False
        )

    def test_pack_on_explicit_true_stays_true(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
        assert (
            is_substrate_in_active_pack("polish_bundle") is True
        )

    def test_unknown_substrate_returns_false(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        assert (
            is_substrate_in_active_pack("unknown_substrate")
            is False
        )

    def test_empty_substrate_name_returns_false(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        assert is_substrate_in_active_pack("") is False
        # None / malformed → False (defensive)
        assert is_substrate_in_active_pack(None) is False  # type: ignore[arg-type]

    def test_case_normalization(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        # Mixed case should normalize
        assert (
            is_substrate_in_active_pack("POLISH_BUNDLE") is True
        )


# ---------------------------------------------------------------------------
# Parametrized: every wired substrate composes the pack
# ---------------------------------------------------------------------------


class TestSubstrateComposition:
    """The load-bearing test: each of the 15 wired substrates'
    master_enabled() correctly composes the pack predicate."""

    @pytest.mark.parametrize(
        "substrate_name", sorted(SUBSTRATE_MODULES.keys()),
    )
    def test_pack_on_activates_substrate(
        self, monkeypatch, substrate_name,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        mod = SUBSTRATE_MODULES[substrate_name]
        assert mod.master_enabled() is True, (
            f"{substrate_name}.master_enabled() should be True "
            "when pack is on"
        )

    @pytest.mark.parametrize(
        "substrate_name", sorted(SUBSTRATE_MODULES.keys()),
    )
    def test_pack_off_substrate_off(self, substrate_name):
        # No env set → pack off → substrate off
        mod = SUBSTRATE_MODULES[substrate_name]
        assert mod.master_enabled() is False

    @pytest.mark.parametrize(
        "substrate_name", sorted(SUBSTRATE_MODULES.keys()),
    )
    def test_substrate_own_flag_still_works_pack_off(
        self, monkeypatch, substrate_name,
    ):
        """The substrate's own flag (default-FALSE) still
        independently controls the substrate when the pack is
        off — backward-compat preserved."""
        env_var = SUBSTRATE_ENV_VARS[substrate_name]
        monkeypatch.setenv(env_var, "true")
        mod = SUBSTRATE_MODULES[substrate_name]
        assert mod.master_enabled() is True

    @pytest.mark.parametrize(
        "substrate_name", sorted(SUBSTRATE_MODULES.keys()),
    )
    def test_operator_veto_wins_over_pack(
        self, monkeypatch, substrate_name,
    ):
        """Pack on + substrate's own flag explicitly false →
        substrate is OFF. Operator-veto is load-bearing."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        env_var = SUBSTRATE_ENV_VARS[substrate_name]
        monkeypatch.setenv(env_var, "false")
        mod = SUBSTRATE_MODULES[substrate_name]
        assert mod.master_enabled() is False


# ---------------------------------------------------------------------------
# Substrate AST pins still pass after wiring
# ---------------------------------------------------------------------------


class TestExistingAstPinsStillPass:
    """Load-bearing regression: the wiring edit MUST NOT break
    the substrates' existing master_default_false + authority_
    asymmetry pins. The naive `"return True" in src` check was
    replaced with a structurally-correct ast-walk that only
    fires on UNCONDITIONAL return True (not gated by If)."""

    def test_no_relevant_violations_in_wired_substrates(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        sci.reset_registry_for_tests()
        violations = sci.validate_all()
        wired_keywords = (
            "polish_bundle", "thinking_progress", "task_panel",
            "posture_palette", "pipeline_progress",
            "activity_radar", "op_fanout_tree",
            "phase_flow_ribbon", "risk_tier_tint",
            "organism_dashboard", "cognitive_heatmap",
            "op_trajectory", "risk_command_preview",
            "session_story", "memory_crystallization",
            "ux_polish_pack",
        )
        relevant = [
            v for v in violations
            if any(kw in v.invariant_name for kw in wired_keywords)
        ]
        assert not relevant, (
            f"existing AST pins broke on wired substrates: "
            f"{[(v.invariant_name, v.detail) for v in relevant]}"
        )


# ---------------------------------------------------------------------------
# §33.5 frozen artifact
# ---------------------------------------------------------------------------


class TestPolishPackReport:
    def test_substrate_state_to_dict(self):
        s = PolishSubstrateState(
            substrate="polish_bundle",
            env_var="JARVIS_POLISH_BUNDLE_ENABLED",
            display_name="Polish bundle",
            pack_grants=True,
            individually_enabled=False,
            individually_disabled=False,
            effective=True,
        )
        d = s.to_dict()
        assert d["substrate"] == "polish_bundle"
        assert d["effective"] is True

    def test_report_to_dict_shape(self):
        r = polish_status()
        d = r.to_dict()
        expected = {
            "reported_at_unix", "pack_master_enabled",
            "substrates", "active_count",
            "explicitly_vetoed_count", "diagnostic",
            "schema_version",
        }
        assert set(d.keys()) == expected
        assert d["schema_version"] == (
            UX_POLISH_PACK_SCHEMA_VERSION
        )

    def test_pack_off_active_zero(self):
        r = polish_status()
        assert r.pack_master_enabled is False
        assert r.active_count == 0

    def test_pack_on_active_fifteen(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = polish_status()
        assert r.pack_master_enabled is True
        assert r.active_count == 15

    def test_veto_counted(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "false")
        r = polish_status()
        assert r.explicitly_vetoed_count == 1
        assert r.active_count == 14


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_pack_off_renders_disabled(self):
        out = format_polish_panel()
        assert "disabled" in out

    def test_pack_on_renders_active(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        out = format_polish_panel()
        assert "active" in out
        assert "15 / 15" in out

    def test_veto_renders_marker(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "false")
        out = format_polish_panel()
        # vetoed substrate gets ✗ marker
        assert "✗" in out


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(pack.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return pack.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "ux_polish_pack_substrate_taxonomy_closed",
            "ux_polish_pack_registry_completeness",
            "ux_polish_pack_master_default_false",
            "ux_polish_pack_authority_asymmetry",
            "ux_polish_pack_predicate_short_circuits",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "ux_polish_pack_substrate_taxonomy_closed",
            "ux_polish_pack_registry_completeness",
            "ux_polish_pack_master_default_false",
            "ux_polish_pack_authority_asymmetry",
            "ux_polish_pack_predicate_short_circuits",
        ],
    )
    def test_pin_passes(self, canonical_source, pins, pin_name):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSynthetic:
    def test_taxonomy_pin_fires_on_missing(self, pins):
        synthetic = """
import enum
class PolishSubstrate(str, enum.Enum):
    POLISH_BUNDLE = "polish_bundle"
    # MISSING the other 14
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "ux_polish_pack_substrate_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "missing" in violations[0]

    def test_master_pin_fires_on_default_true(self, pins):
        synthetic = """
def pack_master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "ux_polish_pack_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_authority_pin_fires(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import foo\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "ux_polish_pack_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_predicate_short_circuit_pin_fires(self, pins):
        # Synthetic with NO `if not pack_master_enabled():` gate
        synthetic = """
def is_substrate_in_active_pack(name):
    return True  # yolo
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "ux_polish_pack_predicate_short_circuits"
        )
        violations = pin.validate(tree, synthetic)
        assert violations


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


class TestFlagSeed:
    def test_seed_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        assert _ENV_MASTER in names

    def test_seed_integration_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == _ENV_MASTER
        )
        assert spec.default is False
        assert spec.category.value == "integration"


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in pack.__all__:
            assert getattr(pack, name) is not None

    def test_schema_version(self):
        assert UX_POLISH_PACK_SCHEMA_VERSION.startswith(
            "ux_polish_pack.",
        )

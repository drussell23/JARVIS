"""render_emit_tier regression suite.

Pins the adaptive-density emit-visibility substrate. Closes the
"every op shows 5-6 lines at every density" UX gap. CC's restraint
shows 1-2 lines per op at default density and reveals deep-debug
detail only when explicitly asked.

Strict directives validated:

  * Closed-taxonomy EmitTier: PRIMARY/SECONDARY/TERTIARY only.
    Adding a tier requires coordinated visible_at_density update.
  * In-code default tier table is the floor; operator overrides
    layer on top via JARVIS_EMIT_TIER_OVERRIDE JSON.
  * Defensive everywhere: every gate failure returns "visible"
    (operator never misses important events because the substrate
    misbehaved).
  * Master flag default false (substrate ships dormant); operator
    opt-in preserves pre-substrate behavior at install time.
  * Cross-file AST pin: every key in the tier map MUST correspond
    to an actual def in serpent_flow.py.

Covers:

  §A   EmitTier closed taxonomy
  §B   tier_for_method default lookup
  §C   tier_for_method operator override
  §D   visible_at_density per (tier, density) combination
  §E   should_emit master flag gate
  §F   Defensive paths (registry unavailable, malformed override)
  §G   AST pins (5) clean + tampering caught (stale entry detected)
  §H   Auto-discovery integration
  §I   End-to-end via SerpentFlow integration sites
"""
from __future__ import annotations

import ast
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_emit_tier as ret


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_EMIT_TIER_GATING_ENABLED",
        "JARVIS_EMIT_TIER_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_EMIT_TIER_GATING_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# §A — EmitTier closed taxonomy
# ---------------------------------------------------------------------------


class TestEmitTierClosedTaxonomy:
    def test_exact_three_members(self):
        assert {m.value for m in ret.EmitTier} == {
            "PRIMARY", "SECONDARY", "TERTIARY",
        }

    def test_str_inheritance(self):
        assert isinstance(ret.EmitTier.PRIMARY, str)


# ---------------------------------------------------------------------------
# §B — tier_for_method default lookup
# ---------------------------------------------------------------------------


class TestTierForMethodDefaults:
    def test_op_started_is_primary(self, fresh_registry):
        assert ret.tier_for_method("op_started") is ret.EmitTier.PRIMARY

    def test_op_failed_is_primary(self, fresh_registry):
        assert ret.tier_for_method("op_failed") is ret.EmitTier.PRIMARY

    def test_set_op_route_is_secondary(self, fresh_registry):
        assert ret.tier_for_method("set_op_route") is ret.EmitTier.SECONDARY

    def test_op_provider_is_tertiary(self, fresh_registry):
        assert ret.tier_for_method("op_provider") is ret.EmitTier.TERTIARY

    def test_show_streaming_start_is_tertiary(self, fresh_registry):
        assert ret.tier_for_method(
            "show_streaming_start",
        ) is ret.EmitTier.TERTIARY

    def test_unknown_method_falls_back_to_primary(self, fresh_registry):
        # Conservative default — unknown methods stay visible until
        # explicitly tagged
        assert ret.tier_for_method(
            "nonexistent_method_xyz",
        ) is ret.EmitTier.PRIMARY

    def test_empty_name_falls_back_to_primary(self, fresh_registry):
        assert ret.tier_for_method("") is ret.EmitTier.PRIMARY

    def test_non_string_falls_back_to_primary(self, fresh_registry):
        assert ret.tier_for_method(None) is ret.EmitTier.PRIMARY  # type: ignore[arg-type]
        assert ret.tier_for_method(42) is ret.EmitTier.PRIMARY  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §C — Operator override
# ---------------------------------------------------------------------------


class TestOperatorOverride:
    def test_override_promotes_method(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE",
            '{"op_provider": "PRIMARY"}',
        )
        assert ret.tier_for_method(
            "op_provider",
        ) is ret.EmitTier.PRIMARY

    def test_override_demotes_method(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE",
            '{"op_started": "TERTIARY"}',
        )
        assert ret.tier_for_method(
            "op_started",
        ) is ret.EmitTier.TERTIARY

    def test_override_unknown_tier_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE",
            '{"op_provider": "BOGUS_TIER"}',
        )
        # Falls back to default for op_provider
        assert ret.tier_for_method(
            "op_provider",
        ) is ret.EmitTier.TERTIARY

    def test_override_non_string_value_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE",
            '{"op_provider": 42}',
        )
        assert ret.tier_for_method(
            "op_provider",
        ) is ret.EmitTier.TERTIARY

    def test_override_malformed_json_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE", "NOT JSON",
        )
        assert ret.tier_for_method(
            "op_provider",
        ) is ret.EmitTier.TERTIARY

    def test_operator_overrides_returns_dict_when_unset(
        self, fresh_registry,
    ):
        assert ret.operator_tier_overrides() == {}


# ---------------------------------------------------------------------------
# §D — visible_at_density per (tier, density) combination
# ---------------------------------------------------------------------------


class TestVisibleAtDensity:
    @pytest.mark.parametrize("tier,density,expected", [
        # COMPACT — PRIMARY only
        (ret.EmitTier.PRIMARY,   "COMPACT", True),
        (ret.EmitTier.SECONDARY, "COMPACT", False),
        (ret.EmitTier.TERTIARY,  "COMPACT", False),
        # NORMAL — PRIMARY + SECONDARY
        (ret.EmitTier.PRIMARY,   "NORMAL",  True),
        (ret.EmitTier.SECONDARY, "NORMAL",  True),
        (ret.EmitTier.TERTIARY,  "NORMAL",  False),
        # FULL — all three
        (ret.EmitTier.PRIMARY,   "FULL",    True),
        (ret.EmitTier.SECONDARY, "FULL",    True),
        (ret.EmitTier.TERTIARY,  "FULL",    True),
    ])
    def test_visibility_matrix(
        self, tier: ret.EmitTier, density: str, expected: bool,
    ):
        assert ret.visible_at_density(tier, density) is expected

    def test_unknown_density_defaults_to_normal_semantics(self):
        # Operator typo'd density → NORMAL behavior (PRIMARY + SECONDARY)
        assert ret.visible_at_density(
            ret.EmitTier.PRIMARY, "BOGUS",
        ) is True
        assert ret.visible_at_density(
            ret.EmitTier.SECONDARY, "BOGUS",
        ) is True
        assert ret.visible_at_density(
            ret.EmitTier.TERTIARY, "BOGUS",
        ) is False

    def test_accepts_render_density_enum(self):
        assert ret.visible_at_density(
            ret.EmitTier.PRIMARY, rc.RenderDensity.COMPACT,
        ) is True
        assert ret.visible_at_density(
            ret.EmitTier.TERTIARY, rc.RenderDensity.FULL,
        ) is True

    def test_lowercase_density_string(self):
        # Defensive — operator typo "compact" should resolve like "COMPACT"
        assert ret.visible_at_density(
            ret.EmitTier.SECONDARY, "compact",
        ) is False


# ---------------------------------------------------------------------------
# §E — should_emit master flag gate
# ---------------------------------------------------------------------------


class TestShouldEmitMasterGate:
    def test_master_off_always_visible(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Hot-revert: explicit env=false preserves pre-substrate
        # behavior even though post-D5 default is true
        monkeypatch.setenv("JARVIS_EMIT_TIER_GATING_ENABLED", "false")
        assert ret.should_emit("op_provider") is True
        assert ret.should_emit("show_streaming_start") is True
        assert ret.should_emit("nonexistent_xyz") is True

    def test_master_on_at_normal_density_filters(self, gate_on):
        # Default density NORMAL — TERTIARY hidden, SECONDARY+ visible
        assert ret.should_emit("op_started") is True       # PRIMARY
        assert ret.should_emit("set_op_route") is True     # SECONDARY
        assert ret.should_emit("op_provider") is False     # TERTIARY
        assert ret.should_emit(
            "show_streaming_start",
        ) is False                                          # TERTIARY

    def test_master_on_compact_density_hides_secondary(
        self, monkeypatch: pytest.MonkeyPatch, gate_on,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "compact",
        )
        # Wire conductor so density resolution finds the override
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        assert ret.should_emit("op_started") is True       # PRIMARY
        assert ret.should_emit("set_op_route") is False    # SECONDARY hidden
        assert ret.should_emit("op_provider") is False     # TERTIARY hidden

    def test_master_on_full_density_shows_everything(
        self, monkeypatch: pytest.MonkeyPatch, gate_on,
    ):
        monkeypatch.setenv(
            "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE", "full",
        )
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        assert ret.should_emit("op_started") is True
        assert ret.should_emit("op_provider") is True      # TERTIARY visible at FULL

    def test_operator_override_overrides_default_at_normal(
        self, monkeypatch: pytest.MonkeyPatch, gate_on,
    ):
        monkeypatch.setenv(
            "JARVIS_EMIT_TIER_OVERRIDE",
            '{"op_provider": "PRIMARY"}',
        )
        # Promoted to PRIMARY — visible everywhere
        assert ret.should_emit("op_provider") is True


# ---------------------------------------------------------------------------
# §F — Defensive paths
# ---------------------------------------------------------------------------


class TestDefensivePaths:
    def test_should_emit_returns_visible_on_internal_failure(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Force an exception inside should_emit — it should still
        # return True (visible) rather than raising
        def _boom() -> bool:
            raise RuntimeError("simulated registry failure")
        monkeypatch.setattr(ret, "is_enabled", _boom)
        assert ret.should_emit("op_provider") is True

    def test_active_density_resolution_no_conductor(
        self, fresh_registry,
    ):
        rc.reset_render_conductor()
        # No conductor registered → density defaults to NORMAL
        assert ret._resolve_active_density() == "NORMAL"


# ---------------------------------------------------------------------------
# §G — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def d3_pins() -> list:
    return list(ret.register_shipped_invariants())


class TestD3ASTPinsClean:
    def test_five_pins_registered(self, d3_pins):
        assert len(d3_pins) == 5
        names = {i.invariant_name for i in d3_pins}
        assert names == {
            "render_emit_tier_no_rich_import",
            "render_emit_tier_no_authority_imports",
            "render_emit_tier_emit_tier_closed_taxonomy",
            "render_emit_tier_map_methods_exist",
            "render_emit_tier_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_module_ast(self):
        import inspect
        src = inspect.getsource(ret)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, d3_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, d3_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_emit_tier_closed_taxonomy_clean(self, d3_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_emit_tier_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_tier_map_methods_exist_clean(self, d3_pins):
        # Cross-file pin — reads serpent_flow.py
        import pathlib
        path = pathlib.Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py",
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_map_methods_exist")
        assert pin.validate(tree, src) == ()


class TestD3ASTPinsCatchTampering:
    def test_authority_import_caught(self, d3_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.cancel_token import x\n"
        )
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("cancel_token" in v for v in violations)

    def test_rich_import_caught(self, d3_pins):
        tampered = ast.parse("from rich.text import Text\n")
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_added_emit_tier_caught(self, d3_pins):
        tampered_src = (
            "class EmitTier:\n"
            "    PRIMARY = 'PRIMARY'\n"
            "    SECONDARY = 'SECONDARY'\n"
            "    TERTIARY = 'TERTIARY'\n"
            "    NEW_TIER = 'NEW_TIER'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_emit_tier_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_stale_tier_map_entry_caught(self, d3_pins):
        # Simulated serpent_flow source missing a method that the
        # tier map references → cross-file pin should fire
        tampered_src = (
            "class SerpentFlow:\n"
            "    def op_started(self): pass\n"
            "    # all other methods removed\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in d3_pins
                   if p.invariant_name ==
                   "render_emit_tier_map_methods_exist")
        violations = pin.validate(tampered, tampered_src)
        assert violations
        # Should mention some of the missing method names
        assert "op_failed" in violations[0] or (
            "op_provider" in violations[0]
        )


# ---------------------------------------------------------------------------
# §H — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_emit_tier(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_EMIT_TIER_GATING_ENABLED" in names
        assert "JARVIS_EMIT_TIER_OVERRIDE" in names

    def test_shipped_invariants_includes_d3_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in ret.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "render_emit_tier_no_rich_import",
            "render_emit_tier_no_authority_imports",
            "render_emit_tier_emit_tier_closed_taxonomy",
            "render_emit_tier_map_methods_exist",
            "render_emit_tier_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_d3_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in ret.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        d3_failures = [
            r for r in results
            if r.invariant_name.startswith("render_emit_tier_")
        ]
        assert d3_failures == [], (
            f"D3 pins reporting violations: "
            f"{[r.to_dict() for r in d3_failures]}"
        )


# ---------------------------------------------------------------------------
# §I — End-to-end via SerpentFlow integration sites
# ---------------------------------------------------------------------------


class TestSerpentFlowIntegrationSites:
    def test_serpent_flow_imports_should_emit(self):
        # Verify the original D3 wire sites exist by reading source.
        # Count is `>=` because D4 (and future polish slices) add more
        # gates as additional update_* methods migrate. Each specific
        # gate-call assertion is exact (catches if an original wire is
        # accidentally removed).
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py",
        ).read_text(encoding="utf-8")
        assert src.count(
            "from backend.core.ouroboros.governance."
            "render_emit_tier import"
        ) >= 3, "D3 wired 3 sites; count must not regress below floor"
        # Original D3 gates — exact-1 each
        assert src.count('should_emit("op_provider")') == 1
        assert src.count('should_emit("_render_plan_phase")') == 1
        assert src.count('should_emit("show_streaming_start")') == 1

    def test_op_provider_state_tracking_preserved(self):
        # State tracking happens BEFORE the gate — _op_providers
        # dict updates regardless of visibility. Verifies the gate
        # doesn't break downstream consumers.
        import pathlib
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py",
        ).read_text(encoding="utf-8")
        # The op_provider method body should have the dict update
        # BEFORE the should_emit gate import
        op_provider_idx = src.find("def op_provider")
        assert op_provider_idx > 0
        # Find the next 30 lines after op_provider def
        body = src[op_provider_idx:op_provider_idx + 1000]
        update_idx = body.find("self._op_providers[op_id] = provider")
        gate_idx = body.find('should_emit("op_provider")')
        assert update_idx > 0 and gate_idx > 0
        assert update_idx < gate_idx, (
            "State update must precede visibility gate so downstream "
            "consumers see the provider mapping regardless of "
            "operator-visible emit decisions"
        )

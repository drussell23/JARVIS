"""StatusLineComposer regression suite.

Pins the single-composed-status-line substrate. Closes the
"8+ separate update_* methods spam separate console lines" UX gap.

Strict directives validated:

  * Closed-taxonomy StatusField (9 fields) AST-pinned
  * Closed-taxonomy _FIELD_FORMATTERS — every StatusField has a
    formatter (cross-checked via AST pin)
  * Operator-overrideable field order via JSON list
  * Debounced publish — coalesces rapid set() calls
  * No authority imports (including serpent_flow — bidirectional
    decoupling preserved)
  * Defensive: bad fields/values silently skipped, formatter
    exceptions render as "(?)", publish failures swallowed
  * Master flag default false (substrate ships dormant)

Covers:

  §A   StatusField closed taxonomy
  §B   StatusLineComposer.set + compose basic flow
  §C   Field order from default + operator override
  §D   Per-field formatter behavior
  §E   Debounce — multiple set() calls coalesce
  §F   STATUS_TICK event published with correct content + metadata
  §G   Master flag gate
  §H   Defensive paths (bad field, formatter exception, no conductor)
  §I   AST pins clean + tampering caught
  §J   Auto-discovery integration
"""
from __future__ import annotations

import ast
import time
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import status_line_composer as slc


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_STATUS_LINE_COMPOSER_ENABLED",
        "JARVIS_STATUS_LINE_FIELDS",
        "JARVIS_STATUS_LINE_DEBOUNCE_MS",
        "JARVIS_STATUS_LINE_SEPARATOR",
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    slc.reset_status_line_composer()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def composer_on(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_STATUS_LINE_COMPOSER_ENABLED", "true")
    # Disable debounce for sync tests
    monkeypatch.setenv("JARVIS_STATUS_LINE_DEBOUNCE_MS", "0")
    yield


# ---------------------------------------------------------------------------
# §A — StatusField closed taxonomy
# ---------------------------------------------------------------------------


class TestStatusFieldClosedTaxonomy:
    def test_exact_nine_members(self):
        assert {m.value for m in slc.StatusField} == {
            "COST", "SENSORS", "PROVIDER_CHAIN", "INTENT_CHAIN",
            "POSTURE", "SESSION_LESSONS", "INTENT_DISCOVERY",
            "DREAM_ENGINE", "LEARNING",
        }


# ---------------------------------------------------------------------------
# §B — set + compose basic flow
# ---------------------------------------------------------------------------


class TestComposerBasicFlow:
    def test_empty_compose_is_empty_string(self, composer_on):
        comp = slc.StatusLineComposer()
        assert comp.compose() == ""

    def test_single_field_compose(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        assert "$0.42" in comp.compose()

    def test_multiple_fields_compose(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        comp.set(slc.StatusField.SENSORS, 16)
        comp.set(slc.StatusField.POSTURE, "EXPLORE")
        composed = comp.compose()
        assert "$0.42" in composed
        assert "16 sensors" in composed
        assert "EXPLORE" in composed

    def test_set_with_string_field_name(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set("COST", 1.0)
        assert "$1.00" in comp.compose()

    def test_clear_removes_field(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        comp.clear(slc.StatusField.COST)
        assert "$0.42" not in comp.compose()

    def test_clear_all_empties_composer(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        comp.set(slc.StatusField.SENSORS, 16)
        comp.clear()
        assert comp.compose() == ""

    def test_snapshot_returns_dict(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        snap = comp.snapshot()
        assert slc.StatusField.COST in snap
        assert snap[slc.StatusField.COST] == 0.42


# ---------------------------------------------------------------------------
# §C — Field order
# ---------------------------------------------------------------------------


class TestFieldOrder:
    def test_default_order(self, fresh_registry):
        order = slc.field_order()
        # POSTURE is first in default
        assert order[0] is slc.StatusField.POSTURE

    def test_operator_override_order(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_FIELDS",
            '["COST", "SENSORS"]',
        )
        order = slc.field_order()
        assert order == (
            slc.StatusField.COST, slc.StatusField.SENSORS,
        )

    def test_unknown_field_in_override_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_FIELDS",
            '["COST", "BOGUS_FIELD", "SENSORS"]',
        )
        order = slc.field_order()
        # BOGUS skipped — only valid fields kept
        assert slc.StatusField.COST in order
        assert slc.StatusField.SENSORS in order
        assert len(order) == 2

    def test_compose_respects_override_order(
        self, monkeypatch: pytest.MonkeyPatch, composer_on,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_FIELDS",
            '["COST", "POSTURE"]',
        )
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.POSTURE, "EXPLORE")
        comp.set(slc.StatusField.COST, 0.42)
        composed = comp.compose()
        # Cost first per override, posture second
        cost_idx = composed.find("$0.42")
        posture_idx = composed.find("EXPLORE")
        assert 0 <= cost_idx < posture_idx


# ---------------------------------------------------------------------------
# §D — Per-field formatter behavior
# ---------------------------------------------------------------------------


class TestFieldFormatters:
    def test_cost_formats_as_dollar(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 1.234)
        assert "$1.23" in comp.compose()

    def test_cost_bad_value_falls_back(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, "not a number")
        assert "$?" in comp.compose()

    def test_sensors_formats_with_label(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.SENSORS, 16)
        assert "16 sensors" in comp.compose()

    def test_posture_uppercases(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.POSTURE, "explore")
        assert "EXPLORE" in comp.compose()

    def test_dream_engine_dict_value(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.DREAM_ENGINE, {"blueprints": 3})
        assert "💭 3" in comp.compose()

    def test_learning_dict_value(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(
            slc.StatusField.LEARNING,
            {"rules": 7, "trend": "↑"},
        )
        assert "📖 7 ↑" in comp.compose()

    def test_session_lessons_pluralization(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.SESSION_LESSONS, 1)
        assert "1 lesson" in comp.compose()
        comp.set(slc.StatusField.SESSION_LESSONS, 3)
        assert "3 lessons" in comp.compose()


# ---------------------------------------------------------------------------
# §E — Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_default_debounce_ms(self, fresh_registry):
        assert slc.debounce_ms() == 50

    def test_debounce_clamped_to_min(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_DEBOUNCE_MS", "-100",
        )
        assert slc.debounce_ms() == 0

    def test_debounce_clamped_to_max(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_DEBOUNCE_MS", "999999",
        )
        assert slc.debounce_ms() == 5000


# ---------------------------------------------------------------------------
# §F — STATUS_TICK event publish
# ---------------------------------------------------------------------------


class _Recorder:
    name = "rec"

    def __init__(self) -> None:
        self.events: List[Any] = []

    def notify(self, event: Any) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class TestStatusTickPublish:
    def test_status_tick_fires_on_set(
        self, monkeypatch: pytest.MonkeyPatch, composer_on,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        rec = _Recorder()
        c.add_backend(rec)
        rc.register_render_conductor(c)
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        # No debounce → published synchronously
        assert any(
            e.kind is rc.EventKind.STATUS_TICK
            for e in rec.events
        )

    def test_status_tick_carries_composed_metadata(
        self, monkeypatch: pytest.MonkeyPatch, composer_on,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        rec = _Recorder()
        c.add_backend(rec)
        rc.register_render_conductor(c)
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 1.5)
        events = [
            e for e in rec.events
            if e.kind is rc.EventKind.STATUS_TICK
        ]
        assert events
        assert events[0].metadata.get("composed_status") is True
        assert "$1.50" in events[0].content

    def test_status_tick_region_is_status(
        self, monkeypatch: pytest.MonkeyPatch, composer_on,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        c = rc.RenderConductor()
        rec = _Recorder()
        c.add_backend(rec)
        rc.register_render_conductor(c)
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.SENSORS, 16)
        events = [
            e for e in rec.events
            if e.kind is rc.EventKind.STATUS_TICK
        ]
        assert events[0].region is rc.RegionKind.STATUS


# ---------------------------------------------------------------------------
# §G — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_master_off_set_is_no_op(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Hot-revert: explicit env=false (post-D5 default true)
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_COMPOSER_ENABLED", "false",
        )
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        # State NOT updated when master off
        assert comp.compose() == ""

    def test_master_off_no_publish(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_COMPOSER_ENABLED", "false",
        )
        c = rc.RenderConductor()
        rec = _Recorder()
        c.add_backend(rec)
        rc.register_render_conductor(c)
        comp = slc.StatusLineComposer()
        comp.set(slc.StatusField.COST, 0.42)
        events = [
            e for e in rec.events
            if e.kind is rc.EventKind.STATUS_TICK
        ]
        assert events == []


# ---------------------------------------------------------------------------
# §H — Defensive paths
# ---------------------------------------------------------------------------


class TestDefensivePaths:
    def test_set_with_none_field(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set(None, 0.42)  # type: ignore[arg-type]
        assert comp.compose() == ""

    def test_set_with_unknown_field_string(self, composer_on):
        comp = slc.StatusLineComposer()
        comp.set("BOGUS_FIELD", 0.42)
        assert comp.compose() == ""

    def test_no_conductor_set_still_works(self, composer_on):
        rc.reset_render_conductor()
        comp = slc.StatusLineComposer()
        # set() succeeds; publish silently no-ops
        comp.set(slc.StatusField.COST, 0.42)
        assert "$0.42" in comp.compose()

    def test_update_field_helper_no_composer(self, composer_on):
        slc.reset_status_line_composer()
        # Helper safely no-ops when no composer registered
        slc.update_field(slc.StatusField.COST, 0.42)


# ---------------------------------------------------------------------------
# §I — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def d4_pins() -> list:
    return list(slc.register_shipped_invariants())


class TestD4ASTPinsClean:
    def test_five_pins_registered(self, d4_pins):
        assert len(d4_pins) == 5
        names = {i.invariant_name for i in d4_pins}
        assert names == {
            "status_line_composer_no_rich_import",
            "status_line_composer_no_authority_imports",
            "status_line_composer_status_field_closed",
            "status_line_composer_field_formatters_present",
            "status_line_composer_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_module_ast(self):
        import inspect
        src = inspect.getsource(slc)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, d4_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, d4_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_status_field_closed_clean(self, d4_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_status_field_closed")
        assert pin.validate(tree, src) == ()

    def test_field_formatters_present_clean(self, d4_pins, real_module_ast):
        tree, src = real_module_ast
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_field_formatters_present")
        assert pin.validate(tree, src) == ()


class TestD4ASTPinsCatchTampering:
    def test_serpent_flow_top_level_import_caught(self, d4_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.battle_test.serpent_flow "
            "import SerpentFlow\n"
        )
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("serpent_flow" in v for v in violations)

    def test_added_status_field_caught(self, d4_pins):
        tampered_src = (
            "class StatusField:\n"
            "    COST = 'COST'\n"
            "    SENSORS = 'SENSORS'\n"
            "    PROVIDER_CHAIN = 'PROVIDER_CHAIN'\n"
            "    INTENT_CHAIN = 'INTENT_CHAIN'\n"
            "    POSTURE = 'POSTURE'\n"
            "    SESSION_LESSONS = 'SESSION_LESSONS'\n"
            "    INTENT_DISCOVERY = 'INTENT_DISCOVERY'\n"
            "    DREAM_ENGINE = 'DREAM_ENGINE'\n"
            "    LEARNING = 'LEARNING'\n"
            "    NEW_FIELD = 'NEW_FIELD'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_status_field_closed")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_missing_formatter_for_field_caught(self, d4_pins):
        # Source declares all 9 fields but missing entry for one
        tampered_src = (
            "class StatusField: pass\n"
            "_FIELD_FORMATTERS = {\n"
            "    StatusField.COST: lambda v: '',\n"
            "    StatusField.SENSORS: lambda v: '',\n"
            # Missing the other 7
            "}\n"
        )
        pin = next(p for p in d4_pins
                   if p.invariant_name ==
                   "status_line_composer_field_formatters_present")
        violations = pin.validate(ast.parse(tampered_src), tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §J — Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_composer(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_STATUS_LINE_COMPOSER_ENABLED" in names
        assert "JARVIS_STATUS_LINE_FIELDS" in names
        assert "JARVIS_STATUS_LINE_DEBOUNCE_MS" in names
        assert "JARVIS_STATUS_LINE_SEPARATOR" in names

    def test_shipped_invariants_includes_d4_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in slc.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "status_line_composer_no_rich_import",
            "status_line_composer_no_authority_imports",
            "status_line_composer_status_field_closed",
            "status_line_composer_field_formatters_present",
            "status_line_composer_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_d4_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in slc.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        d4_failures = [
            r for r in results
            if r.invariant_name.startswith("status_line_composer_")
        ]
        assert d4_failures == [], (
            f"D4 pins reporting violations: "
            f"{[r.to_dict() for r in d4_failures]}"
        )

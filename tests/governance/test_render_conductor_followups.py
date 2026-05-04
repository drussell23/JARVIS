"""RenderConductor arc — Slice 7 follow-ups regression suite.

Pins follow-ups #2-#5:

  * /render REPL verb (read-only substrate inspection)
  * GET /observability/render (IDE-visible substrate state)
  * Per-feature graduation flips (4 producer flags default → true)
  * AST graduation pins on the 4 producer flag defaults

Strict directives validated:

  * No hardcoded values in the introspection layer: arc-flag list +
    verb registration + GET projection all derive from the existing
    typed registries — no hand-rolled lists shadow the seed.
  * Read-only by construction: /render verb never mutates substrate
    state; GET handler never invokes producer methods.
  * Defensive everywhere: every observer projection degrades to
    "(not registered)" when the singleton isn't wired; every
    registry-unavailable error returns a degraded line.
  * Graduation pins fire on revert: each of the 4 producer flag-
    default pins surfaces a violation when the FlagSpec is tampered
    back to default=False.

Covers:

  §A   /render REPL verb dispatch matrix
  §B   /render verb registered with help_dispatcher
  §C   /render flags lists exactly 15 arc flags
  §D   GET /observability/render shape + values
  §E   GET /observability/render port-scanner discipline (403/429)
  §F   Per-flag graduation defaults (4 producer flags = True)
  §G   AST graduation pins on the 4 producer flag defaults
  §H   AST tampering catches a default-False revert
  §I   Combined arc — total invariant + flag count post-follow-ups
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from backend.core.ouroboros.governance import key_input as ki
from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_help as rh
from backend.core.ouroboros.governance import render_primitives as rp
from backend.core.ouroboros.governance import render_repl as rr
from backend.core.ouroboros.governance import render_thread as rt


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_REASONING_STREAM_ENABLED",
        "JARVIS_INPUT_CONTROLLER_ENABLED",
        "JARVIS_THREAD_OBSERVER_ENABLED",
        "JARVIS_CONTEXTUAL_HELP_ENABLED",
        "JARVIS_IDE_OBSERVABILITY_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    rt.reset_thread_observer()
    ki.reset_input_controller()
    rh.reset_help_resolver()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


# ---------------------------------------------------------------------------
# §A — /render REPL verb dispatch matrix
# ---------------------------------------------------------------------------


class TestRenderReplDispatch:
    def test_help_subcommand(self, fresh_registry):
        r = rr.dispatch_render_command("/render help")
        assert r.ok is True
        assert "/render {status|flags|backends|observers|help}" in r.text

    def test_status_subcommand(self, fresh_registry):
        r = rr.dispatch_render_command("/render status")
        assert r.ok is True
        assert "RenderConductor arc" in r.text
        assert "JARVIS_RENDER_CONDUCTOR_ENABLED" in r.text

    def test_flags_subcommand_lists_all(self, fresh_registry):
        r = rr.dispatch_render_command("/render flags")
        assert r.ok is True
        # Should mention every arc flag prefix
        for prefix in (
            "JARVIS_RENDER_CONDUCTOR_",
            "JARVIS_REASONING_STREAM_",
            "JARVIS_INPUT_CONTROLLER_",
            "JARVIS_THREAD_OBSERVER_",
            "JARVIS_CONTEXTUAL_HELP_",
        ):
            assert prefix in r.text

    def test_backends_subcommand_no_conductor(self, fresh_registry):
        r = rr.dispatch_render_command("/render backends")
        assert r.ok is True
        assert "no conductor registered" in r.text

    def test_backends_subcommand_with_conductor(self, fresh_registry):
        c = rc.RenderConductor()
        rc.register_render_conductor(c)
        r = rr.dispatch_render_command("/render backends")
        assert r.ok is True
        # Conductor registered but no backends → reports zero
        assert "zero backends" in r.text

    def test_observers_subcommand(self, fresh_registry):
        r = rr.dispatch_render_command("/render observers")
        assert r.ok is True
        assert "input_ctrl" in r.text
        assert "thread_obs" in r.text
        assert "help_res" in r.text

    def test_default_subcommand_is_status(self, fresh_registry):
        r = rr.dispatch_render_command("/render")
        assert r.ok is True
        # Status header should appear
        assert "RenderConductor arc — status" in r.text

    def test_unknown_subcommand_clear_error(self, fresh_registry):
        r = rr.dispatch_render_command("/render bogus")
        assert r.ok is False
        assert "unknown subcommand 'bogus'" in r.text
        assert "/render help" in r.text

    def test_non_matching_line_returns_unmatched(self):
        r = rr.dispatch_render_command("/posture status")
        assert r.matched is False

    def test_empty_line_returns_unmatched(self):
        r = rr.dispatch_render_command("")
        assert r.matched is False

    def test_question_alias_for_help(self, fresh_registry):
        r = rr.dispatch_render_command("/render ?")
        assert r.ok is True
        assert "/render {status|flags|backends|observers|help}" in r.text


# ---------------------------------------------------------------------------
# §B — Verb registered with help_dispatcher
# ---------------------------------------------------------------------------


class TestRenderVerbRegistration:
    def test_verb_registered_in_help_dispatcher(self):
        # Backlog #2: dynamic discovery in help_dispatcher seeds the
        # verb on every reset_default_verb_registry → ensure_seeded
        # cycle. No explicit re-registration needed here.
        from backend.core.ouroboros.governance.help_dispatcher import (
            get_default_verb_registry,
            reset_default_verb_registry,
        )
        reset_default_verb_registry()  # forces re-seed + re-discovery
        spec = get_default_verb_registry().get("/render")
        assert spec is not None
        assert spec.category == "observability"
        assert "RenderConductor" in spec.one_line

    def test_render_repl_exposes_register_verbs(self):
        # Auto-discovery contract — the function must be importable
        # + callable, and return the count of registered verbs.
        assert callable(getattr(rr, "register_verbs", None))
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        reg = VerbRegistry()
        count = rr.register_verbs(reg)
        assert count == 1
        assert reg.get("/render") is not None


# ---------------------------------------------------------------------------
# §C — /render flags lists exactly 15
# ---------------------------------------------------------------------------


class TestArcFlagListSize:
    def test_arc_flags_constant_size(self):
        assert len(rr._ARC_FLAGS) == 15

    def test_arc_flags_unique(self):
        names = [name for name, _ in rr._ARC_FLAGS]
        assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# §D — GET /observability/render shape + values
# ---------------------------------------------------------------------------


class _StubReq:
    """Minimal aiohttp Request stub for handler testing."""

    def __init__(self, headers=None, query=None) -> None:
        self.headers = headers or {}
        self.query = query or {}
        self.remote = "127.0.0.1"


class TestObservabilityRenderRoute:
    def _call(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        return asyncio.get_event_loop().run_until_complete(
            router._handle_render_substrate(_StubReq())
        )

    def test_returns_200(self, monkeypatch: pytest.MonkeyPatch, fresh_registry):
        resp = self._call(monkeypatch)
        assert resp.status == 200

    def test_response_includes_schema_version(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        resp = self._call(monkeypatch)
        import json
        body = json.loads(resp.body)
        assert body["schema_version"] == "1.0"

    def test_response_master_enabled_default_true(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        resp = self._call(monkeypatch)
        import json
        body = json.loads(resp.body)
        assert body["master_enabled"] is True

    def test_response_producer_flags_default_true(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        # Post-Slice-7-fu#4 — all 4 producer flags default true
        resp = self._call(monkeypatch)
        import json
        body = json.loads(resp.body)
        for name in (
            "JARVIS_REASONING_STREAM_ENABLED",
            "JARVIS_INPUT_CONTROLLER_ENABLED",
            "JARVIS_THREAD_OBSERVER_ENABLED",
            "JARVIS_CONTEXTUAL_HELP_ENABLED",
        ):
            assert body["producer_flags"][name] is True, name

    def test_response_includes_observers_section(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        resp = self._call(monkeypatch)
        import json
        body = json.loads(resp.body)
        assert "input_controller" in body["observers"]
        assert "thread_observer" in body["observers"]
        assert "help_resolver" in body["observers"]

    def test_arc_totals_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        resp = self._call(monkeypatch)
        import json
        body = json.loads(resp.body)
        assert body["arc_flags_total"] >= 13  # at least 13 arc flags
        assert body["arc_invariants_total"] >= 35


# ---------------------------------------------------------------------------
# §E — Port-scanner discipline (403)
# ---------------------------------------------------------------------------


class TestObservabilityRenderGates:
    def test_returns_403_when_master_off(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.delenv("JARVIS_IDE_OBSERVABILITY_ENABLED", raising=False)
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = asyncio.get_event_loop().run_until_complete(
            router._handle_render_substrate(_StubReq())
        )
        assert resp.status == 403


# ---------------------------------------------------------------------------
# §F — Per-flag graduation defaults
# ---------------------------------------------------------------------------


class TestProducerGraduationDefaults:
    def test_reasoning_stream_default_true(self, fresh_registry):
        assert rp.reasoning_stream_enabled() is True

    def test_input_controller_default_true(self, fresh_registry):
        assert ki.is_enabled() is True

    def test_thread_observer_default_true(self, fresh_registry):
        assert rt.is_enabled() is True

    def test_contextual_help_default_true(self, fresh_registry):
        assert rh.is_enabled() is True

    def test_each_can_hot_revert_via_env(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        for env_name, accessor_module, accessor_attr in (
            ("JARVIS_REASONING_STREAM_ENABLED", rp,
             "reasoning_stream_enabled"),
            ("JARVIS_INPUT_CONTROLLER_ENABLED", ki, "is_enabled"),
            ("JARVIS_THREAD_OBSERVER_ENABLED", rt, "is_enabled"),
            ("JARVIS_CONTEXTUAL_HELP_ENABLED", rh, "is_enabled"),
        ):
            monkeypatch.setenv(env_name, "false")
            assert getattr(accessor_module, accessor_attr)() is False, (
                f"{env_name} hot-revert failed"
            )
            monkeypatch.delenv(env_name, raising=False)
            assert getattr(accessor_module, accessor_attr)() is True, (
                f"{env_name} re-graduate failed"
            )


# ---------------------------------------------------------------------------
# §G — AST graduation pins on producer flag defaults
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fu5_pins() -> list:
    from backend.core.ouroboros.governance import render_backends as rb
    all_pins = list(rb.register_shipped_invariants())
    return [p for p in all_pins if "default_true" in p.invariant_name]


class TestProducerDefaultPins:
    def test_followup_5_original_four_pins_present(self, fu5_pins):
        # fu#5 shipped 4 graduation pins. Future arc work (D5, etc.)
        # adds more "default_true" pins to the same register_-
        # shipped_invariants. Assert the original 4 are still present;
        # use >= for total count to remain forward-compatible.
        assert len(fu5_pins) >= 4
        names = {i.invariant_name for i in fu5_pins}
        for expected in (
            "render_primitives_reasoning_stream_default_true",
            "key_input_input_controller_default_true",
            "render_thread_thread_observer_default_true",
            "render_help_contextual_help_default_true",
        ):
            assert expected in names

    def test_each_pin_clean_against_real_source(self, fu5_pins):
        import ast
        import inspect
        # Map pin names to their target modules. Unknown pins
        # (added in future slices) are skipped — the per-pin AST
        # validation is owned by the slice that added the pin, not
        # this followups spine.
        for pin in fu5_pins:
            target_module: Any = None
            if "reasoning_stream" in pin.invariant_name:
                target_module = rp
            elif "input_controller" in pin.invariant_name:
                target_module = ki
            elif "thread_observer" in pin.invariant_name:
                target_module = rt
            elif "contextual_help" in pin.invariant_name:
                target_module = rh
            else:
                # Unknown pin (e.g. D5's emit_tier / composer
                # graduations) — owned + tested by its own slice.
                continue
            src = inspect.getsource(target_module)
            tree = ast.parse(src)
            assert pin.validate(tree, src) == (), (
                f"Pin {pin.invariant_name} fails on real source"
            )


# ---------------------------------------------------------------------------
# §H — AST tampering catches default-False revert
# ---------------------------------------------------------------------------


class TestProducerDefaultPinsCatchTampering:
    def _tampered_source(self, flag_name: str, default_value: str) -> str:
        # Mimics the substrate convention: constant assignment +
        # FlagSpec(name=constant, ..., default=...).
        const_name = "_FLAG_TAMPER"
        return (
            f'{const_name} = "{flag_name}"\n\n'
            f'def register_flags(registry):\n'
            f'    spec = FlagSpec(\n'
            f'        name={const_name},\n'
            f'        type=FlagType.BOOL,\n'
            f'        default={default_value},\n'
            f'    )\n'
        )

    def test_reasoning_stream_revert_to_false_caught(self, fu5_pins):
        import ast
        pin = next(p for p in fu5_pins
                   if "reasoning_stream" in p.invariant_name)
        src = self._tampered_source(
            "JARVIS_REASONING_STREAM_ENABLED", "False",
        )
        tree = ast.parse(src)
        violations = pin.validate(tree, src)
        assert violations
        assert "default=False" in violations[0]

    def test_input_controller_revert_to_false_caught(self, fu5_pins):
        import ast
        pin = next(p for p in fu5_pins
                   if "input_controller" in p.invariant_name)
        src = self._tampered_source(
            "JARVIS_INPUT_CONTROLLER_ENABLED", "False",
        )
        tree = ast.parse(src)
        violations = pin.validate(tree, src)
        assert violations

    def test_thread_observer_revert_to_false_caught(self, fu5_pins):
        import ast
        pin = next(p for p in fu5_pins
                   if "thread_observer" in p.invariant_name)
        src = self._tampered_source(
            "JARVIS_THREAD_OBSERVER_ENABLED", "False",
        )
        tree = ast.parse(src)
        violations = pin.validate(tree, src)
        assert violations

    def test_contextual_help_revert_to_false_caught(self, fu5_pins):
        import ast
        pin = next(p for p in fu5_pins
                   if "contextual_help" in p.invariant_name)
        src = self._tampered_source(
            "JARVIS_CONTEXTUAL_HELP_ENABLED", "False",
        )
        tree = ast.parse(src)
        violations = pin.validate(tree, src)
        assert violations

    def test_missing_flagspec_caught(self, fu5_pins):
        import ast
        pin = next(p for p in fu5_pins
                   if "reasoning_stream" in p.invariant_name)
        # Source with no FlagSpec at all
        src = "def something_else(): pass\n"
        tree = ast.parse(src)
        violations = pin.validate(tree, src)
        assert violations
        assert "not located" in violations[0]


# ---------------------------------------------------------------------------
# §I — render_repl AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repl_pins() -> list:
    return list(rr.register_shipped_invariants())


class TestRenderReplASTPins:
    def test_four_pins_registered(self, repl_pins):
        assert len(repl_pins) == 4
        names = {i.invariant_name for i in repl_pins}
        assert "render_repl_no_rich_import" in names
        assert "render_repl_no_authority_imports" in names
        assert "render_repl_subcommand_closed_taxonomy" in names
        assert "render_repl_discovery_symbols_present" in names

    def test_pins_clean_against_real_source(self, repl_pins):
        import ast
        import inspect
        src = inspect.getsource(rr)
        tree = ast.parse(src)
        for pin in repl_pins:
            assert pin.validate(tree, src) == (), (
                f"Pin {pin.invariant_name} fails on real source"
            )


# ---------------------------------------------------------------------------
# §J — Combined arc totals post-follow-ups
# ---------------------------------------------------------------------------


class TestPostFollowupsArcTotals:
    def test_arc_invariant_total_at_least_43(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        arc_invs = [
            i for i in sci.list_shipped_code_invariants()
            if any(prefix in i.invariant_name for prefix in (
                "render_conductor_", "render_backends_",
                "render_primitives_", "render_thread_",
                "render_help_", "render_repl_",
                "key_input_", "streamrenderer_",
                "harness_wires_", "default_true",
            ))
        ]
        # Pre-followup: 35. Followups added 4 producer-default + 4
        # render_repl pins → 43. Pin counts can grow but never shrink.
        assert len(arc_invs) >= 43, (
            f"Expected ≥43 arc invariants, got {len(arc_invs)}"
        )

    def test_arc_flag_total_unchanged(self, fresh_registry):
        # Followups added zero flags (introspection is read-only).
        names = {s.name for s in fresh_registry.list_all()}
        arc_flag_count = sum(
            1 for n in names
            if any(p in n for p in (
                "JARVIS_RENDER_CONDUCTOR_",
                "JARVIS_REASONING_STREAM_",
                "JARVIS_FILE_REF_",
                "JARVIS_INPUT_CONTROLLER_",
                "JARVIS_KEY_BINDINGS",
                "JARVIS_THREAD_OBSERVER_",
                "JARVIS_THREAD_SPEAKER_",
                "JARVIS_CONTEXTUAL_HELP_",
                "JARVIS_HELP_RANKING_",
                "JARVIS_HELP_PAGE_",
            ))
        )
        assert arc_flag_count == 15

    def test_no_arc_invariant_violates(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        arc_failures = [
            r for r in results
            if any(prefix in r.invariant_name for prefix in (
                "render_conductor_", "render_backends_",
                "render_primitives_", "render_thread_",
                "render_help_", "render_repl_",
                "key_input_", "streamrenderer_",
                "harness_wires_", "default_true",
            ))
        ]
        assert arc_failures == [], (
            f"Render arc invariants failing: "
            f"{[r.to_dict() for r in arc_failures]}"
        )

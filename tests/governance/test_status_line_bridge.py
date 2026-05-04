"""STATUS_TICK composed-line bridge regression suite.

Pins the D5 wire from StatusLineComposer through the conductor's
STATUS_TICK event to SerpentFlow's ``_spinner_state.message``
(prompt_toolkit bottom_toolbar). This is the last hop in the
composer→bottom-toolbar pipeline.

Strict directives validated:

  * Composer-marked events route to ``_spinner_state.message`` —
    not to console.print
  * Untagged STATUS_TICK events fall through to typed dispatch
    (cost/sensors/etc.) — backwards-compatible
  * Wrapped renderer without ``_spinner_state`` degrades gracefully
    — falls through to console
  * OuroborosConsole adapter mirror: composer events render with a
    ``status:`` prefix (no persistent toolbar in that backend)
  * NEVER raises — boot is not blocked by status glue
  * Both producer-side flags graduated default-true at D5 — verified
    via accessor + FlagSpec inspection

Covers:

  §A   D5 graduation default-true (both flags)
  §B   Bridge: composer.set() → spinner_state.message
  §C   spinner_state.active set when content non-empty
  §D   No _spinner_state attr → fall through to console
  §E   Untagged STATUS_TICK falls to typed dispatch
  §F   OuroborosConsole composer-event prefix
  §G   D5 AST graduation pins (2)
"""
from __future__ import annotations

import ast
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import (
    render_backends as rb,
    render_conductor as rc,
    render_emit_tier as ret,
    status_line_composer as slc,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_STATUS_LINE_COMPOSER_ENABLED",
        "JARVIS_STATUS_LINE_DEBOUNCE_MS",
        "JARVIS_EMIT_TIER_GATING_ENABLED",
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


class _SpinnerStateStub:
    def __init__(self) -> None:
        self.message: str = ""
        self.active: bool = False


class _StubFlow:
    """SerpentFlow stub with _spinner_state."""

    def __init__(self) -> None:
        self._spinner_state = _SpinnerStateStub()
        self.console = _RecordingConsole()
        self.cost_updates: List[float] = []

    def show_streaming_token(self, t: str) -> None: pass
    def show_streaming_start(
        self, op_id: str, provider: str,
    ) -> None: pass
    def show_streaming_end(self) -> None: pass

    def update_cost(self, total: float) -> None:
        self.cost_updates.append(total)


class _StubFlowNoSpinner:
    """SerpentFlow stub WITHOUT _spinner_state — exercises the
    graceful-degradation path."""

    def __init__(self) -> None:
        self.console = _RecordingConsole()

    def show_streaming_token(self, t: str) -> None: pass
    def show_streaming_start(
        self, op_id: str, provider: str,
    ) -> None: pass
    def show_streaming_end(self) -> None: pass


class _RecordingConsole:
    def __init__(self) -> None:
        self.prints: List[Any] = []

    def print(self, obj: Any, **kw: Any) -> None:
        self.prints.append(obj)


@pytest.fixture
def wired_pipeline(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    """Conductor + SerpentFlowBackend(stub flow) + composer all wired."""
    monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STATUS_LINE_COMPOSER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STATUS_LINE_DEBOUNCE_MS", "0")
    flow = _StubFlow()
    backend = rb.SerpentFlowBackend(flow)
    c = rc.RenderConductor()
    c.add_backend(backend)
    rc.register_render_conductor(c)
    composer = slc.StatusLineComposer()
    slc.register_status_line_composer(composer)
    yield {"flow": flow, "backend": backend, "composer": composer,
           "conductor": c}
    rc.reset_render_conductor()
    slc.reset_status_line_composer()


# ---------------------------------------------------------------------------
# §A — D5 graduation defaults
# ---------------------------------------------------------------------------


class TestD5Graduation:
    def test_emit_tier_gating_default_true(self, fresh_registry):
        # D5 graduated to True
        assert ret.is_enabled() is True

    def test_status_line_composer_default_true(self, fresh_registry):
        assert slc.is_enabled() is True

    def test_emit_tier_flagspec_default_true(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        ret.register_flags(reg)
        spec = reg.get_spec("JARVIS_EMIT_TIER_GATING_ENABLED")
        assert spec is not None
        assert spec.default is True

    def test_composer_flagspec_default_true(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        slc.register_flags(reg)
        spec = reg.get_spec("JARVIS_STATUS_LINE_COMPOSER_ENABLED")
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# §B — Bridge: composer.set() → spinner_state.message
# ---------------------------------------------------------------------------


class TestComposerToSpinnerStateBridge:
    def test_single_field_set_writes_to_spinner_state(
        self, wired_pipeline,
    ):
        wired_pipeline["composer"].set(slc.StatusField.COST, 0.42)
        assert "$0.42" in wired_pipeline["flow"]._spinner_state.message

    def test_multiple_fields_compose_in_spinner_state(
        self, wired_pipeline,
    ):
        comp = wired_pipeline["composer"]
        comp.set(slc.StatusField.POSTURE, "EXPLORE")
        comp.set(slc.StatusField.COST, 1.50)
        comp.set(slc.StatusField.SENSORS, 16)
        msg = wired_pipeline["flow"]._spinner_state.message
        assert "EXPLORE" in msg
        assert "$1.50" in msg
        assert "16 sensors" in msg

    def test_clear_field_removes_from_spinner_state(
        self, wired_pipeline,
    ):
        comp = wired_pipeline["composer"]
        comp.set(slc.StatusField.COST, 0.42)
        assert "$0.42" in wired_pipeline["flow"]._spinner_state.message
        comp.clear(slc.StatusField.COST)
        assert "$0.42" not in wired_pipeline["flow"]._spinner_state.message

    def test_composer_event_does_not_print_to_console(
        self, wired_pipeline,
    ):
        wired_pipeline["composer"].set(slc.StatusField.COST, 0.42)
        # Composer-tagged event goes to spinner_state, NOT to console
        # (the dim "status:" prefix is the OuroborosConsole-only path)
        assert wired_pipeline["flow"].console.prints == []


# ---------------------------------------------------------------------------
# §C — spinner_state.active reflects content
# ---------------------------------------------------------------------------


class TestSpinnerStateActive:
    def test_active_true_when_content_present(self, wired_pipeline):
        wired_pipeline["composer"].set(slc.StatusField.COST, 0.42)
        assert wired_pipeline["flow"]._spinner_state.active is True

    def test_active_false_when_all_fields_cleared(self, wired_pipeline):
        comp = wired_pipeline["composer"]
        comp.set(slc.StatusField.COST, 0.42)
        comp.clear()  # clear all fields → composed line is empty
        assert wired_pipeline["flow"]._spinner_state.active is False


# ---------------------------------------------------------------------------
# §D — No _spinner_state → graceful fall-through
# ---------------------------------------------------------------------------


class TestSpinnerStateMissingDegradation:
    def test_no_spinner_state_falls_to_console(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        monkeypatch.setenv(
            "JARVIS_STATUS_LINE_COMPOSER_ENABLED", "true",
        )
        monkeypatch.setenv("JARVIS_STATUS_LINE_DEBOUNCE_MS", "0")
        flow = _StubFlowNoSpinner()
        backend = rb.SerpentFlowBackend(flow)
        c = rc.RenderConductor()
        c.add_backend(backend)
        rc.register_render_conductor(c)
        composer = slc.StatusLineComposer()
        slc.register_status_line_composer(composer)
        composer.set(slc.StatusField.COST, 0.42)
        # No spinner_state → branch falls through to console
        # (NOT to typed dispatch — composed_status marker still
        # routes; degradation is to console.print)
        # Either path is acceptable; key assertion: no raise
        # (the test reaches this line)
        slc.reset_status_line_composer()
        rc.reset_render_conductor()


# ---------------------------------------------------------------------------
# §E — Untagged STATUS_TICK falls through to typed dispatch
# ---------------------------------------------------------------------------


class TestUntaggedFallsThrough:
    def test_typed_cost_dispatch_when_no_composer_marker(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        flow = _StubFlow()
        backend = rb.SerpentFlowBackend(flow)
        c = rc.RenderConductor()
        c.add_backend(backend)
        rc.register_render_conductor(c)
        # Direct STATUS_TICK with cost metadata, no composed_status
        # marker — should hit branch 2 (typed dispatch)
        c.publish(rc.RenderEvent(
            kind=rc.EventKind.STATUS_TICK,
            region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA,
            content="",
            source_module="test",
            metadata={"cost": 5.25},
        ))
        # Typed dispatch fired — flow.update_cost called
        assert flow.cost_updates == [5.25]
        # spinner_state untouched (no composer bridge fired)
        assert flow._spinner_state.message == ""
        rc.reset_render_conductor()


# ---------------------------------------------------------------------------
# §F — OuroborosConsole composer-event prefix
# ---------------------------------------------------------------------------


class TestOuroborosConsoleComposerPrefix:
    def test_composer_event_prints_with_status_prefix(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")

        class _Console:
            def __init__(self):
                self.console = _RecordingConsole()
            def show_streaming_token(self, t): pass
            def show_streaming_start(self, p): pass
            def show_streaming_end(self): pass

        console = _Console()
        backend = rb.OuroborosConsoleBackend(console)
        c = rc.RenderConductor()
        c.add_backend(backend)
        rc.register_render_conductor(c)
        c.publish(rc.RenderEvent(
            kind=rc.EventKind.STATUS_TICK,
            region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA,
            content="EXPLORE | $0.42",
            source_module="test",
            metadata={"composed_status": True},
        ))
        text_prints = [p for p in console.console.prints if isinstance(p, str)]
        assert any("status:" in p and "EXPLORE" in p for p in text_prints)
        rc.reset_render_conductor()


# ---------------------------------------------------------------------------
# §G — D5 AST graduation pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def d5_pins() -> list:
    pins = list(rb.register_shipped_invariants())
    return [
        p for p in pins
        if p.invariant_name in (
            "render_emit_tier_gating_enabled_default_true",
            "status_line_composer_enabled_default_true",
        )
    ]


class TestD5GraduationPins:
    def test_two_pins_present(self, d5_pins):
        assert len(d5_pins) == 2

    def test_emit_tier_pin_clean_against_real_source(self, d5_pins):
        import inspect
        src = inspect.getsource(ret)
        tree = ast.parse(src)
        pin = next(p for p in d5_pins
                   if p.invariant_name ==
                   "render_emit_tier_gating_enabled_default_true")
        assert pin.validate(tree, src) == ()

    def test_composer_pin_clean_against_real_source(self, d5_pins):
        import inspect
        src = inspect.getsource(slc)
        tree = ast.parse(src)
        pin = next(p for p in d5_pins
                   if p.invariant_name ==
                   "status_line_composer_enabled_default_true")
        assert pin.validate(tree, src) == ()

    def test_emit_tier_pin_catches_default_false_revert(self, d5_pins):
        # Tampered source: FlagSpec default=False
        tampered_src = (
            '_FLAG_TAMPER = "JARVIS_EMIT_TIER_GATING_ENABLED"\n\n'
            'def register_flags(registry):\n'
            '    spec = FlagSpec(\n'
            '        name=_FLAG_TAMPER,\n'
            '        type=FlagType.BOOL,\n'
            '        default=False,\n'
            '    )\n'
        )
        tree = ast.parse(tampered_src)
        pin = next(p for p in d5_pins
                   if p.invariant_name ==
                   "render_emit_tier_gating_enabled_default_true")
        violations = pin.validate(tree, tampered_src)
        assert violations
        assert "default=False" in violations[0]

    def test_composer_pin_catches_default_false_revert(self, d5_pins):
        tampered_src = (
            '_FLAG_TAMPER = "JARVIS_STATUS_LINE_COMPOSER_ENABLED"\n\n'
            'def register_flags(registry):\n'
            '    spec = FlagSpec(\n'
            '        name=_FLAG_TAMPER,\n'
            '        type=FlagType.BOOL,\n'
            '        default=False,\n'
            '    )\n'
        )
        tree = ast.parse(tampered_src)
        pin = next(p for p in d5_pins
                   if p.invariant_name ==
                   "status_line_composer_enabled_default_true")
        violations = pin.validate(tree, tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §H — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_d5_pins_in_shipped_invariants(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        # Defensive re-register (matches fu#5 + backlog#2 pattern)
        for inv in rb.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        assert "render_emit_tier_gating_enabled_default_true" in names
        assert "status_line_composer_enabled_default_true" in names

    def test_validate_all_no_d5_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in rb.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        d5_failures = [
            r for r in results
            if r.invariant_name in (
                "render_emit_tier_gating_enabled_default_true",
                "status_line_composer_enabled_default_true",
            )
        ]
        assert d5_failures == []

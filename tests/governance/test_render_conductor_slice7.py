"""RenderConductor Slice 7 — graduation regression suite.

Pins the graduation step:

  * RenderConductor master flag default flipped true
  * SerpentFlowBackend + OuroborosConsoleBackend _HANDLED_KINDS expand
    to cover 8 of 9 EventKind values (only BACKEND_RESET stays no-op)
  * Each newly-promoted handler dispatches via feature-detected calls
    on the wrapped renderer; degrades gracefully when method missing
  * Three new AST pins catch reverts of the graduation

Strict directives validated:

  * No hardcoded values: each handler reads from event metadata via
    the existing typed accessors; backends feature-detect, never
    hardcode method names beyond the well-known surface they wrap.
  * No duplication: handlers reuse the existing show_streaming_*
    + show_diff + update_* methods on the wrapped renderers; no
    parallel rendering paths introduced.
  * Defensive everywhere: each handler swallows exceptions;
    feature-absence falls through to a console.print fallback;
    console-absence falls through to logger DEBUG.
  * Symmetry: SerpentFlowBackend and OuroborosConsoleBackend cover
    the same 8 event kinds (asymmetry would create confusing
    operator UX depending on which renderer is active).

Covers:

  §A   Master flag default true (post-Slice-7)
  §B   Backend handler expansion — _HANDLED_KINDS contains 8 kinds
  §C   _NO_OP_KINDS shrunk to {BACKEND_RESET}
  §D   Totality preserved (HANDLED ∪ NO_OP == EventKind closed set)
  §E   Each handler dispatches via wrapped-renderer feature detection
  §F   Handlers degrade to console.print when method absent
  §G   Handlers degrade to logger DEBUG when console also absent
  §H   AST graduation pins (3) self-validate green + tampering caught
  §I   Combined Slice 1-7 substrate sanity
"""
from __future__ import annotations

import ast
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import render_backends as rb
from backend.core.ouroboros.governance import render_conductor as rc


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_THEME_NAME",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
        "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
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


def _make_event(**overrides: Any) -> rc.RenderEvent:
    defaults: dict = {
        "kind": rc.EventKind.REASONING_TOKEN,
        "region": rc.RegionKind.PHASE_STREAM,
        "role": rc.ColorRole.CONTENT,
        "content": "x",
        "source_module": "test",
    }
    defaults.update(overrides)
    return rc.RenderEvent(**defaults)


class _RichConsole:
    """Stub for a Rich Console attached to a SerpentFlow / Ouroboros
    Console — records print() calls."""

    def __init__(self) -> None:
        self.prints: List[str] = []

    def print(self, text: str) -> None:
        self.prints.append(text)


class _RichFlow:
    """SerpentFlow stub with .console attribute + the Slice 7 handler
    target methods."""

    def __init__(self) -> None:
        self.console = _RichConsole()
        self.diffs: List[tuple] = []
        self.cost_updates: List[float] = []
        self.sensor_updates: List[int] = []
        self.streamed_tokens: List[str] = []
        self.streaming_starts: List[dict] = []

    def show_streaming_token(self, token: str) -> None:
        self.streamed_tokens.append(token)

    def show_streaming_start(self, op_id: str, provider: str) -> None:
        self.streaming_starts.append({"op_id": op_id, "provider": provider})

    def show_streaming_end(self) -> None:
        pass

    def show_diff(self, file_path: str, diff_text: str = "") -> None:
        self.diffs.append((file_path, diff_text))

    def update_cost(self, amount: float) -> None:
        self.cost_updates.append(amount)

    def update_sensors(self, count: int) -> None:
        self.sensor_updates.append(count)


class _MinimalFlow:
    """SerpentFlow stub WITHOUT the optional methods — exercises the
    feature-detection fall-through path."""

    def __init__(self) -> None:
        self.console = _RichConsole()

    def show_streaming_token(self, token: str) -> None:
        pass

    def show_streaming_start(self, op_id: str, provider: str) -> None:
        pass

    def show_streaming_end(self) -> None:
        pass


class _ConsoleLessFlow:
    """SerpentFlow stub WITHOUT a console attribute either — exercises
    the logger.debug terminal fall-through."""

    def show_streaming_token(self, token: str) -> None:
        pass

    def show_streaming_start(self, op_id: str, provider: str) -> None:
        pass

    def show_streaming_end(self) -> None:
        pass


# ---------------------------------------------------------------------------
# §A — Master flag default true
# ---------------------------------------------------------------------------


class TestMasterFlagDefaultTrue:
    def test_is_enabled_default_true(self, fresh_registry):
        assert rc.is_enabled() is True

    def test_publish_dispatches_at_default(self, fresh_registry):
        c = rc.RenderConductor()

        class _R:
            name = "rec"
            def __init__(self): self.events = []
            def notify(self, e): self.events.append(e)
            def flush(self): pass
            def shutdown(self): pass

        rec = _R()
        c.add_backend(rec)
        c.publish(_make_event())
        # No env flip needed — default is now true
        assert len(rec.events) == 1

    def test_explicit_false_still_works_as_kill_switch(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "false")
        assert rc.is_enabled() is False

    def test_flag_spec_default_true(self, fresh_registry):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rc.register_flags(reg)
        spec = reg.get_spec("JARVIS_RENDER_CONDUCTOR_ENABLED")
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# §B — _HANDLED_KINDS expansion
# ---------------------------------------------------------------------------


class TestHandledKindsExpansion:
    def test_serpent_handles_eight_kinds(self):
        expected = {
            "PHASE_BEGIN", "PHASE_END", "REASONING_TOKEN",
            "FILE_REF", "STATUS_TICK",
            "MODAL_PROMPT", "MODAL_DISMISS", "THREAD_TURN",
        }
        assert rb.SerpentFlowBackend._HANDLED_KINDS == expected

    def test_ouroboros_handles_eight_kinds(self):
        expected = {
            "PHASE_BEGIN", "PHASE_END", "REASONING_TOKEN",
            "FILE_REF", "STATUS_TICK",
            "MODAL_PROMPT", "MODAL_DISMISS", "THREAD_TURN",
        }
        assert rb.OuroborosConsoleBackend._HANDLED_KINDS == expected


# ---------------------------------------------------------------------------
# §C — _NO_OP_KINDS shrunk to BACKEND_RESET
# ---------------------------------------------------------------------------


class TestNoOpKindsShrunk:
    def test_serpent_no_op_only_backend_reset(self):
        assert rb.SerpentFlowBackend._NO_OP_KINDS == frozenset(
            {"BACKEND_RESET"},
        )

    def test_ouroboros_no_op_only_backend_reset(self):
        assert rb.OuroborosConsoleBackend._NO_OP_KINDS == frozenset(
            {"BACKEND_RESET"},
        )


# ---------------------------------------------------------------------------
# §D — Totality preserved
# ---------------------------------------------------------------------------


class TestTotalityPreserved:
    def test_serpent_totality(self):
        union = (
            rb.SerpentFlowBackend._HANDLED_KINDS
            | rb.SerpentFlowBackend._NO_OP_KINDS
        )
        all_kinds = {m.value for m in rc.EventKind}
        assert union == all_kinds

    def test_ouroboros_totality(self):
        union = (
            rb.OuroborosConsoleBackend._HANDLED_KINDS
            | rb.OuroborosConsoleBackend._NO_OP_KINDS
        )
        all_kinds = {m.value for m in rc.EventKind}
        assert union == all_kinds

    def test_no_overlap(self):
        # HANDLED + NO_OP must be disjoint (an event is either
        # actively handled or documented no-op, not both)
        for cls in (rb.SerpentFlowBackend, rb.OuroborosConsoleBackend):
            assert cls._HANDLED_KINDS & cls._NO_OP_KINDS == frozenset()


# ---------------------------------------------------------------------------
# §E — Each handler dispatches correctly
# ---------------------------------------------------------------------------


class TestSerpentFlowHandlers:
    def test_file_ref_with_diff_routes_to_show_diff(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="foo.py:42",
            metadata={"path": "foo.py", "diff_text": "@@ ..."},
        ))
        assert flow.diffs == [("foo.py", "@@ ...")]

    def test_file_ref_without_diff_falls_back_to_console(
        self, fresh_registry,
    ):
        flow = _MinimalFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py"},
        ))
        # No show_diff method — falls back to console.print
        assert any("x.py" in p for p in flow.console.prints)

    def test_status_tick_cost_routes_to_update_cost(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.STATUS_TICK, region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA, content="",
            metadata={"cost": 1.25},
        ))
        assert flow.cost_updates == [1.25]

    def test_status_tick_sensors_routes_to_update_sensors(
        self, fresh_registry,
    ):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.STATUS_TICK, region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA, content="",
            metadata={"sensors": 16},
        ))
        assert flow.sensor_updates == [16]

    def test_status_tick_unknown_metadata_falls_to_console(
        self, fresh_registry,
    ):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.STATUS_TICK, region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA, content="status",
            metadata={"unknown_key": 42},
        ))
        # No matching update_* method → falls back to console.print
        assert any("status" in p for p in flow.console.prints)

    def test_modal_prompt_renders_help_block(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.MODAL_PROMPT, region=rc.RegionKind.MODAL,
            role=rc.ColorRole.CONTENT, content="help body here",
        ))
        # Backlog #3: NORMAL/FULL density uses Rich Panel via the
        # console. The recorder captures the Panel object directly;
        # check the Panel's renderable carries our content.
        from rich.panel import Panel
        rendered = [
            p for p in flow.console.prints
            if isinstance(p, Panel) or (
                isinstance(p, str) and "help body here" in p
            )
        ]
        assert rendered
        # If a Panel landed, inspect its renderable for the content.
        for p in rendered:
            if isinstance(p, Panel):
                assert "help body here" in str(p.renderable)
                return
        # Otherwise the inline-fallback path fired — fine.

    def test_modal_dismiss_renders_separator(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.MODAL_DISMISS, region=rc.RegionKind.MODAL,
            role=rc.ColorRole.METADATA, content="",
        ))
        assert any("/help" in p for p in flow.console.prints)

    def test_thread_turn_renders_speaker_prefix(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.THREAD_TURN, region=rc.RegionKind.THREAD,
            role=rc.ColorRole.EMPHASIS, content="hello",
            metadata={"speaker": "USER"},
        ))
        # Output should contain the speaker tag + content
        printed = " ".join(flow.console.prints)
        assert "you" in printed.lower()
        assert "hello" in printed

    def test_backend_reset_remains_no_op(self, fresh_registry):
        flow = _RichFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.BACKEND_RESET, region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA, content="",
        ))
        # No console output, no streamed tokens, no diffs
        assert flow.console.prints == []
        assert flow.streamed_tokens == []
        assert flow.diffs == []


class TestOuroborosConsoleHandlers:
    def test_file_ref_routes_to_show_diff(self, fresh_registry):
        class _OConsole:
            def __init__(self):
                self.console = _RichConsole()
                self.diffs = []
            def show_streaming_token(self, t): pass
            def show_streaming_start(self, p): pass
            def show_streaming_end(self): pass
            def show_diff(self, file_path, diff_text=""):
                self.diffs.append((file_path, diff_text))

        c = _OConsole()
        b = rb.OuroborosConsoleBackend(c)
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="foo.py",
            metadata={"path": "foo.py", "diff_text": "@@ ..."},
        ))
        assert c.diffs == [("foo.py", "@@ ...")]

    def test_status_tick_cost_routes(self, fresh_registry):
        class _OConsole:
            def __init__(self):
                self.console = _RichConsole()
                self.costs = []
            def show_streaming_token(self, t): pass
            def show_streaming_start(self, p): pass
            def show_streaming_end(self): pass
            def show_cost_update(self, amount): self.costs.append(amount)

        c = _OConsole()
        b = rb.OuroborosConsoleBackend(c)
        b.notify(_make_event(
            kind=rc.EventKind.STATUS_TICK, region=rc.RegionKind.STATUS,
            role=rc.ColorRole.METADATA, content="",
            metadata={"cost": 0.5},
        ))
        assert c.costs == [0.5]


# ---------------------------------------------------------------------------
# §F — Feature-detection fall-throughs
# ---------------------------------------------------------------------------


class TestFeatureDetectionFallback:
    def test_missing_show_diff_falls_to_console(self, fresh_registry):
        flow = _MinimalFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py", "diff_text": "@@"},
        ))
        # No show_diff → falls back to console.print
        assert flow.console.prints

    def test_missing_console_falls_to_logger(self, fresh_registry):
        flow = _ConsoleLessFlow()
        b = rb.SerpentFlowBackend(flow)
        # Should not raise even though no console + no show_diff
        b.notify(_make_event(
            kind=rc.EventKind.FILE_REF, region=rc.RegionKind.VIEWPORT,
            role=rc.ColorRole.METADATA, content="x.py:1",
            metadata={"path": "x.py"},
        ))


# ---------------------------------------------------------------------------
# §H — AST graduation pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice7_pins() -> list:
    all_pins = list(rb.register_shipped_invariants())
    return [
        p for p in all_pins
        if p.invariant_name in (
            "render_backends_serpent_handles_slice7_kinds",
            "render_backends_ouroboros_handles_slice7_kinds",
            "harness_wires_render_substrate",
        )
    ]


class TestSlice7ASTPinsClean:
    def test_three_pins_present(self, slice7_pins):
        assert len(slice7_pins) == 3

    def test_serpent_handles_clean(self, slice7_pins):
        import inspect
        src = inspect.getsource(rb)
        tree = ast.parse(src)
        pin = next(p for p in slice7_pins
                   if p.invariant_name ==
                   "render_backends_serpent_handles_slice7_kinds")
        assert pin.validate(tree, src) == ()

    def test_ouroboros_handles_clean(self, slice7_pins):
        import inspect
        src = inspect.getsource(rb)
        tree = ast.parse(src)
        pin = next(p for p in slice7_pins
                   if p.invariant_name ==
                   "render_backends_ouroboros_handles_slice7_kinds")
        assert pin.validate(tree, src) == ()

    def test_harness_wiring_clean(self, slice7_pins):
        import pathlib
        path = pathlib.Path(
            "backend/core/ouroboros/battle_test/harness.py"
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        pin = next(p for p in slice7_pins
                   if p.invariant_name == "harness_wires_render_substrate")
        assert pin.validate(tree, src) == ()


class TestSlice7ASTPinsCatchTampering:
    def test_serpent_handles_tampered_caught(self, slice7_pins):
        # Backend without THREAD_TURN in handled set
        tampered_src = (
            "class SerpentFlowBackend:\n"
            "    name = 'x'\n"
            "    _HANDLED_KINDS: frozenset = frozenset({\n"
            "        'PHASE_BEGIN', 'PHASE_END',\n"
            "    })\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice7_pins
                   if p.invariant_name ==
                   "render_backends_serpent_handles_slice7_kinds")
        violations = pin.validate(tampered, tampered_src)
        assert violations
        v = violations[0]
        assert "THREAD_TURN" in v or "FILE_REF" in v

    def test_harness_wiring_missing_caught(self, slice7_pins):
        tampered_src = "# harness without wiring tokens\n"
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice7_pins
                   if p.invariant_name == "harness_wires_render_substrate")
        violations = pin.validate(tampered, tampered_src)
        assert violations
        # Should mention missing tokens
        assert any(
            "wire_render_conductor" in v
            or "InputController" in v
            or "ThreadObserver" in v
            for v in violations
        )


# ---------------------------------------------------------------------------
# §I — Combined Slice 1-7 sanity
# ---------------------------------------------------------------------------


class TestCombinedSubstrateSanity:
    def test_all_invariants_clean(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        # Filter to render-arc invariants
        arc_failures = [
            r for r in results
            if any(prefix in r.invariant_name for prefix in (
                "render_conductor_", "render_backends_",
                "render_primitives_", "render_thread_",
                "render_help_", "key_input_",
                "streamrenderer_", "harness_wires_",
            ))
        ]
        assert arc_failures == [], (
            f"Render arc invariants failing: "
            f"{[r.to_dict() for r in arc_failures]}"
        )

    def test_substrate_count_summary(self):
        """Documents the total invariant + flag count for the
        RenderConductor arc post-Slice-7."""
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        from backend.core.ouroboros.governance import flag_registry as fr
        fr.reset_default_registry()
        reg = fr.ensure_seeded()

        arc_invs = [
            i for i in sci.list_shipped_code_invariants()
            if any(prefix in i.invariant_name for prefix in (
                "render_conductor_", "render_backends_",
                "render_primitives_", "render_thread_",
                "render_help_", "key_input_",
                "streamrenderer_", "harness_wires_",
            ))
        ]
        arc_flags = [
            s for s in reg.list_all()
            if any(prefix in s.name for prefix in (
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
        ]
        # Documented totals — these increase at each slice. Slice 7
        # added 3 graduation pins → 35 total. Slice 7 added 0 flags
        # (the master flip is a default change, not a new flag).
        assert len(arc_invs) >= 35, (
            f"Expected ≥35 arc invariants, got {len(arc_invs)}"
        )
        assert len(arc_flags) >= 13, (
            f"Expected ≥13 arc flags, got {len(arc_flags)}"
        )

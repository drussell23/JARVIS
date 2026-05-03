"""RenderConductor Slice 2 — backend migration regression suite.

Pins the three-renderer migration: ``StreamRenderer`` (inline backend),
``SerpentFlow`` and ``OuroborosConsole`` (composition adapters in
``render_backends.py``). Closes Slice 2's load-bearing risk by proving
every renderer is RenderBackend-compliant AND that conductor.publish
reaches each backend's wrapped renderer.

Strict directives validated:

  * No duplication: backends route to the SAME internal pipeline as
    legacy producer entry points (on_token, show_streaming_token, etc).
    Both call paths land at the wrapped renderer's existing methods.
  * Adapter totality: every EventKind is either handled or
    documented-no-op'd by each adapter. The HANDLED ∪ NO_OP partition
    must equal the full EventKind closed set — tested explicitly.
  * Defensive everywhere: every notify/flush/shutdown swallows
    exceptions; backend-side failure can never propagate to siblings
    or block the conductor's fan-out.
  * AST-pinned cross-file contract: StreamRenderer's backend
    conformance is pinned from render_backends.py via a cross-file
    target_file — caught at boot if any renderer drops a method.
  * Boot wire is total: wire_render_conductor accepts any subset of
    {None, alive} for each renderer; never raises out of itself even
    on partial backend wiring failure.

Covers:

  §A   StreamRenderer satisfies RenderBackend Protocol (4 symbols)
  §B   StreamRenderer.notify dispatch matrix per EventKind
  §C   StreamRenderer flush/shutdown idempotent + defensive
  §D   SerpentFlowBackend Protocol conformance (4 symbols + name)
  §E   SerpentFlowBackend.notify dispatch matrix
  §F   SerpentFlowBackend HANDLED ∪ NO_OP totality over EventKind
  §G   SerpentFlowBackend exception isolation
  §H   OuroborosConsoleBackend mirror of §D-§G
  §I   wire_render_conductor — all 3 backends, single, all-None,
       never-raises on backend failure
  §J   End-to-end: conductor.publish → all attached backends
  §K   AST pins (Slice 2's 3 invariants) self-validate green +
       tamper-detection
"""
from __future__ import annotations

import ast
import threading
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance import render_backends as rb
from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.battle_test import stream_renderer as sr


# ---------------------------------------------------------------------------
# Shared fixtures + stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_THEME_NAME",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
        "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
        "JARVIS_UI_STREAMING_ENABLED",
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


class _StubFlow:
    """Duck-typed SerpentFlow stub recording every adapter dispatch."""

    def __init__(self) -> None:
        self.tokens: List[str] = []
        self.starts: List[Dict[str, Any]] = []
        self.ends: int = 0

    def show_streaming_token(self, token: str) -> None:
        self.tokens.append(token)

    def show_streaming_start(
        self, op_id: str = "", provider: str = "",
    ) -> None:
        self.starts.append({"op_id": op_id, "provider": provider})

    def show_streaming_end(self) -> None:
        self.ends += 1


class _StubFlowKwOnly:
    """Variant exposing kwarg-only show_streaming_start (drives the
    TypeError fallback path)."""

    def __init__(self) -> None:
        self.starts: List[Dict[str, Any]] = []

    def show_streaming_start(self, *, op_id: str, provider: str) -> None:
        self.starts.append({"op_id": op_id, "provider": provider})

    def show_streaming_token(self, token: str) -> None:
        pass

    def show_streaming_end(self) -> None:
        pass


class _RaisingFlow:
    """Stub whose every method raises — proves adapter exception
    swallowing."""

    def show_streaming_token(self, token: str) -> None:
        raise RuntimeError("token boom")

    def show_streaming_start(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("start boom")

    def show_streaming_end(self) -> None:
        raise RuntimeError("end boom")


def _make_event(**overrides: Any) -> rc.RenderEvent:
    defaults: Dict[str, Any] = {
        "kind": rc.EventKind.REASONING_TOKEN,
        "region": rc.RegionKind.PHASE_STREAM,
        "role": rc.ColorRole.CONTENT,
        "content": "tok",
        "source_module": "test",
    }
    defaults.update(overrides)
    return rc.RenderEvent(**defaults)


# ---------------------------------------------------------------------------
# §A — StreamRenderer satisfies RenderBackend Protocol
# ---------------------------------------------------------------------------


class TestStreamRendererProtocolConformance:
    def test_has_name_attribute(self):
        renderer = sr.StreamRenderer()
        assert getattr(renderer, "name", None) == "stream_renderer"

    def test_has_notify(self):
        assert callable(getattr(sr.StreamRenderer, "notify", None))

    def test_has_flush(self):
        assert callable(getattr(sr.StreamRenderer, "flush", None))

    def test_has_shutdown(self):
        assert callable(getattr(sr.StreamRenderer, "shutdown", None))

    def test_satisfies_render_backend_protocol(self):
        renderer = sr.StreamRenderer()
        assert isinstance(renderer, rc.RenderBackend)


# ---------------------------------------------------------------------------
# §B — StreamRenderer.notify dispatch matrix
# ---------------------------------------------------------------------------


class TestStreamRendererNotifyDispatch:
    def test_reasoning_token_routes_to_on_token(self):
        renderer = sr.StreamRenderer()
        with mock.patch.object(renderer, "on_token") as on_token:
            renderer.notify(_make_event(content="abc"))
        on_token.assert_called_once_with("abc")

    def test_phase_begin_routes_to_start_with_provider_from_metadata(self):
        renderer = sr.StreamRenderer()
        ev = _make_event(
            kind=rc.EventKind.PHASE_BEGIN,
            content="",
            op_id="op-42",
            metadata={"provider": "claude"},
        )
        with mock.patch.object(renderer, "start") as start:
            renderer.notify(ev)
        start.assert_called_once_with("op-42", "claude")

    def test_phase_begin_without_op_id_skipped(self):
        renderer = sr.StreamRenderer()
        ev = _make_event(kind=rc.EventKind.PHASE_BEGIN, content="")
        with mock.patch.object(renderer, "start") as start:
            renderer.notify(ev)
        start.assert_not_called()

    def test_phase_end_routes_to_end(self):
        renderer = sr.StreamRenderer()
        ev = _make_event(kind=rc.EventKind.PHASE_END, content="")
        with mock.patch.object(renderer, "end") as end:
            renderer.notify(ev)
        end.assert_called_once()

    def test_backend_reset_routes_to_end(self):
        renderer = sr.StreamRenderer()
        ev = _make_event(kind=rc.EventKind.BACKEND_RESET, content="")
        with mock.patch.object(renderer, "end") as end:
            renderer.notify(ev)
        end.assert_called_once()

    def test_unrelated_event_kind_no_op(self):
        renderer = sr.StreamRenderer()
        for kind in (
            rc.EventKind.STATUS_TICK, rc.EventKind.MODAL_PROMPT,
            rc.EventKind.MODAL_DISMISS, rc.EventKind.THREAD_TURN,
            rc.EventKind.FILE_REF,
        ):
            ev = _make_event(kind=kind, content="")
            with mock.patch.object(renderer, "on_token") as on_token, \
                 mock.patch.object(renderer, "start") as start, \
                 mock.patch.object(renderer, "end") as end:
                renderer.notify(ev)
            on_token.assert_not_called()
            start.assert_not_called()
            end.assert_not_called()

    def test_notify_none_no_crash(self):
        renderer = sr.StreamRenderer()
        renderer.notify(None)  # should not raise

    def test_notify_swallows_exception(self):
        renderer = sr.StreamRenderer()
        with mock.patch.object(
            renderer, "on_token", side_effect=RuntimeError("boom"),
        ):
            renderer.notify(_make_event(content="x"))  # should not raise


# ---------------------------------------------------------------------------
# §C — StreamRenderer flush/shutdown idempotent + defensive
# ---------------------------------------------------------------------------


class TestStreamRendererLifecycle:
    def test_flush_when_inactive_no_raise(self):
        renderer = sr.StreamRenderer()
        renderer.flush()

    def test_shutdown_when_inactive_no_raise(self):
        renderer = sr.StreamRenderer()
        renderer.shutdown()

    def test_shutdown_idempotent(self):
        renderer = sr.StreamRenderer()
        renderer.shutdown()
        renderer.shutdown()


# ---------------------------------------------------------------------------
# §D — SerpentFlowBackend Protocol conformance
# ---------------------------------------------------------------------------


class TestSerpentFlowBackendConformance:
    def test_name(self):
        b = rb.SerpentFlowBackend(_StubFlow())
        assert b.name == "serpent_flow"

    def test_satisfies_render_backend_protocol(self):
        b = rb.SerpentFlowBackend(_StubFlow())
        assert isinstance(b, rc.RenderBackend)

    def test_handled_kinds_disjoint_from_no_op(self):
        assert (
            rb.SerpentFlowBackend._HANDLED_KINDS
            & rb.SerpentFlowBackend._NO_OP_KINDS
        ) == frozenset()


# ---------------------------------------------------------------------------
# §E — SerpentFlowBackend.notify dispatch matrix
# ---------------------------------------------------------------------------


class TestSerpentFlowBackendDispatch:
    def test_reasoning_token_routes(self):
        flow = _StubFlow()
        rb.SerpentFlowBackend(flow).notify(_make_event(content="hi "))
        assert flow.tokens == ["hi "]

    def test_empty_token_skipped(self):
        flow = _StubFlow()
        rb.SerpentFlowBackend(flow).notify(_make_event(content=""))
        assert flow.tokens == []

    def test_phase_begin_routes(self):
        flow = _StubFlow()
        rb.SerpentFlowBackend(flow).notify(_make_event(
            kind=rc.EventKind.PHASE_BEGIN, content="",
            op_id="op-1", metadata={"provider": "dw"},
        ))
        assert flow.starts == [{"op_id": "op-1", "provider": "dw"}]

    def test_phase_begin_kwarg_only_signature_fallback(self):
        flow = _StubFlowKwOnly()
        rb.SerpentFlowBackend(flow).notify(_make_event(
            kind=rc.EventKind.PHASE_BEGIN, content="",
            op_id="op-2", metadata={"provider": "claude"},
        ))
        assert flow.starts == [{"op_id": "op-2", "provider": "claude"}]

    def test_phase_end_routes(self):
        flow = _StubFlow()
        b = rb.SerpentFlowBackend(flow)
        b.notify(_make_event(kind=rc.EventKind.PHASE_END, content=""))
        assert flow.ends == 1

    @pytest.mark.parametrize("kind", [
        rc.EventKind.FILE_REF, rc.EventKind.STATUS_TICK,
        rc.EventKind.MODAL_PROMPT, rc.EventKind.MODAL_DISMISS,
        rc.EventKind.THREAD_TURN, rc.EventKind.BACKEND_RESET,
    ])
    def test_no_op_kinds_dont_reach_renderer(self, kind: rc.EventKind):
        flow = _StubFlow()
        rb.SerpentFlowBackend(flow).notify(_make_event(kind=kind, content=""))
        assert flow.tokens == []
        assert flow.starts == []
        assert flow.ends == 0

    def test_notify_none_no_crash(self):
        rb.SerpentFlowBackend(_StubFlow()).notify(None)


# ---------------------------------------------------------------------------
# §F — Adapter totality over EventKind
# ---------------------------------------------------------------------------


class TestSerpentFlowBackendTotality:
    def test_handled_union_no_op_equals_event_kind_closed_set(self):
        union = (
            rb.SerpentFlowBackend._HANDLED_KINDS
            | rb.SerpentFlowBackend._NO_OP_KINDS
        )
        all_kinds = {m.value for m in rc.EventKind}
        assert union == all_kinds


# ---------------------------------------------------------------------------
# §G — SerpentFlowBackend exception isolation
# ---------------------------------------------------------------------------


class TestSerpentFlowBackendExceptionIsolation:
    def test_notify_swallows_renderer_exception(self):
        b = rb.SerpentFlowBackend(_RaisingFlow())
        b.notify(_make_event(content="x"))
        b.notify(_make_event(kind=rc.EventKind.PHASE_BEGIN, op_id="op"))
        b.notify(_make_event(kind=rc.EventKind.PHASE_END, content=""))
        # No exception escaped — test passes by reaching here

    def test_shutdown_swallows_exception(self):
        rb.SerpentFlowBackend(_RaisingFlow()).shutdown()

    def test_flush_no_raise(self):
        rb.SerpentFlowBackend(_RaisingFlow()).flush()


# ---------------------------------------------------------------------------
# §H — OuroborosConsoleBackend mirror of §D-§G
# ---------------------------------------------------------------------------


class _StubConsole:
    def __init__(self) -> None:
        self.tokens: List[str] = []
        self.starts: List[str] = []
        self.ends: int = 0

    def show_streaming_token(self, token: str) -> None:
        self.tokens.append(token)

    def show_streaming_start(self, provider: str) -> None:
        self.starts.append(provider)

    def show_streaming_end(self) -> None:
        self.ends += 1


class TestOuroborosConsoleBackend:
    def test_name(self):
        b = rb.OuroborosConsoleBackend(_StubConsole())
        assert b.name == "ouroboros_console"

    def test_satisfies_protocol(self):
        b = rb.OuroborosConsoleBackend(_StubConsole())
        assert isinstance(b, rc.RenderBackend)

    def test_reasoning_token_routes(self):
        c = _StubConsole()
        rb.OuroborosConsoleBackend(c).notify(_make_event(content="zz"))
        assert c.tokens == ["zz"]

    def test_phase_begin_routes(self):
        c = _StubConsole()
        rb.OuroborosConsoleBackend(c).notify(_make_event(
            kind=rc.EventKind.PHASE_BEGIN, content="",
            metadata={"provider": "openai"},
        ))
        assert c.starts == ["openai"]

    def test_phase_end_routes(self):
        c = _StubConsole()
        rb.OuroborosConsoleBackend(c).notify(_make_event(
            kind=rc.EventKind.PHASE_END, content="",
        ))
        assert c.ends == 1

    def test_handled_union_no_op_equals_event_kind_closed_set(self):
        union = (
            rb.OuroborosConsoleBackend._HANDLED_KINDS
            | rb.OuroborosConsoleBackend._NO_OP_KINDS
        )
        all_kinds = {m.value for m in rc.EventKind}
        assert union == all_kinds

    def test_notify_swallows_exception(self):
        b = rb.OuroborosConsoleBackend(_RaisingFlow())
        b.notify(_make_event(content="x"))
        b.shutdown()
        b.flush()


# ---------------------------------------------------------------------------
# §I — wire_render_conductor boot helper
# ---------------------------------------------------------------------------


class TestWireRenderConductor:
    def test_all_three_backends_attached(self, fresh_registry):
        renderer = sr.StreamRenderer()
        flow = _StubFlow()
        console = _StubConsole()
        c = rb.wire_render_conductor(
            stream_renderer=renderer,
            serpent_flow=flow,
            ouroboros_console=console,
        )
        assert c is not None
        assert len(c.backends()) == 3

    def test_partial_only_stream_renderer(self, fresh_registry):
        renderer = sr.StreamRenderer()
        c = rb.wire_render_conductor(stream_renderer=renderer)
        assert c is not None
        assert len(c.backends()) == 1

    def test_all_none_yields_empty_conductor(self, fresh_registry):
        c = rb.wire_render_conductor()
        assert c is not None
        assert c.backends() == ()

    def test_registers_as_process_global_singleton(self, fresh_registry):
        c = rb.wire_render_conductor()
        assert rc.get_render_conductor() is c

    def test_replaces_prior_conductor(self, fresh_registry):
        c1 = rb.wire_render_conductor()
        c2 = rb.wire_render_conductor()
        assert c1 is not c2
        assert rc.get_render_conductor() is c2

    def test_posture_provider_wired(self, fresh_registry):
        rb.wire_render_conductor(posture_provider=lambda: "EXPLORE")
        c = rc.get_render_conductor()
        assert c is not None
        assert c.active_density() is rc.RenderDensity.FULL


# ---------------------------------------------------------------------------
# §J — End-to-end conductor.publish reaches all attached backends
# ---------------------------------------------------------------------------


class TestEndToEndConductorPublish:
    def test_token_event_reaches_all_three_backends(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        renderer = sr.StreamRenderer()
        flow = _StubFlow()
        console = _StubConsole()
        c = rb.wire_render_conductor(
            stream_renderer=renderer,
            serpent_flow=flow,
            ouroboros_console=console,
        )
        ev = _make_event(content="ABC")
        with mock.patch.object(renderer, "on_token") as on_token:
            assert c is not None
            c.publish(ev)
        on_token.assert_called_once_with("ABC")
        assert flow.tokens == ["ABC"]
        assert console.tokens == ["ABC"]

    def test_master_off_no_event_reaches_backends(self, fresh_registry):
        flow = _StubFlow()
        c = rb.wire_render_conductor(serpent_flow=flow)
        assert c is not None
        c.publish(_make_event(content="X"))
        assert flow.tokens == []

    def test_one_backend_exception_doesnt_block_siblings(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        flow = _StubFlow()
        c = rb.wire_render_conductor(
            serpent_flow=_RaisingFlow(),
        )
        assert c is not None
        # Add a second working backend after wire
        c.add_backend(rb.SerpentFlowBackend(flow))
        c.publish(_make_event(content="z"))
        assert flow.tokens == ["z"]

    def test_concurrent_publish_thread_safe(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        flow = _StubFlow()
        c = rb.wire_render_conductor(serpent_flow=flow)
        assert c is not None
        N = 50

        def _publish() -> None:
            c.publish(_make_event(content="x"))

        threads = [threading.Thread(target=_publish) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(flow.tokens) == N


# ---------------------------------------------------------------------------
# §K — AST pins self-validate green + tamper detection
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice2_pins() -> list:
    return list(rb.register_shipped_invariants())


class TestSlice2ASTPinsClean:
    def test_three_pins_registered(self, slice2_pins):
        assert len(slice2_pins) == 3
        names = {i.invariant_name for i in slice2_pins}
        assert names == {
            "render_backends_no_authority_imports",
            "render_backends_adapter_protocol_conformance",
            "streamrenderer_protocol_conformance",
        }

    def test_no_authority_imports_clean(self, slice2_pins):
        import inspect
        src = inspect.getsource(rb)
        tree = ast.parse(src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "render_backends_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_adapter_conformance_clean(self, slice2_pins):
        import inspect
        src = inspect.getsource(rb)
        tree = ast.parse(src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "render_backends_adapter_protocol_conformance")
        assert pin.validate(tree, src) == ()

    def test_streamrenderer_conformance_clean(self, slice2_pins):
        import inspect
        src = inspect.getsource(sr)
        tree = ast.parse(src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "streamrenderer_protocol_conformance")
        assert pin.validate(tree, src) == ()


class TestSlice2ASTPinsCatchTampering:
    def test_authority_import_caught(self, slice2_pins):
        tampered_src = (
            "from backend.core.ouroboros.governance.policy import x\n"
        )
        tree = ast.parse(tampered_src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "render_backends_no_authority_imports")
        violations = pin.validate(tree, tampered_src)
        assert any("policy" in v for v in violations)

    def test_missing_adapter_method_caught(self, slice2_pins):
        tampered_src = (
            "class SerpentFlowBackend:\n"
            "    name = 'x'\n"
            "    def notify(self, e): pass\n"
            "    # flush + shutdown intentionally missing\n"
            "class OuroborosConsoleBackend:\n"
            "    name = 'y'\n"
            "    def notify(self, e): pass\n"
            "    def flush(self): pass\n"
            "    def shutdown(self): pass\n"
        )
        tree = ast.parse(tampered_src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "render_backends_adapter_protocol_conformance")
        violations = pin.validate(tree, tampered_src)
        assert any("SerpentFlowBackend" in v for v in violations)
        assert any("flush" in v or "shutdown" in v for v in violations)

    def test_missing_adapter_class_caught(self, slice2_pins):
        tampered_src = "class SomethingElse: pass\n"
        tree = ast.parse(tampered_src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "render_backends_adapter_protocol_conformance")
        violations = pin.validate(tree, tampered_src)
        assert any("SerpentFlowBackend" in v for v in violations)

    def test_streamrenderer_missing_method_caught(self, slice2_pins):
        tampered_src = (
            "class StreamRenderer:\n"
            "    name = 'x'\n"
            "    def notify(self, e): pass\n"
            "    # flush + shutdown intentionally missing\n"
        )
        tree = ast.parse(tampered_src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "streamrenderer_protocol_conformance")
        violations = pin.validate(tree, tampered_src)
        assert violations

    def test_streamrenderer_class_missing_caught(self, slice2_pins):
        tampered_src = "class SomethingElse: pass\n"
        tree = ast.parse(tampered_src)
        pin = next(p for p in slice2_pins
                   if p.invariant_name ==
                   "streamrenderer_protocol_conformance")
        violations = pin.validate(tree, tampered_src)
        assert violations


class TestAutoDiscoveryIntegration:
    def test_shipped_invariants_includes_slice2_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        assert "render_backends_no_authority_imports" in names
        assert "render_backends_adapter_protocol_conformance" in names
        assert "streamrenderer_protocol_conformance" in names

    def test_validate_all_no_slice2_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        slice2_names = {
            "render_backends_no_authority_imports",
            "render_backends_adapter_protocol_conformance",
            "streamrenderer_protocol_conformance",
        }
        ours = [r for r in results
                if r.invariant_name in slice2_names]
        assert ours == [], (
            f"Slice 2 pins reporting violations: "
            f"{[r.to_dict() for r in ours]}"
        )

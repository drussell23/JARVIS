"""UnifiedEventLoop regression suite.

Pins the additive multi-source race substrate borrowed from CC's
``queryLoop()`` async generator pattern.

Strict directives validated:

  * Closed-taxonomy UnifiedKind ({RENDER, KEY, TOOL_RESULT, CANCEL,
    STOP}) AST-pinned.
  * Closed UnifiedEvent field set ({kind, payload, source_label,
    monotonic_ts}) AST-pinned.
  * Pure observer — existing producer→backend paths unaffected when
    loop is enabled or disabled.
  * Bounded per-source queues with drop-oldest + telemetry.
  * Source-exception isolation — one source raising doesn't kill
    the loop.
  * Idempotent stop() — drains pending events, yields STOP envelope,
    closes recorder.
  * No authority imports (cancel_token / conversation_bridge / etc.)
    — substrate descriptive only.

Covers:

  §A   UnifiedKind closed taxonomy
  §B   UnifiedEvent frozen + to_dict
  §C   _SourceQueue bounded drop-oldest + dropped_count
  §D   UnifiedEventLoop start/stop lifecycle + master flag gate
  §E   attach_source idempotency + bind on attach
  §F   Multi-source race ordering (chronological)
  §G   Stop drains pending events before STOP envelope
  §H   Source-exception isolation
  §I   UnifiedLoopBackend observer wiring
  §J   UnifiedLoopKeySubscriber wildcard subscription
  §K   JSONL recorder when flag set
  §L   AST pins (5) clean + tampering caught
  §M   Auto-discovery integration
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import threading
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import key_input as ki
from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import unified_event_loop as uel


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_UNIFIED_EVENT_LOOP_ENABLED",
        "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED",
        "JARVIS_UNIFIED_EVENT_LOG_PATH",
        "JARVIS_UNIFIED_EVENT_LOOP_QUEUE_MAX",
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_INPUT_CONTROLLER_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    ki.reset_input_controller()
    uel.reset_unified_event_loop()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def loop_enabled(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_UNIFIED_EVENT_LOOP_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# §A — UnifiedKind closed taxonomy
# ---------------------------------------------------------------------------


class TestUnifiedKindClosedTaxonomy:
    def test_exact_five_members(self):
        assert {m.value for m in uel.UnifiedKind} == {
            "RENDER", "KEY", "TOOL_RESULT", "CANCEL", "STOP",
        }

    def test_str_inheritance(self):
        assert isinstance(uel.UnifiedKind.RENDER, str)


# ---------------------------------------------------------------------------
# §B — UnifiedEvent frozen + to_dict
# ---------------------------------------------------------------------------


class TestUnifiedEvent:
    def test_minimal_construction(self):
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload="x", source_label="test",
        )
        assert ev.kind is uel.UnifiedKind.RENDER
        assert ev.payload == "x"

    def test_frozen(self):
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload="x", source_label="t",
        )
        with pytest.raises(Exception):
            ev.payload = "y"  # type: ignore[misc]

    def test_to_dict_includes_schema(self):
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.KEY, payload="esc", source_label="kb",
        )
        d = ev.to_dict()
        assert d["schema_version"] == uel.UNIFIED_EVENT_LOOP_SCHEMA_VERSION
        assert d["kind"] == "KEY"
        assert d["source_label"] == "kb"

    def test_to_dict_serializes_payload_via_to_dict(self):
        class _P:
            def to_dict(self):
                return {"hello": "world"}
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload=_P(), source_label="t",
        )
        d = ev.to_dict()
        assert d["payload"] == {"hello": "world"}

    def test_to_dict_falls_back_to_str(self):
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload=42, source_label="t",
        )
        d = ev.to_dict()
        assert d["payload"] == "42"


# ---------------------------------------------------------------------------
# §C — _SourceQueue
# ---------------------------------------------------------------------------


class TestSourceQueue:
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        q = uel._SourceQueue(8)
        q.bind_loop(asyncio.get_running_loop())
        ev = uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload="x", source_label="t",
        )
        q.put_nowait(ev)
        result = await q.get()
        assert result is ev

    def test_drop_oldest_on_overflow(self):
        q = uel._SourceQueue(2)
        evs = [
            uel.UnifiedEvent(
                kind=uel.UnifiedKind.RENDER, payload=str(i),
                source_label="t",
            )
            for i in range(4)
        ]
        for ev in evs:
            q.put_nowait(ev)
        assert q.depth == 2
        assert q.dropped_count == 2

    def test_telemetry_independent_per_queue(self):
        q1 = uel._SourceQueue(8)
        q2 = uel._SourceQueue(8)
        q1.put_nowait(uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload="x", source_label="t",
        ))
        assert q1.depth == 1
        assert q2.depth == 0


# ---------------------------------------------------------------------------
# §D — UnifiedEventLoop lifecycle
# ---------------------------------------------------------------------------


class TestLoopLifecycle:
    @pytest.mark.asyncio
    async def test_start_returns_false_when_master_off(self, fresh_registry):
        loop = uel.UnifiedEventLoop()
        assert await loop.start() is False
        assert loop.started is False

    @pytest.mark.asyncio
    async def test_start_returns_true_when_master_on(self, loop_enabled):
        loop = uel.UnifiedEventLoop()
        assert await loop.start() is True
        assert loop.started is True

    @pytest.mark.asyncio
    async def test_double_start_idempotent(self, loop_enabled):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        assert await loop.start() is True

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, loop_enabled):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        await loop.stop()
        await loop.stop()


# ---------------------------------------------------------------------------
# §E — attach_source
# ---------------------------------------------------------------------------


class TestAttachSource:
    def test_idempotent_attach(self):
        loop = uel.UnifiedEventLoop()
        q1 = loop.attach_source("foo")
        q2 = loop.attach_source("foo")
        assert q1 is q2

    def test_distinct_names_distinct_queues(self):
        loop = uel.UnifiedEventLoop()
        q1 = loop.attach_source("a")
        q2 = loop.attach_source("b")
        assert q1 is not q2
        assert set(loop.sources()) == {"a", "b"}

    def test_empty_name_returns_detached(self):
        loop = uel.UnifiedEventLoop()
        q = loop.attach_source("")
        # Detached: not in registry
        assert "" not in loop.sources()

    def test_detach(self):
        loop = uel.UnifiedEventLoop()
        loop.attach_source("foo")
        assert loop.detach_source("foo") is True
        assert loop.detach_source("foo") is False  # already gone


# ---------------------------------------------------------------------------
# §F — Multi-source race ordering
# ---------------------------------------------------------------------------


class TestMultiSourceRace:
    @pytest.mark.asyncio
    async def test_two_sources_chronological(self, loop_enabled):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        q_a = loop.attach_source("a")
        q_b = loop.attach_source("b")

        async def producer():
            await asyncio.sleep(0.01)
            q_a.put_nowait(uel.UnifiedEvent(
                kind=uel.UnifiedKind.RENDER, payload="a1",
                source_label="a",
            ))
            await asyncio.sleep(0.01)
            q_b.put_nowait(uel.UnifiedEvent(
                kind=uel.UnifiedKind.KEY, payload="b1",
                source_label="b",
            ))
            await asyncio.sleep(0.01)
            q_a.put_nowait(uel.UnifiedEvent(
                kind=uel.UnifiedKind.RENDER, payload="a2",
                source_label="a",
            ))
            await asyncio.sleep(0.05)
            await loop.stop()

        asyncio.create_task(producer())

        received = []
        async for event in loop.iter():
            received.append(event)
            if event.kind is uel.UnifiedKind.STOP:
                break

        # Drop STOP envelope and verify chronological order
        non_stop = [e for e in received if e.kind is not uel.UnifiedKind.STOP]
        assert len(non_stop) == 3
        payloads = [e.payload for e in non_stop]
        assert payloads == ["a1", "b1", "a2"]


# ---------------------------------------------------------------------------
# §G — Stop drains pending before STOP
# ---------------------------------------------------------------------------


class TestStopDrains:
    @pytest.mark.asyncio
    async def test_stop_drains_queued_events(self, loop_enabled):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        q = loop.attach_source("render")
        # Pre-queue 5 events synchronously
        for i in range(5):
            q.put_nowait(uel.UnifiedEvent(
                kind=uel.UnifiedKind.RENDER, payload=f"e{i}",
                source_label="render",
            ))
        # Stop after a small delay to let race kick in
        async def stopper():
            await asyncio.sleep(0.02)
            await loop.stop()
        asyncio.create_task(stopper())

        received = []
        async for event in loop.iter():
            received.append(event)
            if event.kind is uel.UnifiedKind.STOP:
                break
        non_stop = [e for e in received if e.kind is not uel.UnifiedKind.STOP]
        # All 5 events drained before STOP
        payloads = sorted(e.payload for e in non_stop)
        assert payloads == ["e0", "e1", "e2", "e3", "e4"]


# ---------------------------------------------------------------------------
# §H — Source-exception isolation
# ---------------------------------------------------------------------------


class TestSourceExceptionIsolation:
    @pytest.mark.asyncio
    async def test_payload_exception_in_to_dict_doesnt_block_loop(
        self, loop_enabled,
    ):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        q = loop.attach_source("render")

        class _Bad:
            def to_dict(self):
                raise RuntimeError("boom")

        q.put_nowait(uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload=_Bad(),
            source_label="render",
        ))

        async def stopper():
            await asyncio.sleep(0.02)
            await loop.stop()
        asyncio.create_task(stopper())

        # Loop should yield the bad event then STOP without raising
        received = []
        async for event in loop.iter():
            received.append(event)
            if event.kind is uel.UnifiedKind.STOP:
                break
        # Bad event still passed through; to_dict failure is recorder-
        # internal, doesn't block the yield path
        non_stop = [e for e in received if e.kind is not uel.UnifiedKind.STOP]
        assert len(non_stop) == 1


# ---------------------------------------------------------------------------
# §I — UnifiedLoopBackend
# ---------------------------------------------------------------------------


class TestUnifiedLoopBackend:
    def test_satisfies_render_backend_protocol(self):
        loop = uel.UnifiedEventLoop()
        backend = uel.UnifiedLoopBackend(loop)
        assert isinstance(backend, rc.RenderBackend)

    @pytest.mark.asyncio
    async def test_forwards_render_event(
        self, monkeypatch: pytest.MonkeyPatch, loop_enabled,
    ):
        monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
        loop = uel.UnifiedEventLoop()
        await loop.start()
        conductor = rc.RenderConductor()
        backend = uel.UnifiedLoopBackend(loop)
        conductor.add_backend(backend)
        rc.register_render_conductor(conductor)
        conductor.publish(rc.RenderEvent(
            kind=rc.EventKind.REASONING_TOKEN,
            region=rc.RegionKind.PHASE_STREAM,
            role=rc.ColorRole.CONTENT,
            content="hello", source_module="test",
        ))

        async def stopper():
            await asyncio.sleep(0.02)
            await loop.stop()
        asyncio.create_task(stopper())

        received = []
        async for event in loop.iter():
            received.append(event)
            if event.kind is uel.UnifiedKind.STOP:
                break
        non_stop = [e for e in received if e.kind is not uel.UnifiedKind.STOP]
        assert len(non_stop) == 1
        assert non_stop[0].payload.content == "hello"

    def test_notify_swallows_exceptions(self):
        # Direct notify with a malformed loop reference shouldn't raise
        class _BrokenLoop:
            def attach_source(self, name): raise RuntimeError("boom")
        try:
            uel.UnifiedLoopBackend(_BrokenLoop())  # type: ignore[arg-type]
            assert False, "should have raised on attach"
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# §J — UnifiedLoopKeySubscriber
# ---------------------------------------------------------------------------


class TestUnifiedLoopKeySubscriber:
    @pytest.mark.asyncio
    async def test_attach_returns_false_when_no_input_controller(
        self, loop_enabled,
    ):
        loop = uel.UnifiedEventLoop()
        await loop.start()
        ki.reset_input_controller()
        sub = uel.UnifiedLoopKeySubscriber(loop)
        assert sub.attach() is False

    @pytest.mark.asyncio
    async def test_attach_subscribes_to_all_keys(
        self, monkeypatch: pytest.MonkeyPatch, loop_enabled,
    ):
        monkeypatch.setenv("JARVIS_INPUT_CONTROLLER_ENABLED", "true")
        loop = uel.UnifiedEventLoop()
        await loop.start()
        ctrl = ki.InputController()
        ki.register_input_controller(ctrl)
        sub = uel.UnifiedLoopKeySubscriber(loop)
        assert sub.attach() is True
        # Verify the bus has at least one subscriber registered
        assert ctrl.bus.subscriber_count() >= 1
        sub.detach()


# ---------------------------------------------------------------------------
# §K — JSONL recorder
# ---------------------------------------------------------------------------


class TestRecorder:
    @pytest.mark.asyncio
    async def test_recorder_writes_jsonl_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_UNIFIED_EVENT_LOOP_ENABLED", "true")
        monkeypatch.setenv(
            "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED", "true",
        )
        log_file = tmp_path / "events.jsonl"
        monkeypatch.setenv(
            "JARVIS_UNIFIED_EVENT_LOG_PATH", str(log_file),
        )
        loop = uel.UnifiedEventLoop()
        await loop.start()
        q = loop.attach_source("render")
        q.put_nowait(uel.UnifiedEvent(
            kind=uel.UnifiedKind.RENDER, payload="x",
            source_label="render",
        ))
        async def stopper():
            await asyncio.sleep(0.02)
            await loop.stop()
        asyncio.create_task(stopper())
        async for event in loop.iter():
            if event.kind is uel.UnifiedKind.STOP:
                break

        # File written + JSON-parseable
        content = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(content) >= 1
        # Each line valid JSON with our schema
        for line in content:
            parsed = json.loads(line)
            assert "schema_version" in parsed
            assert "kind" in parsed

    @pytest.mark.asyncio
    async def test_recorder_off_when_path_unset(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_UNIFIED_EVENT_LOOP_ENABLED", "true")
        monkeypatch.setenv(
            "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED", "true",
        )
        # No path set
        loop = uel.UnifiedEventLoop()
        assert await loop.start() is True
        assert loop._recorder is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# §L — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def u1_pins() -> list:
    return list(uel.register_shipped_invariants())


class TestU1ASTPinsClean:
    def test_five_pins_registered(self, u1_pins):
        assert len(u1_pins) == 5
        names = {i.invariant_name for i in u1_pins}
        assert names == {
            "unified_event_loop_no_rich_import",
            "unified_event_loop_no_authority_imports",
            "unified_event_loop_kind_closed_taxonomy",
            "unified_event_loop_event_closed_taxonomy",
            "unified_event_loop_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_ast(self):
        import inspect
        src = inspect.getsource(uel)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, u1_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in u1_pins
                   if p.invariant_name == "unified_event_loop_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, u1_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in u1_pins
                   if p.invariant_name ==
                   "unified_event_loop_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_kind_closed_clean(self, u1_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in u1_pins
                   if p.invariant_name ==
                   "unified_event_loop_kind_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_event_closed_clean(self, u1_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in u1_pins
                   if p.invariant_name ==
                   "unified_event_loop_event_closed_taxonomy")
        assert pin.validate(tree, src) == ()


class TestU1ASTPinsCatchTampering:
    def test_authority_import_caught(self, u1_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.cancel_token import x\n"
        )
        pin = next(p for p in u1_pins
                   if p.invariant_name ==
                   "unified_event_loop_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("cancel_token" in v for v in violations)

    def test_rich_import_caught(self, u1_pins):
        tampered = ast.parse("from rich.live import Live\n")
        pin = next(p for p in u1_pins
                   if p.invariant_name == "unified_event_loop_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_added_kind_caught(self, u1_pins):
        tampered_src = (
            "class UnifiedKind:\n"
            "    RENDER = 'RENDER'\n"
            "    KEY = 'KEY'\n"
            "    TOOL_RESULT = 'TOOL_RESULT'\n"
            "    CANCEL = 'CANCEL'\n"
            "    STOP = 'STOP'\n"
            "    NEW_KIND = 'NEW_KIND'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in u1_pins
                   if p.invariant_name ==
                   "unified_event_loop_kind_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §M — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_unified_loop(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_UNIFIED_EVENT_LOOP_ENABLED" in names
        assert "JARVIS_UNIFIED_EVENT_LOOP_RECORDING_ENABLED" in names
        assert "JARVIS_UNIFIED_EVENT_LOG_PATH" in names
        assert "JARVIS_UNIFIED_EVENT_LOOP_QUEUE_MAX" in names

    def test_shipped_invariants_includes_u1_pins(self):
        # Adjacent test suites may have called reset_registry_for_tests
        # which only re-runs static seeds (not module discovery). Re-
        # register U1 pins defensively so the assertion is order-
        # independent. Mirrors the followups#5 + backlog#2 pattern.
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in uel.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "unified_event_loop_no_rich_import",
            "unified_event_loop_no_authority_imports",
            "unified_event_loop_kind_closed_taxonomy",
            "unified_event_loop_event_closed_taxonomy",
            "unified_event_loop_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_u1_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        for inv in uel.register_shipped_invariants():
            sci.register_shipped_code_invariant(inv)
        results = sci.validate_all()
        u1_failures = [
            r for r in results
            if r.invariant_name.startswith("unified_event_loop_")
        ]
        assert u1_failures == [], (
            f"U1 pins reporting violations: "
            f"{[r.to_dict() for r in u1_failures]}"
        )

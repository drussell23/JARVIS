"""RenderConductor Slice 5 — visible conversational thread regression suite.

Pins the ThreadTurn primitive + ThreadObserver bridge consumer that
closes Gap #7. The substrate plugs into ConversationBridge's existing
``register_turn_observer`` push fan-out — no polling, no parallel
storage, no rendering changes (Slice 7 wires backend treatment).

Strict directives validated:

  * No hardcoded values: every operator-tunable knob (master flag,
    JARVIS_THREAD_SPEAKER_MAPPING) flows through FlagRegistry. The
    default source→Speaker map is in-code; operators overlay via JSON.
  * Closed taxonomies: Speaker (5 values) + ThreadTurn field set
    AST-pinned. Adding a member requires coordinated registry update.
  * No authority imports: substrate explicitly forbids
    conversation_bridge AT TOP LEVEL. The bridge binding is via lazy
    import inside ThreadObserver.start — caught by AST pin.
  * Defensive everywhere: every method swallows exceptions; the
    bridge contract is "never raise from observer" and the substrate
    honors it by construction.
  * Bridge stays alive when observer disabled — descriptive vs
    rendering split. ConversationBridge's CONTEXT_EXPANSION consumer
    keeps working regardless of the observer's master flag.

Covers:

  §A   Speaker closed taxonomy + str inheritance
  §B   resolve_speaker default mapping + role fallback + override
  §C   ThreadTurn construction + __post_init__ validation
  §D   ThreadTurn to_metadata / from_metadata round-trip
  §E   publish_thread_turn — happy path / missing conductor / role
  §F   ThreadObserver lifecycle — start / stop / idempotency
  §G   ThreadObserver — bridge translation + dedup metric
  §H   ThreadObserver — defensive on bridge failures
  §I   Master flag gate
  §J   AST pins (5) self-validate green + tampering caught
  §K   Auto-discovery integration
"""
from __future__ import annotations

import ast
import threading
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_thread as rt


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
        "JARVIS_THREAD_OBSERVER_ENABLED",
        "JARVIS_THREAD_SPEAKER_MAPPING",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()
    rt.reset_thread_observer()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


class _Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.events: List[Any] = []

    def notify(self, event: Any) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def wired_conductor(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    """Conductor with master flag on + recording backend attached."""
    monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_THREAD_OBSERVER_ENABLED", "true")
    c = rc.RenderConductor()
    rec = _Recorder()
    c.add_backend(rec)
    rc.register_render_conductor(c)
    yield c, rec
    rc.reset_render_conductor()


class _StubBridge:
    """Duck-typed ConversationBridge — only the two observer hooks
    + a synthetic emit method for tests."""

    def __init__(self) -> None:
        self.observers: List[Any] = []
        self.unregistered: List[Any] = []

    def register_turn_observer(self, observer: Any) -> None:
        self.observers.append(observer)

    def unregister_turn_observer(self, observer: Any) -> None:
        self.unregistered.append(observer)
        try:
            self.observers.remove(observer)
        except ValueError:
            pass

    def emit(
        self, role: str, text: str, source: str = "tui_user",
        op_id: str = "", ts: float = 1.0,
    ) -> None:
        """Test helper — synthesize a turn and fan out to observers."""
        for obs in list(self.observers):
            try:
                obs(_StubTurn(role, text, source, op_id, ts))
            except Exception:
                pass


class _StubTurn:
    def __init__(
        self, role: str, text: str, source: str, op_id: str, ts: float,
    ) -> None:
        self.role = role
        self.text = text
        self.source = source
        self.op_id = op_id
        self.ts = ts


# ---------------------------------------------------------------------------
# §A — Speaker closed taxonomy
# ---------------------------------------------------------------------------


class TestSpeakerClosedTaxonomy:
    def test_exact_five_members(self):
        names = {m.value for m in rt.Speaker}
        assert names == {
            "USER", "ASSISTANT", "TOOL", "POSTMORTEM", "SYSTEM",
        }

    def test_str_inheritance(self):
        assert isinstance(rt.Speaker.USER, str)


# ---------------------------------------------------------------------------
# §B — resolve_speaker
# ---------------------------------------------------------------------------


class TestResolveSpeaker:
    def test_tui_user_to_user(self, fresh_registry):
        assert rt.resolve_speaker("tui_user") is rt.Speaker.USER

    def test_voice_to_user(self, fresh_registry):
        assert rt.resolve_speaker("voice") is rt.Speaker.USER

    def test_ask_human_q_to_assistant(self, fresh_registry):
        assert rt.resolve_speaker("ask_human_q") is rt.Speaker.ASSISTANT

    def test_ask_human_a_to_user(self, fresh_registry):
        assert rt.resolve_speaker("ask_human_a") is rt.Speaker.USER

    def test_postmortem_to_postmortem(self, fresh_registry):
        assert rt.resolve_speaker("postmortem") is rt.Speaker.POSTMORTEM

    def test_unknown_with_user_role(self, fresh_registry):
        assert rt.resolve_speaker(
            "mystery", role="user",
        ) is rt.Speaker.USER

    def test_unknown_with_assistant_role(self, fresh_registry):
        assert rt.resolve_speaker(
            "mystery", role="assistant",
        ) is rt.Speaker.ASSISTANT

    def test_unknown_no_role_falls_to_system(self, fresh_registry):
        assert rt.resolve_speaker("mystery") is rt.Speaker.SYSTEM

    def test_non_string_source_safe(self, fresh_registry):
        # Defensive — non-string source treated as ""
        result = rt.resolve_speaker(123)  # type: ignore[arg-type]
        assert result is rt.Speaker.SYSTEM

    def test_override_replaces_default(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_THREAD_SPEAKER_MAPPING",
            '{"voice": "ASSISTANT"}',
        )
        assert rt.resolve_speaker("voice") is rt.Speaker.ASSISTANT

    def test_override_adds_new_source(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_THREAD_SPEAKER_MAPPING",
            '{"new_source": "TOOL"}',
        )
        assert rt.resolve_speaker("new_source") is rt.Speaker.TOOL

    def test_override_unknown_speaker_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_THREAD_SPEAKER_MAPPING",
            '{"voice": "BOGUS"}',
        )
        # Falls back to default for voice → USER
        assert rt.resolve_speaker("voice") is rt.Speaker.USER

    def test_malformed_json_safe(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_THREAD_SPEAKER_MAPPING", "NOT JSON")
        assert rt.resolve_speaker("tui_user") is rt.Speaker.USER


# ---------------------------------------------------------------------------
# §C — ThreadTurn construction + validation
# ---------------------------------------------------------------------------


class TestThreadTurnConstruction:
    def test_minimal(self):
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="hello")
        assert t.speaker is rt.Speaker.USER
        assert t.content == "hello"
        assert t.source == ""
        assert t.op_id is None

    def test_full_fields(self):
        t = rt.ThreadTurn(
            speaker=rt.Speaker.ASSISTANT, content="hi",
            source="ask_human_q", op_id="op-1", monotonic_ts=42.0,
        )
        assert t.source == "ask_human_q"
        assert t.op_id == "op-1"
        assert t.monotonic_ts == 42.0

    def test_frozen(self):
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x")
        with pytest.raises(Exception):
            t.content = "y"  # type: ignore[misc]

    def test_hashable(self):
        a = rt.ThreadTurn(
            speaker=rt.Speaker.USER, content="x", monotonic_ts=1.0,
        )
        b = rt.ThreadTurn(
            speaker=rt.Speaker.USER, content="x", monotonic_ts=1.0,
        )
        assert hash(a) == hash(b)
        assert a == b

    def test_non_string_content_raises(self):
        with pytest.raises(ValueError, match="content"):
            rt.ThreadTurn(speaker=rt.Speaker.USER, content=42)  # type: ignore[arg-type]

    def test_non_string_source_raises(self):
        with pytest.raises(ValueError, match="source"):
            rt.ThreadTurn(
                speaker=rt.Speaker.USER, content="x", source=42,  # type: ignore[arg-type]
            )

    def test_non_string_op_id_raises(self):
        with pytest.raises(ValueError, match="op_id"):
            rt.ThreadTurn(
                speaker=rt.Speaker.USER, content="x", op_id=42,  # type: ignore[arg-type]
            )

    def test_op_id_none_allowed(self):
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x", op_id=None)
        assert t.op_id is None


# ---------------------------------------------------------------------------
# §D — to_metadata / from_metadata round-trip
# ---------------------------------------------------------------------------


class TestThreadTurnMetadata:
    def test_to_metadata_includes_schema(self):
        md = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x").to_metadata()
        assert md["schema_version"] == rt.RENDER_THREAD_SCHEMA_VERSION
        assert md["kind"] == "thread_turn"

    def test_round_trip(self):
        t = rt.ThreadTurn(
            speaker=rt.Speaker.POSTMORTEM, content="closure",
            source="postmortem", op_id="op-99", monotonic_ts=9.0,
        )
        t2 = rt.ThreadTurn.from_metadata(t.to_metadata())
        assert t == t2

    def test_from_metadata_missing_content(self):
        assert rt.ThreadTurn.from_metadata({"speaker": "USER"}) is None

    def test_from_metadata_missing_speaker(self):
        assert rt.ThreadTurn.from_metadata({"content": "x"}) is None

    def test_from_metadata_unknown_speaker(self):
        assert rt.ThreadTurn.from_metadata(
            {"speaker": "BOGUS", "content": "x"},
        ) is None

    def test_from_metadata_garbage(self):
        assert rt.ThreadTurn.from_metadata("not a dict") is None  # type: ignore[arg-type]

    def test_from_metadata_handles_invalid_op_id(self):
        # Coerces invalid op_id to None defensively
        t = rt.ThreadTurn.from_metadata({
            "speaker": "USER", "content": "x", "op_id": 42,
        })
        assert t is not None
        assert t.op_id is None

    def test_from_metadata_handles_invalid_ts(self):
        t = rt.ThreadTurn.from_metadata({
            "speaker": "USER", "content": "x", "monotonic_ts": "bogus",
        })
        assert t is not None  # ts coerced to current monotonic


# ---------------------------------------------------------------------------
# §E — publish_thread_turn
# ---------------------------------------------------------------------------


class TestPublishThreadTurn:
    def test_publishes_event(self, wired_conductor):
        c, rec = wired_conductor
        t = rt.ThreadTurn(
            speaker=rt.Speaker.USER, content="hello",
            source="tui_user", op_id="op-1",
        )
        ok = rt.publish_thread_turn(t, source_module="test")
        assert ok is True
        assert len(rec.events) == 1
        ev = rec.events[0]
        assert ev.kind is rc.EventKind.THREAD_TURN
        assert ev.region is rc.RegionKind.THREAD
        assert ev.role is rc.ColorRole.EMPHASIS  # USER → EMPHASIS
        assert ev.content == "hello"
        assert ev.metadata["speaker"] == "USER"
        assert ev.op_id == "op-1"

    def test_assistant_uses_content_role(self, wired_conductor):
        c, rec = wired_conductor
        t = rt.ThreadTurn(speaker=rt.Speaker.ASSISTANT, content="hi")
        rt.publish_thread_turn(t, source_module="test")
        assert rec.events[0].role is rc.ColorRole.CONTENT

    def test_postmortem_uses_muted_role(self, wired_conductor):
        c, rec = wired_conductor
        t = rt.ThreadTurn(speaker=rt.Speaker.POSTMORTEM, content="x")
        rt.publish_thread_turn(t, source_module="test")
        assert rec.events[0].role is rc.ColorRole.MUTED

    def test_no_conductor_returns_false(self, fresh_registry):
        rc.reset_render_conductor()
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x")
        assert rt.publish_thread_turn(t, source_module="t") is False

    def test_extra_metadata_merged(self, wired_conductor):
        c, rec = wired_conductor
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x")
        rt.publish_thread_turn(
            t, source_module="t",
            extra_metadata={"reason": "test", "weight": 1.0},
        )
        md = rec.events[0].metadata
        assert md["speaker"] == "USER"
        assert md["reason"] == "test"
        assert md["weight"] == 1.0


# ---------------------------------------------------------------------------
# §F — ThreadObserver lifecycle
# ---------------------------------------------------------------------------


class TestThreadObserverLifecycle:
    def test_start_registers_on_bridge(self, wired_conductor):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        ok = obs.start(bridge=bridge)
        assert ok is True
        assert obs.active is True
        assert len(bridge.observers) == 1

    def test_double_start_idempotent(self, wired_conductor):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        obs.start(bridge=bridge)  # should not double-register
        assert len(bridge.observers) == 1

    def test_stop_unregisters(self, wired_conductor):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        obs.stop()
        assert obs.active is False
        assert len(bridge.unregistered) == 1

    def test_double_stop_idempotent(self, wired_conductor):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        obs.stop()
        obs.stop()
        # Should not unregister twice
        assert len(bridge.unregistered) == 1

    def test_start_master_off_returns_false(self, fresh_registry):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        assert obs.start(bridge=bridge) is False
        assert obs.active is False
        assert bridge.observers == []


# ---------------------------------------------------------------------------
# §G — ThreadObserver bridge translation
# ---------------------------------------------------------------------------


class TestThreadObserverTranslation:
    def test_user_turn_translates(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        bridge.emit(
            role="user", text="hello", source="tui_user", op_id="op-1",
        )
        assert obs.turn_count == 1
        assert len(rec.events) == 1
        assert rec.events[0].metadata["speaker"] == "USER"
        assert rec.events[0].content == "hello"
        assert rec.events[0].op_id == "op-1"

    def test_assistant_turn_translates(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        bridge.emit(
            role="assistant", text="response", source="ask_human_q",
        )
        assert rec.events[0].metadata["speaker"] == "ASSISTANT"

    def test_postmortem_turn_translates(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        bridge.emit(
            role="assistant", text="closure note", source="postmortem",
        )
        assert rec.events[0].metadata["speaker"] == "POSTMORTEM"
        assert rec.events[0].role is rc.ColorRole.MUTED

    def test_multiple_turns_count_increments(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        for i in range(5):
            bridge.emit(
                role="user", text=f"t{i}", source="tui_user",
            )
        assert obs.turn_count == 5
        assert len(rec.events) == 5

    def test_op_id_none_when_empty(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        bridge.emit(role="user", text="x", source="tui_user", op_id="")
        assert rec.events[0].op_id is None


# ---------------------------------------------------------------------------
# §H — Defensive: bridge failures don't propagate
# ---------------------------------------------------------------------------


class TestThreadObserverDefensive:
    def test_translation_swallows_exception(self, wired_conductor):
        c, rec = wired_conductor
        obs = rt.ThreadObserver()
        # Construct an object missing required attrs — getattr defaults
        # carry it through; translation should not raise
        obs._on_turn(object())  # should not raise

    def test_observer_with_unhashable_op_id_safe(self, wired_conductor):
        c, rec = wired_conductor
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)

        class _BadTurn:
            role = "user"
            text = "x"
            source = "tui_user"
            op_id = 42  # int, not str → defensive path filters
            ts = 1.0

        bridge.observers[0](_BadTurn())
        # Should not raise; op_id treated as None per shape filter

    def test_bridge_without_unregister_method_safe(self, wired_conductor):
        class _MinimalBridge:
            def __init__(self) -> None:
                self.observers = []

            def register_turn_observer(self, obs: Any) -> None:
                self.observers.append(obs)

        bridge = _MinimalBridge()
        obs = rt.ThreadObserver()
        obs.start(bridge=bridge)
        obs.stop()  # missing unregister — should not raise


# ---------------------------------------------------------------------------
# §I — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    def test_observer_inactive_when_master_off(self, fresh_registry):
        bridge = _StubBridge()
        obs = rt.ThreadObserver()
        # No env override → default false
        assert obs.start(bridge=bridge) is False
        assert obs.active is False

    def test_publish_works_independently_of_observer_flag(
        self, wired_conductor,
    ):
        # publish_thread_turn doesn't gate on the observer's flag —
        # only the conductor's master flag (already on via fixture).
        c, rec = wired_conductor
        t = rt.ThreadTurn(speaker=rt.Speaker.USER, content="x")
        assert rt.publish_thread_turn(t, source_module="t") is True


# ---------------------------------------------------------------------------
# §J — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice5_pins() -> list:
    return list(rt.register_shipped_invariants())


class TestSlice5ASTPinsClean:
    def test_five_pins_registered(self, slice5_pins):
        assert len(slice5_pins) == 5
        names = {i.invariant_name for i in slice5_pins}
        assert names == {
            "render_thread_no_rich_import",
            "render_thread_no_authority_imports",
            "render_thread_speaker_closed_taxonomy",
            "render_thread_thread_turn_closed_taxonomy",
            "render_thread_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_ast(self) -> tuple:
        import inspect
        src = inspect.getsource(rt)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, slice5_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice5_pins
                   if p.invariant_name == "render_thread_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, slice5_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_speaker_closed_clean(self, slice5_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_speaker_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_thread_turn_closed_clean(self, slice5_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_thread_turn_closed_taxonomy")
        assert pin.validate(tree, src) == ()


class TestSlice5ASTPinsCatchTampering:
    def test_conversation_bridge_top_level_import_caught(self, slice5_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.conversation_bridge "
            "import x\n"
        )
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("conversation_bridge" in v for v in violations)

    def test_orchestrator_import_caught(self, slice5_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.orchestrator import x\n"
        )
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("orchestrator" in v for v in violations)

    def test_rich_import_caught(self, slice5_pins):
        tampered = ast.parse("from rich.text import Text\n")
        pin = next(p for p in slice5_pins
                   if p.invariant_name == "render_thread_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_added_speaker_caught(self, slice5_pins):
        tampered_src = (
            "class Speaker:\n"
            "    USER = 'USER'\n"
            "    ASSISTANT = 'ASSISTANT'\n"
            "    TOOL = 'TOOL'\n"
            "    POSTMORTEM = 'POSTMORTEM'\n"
            "    SYSTEM = 'SYSTEM'\n"
            "    NEW_SPEAKER = 'NEW_SPEAKER'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_speaker_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_removed_thread_turn_field_caught(self, slice5_pins):
        tampered_src = (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class ThreadTurn:\n"
            "    speaker: str\n"
            "    content: str\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice5_pins
                   if p.invariant_name ==
                   "render_thread_thread_turn_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §K — Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_render_thread(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_THREAD_OBSERVER_ENABLED" in names
        assert "JARVIS_THREAD_SPEAKER_MAPPING" in names

    def test_shipped_invariants_includes_slice5_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "render_thread_no_rich_import",
            "render_thread_no_authority_imports",
            "render_thread_speaker_closed_taxonomy",
            "render_thread_thread_turn_closed_taxonomy",
            "render_thread_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_slice5_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        slice5_failures = [
            r for r in results
            if r.invariant_name.startswith("render_thread_")
        ]
        assert slice5_failures == [], (
            f"Slice 5 pins reporting violations: "
            f"{[r.to_dict() for r in slice5_failures]}"
        )

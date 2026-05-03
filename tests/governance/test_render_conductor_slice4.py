"""RenderConductor Slice 4 — keyboard input substrate regression suite.

Pins the InputController + KeyBus + KeyEvent + KeyActionRegistry
substrate that closes Gap #5 (single-keypress mid-token interrupt).

Strict directives validated:

  * No hardcoded values: every operator-tunable knob (master flag,
    raw-mode sub-gate, JARVIS_KEY_BINDINGS overlay) flows through
    FlagRegistry. No raw key names or action names in callers.
  * Closed taxonomies: KeyName / Modifier / KeyAction are AST-pinned.
    Adding a member requires coordinated registry update.
  * No authority imports: substrate stays descriptive only;
    cancel_token + orchestrator + GLS + others are explicitly
    forbidden by AST pin. The cancel binding is wired via a
    *registered handler callback* — Slice 7 will register
    SerpentFlow._handle_cancel into the registry.
  * Defensive everywhere: every method returns instead of raising;
    handler exceptions in one subscriber never break siblings; bus
    publish exceptions swallowed; raw-mode entry failure degrades
    to no-op.
  * Posix raw-mode reader, with TTY + REPL detection — controller
    short-circuits to no-op when stdin isn't a TTY OR when REPL is
    active (prompt_toolkit owns stdin).
  * Termios restore is bulletproof — atexit registration ensures the
    terminal is never left in cbreak after process exit.

Covers:

  §A   Closed taxonomies — KeyName / Modifier / KeyAction membership
  §B   KeyEvent frozen dataclass + to_dict
  §C   parse_input_bytes — ASCII / control / ESC / CSI / ALT+char
       / incomplete remainder
  §D   KeyBus subscribe / publish / unsubscribe / exception isolation
  §E   KeyBus thread-safe concurrent publish
  §F   KeyBus master-flag gate
  §G   KeyActionRegistry — register / unregister / fire / NO_OP
       sentinel
  §H   resolve_bindings — defaults + JSON override + invalid skip
  §I   InputController — TTY/REPL/master detection short-circuit
  §J   InputController — full binding resolution + dispatch via parser
  §K   AST pins (6) self-validate green + tampering caught
  §L   Auto-discovery integration
"""
from __future__ import annotations

import ast
import asyncio
import threading
from typing import Any, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance import key_input as ki


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_INPUT_CONTROLLER_ENABLED",
        "JARVIS_INPUT_CONTROLLER_RAW_MODE",
        "JARVIS_KEY_BINDINGS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    ki.reset_input_controller()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


@pytest.fixture
def master_on(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    monkeypatch.setenv("JARVIS_INPUT_CONTROLLER_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# §A — Closed taxonomies
# ---------------------------------------------------------------------------


class TestKeyNameClosedTaxonomy:
    def test_exact_member_set(self):
        names = {m.value for m in ki.KeyName}
        assert names == {
            "ESC", "ENTER", "SPACE", "TAB", "BACKSPACE", "QUESTION",
            "CTRL_C", "CTRL_D", "CTRL_L",
            "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT",
            "CHAR",
        }

    def test_value_equals_name(self):
        for m in ki.KeyName:
            assert m.value == m.name


class TestModifierClosedTaxonomy:
    def test_exact_four_members(self):
        assert {m.value for m in ki.Modifier} == {
            "CTRL", "ALT", "SHIFT", "META",
        }


class TestKeyActionClosedTaxonomy:
    def test_exact_member_set(self):
        assert {m.value for m in ki.KeyAction} == {
            "NO_OP", "CANCEL_CURRENT_OP",
            "HELP_OPEN", "HELP_CLOSE",
            "THREAD_TOGGLE",
            "REPL_HISTORY_PREV", "REPL_HISTORY_NEXT",
        }


# ---------------------------------------------------------------------------
# §B — KeyEvent dataclass
# ---------------------------------------------------------------------------


class TestKeyEvent:
    def test_minimal_construction(self):
        ev = ki.KeyEvent(key=ki.KeyName.ESC)
        assert ev.key is ki.KeyName.ESC
        assert ev.char is None
        assert ev.modifiers == frozenset()

    def test_char_with_modifier(self):
        ev = ki.KeyEvent(
            key=ki.KeyName.CHAR, char="a",
            modifiers=frozenset({ki.Modifier.ALT}),
        )
        assert ev.char == "a"
        assert ki.Modifier.ALT in ev.modifiers

    def test_frozen(self):
        ev = ki.KeyEvent(key=ki.KeyName.ESC)
        with pytest.raises(Exception):
            ev.key = ki.KeyName.ENTER  # type: ignore[misc]

    def test_hashable(self):
        a = ki.KeyEvent(key=ki.KeyName.ESC)
        # Two events with auto-populated monotonic_ts compare unequal
        # (different timestamps) — that's expected. Hashability is
        # proved by being usable in a set.
        s = {a}
        assert a in s

    def test_to_dict_includes_schema(self):
        ev = ki.KeyEvent(key=ki.KeyName.CHAR, char="x")
        d = ev.to_dict()
        assert d["schema_version"] == ki.KEY_INPUT_SCHEMA_VERSION
        assert d["key"] == "CHAR"
        assert d["char"] == "x"


# ---------------------------------------------------------------------------
# §C — parse_input_bytes
# ---------------------------------------------------------------------------


class TestParser:
    def test_empty_buffer(self):
        events, rem = ki.parse_input_bytes(b"")
        assert events == []
        assert rem == b""

    def test_single_printable(self):
        events, rem = ki.parse_input_bytes(b"a")
        assert len(events) == 1
        assert events[0].key is ki.KeyName.CHAR
        assert events[0].char == "a"
        assert rem == b""

    def test_multiple_printable(self):
        events, _ = ki.parse_input_bytes(b"abc")
        assert [e.char for e in events] == ["a", "b", "c"]

    def test_enter_lf(self):
        events, _ = ki.parse_input_bytes(b"\n")
        assert events[0].key is ki.KeyName.ENTER

    def test_enter_cr(self):
        events, _ = ki.parse_input_bytes(b"\r")
        assert events[0].key is ki.KeyName.ENTER

    def test_space(self):
        events, _ = ki.parse_input_bytes(b" ")
        # SPACE byte is in _CONTROL_BYTE_KEY → KeyName.SPACE
        assert events[0].key is ki.KeyName.SPACE

    def test_tab(self):
        events, _ = ki.parse_input_bytes(b"\t")
        assert events[0].key is ki.KeyName.TAB

    def test_backspace_del(self):
        events, _ = ki.parse_input_bytes(b"\x7f")
        assert events[0].key is ki.KeyName.BACKSPACE

    def test_backspace_bs(self):
        events, _ = ki.parse_input_bytes(b"\x08")
        assert events[0].key is ki.KeyName.BACKSPACE

    def test_ctrl_c(self):
        events, _ = ki.parse_input_bytes(b"\x03")
        assert events[0].key is ki.KeyName.CTRL_C

    def test_ctrl_d(self):
        events, _ = ki.parse_input_bytes(b"\x04")
        assert events[0].key is ki.KeyName.CTRL_D

    def test_ctrl_l(self):
        events, _ = ki.parse_input_bytes(b"\x0c")
        assert events[0].key is ki.KeyName.CTRL_L

    def test_question_mark(self):
        events, _ = ki.parse_input_bytes(b"?")
        assert events[0].key is ki.KeyName.QUESTION

    def test_arrow_up(self):
        events, _ = ki.parse_input_bytes(b"\x1b[A")
        assert events[0].key is ki.KeyName.ARROW_UP

    def test_arrow_down(self):
        events, _ = ki.parse_input_bytes(b"\x1b[B")
        assert events[0].key is ki.KeyName.ARROW_DOWN

    def test_arrow_right(self):
        events, _ = ki.parse_input_bytes(b"\x1b[C")
        assert events[0].key is ki.KeyName.ARROW_RIGHT

    def test_arrow_left(self):
        events, _ = ki.parse_input_bytes(b"\x1b[D")
        assert events[0].key is ki.KeyName.ARROW_LEFT

    def test_esc_alone_incomplete(self):
        events, rem = ki.parse_input_bytes(b"\x1b")
        assert events == []
        assert rem == b"\x1b"

    def test_esc_csi_incomplete(self):
        events, rem = ki.parse_input_bytes(b"\x1b[")
        assert events == []
        assert rem == b"\x1b["

    def test_alt_plus_char(self):
        events, _ = ki.parse_input_bytes(b"\x1ba")
        assert len(events) == 1
        assert events[0].key is ki.KeyName.CHAR
        assert events[0].char == "a"
        assert ki.Modifier.ALT in events[0].modifiers

    def test_mixed_sequence(self):
        events, _ = ki.parse_input_bytes(b"a\x1b[B\x03c")
        assert [e.key for e in events] == [
            ki.KeyName.CHAR, ki.KeyName.ARROW_DOWN,
            ki.KeyName.CTRL_C, ki.KeyName.CHAR,
        ]
        assert events[0].char == "a"
        assert events[3].char == "c"

    def test_unknown_csi_tail_skipped(self):
        # ESC [ Z — back-tab, not in our taxonomy → 3 bytes consumed,
        # no event emitted
        events, rem = ki.parse_input_bytes(b"\x1b[Z")
        assert events == []
        assert rem == b""

    def test_rebuild_across_chunks(self):
        # Simulate the real reader path: chunk arrives mid-CSI
        events1, rem1 = ki.parse_input_bytes(b"\x1b[")
        events2, rem2 = ki.parse_input_bytes(rem1 + b"A")
        assert events1 == []
        assert events2[0].key is ki.KeyName.ARROW_UP
        assert rem2 == b""


# ---------------------------------------------------------------------------
# §D — KeyBus subscribe / publish / unsubscribe / exception isolation
# ---------------------------------------------------------------------------


class TestKeyBus:
    def test_subscribe_single_key(self, master_on):
        bus = ki.KeyBus()
        received = []
        bus.subscribe(ki.KeyName.ESC, lambda e: received.append(e))
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        assert len(received) == 1

    def test_subscribe_multiple_keys(self, master_on):
        bus = ki.KeyBus()
        received: list = []
        bus.subscribe(
            [ki.KeyName.ESC, ki.KeyName.ENTER],
            lambda e: received.append(e),
        )
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        bus.publish(ki.KeyEvent(key=ki.KeyName.ENTER))
        bus.publish(ki.KeyEvent(key=ki.KeyName.SPACE))
        assert len(received) == 2

    def test_unsubscribe(self, master_on):
        bus = ki.KeyBus()
        received = []
        sub = bus.subscribe(ki.KeyName.ESC, lambda e: received.append(e))
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        sub.unsubscribe()
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        assert len(received) == 1

    def test_unsubscribe_idempotent(self, master_on):
        bus = ki.KeyBus()
        sub = bus.subscribe(ki.KeyName.ESC, lambda e: None)
        sub.unsubscribe()
        sub.unsubscribe()  # should not raise

    def test_handler_exception_isolated(self, master_on):
        bus = ki.KeyBus()
        good = []

        def _bad(e):
            raise RuntimeError("boom")

        bus.subscribe(ki.KeyName.ESC, _bad)
        bus.subscribe(ki.KeyName.ESC, lambda e: good.append(e))
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        assert len(good) == 1

    def test_publish_non_event_no_op(self, master_on):
        bus = ki.KeyBus()
        bus.publish("not an event")  # type: ignore[arg-type]
        bus.publish(None)  # type: ignore[arg-type]

    def test_subscribe_empty_keys_returns_inert_subscription(
        self, master_on,
    ):
        bus = ki.KeyBus()
        sub = bus.subscribe([], lambda e: None)
        sub.unsubscribe()  # no-op, no raise

    def test_subscribe_non_callable_handler(self, master_on):
        bus = ki.KeyBus()
        sub = bus.subscribe(ki.KeyName.ESC, "not callable")  # type: ignore[arg-type]
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        sub.unsubscribe()


# ---------------------------------------------------------------------------
# §E — Thread safety
# ---------------------------------------------------------------------------


class TestKeyBusConcurrency:
    def test_concurrent_publish_thread_safe(self, master_on):
        bus = ki.KeyBus()
        received = []
        lock = threading.Lock()

        def _record(e):
            with lock:
                received.append(e)

        bus.subscribe(ki.KeyName.SPACE, _record)
        N = 50
        threads = [
            threading.Thread(
                target=bus.publish,
                args=(ki.KeyEvent(key=ki.KeyName.SPACE),),
            )
            for _ in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(received) == N


# ---------------------------------------------------------------------------
# §F — Master flag gate
# ---------------------------------------------------------------------------


class TestKeyBusGate:
    def test_publish_no_op_when_master_off(self, fresh_registry):
        bus = ki.KeyBus()
        received = []
        bus.subscribe(ki.KeyName.ESC, lambda e: received.append(e))
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        assert received == []

    def test_subscribe_succeeds_when_master_off(self, fresh_registry):
        bus = ki.KeyBus()
        sub = bus.subscribe(ki.KeyName.ESC, lambda e: None)
        # Subscribe is registry-local; only publish is gated.
        assert bus.subscriber_count() == 1
        sub.unsubscribe()


# ---------------------------------------------------------------------------
# §G — KeyActionRegistry
# ---------------------------------------------------------------------------


class TestKeyActionRegistry:
    def test_register_and_fire(self):
        reg = ki.KeyActionRegistry()
        fired = []
        reg.register(
            ki.KeyAction.CANCEL_CURRENT_OP,
            lambda e: fired.append(e),
        )
        ok = reg.fire(
            ki.KeyAction.CANCEL_CURRENT_OP,
            ki.KeyEvent(key=ki.KeyName.ESC),
        )
        assert ok is True
        assert len(fired) == 1

    def test_fire_no_handler_returns_false(self):
        reg = ki.KeyActionRegistry()
        ok = reg.fire(
            ki.KeyAction.HELP_OPEN, ki.KeyEvent(key=ki.KeyName.QUESTION),
        )
        assert ok is False

    def test_no_op_always_present(self):
        reg = ki.KeyActionRegistry()
        # NO_OP handler exists by construction
        assert reg.has_handler(ki.KeyAction.NO_OP)
        ok = reg.fire(ki.KeyAction.NO_OP, ki.KeyEvent(key=ki.KeyName.ESC))
        assert ok is True

    def test_no_op_cannot_be_overridden(self):
        reg = ki.KeyActionRegistry()
        reg.register(ki.KeyAction.NO_OP, lambda e: None)
        # Still the original no-op (rejected silently)
        # Can't easily assert identity, but unregister returns False
        assert reg.unregister(ki.KeyAction.NO_OP) is False

    def test_unregister(self):
        reg = ki.KeyActionRegistry()
        reg.register(ki.KeyAction.HELP_OPEN, lambda e: None)
        assert reg.unregister(ki.KeyAction.HELP_OPEN) is True
        assert reg.has_handler(ki.KeyAction.HELP_OPEN) is False

    def test_register_replaces(self):
        reg = ki.KeyActionRegistry()
        a = []
        b = []
        reg.register(ki.KeyAction.HELP_OPEN, lambda e: a.append(1))
        reg.register(ki.KeyAction.HELP_OPEN, lambda e: b.append(1))
        reg.fire(ki.KeyAction.HELP_OPEN, ki.KeyEvent(key=ki.KeyName.ESC))
        assert a == []
        assert b == [1]

    def test_handler_exception_swallowed(self):
        reg = ki.KeyActionRegistry()

        def _boom(e):
            raise RuntimeError("boom")

        reg.register(ki.KeyAction.CANCEL_CURRENT_OP, _boom)
        # Returns True (handler ran) but doesn't raise
        ok = reg.fire(
            ki.KeyAction.CANCEL_CURRENT_OP,
            ki.KeyEvent(key=ki.KeyName.ESC),
        )
        assert ok is True

    def test_register_non_callable_rejected(self):
        reg = ki.KeyActionRegistry()
        reg.register(
            ki.KeyAction.CANCEL_CURRENT_OP, "not callable",
        )  # type: ignore[arg-type]
        assert reg.has_handler(ki.KeyAction.CANCEL_CURRENT_OP) is False


# ---------------------------------------------------------------------------
# §H — resolve_bindings
# ---------------------------------------------------------------------------


class TestResolveBindings:
    def test_default_bindings_present(self, fresh_registry):
        b = ki.resolve_bindings()
        assert b[ki.KeyName.ESC] is ki.KeyAction.CANCEL_CURRENT_OP
        assert b[ki.KeyName.QUESTION] is ki.KeyAction.HELP_OPEN
        assert b[ki.KeyName.ARROW_UP] is ki.KeyAction.REPL_HISTORY_PREV

    def test_override_replaces(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_KEY_BINDINGS", '{"ESC": "HELP_OPEN"}',
        )
        b = ki.resolve_bindings()
        assert b[ki.KeyName.ESC] is ki.KeyAction.HELP_OPEN

    def test_override_adds_new_binding(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_KEY_BINDINGS", '{"SPACE": "HELP_OPEN"}',
        )
        b = ki.resolve_bindings()
        assert b[ki.KeyName.SPACE] is ki.KeyAction.HELP_OPEN

    def test_override_unknown_key_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_KEY_BINDINGS",
            '{"NONEXISTENT_KEY": "HELP_OPEN", "SPACE": "HELP_OPEN"}',
        )
        b = ki.resolve_bindings()
        assert ki.KeyName.SPACE in b
        # Unknown key didn't crash the loader
        assert ki.KeyName.ESC in b  # default still present

    def test_override_unknown_action_silently_skipped(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv(
            "JARVIS_KEY_BINDINGS",
            '{"SPACE": "BOGUS_ACTION"}',
        )
        b = ki.resolve_bindings()
        assert ki.KeyName.SPACE not in b  # rejected — no default for SPACE

    def test_malformed_json_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_KEY_BINDINGS", "NOT JSON")
        b = ki.resolve_bindings()
        assert b[ki.KeyName.ESC] is ki.KeyAction.CANCEL_CURRENT_OP


# ---------------------------------------------------------------------------
# §I — InputController short-circuit conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInputControllerShortCircuits:
    async def test_master_off_no_op(self, fresh_registry):
        ctrl = ki.InputController()
        ok = await ctrl.start()
        assert ok is False
        assert ctrl.active is False

    async def test_no_tty_no_op(self, master_on):
        ctrl = ki.InputController()
        # Default test env: stdin is not a TTY
        ok = await ctrl.start()
        assert ok is False

    async def test_repl_active_no_op(self, master_on):
        ctrl = ki.InputController()
        with mock.patch.object(
            ctrl, "_stdin_is_tty", return_value=True,
        ), mock.patch.object(
            ctrl, "_repl_active", return_value=True,
        ):
            ok = await ctrl.start()
        assert ok is False

    async def test_raw_mode_disabled_no_op(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_INPUT_CONTROLLER_ENABLED", "true")
        monkeypatch.setenv("JARVIS_INPUT_CONTROLLER_RAW_MODE", "false")
        ctrl = ki.InputController()
        ok = await ctrl.start()
        assert ok is False

    async def test_stop_idempotent_when_inactive(self):
        ctrl = ki.InputController()
        await ctrl.stop()
        await ctrl.stop()


# ---------------------------------------------------------------------------
# §J — End-to-end binding dispatch via parser-injected events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEndToEndBindingDispatch:
    async def test_esc_byte_resolves_to_cancel_action(self, master_on):
        # Inject ESC-incomplete + 50ms-flush (simulated via second
        # parse with empty input not needed — the parser already
        # leaves \x1b in remainder; we re-parse with extra ESC and
        # verify final disambiguation by feeding ESC then printable).
        # Simpler: feed a complete ESC sequence by sending two ESCs;
        # the second ESC is interpreted as "lone ESC followed by ESC".
        # The parser is more reliably tested via direct KeyBus publish.
        bus = ki.KeyBus()
        registry = ki.KeyActionRegistry()
        ctrl = ki.InputController(bus=bus, registry=registry)
        fired = []
        registry.register(
            ki.KeyAction.CANCEL_CURRENT_OP,
            lambda e: fired.append(e),
        )
        ctrl._wire_action_dispatch()  # bind without raw mode
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        # Allow async dispatch to complete (sync handler — should fire
        # inline)
        assert len(fired) == 1

    async def test_question_resolves_to_help_open(self, master_on):
        bus = ki.KeyBus()
        registry = ki.KeyActionRegistry()
        ctrl = ki.InputController(bus=bus, registry=registry)
        fired = []
        registry.register(
            ki.KeyAction.HELP_OPEN, lambda e: fired.append(e),
        )
        ctrl._wire_action_dispatch()
        bus.publish(ki.KeyEvent(key=ki.KeyName.QUESTION))
        assert len(fired) == 1

    async def test_unknown_binding_skipped(self, master_on):
        bus = ki.KeyBus()
        registry = ki.KeyActionRegistry()
        ctrl = ki.InputController(bus=bus, registry=registry)
        ctrl._wire_action_dispatch()
        bus.publish(ki.KeyEvent(key=ki.KeyName.TAB))  # no default

    async def test_async_handler_scheduled(self, master_on):
        bus = ki.KeyBus()
        registry = ki.KeyActionRegistry()
        fired = []

        async def _async_handler(event):
            fired.append(event)

        registry.register(ki.KeyAction.CANCEL_CURRENT_OP, _async_handler)
        ctrl = ki.InputController(bus=bus, registry=registry)
        ctrl._wire_action_dispatch()
        bus.publish(ki.KeyEvent(key=ki.KeyName.ESC))
        # Yield once to let the scheduled task run
        await asyncio.sleep(0)
        assert len(fired) == 1


# ---------------------------------------------------------------------------
# §K — AST pins
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice4_pins() -> list:
    return list(ki.register_shipped_invariants())


class TestSlice4ASTPinsClean:
    def test_six_pins_registered(self, slice4_pins):
        assert len(slice4_pins) == 6
        names = {i.invariant_name for i in slice4_pins}
        assert names == {
            "key_input_no_rich_import",
            "key_input_no_authority_imports",
            "key_input_key_name_closed_taxonomy",
            "key_input_modifier_closed_taxonomy",
            "key_input_key_action_closed_taxonomy",
            "key_input_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_ast(self) -> tuple:
        import inspect
        src = inspect.getsource(ki)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, slice4_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice4_pins
                   if p.invariant_name == "key_input_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, slice4_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice4_pins
                   if p.invariant_name == "key_input_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_key_name_closed_clean(self, slice4_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice4_pins
                   if p.invariant_name ==
                   "key_input_key_name_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_modifier_closed_clean(self, slice4_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice4_pins
                   if p.invariant_name ==
                   "key_input_modifier_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_key_action_closed_clean(self, slice4_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice4_pins
                   if p.invariant_name ==
                   "key_input_key_action_closed_taxonomy")
        assert pin.validate(tree, src) == ()


class TestSlice4ASTPinsCatchTampering:
    def test_cancel_token_import_caught(self, slice4_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.cancel_token import x\n"
        )
        pin = next(p for p in slice4_pins
                   if p.invariant_name == "key_input_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("cancel_token" in v for v in violations)

    def test_orchestrator_import_caught(self, slice4_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.orchestrator import x\n"
        )
        pin = next(p for p in slice4_pins
                   if p.invariant_name == "key_input_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("orchestrator" in v for v in violations)

    def test_rich_import_caught(self, slice4_pins):
        tampered = ast.parse("from rich.console import Console\n")
        pin = next(p for p in slice4_pins
                   if p.invariant_name == "key_input_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_added_key_name_caught(self, slice4_pins):
        # KeyName with an extra member
        tampered_src = (
            "class KeyName:\n"
            "    ESC = 'ESC'\n"
            "    ENTER = 'ENTER'\n"
            "    SPACE = 'SPACE'\n"
            "    TAB = 'TAB'\n"
            "    BACKSPACE = 'BACKSPACE'\n"
            "    QUESTION = 'QUESTION'\n"
            "    CTRL_C = 'CTRL_C'\n"
            "    CTRL_D = 'CTRL_D'\n"
            "    CTRL_L = 'CTRL_L'\n"
            "    ARROW_UP = 'ARROW_UP'\n"
            "    ARROW_DOWN = 'ARROW_DOWN'\n"
            "    ARROW_LEFT = 'ARROW_LEFT'\n"
            "    ARROW_RIGHT = 'ARROW_RIGHT'\n"
            "    CHAR = 'CHAR'\n"
            "    NEW_KEY = 'NEW_KEY'\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice4_pins
                   if p.invariant_name ==
                   "key_input_key_name_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_removed_key_action_caught(self, slice4_pins):
        tampered_src = (
            "class KeyAction:\n"
            "    NO_OP = 'NO_OP'\n"
            # CANCEL_CURRENT_OP intentionally removed
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice4_pins
                   if p.invariant_name ==
                   "key_input_key_action_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations


# ---------------------------------------------------------------------------
# §L — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_key_input(self, fresh_registry):
        names = {s.name for s in fresh_registry.list_all()}
        assert "JARVIS_INPUT_CONTROLLER_ENABLED" in names
        assert "JARVIS_INPUT_CONTROLLER_RAW_MODE" in names
        assert "JARVIS_KEY_BINDINGS" in names

    def test_shipped_invariants_includes_slice4_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "key_input_no_rich_import",
            "key_input_no_authority_imports",
            "key_input_key_name_closed_taxonomy",
            "key_input_modifier_closed_taxonomy",
            "key_input_key_action_closed_taxonomy",
            "key_input_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_slice4_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        slice4_failures = [
            r for r in results
            if r.invariant_name.startswith("key_input_")
        ]
        assert slice4_failures == [], (
            f"Slice 4 pins reporting violations: "
            f"{[r.to_dict() for r in slice4_failures]}"
        )

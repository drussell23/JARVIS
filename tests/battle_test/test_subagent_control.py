"""Keyboard control over running subagents — CC's `Ctrl+X Ctrl+K`.

ov dispatched L3 subagents into isolated worktrees, showed them in the roster,
and gave the operator no key to stop them. `Esc` is narrow by construction
("stop what I asked for", never "stop the organism"), so there was nothing
between cancelling one's own op and killing the process.

The tests that matter are not "does it bind". They are:

  * does a single press do NOTHING (the guard is the whole safety argument),
  * does an expired arm refuse to confirm late,
  * does the confirmation reach ONE authority by ONE route from BOTH cockpits,
  * and does the cancel stay cooperative rather than severing mid-phase.
"""
from __future__ import annotations

import ast
import inspect
import types

import pytest

from backend.core.ouroboros.battle_test.confirm_chord import (
    ConfirmLatch,
    confirm_window_s,
)
from backend.core.ouroboros.battle_test.subagent_control import (
    STOP_ALL_ACTION,
    STOP_ALL_VERB,
    install_stop_all_binding,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def latch():
    clock = _Clock()
    return ConfirmLatch(window_s=3.0, clock=clock), clock


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestConfirmLatch:
    def test_one_press_does_not_confirm(self, latch):
        lat, _clock = latch
        assert lat.press() is False, (
            "a single press must never fire — the whole point is that a "
            "mis-hit cannot reach every running agent"
        )
        assert lat.armed() is True

    def test_a_repeat_inside_the_window_confirms(self, latch):
        lat, clock = latch
        lat.press()
        clock.now += 2.0
        assert lat.press() is True

    def test_an_expired_arm_refuses_to_confirm_late(self, latch):
        """Otherwise the second half of a confirmation arrives minutes later
        attached to a completely different intention."""
        lat, clock = latch
        lat.press()
        clock.now += 10.0
        assert lat.press() is False, "a late repeat must RE-ARM, not fire"
        assert lat.armed() is True

    def test_confirming_consumes_the_arm(self, latch):
        """Holding the chord down must not fire once per key-repeat."""
        lat, clock = latch
        lat.press()
        assert lat.press() is True
        assert lat.press() is False, "a third press starts over"

    def test_armed_reports_expiry_rather_than_the_stored_flag(self, latch):
        """A toolbar hint reading this must not keep advertising a
        confirmation that the next press will no longer give."""
        lat, clock = latch
        lat.press()
        assert lat.armed() is True
        clock.now += 10.0
        assert lat.armed() is False

    def test_disarm_drops_a_pending_arm(self, latch):
        lat, _clock = latch
        lat.press()
        lat.disarm()
        assert lat.armed() is False
        assert lat.press() is False, "disarmed, the next press only arms"

    def test_the_window_is_clamped_not_trusted(self, monkeypatch):
        """Zero would remove the guard by configuration, silently."""
        monkeypatch.setenv("JARVIS_CONFIRM_CHORD_WINDOW_S", "0")
        assert confirm_window_s() >= 0.5
        monkeypatch.setenv("JARVIS_CONFIRM_CHORD_WINDOW_S", "9999")
        assert confirm_window_s() <= 30.0
        monkeypatch.setenv("JARVIS_CONFIRM_CHORD_WINDOW_S", "not-a-number")
        assert confirm_window_s() > 0


# ---------------------------------------------------------------------------
# The binding
# ---------------------------------------------------------------------------


def _bind(running=None):
    from prompt_toolkit.key_binding import KeyBindings

    sent, said = [], []
    client = types.SimpleNamespace(send_input=sent.append)
    kb = KeyBindings()
    ok = install_stop_all_binding(
        kb, client, notify=said.append, running=running,
    )
    return ok, kb, sent, said


class TestStopAllBinding:
    def test_it_binds_ccs_chord(self):
        ok, kb, _sent, _said = _bind()
        assert ok
        sequences = [tuple(str(k) for k in b.keys) for b in kb.bindings]
        assert ("Keys.ControlX", "Keys.ControlK") in sequences

    def test_the_arming_press_sends_nothing(self):
        _ok, kb, sent, said = _bind(running=lambda: 3)
        kb.bindings[0].handler(None)
        assert sent == [], "the first press must not reach the organism"
        assert "again" in said[-1]

    def test_the_arming_press_names_what_it_would_stop(self):
        """"Press again to confirm" on an idle organism asks the operator to
        weigh a consequence that does not exist, and teaches them to confirm
        without reading."""
        _ok, kb, _sent, said = _bind(running=lambda: 3)
        kb.bindings[0].handler(None)
        assert "3 agents" in said[-1]

    def test_a_confirmed_chord_sends_the_verb(self):
        _ok, kb, sent, _said = _bind(running=lambda: 2)
        kb.bindings[0].handler(None)
        kb.bindings[0].handler(None)
        assert sent == [STOP_ALL_VERB]

    def test_an_idle_organism_short_circuits(self):
        """Nothing to stop is an answer, not an arming prompt."""
        _ok, kb, sent, said = _bind(running=lambda: 0)
        kb.bindings[0].handler(None)
        assert sent == []
        assert said[-1] == "nothing running"
        # And it did NOT stay armed — the next press starts clean.
        kb.bindings[0].handler(None)
        assert sent == [], "an idle press must not have half-armed the chord"

    def test_a_broken_count_still_arms(self):
        """The roster is chrome. It must not be able to disarm a control."""
        def _boom() -> int:
            raise RuntimeError("roster unavailable")

        _ok, kb, sent, said = _bind(running=_boom)
        kb.bindings[0].handler(None)
        assert "all running work" in said[-1]
        kb.bindings[0].handler(None)
        assert sent == [STOP_ALL_VERB]

    def test_a_send_failure_is_reported_not_swallowed(self):
        from prompt_toolkit.key_binding import KeyBindings

        def _fail(_line):
            raise ConnectionError("socket gone")

        said = []
        kb = KeyBindings()
        install_stop_all_binding(
            kb, types.SimpleNamespace(send_input=_fail),
            notify=said.append, running=lambda: 1,
        )
        kb.bindings[0].handler(None)
        kb.bindings[0].handler(None)
        assert "could not reach" in said[-1], (
            "a chord that silently failed would leave the operator believing "
            "they stopped work that is still running"
        )

    def test_a_client_that_cannot_send_binds_nothing(self):
        from prompt_toolkit.key_binding import KeyBindings

        assert install_stop_all_binding(
            KeyBindings(), object(),
        ) is False
        assert install_stop_all_binding(None, types.SimpleNamespace(
            send_input=lambda _l: None)) is False

    def test_the_action_is_remappable(self):
        """Registered in the catalog, so `/keys` lists it and
        keybindings.json can move it."""
        from backend.core.ouroboros.battle_test.keymap import action_catalog

        _bind()
        assert any(s.action == STOP_ALL_ACTION for s in action_catalog())


# ---------------------------------------------------------------------------
# The authority
# ---------------------------------------------------------------------------


class TestCancelAll:
    def _gls(self, active, requested=()):
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService,
        )
        g = object.__new__(GovernedLoopService)
        g._active_ops = set(active)
        g._cancel_requested = set(requested)
        return g, GovernedLoopService.request_cancel_all

    def test_it_reports_what_it_actually_stopped(self):
        g, fn = self._gls({"op-1", "op-2"})
        assert fn(g) == ["op-1", "op-2"]
        assert g._cancel_requested == {"op-1", "op-2"}

    def test_an_op_already_asked_is_not_re_reported(self):
        """The operator reads this list as "what I just did"."""
        g, fn = self._gls({"op-1", "op-2"}, requested={"op-2"})
        assert fn(g) == ["op-1"]
        assert g._cancel_requested == {"op-1", "op-2"}

    def test_nothing_running_is_an_empty_list_not_a_lie(self):
        g, fn = self._gls(set())
        assert fn(g) == []

    def test_it_is_idempotent(self):
        g, fn = self._gls({"op-1"})
        assert fn(g) == ["op-1"]
        assert fn(g) == []

    def test_it_is_cooperative_never_a_kill(self):
        """It marks the cancel set the orchestrator reads at phase
        transitions. Anything that severed an op mid-APPLY would leave the
        half-written trees the phase model exists to prevent."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopService,
        )
        src = inspect.getsource(GovernedLoopService.request_cancel_all)
        tree = ast.parse(src.lstrip())
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert not (called & {"cancel", "kill", "terminate", "close"}), (
            f"cancel-all reached for a hard stop: {called}"
        )


# ---------------------------------------------------------------------------
# Both cockpits, one route
# ---------------------------------------------------------------------------


class TestBothSurfaces:
    """A chord mounted on one of two cockpits is the defect shape this repo
    keeps finding late: the surface nobody opened that week is the one that
    silently lacks the control."""

    def test_the_daemon_cockpit_mounts_it(self):
        from backend.core.ouroboros.battle_test.cockpit_mount import (
            daemon_key_bindings,
        )
        repl = types.SimpleNamespace(
            _dispatch_verb=lambda _l: None,
            _flow=types.SimpleNamespace(
                console=types.SimpleNamespace(print=lambda *a, **k: None)),
        )
        kb = daemon_key_bindings(repl)
        assert kb is not None
        sequences = [tuple(str(k) for k in b.keys) for b in kb.bindings]
        assert ("Keys.ControlX", "Keys.ControlK") in sequences

    def test_the_attach_client_mounts_it(self):
        """Structural, because the client's binding block needs a live
        `client`, `ui` and `fsm` that only exist inside a running attach.
        Proves the CALL is present, not that a key was pressed — the
        daemon-side test above covers the runtime behaviour."""
        from backend.core.ouroboros.cli import ov

        tree = ast.parse(inspect.getsource(ov))
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "install_stop_all_binding"
            for node in ast.walk(tree)
        ), "ov.py never calls the shared installer — the chord is dark there"

    def test_both_surfaces_route_through_the_same_verb(self):
        """One authority, one route. A client-side implementation would have
        had to invent its own idea of "all" on a process that holds no
        governed loop and cannot see the ops."""
        from backend.core.ouroboros.battle_test import subagent_control

        src = inspect.getsource(subagent_control)
        tree = ast.parse(src)
        sends = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_input"
        ]
        assert len(sends) == 1, (
            "exactly one send site — a second would be a second opinion "
            "about what the chord does"
        )

    def test_the_daemon_handler_answers_the_verb(self):
        from backend.core.ouroboros.battle_test import serpent_flow

        printed = []
        repl = types.SimpleNamespace(
            _flow=types.SimpleNamespace(console=types.SimpleNamespace(
                print=lambda *a, **k: printed.append(a[0] if a else ""))),
            _gls=types.SimpleNamespace(
                request_cancel_all=lambda: ["op-aaa", "op-bbb"]),
        )
        serpent_flow.SerpentREPL._handle_stop_all(repl)
        assert "2 ops" in printed[-1]
        assert "op-aaa" in printed[-1] and "op-bbb" in printed[-1]
        assert "phase boundary" in printed[-1], (
            "an operator told 'stopped' who then watches a VERIFY finish "
            "concludes the verb is broken — say it is cooperative"
        )

    def test_the_daemon_handler_says_so_when_it_cannot_act(self):
        from backend.core.ouroboros.battle_test import serpent_flow

        printed = []
        repl = types.SimpleNamespace(
            _flow=types.SimpleNamespace(console=types.SimpleNamespace(
                print=lambda *a, **k: printed.append(a[0] if a else ""))),
            _gls=None,
        )
        serpent_flow.SerpentREPL._handle_stop_all(repl)
        assert "unavailable" in printed[-1]

    def test_the_verb_is_wired_into_the_dispatch_ladder(self):
        """Structural: the ladder is a 400-line async method with many early
        returns, so reaching this branch for real needs a whole REPL. What
        this pins is that the branch CALLS the handler — which is what was
        missing when `search_rows` shipped routed-and-dark."""
        from backend.core.ouroboros.battle_test import serpent_flow

        tree = ast.parse(inspect.getsource(
            serpent_flow.SerpentREPL._dispatch_repl_command,
        ).lstrip())
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_handle_stop_all"
            for node in ast.walk(tree)
        ), "/stop-all has a handler and no branch that reaches it"

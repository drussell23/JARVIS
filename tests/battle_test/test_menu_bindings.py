"""The completion menu and the gate, made remappable.

Both surfaces WORKED and neither could be rebound — `/keys` could not list
them and `keybindings.json` could not move them. They are the last of CC's
interactive-action catalog ov had no answer for.

The work is a different SHAPE from every other action in this cockpit: those
were new, so declaring them was the whole job. These are already bound inside
prompt_toolkit, so the lever is the filter — a binding registered after pt's
and gated on `has_completions` wins exactly while the menu is open.

The failure that matters most is pinned first: ov's gate is a STRIP over a
live prompt, not CC's modal, so a bare `y` would accept a patch on the first
character of "yes, rerun the suite".
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import menu_bindings as M
from backend.core.ouroboros.battle_test import pending_apply as PA


@pytest.fixture(autouse=True)
def _clean():
    PA.reset_for_tests()
    yield
    PA.reset_for_tests()


def _kb():
    from prompt_toolkit.key_binding import KeyBindings
    return KeyBindings()


class TestCompletionActions:
    def test_all_four_bind(self):
        kb = _kb()
        assert M.install_completion_actions(kb) == 4

    def test_they_are_registered_as_remappable(self):
        """The entire point: `/keys` can list them and keybindings.json can
        move them."""
        from backend.core.ouroboros.battle_test.keymap import action_catalog

        M.install_completion_actions(_kb())
        cat = {s.action: s.context for s in action_catalog()}
        for action in ("autocomplete:accept", "autocomplete:dismiss",
                       "autocomplete:next", "autocomplete:previous"):
            assert cat.get(action) == "Autocomplete", action

    def test_they_are_gated_on_the_menu_being_open(self):
        """With the menu closed, pt's own handling must be untouched — Tab
        still completes, the arrows still walk history. A binding that
        applied always would have taken both."""
        kb = _kb()
        M.install_completion_actions(kb)
        assert all(b.filter is not None for b in kb.bindings)

    def test_accept_applies_the_highlighted_completion(self):
        import types

        applied = []
        buf = types.SimpleNamespace(
            complete_state=types.SimpleNamespace(current_completion="X"),
            apply_completion=applied.append,
            complete_next=lambda: applied.append("NEXT"),
        )
        kb = _kb()
        M.install_completion_actions(kb)
        handler = next(b.handler for b in kb.bindings
                       if [str(k) for k in b.keys] == ["Keys.ControlI"])
        handler(types.SimpleNamespace(current_buffer=buf, app=None))
        assert applied == ["X"]

    def test_accept_selects_the_first_when_nothing_is_highlighted(self):
        """The menu is open but the operator has not walked it. Selecting the
        first entry is what every shell does and what makes one Tab enough."""
        import types

        calls = []
        buf = types.SimpleNamespace(
            complete_state=types.SimpleNamespace(current_completion=None),
            apply_completion=lambda c: calls.append(("apply", c)),
            complete_next=lambda: calls.append(("next", None)),
        )
        kb = _kb()
        M.install_completion_actions(kb)
        handler = next(b.handler for b in kb.bindings
                       if [str(k) for k in b.keys] == ["Keys.ControlI"])
        handler(types.SimpleNamespace(current_buffer=buf, app=None))
        assert calls == [("next", None)]

    def test_a_broken_buffer_never_raises(self):
        import types

        kb = _kb()
        M.install_completion_actions(kb)
        for b in kb.bindings:
            b.handler(types.SimpleNamespace(current_buffer=None, app=None))

    def test_the_kill_switch_leaves_pt_alone(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MENU_BINDINGS_ENABLED", "0")
        assert M.install_completion_actions(_kb()) == 0


class TestGatePredicate:
    def test_no_gate_is_not_answerable(self):
        assert M.gate_is_pending() is False

    def test_an_open_gate_is(self):
        PA.note_pending("op-1", delay_s=5.0, reason="notify_apply")
        assert M.gate_is_pending() is True

    def test_a_cleared_gate_is_not(self):
        PA.note_pending("op-1", delay_s=5.0, reason="notify_apply")
        PA.clear_pending("op-1")
        assert M.gate_is_pending() is False

    def test_it_never_decides_expiry_itself(self):
        """`snapshot` drops expired rows where the clock that set them lives.
        A confirm key answering a gate that had already auto-applied would be
        worse than one that did nothing."""
        import inspect

        src = inspect.getsource(M.gate_is_pending)
        assert "snapshot" in src
        assert "time" not in src and "monotonic" not in src


class TestConfirmActions:
    def test_both_bind(self):
        assert M.install_confirm_actions(_kb(), submit=lambda _l: None) == 2

    def test_nothing_binds_without_a_submit(self):
        """A confirm key with nowhere to send the answer is a key that
        silently does nothing while looking like governance."""
        assert M.install_confirm_actions(_kb(), submit=None) == 0

    def test_they_route_the_verb_not_a_private_path(self):
        """The risk tier, the audit trail and the countdown clear exactly as
        they do for a typed `/accept`. A second approval path that disagreed
        with the verb would be a governance problem, not a UI one."""
        sent = []
        kb = _kb()
        M.install_confirm_actions(kb, submit=sent.append)
        for key, verb in (("y", "/accept"), ("n", "/reject")):
            handler = next(b.handler for b in kb.bindings
                           if [str(k) for k in b.keys] == [key])
            handler(None)
            assert sent[-1] == verb

    def test_they_are_registered_as_remappable(self):
        from backend.core.ouroboros.battle_test.keymap import action_catalog

        M.install_confirm_actions(_kb(), submit=lambda _l: None)
        cat = {s.action: s.context for s in action_catalog()}
        assert cat.get("confirm:yes") == "Confirmation"
        assert cat.get("confirm:no") == "Confirmation"

    def test_they_require_both_a_gate_and_an_empty_prompt(self):
        """THE guard CC does not need. Its Confirmation context is a MODAL,
        so a bare `y` is free there. ov's gate is a strip over a live prompt,
        and an unconditional `y` would accept a patch on the first character
        of 'yes, rerun the suite'."""
        import inspect

        src = inspect.getsource(M.install_confirm_actions)
        assert "gate_is_pending()" in src and "_buffer_empty()" in src

    def test_an_unknown_buffer_state_refuses(self):
        """False when it cannot tell — the safe direction, because a confirm
        key must not fire on a prompt that might have text in it."""
        assert M._buffer_empty() is False        # no running Application

    def test_escape_is_not_bound_to_reject(self):
        """Escape already means interrupt-or-dismiss and the overlay arbiter
        owns it. A third meaning that appears only while a gate is up is how
        an operator learns to distrust the key."""
        kb = _kb()
        M.install_confirm_actions(kb, submit=lambda _l: None)
        keys = {" ".join(str(k) for k in b.keys) for b in kb.bindings}
        assert not any("Escape" in k for k in keys), keys

    def test_a_failing_submit_never_raises(self):
        def _boom(_line):
            raise RuntimeError("router gone")

        kb = _kb()
        M.install_confirm_actions(kb, submit=_boom)
        next(b.handler for b in kb.bindings
             if [str(k) for k in b.keys] == ["y"])(None)


class TestMountedOnTheCockpit:
    def test_the_real_application_carries_them(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
        mux = BipartiteLayout(width=80, height=14)
        app = build_bipartite_application(mux, on_accept=lambda _t: None)
        keys = {" ".join(str(k).replace("Keys.", "") for k in b.keys)
                for b in app.key_bindings.bindings}
        assert "y" in keys and "n" in keys

    def test_confirm_routes_through_on_accept(self):
        import ast
        import inspect
        import textwrap

        from backend.core.ouroboros.battle_test import bipartite_layout

        src = textwrap.dedent(inspect.getsource(
            bipartite_layout.build_bipartite_application))
        tree = ast.parse(src)
        assert any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "install_confirm_actions"
            for n in ast.walk(tree)
        )
        assert "on_accept(line)" in src

"""`Escape` must mean two things, and the input processor must never guess.

    Escape         dismiss the overlay covering the cockpit
    Escape Escape  open the rewind menu

`prompt_toolkit` disambiguates by BUFFERING the first `Escape` to see whether a
second follows, and `eager=True` opts out of that wait. Neither static choice
works: without eager the panic overlay needs a timeout to close while advertising
"esc dismisses"; with eager the `esc esc` sequence can never complete and rewind
becomes unreachable.

The state machine is asserted against the REAL `KeyProcessor`, driven
asynchronously because `process_keys` schedules its own buffering timer on the
running loop — the very mechanism under test. A synchronous test cannot exercise
the wait at all, which is precisely why the collision survived: it is invisible
until keys are actually fed through the processor.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test import overlay_arbiter as oa


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is process-global; a leaked overlay holds `Escape` eager and
    would silently delete `esc esc` for every later test."""
    oa.reset_for_tests()
    yield
    oa.reset_for_tests()


class _Harness:
    """Feeds real key presses through a real processor inside a real app session."""

    def __init__(self):
        self.fired = []
        self.overlay_up = False
        oa.register_overlay(
            "panic", z=oa.Z_PANIC,
            is_active=lambda: self.overlay_up,
            dismiss=lambda: self.fired.append("dismiss"),
        )
        from prompt_toolkit.key_binding import KeyBindings
        self.kb = KeyBindings()
        assert oa.install_escape_arbiter(
            self.kb, rewind=lambda _e: self.fired.append("rewind"))

    async def press(self, count: int):
        """Feed ``count`` Escapes and return what fired."""
        from prompt_toolkit.application import Application
        from prompt_toolkit.application.current import set_app
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.key_binding.key_processor import (
            KeyPress, KeyProcessor,
        )
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.output import DummyOutput

        self.fired.clear()
        with create_pipe_input() as pipe:
            app = Application(key_bindings=self.kb, input=pipe,
                              output=DummyOutput())
            with set_app(app):
                proc = KeyProcessor(self.kb)
                for _ in range(count):
                    proc.feed(KeyPress(Keys.Escape, ""))
                proc.process_keys()
                # Let the buffering timer settle, so a binding that is waiting
                # for a longer match gets its chance to time out. Without this a
                # non-eager Escape looks identical to one that never matched.
                await asyncio.sleep(0.05)
                proc.process_keys()
        return list(self.fired)


class TestTheThreeTransitions:
    """The mandated state machine."""

    @pytest.mark.asyncio
    async def test_an_overlay_makes_escape_eager(self):
        """(1) Overlay up: ONE Escape closes it, with no wait for a second."""
        h = _Harness()
        h.overlay_up = True
        assert await h.press(1) == ["dismiss"]

    @pytest.mark.asyncio
    async def test_a_clear_cockpit_ignores_a_single_escape(self):
        """(2) Nothing to close: a lone Escape must NOT reach the dismiss
        callback. If it does, the binding is eager unconditionally and `esc esc`
        is already dead."""
        h = _Harness()
        h.overlay_up = False
        assert await h.press(1) == []

    @pytest.mark.asyncio
    async def test_escape_escape_opens_rewind_when_clear(self):
        """(3) The sequence completes because nothing is competing for the
        prefix — buffered naturally by the processor, not by a tuned timer."""
        h = _Harness()
        h.overlay_up = False
        assert await h.press(2) == ["rewind"]

    @pytest.mark.asyncio
    async def test_the_two_meanings_never_fire_together(self):
        """The filters are complements, so no keystroke can satisfy both. An
        overlay dismissal that ALSO opened the rewind menu would be the
        collision wearing a different costume."""
        h = _Harness()
        h.overlay_up = True
        assert "rewind" not in await h.press(2)

    @pytest.mark.asyncio
    async def test_one_press_closes_exactly_one_overlay(self):
        """A panic over a gate: Escape means "close what I am looking at".
        Cascading would discard a decision the operator never saw."""
        h = _Harness()
        h.overlay_up = True
        gate = {"up": True}
        oa.register_overlay("gate", z=oa.Z_IRON_GATE,
                            is_active=lambda: gate["up"],
                            dismiss=lambda: h.fired.append("gate"))
        assert await h.press(1) == ["dismiss"]      # panic outranks the gate
        h.overlay_up = False
        assert await h.press(1) == ["gate"]


class TestTheBindingItself:
    def test_escape_is_bound_eagerly_by_a_FILTER_not_a_bool(self):
        """The root fix, pinned. `eager=True` breaks the sequence and
        `eager=False` breaks the dismiss; only a per-keystroke filter can be
        right in both states, and a well-meaning simplification to a bool would
        pass every behavioural test in one of the two states."""
        from prompt_toolkit.filters import Filter
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()
        oa.register_overlay("x", z=1, is_active=lambda: False,
                            dismiss=lambda: None)
        oa.install_escape_arbiter(kb, rewind=lambda _e: None)
        single = [b for b in kb.bindings if len(b.keys) == 1]
        assert single, "no single-Escape binding was installed"
        assert isinstance(single[0].eager, Filter), (
            f"eager must be a Filter evaluated per keystroke, got "
            f"{single[0].eager!r}")

    def test_both_the_key_and_the_sequence_are_bound(self):
        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()
        oa.install_escape_arbiter(kb, rewind=lambda _e: None)
        lengths = sorted(len(b.keys) for b in kb.bindings)
        assert lengths == [1, 2], f"expected Esc and Esc-Esc, got {lengths}"

    def test_the_sequence_uses_rewinds_own_default_keys(self):
        """Spelled once, in `rewind_menu`. Re-typing "esc esc" here is how two
        modules end up disagreeing about a key."""
        from backend.core.ouroboros.battle_test.rewind_menu import (
            REWIND_DEFAULT_KEYS,
        )
        import inspect
        src = inspect.getsource(oa.install_escape_arbiter)
        assert "REWIND_DEFAULT_KEYS" in src
        assert REWIND_DEFAULT_KEYS

    def test_no_rewind_handler_still_binds_the_dismiss(self):
        """A surface with no rewind (there is no `/undo` planner on every
        cockpit) must still be able to close its overlays."""
        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()
        assert oa.install_escape_arbiter(kb, rewind=None)
        assert [len(b.keys) for b in kb.bindings] == [1]


class TestTheRegistry:
    def test_nothing_registered_means_escape_is_not_eager(self):
        """The DEFAULT must be the safe one: with no overlays the sequence has
        to keep working, because that is the state a cockpit is in almost all of
        the time."""
        assert oa.overlay_active() is False
        assert oa.dismiss_top() is None

    def test_topmost_wins_by_z(self):
        oa.register_overlay("diff", z=oa.Z_DIFF_PREVIEW,
                            is_active=lambda: True, dismiss=lambda: None)
        oa.register_overlay("panic", z=oa.Z_PANIC,
                            is_active=lambda: True, dismiss=lambda: None)
        top = oa.top_overlay()
        assert top is not None and top.name == "panic"

    def test_a_tie_is_broken_deterministically(self):
        """Two overlays at one Z must not be dismissed in dict order — "which
        one did Escape close" cannot depend on registration sequence."""
        for name in ("zebra", "alpha"):
            oa.register_overlay(name, z=50, is_active=lambda: True,
                                dismiss=lambda: None)
        assert [o.name for o in oa.active_overlays()] == ["alpha", "zebra"]

    def test_an_inactive_overlay_is_not_counted(self):
        oa.register_overlay("panic", z=oa.Z_PANIC, is_active=lambda: False,
                            dismiss=lambda: None)
        assert oa.overlay_active() is False

    def test_an_is_active_that_raises_FAILS_OPEN(self):
        """Guessing "active" would hold Escape eager forever and silently delete
        `esc esc` for the session — indistinguishable from the bug this module
        exists to end. Guessing "clear" opens a rewind menu unexpectedly, which
        is visible and recoverable."""
        def _boom():
            raise RuntimeError("state unreadable")
        oa.register_overlay("broken", z=1, is_active=_boom,
                            dismiss=lambda: None)
        assert oa.overlay_active() is False

    def test_a_dismiss_that_raises_does_not_claim_success(self):
        """The overlay is still up. Reporting a name would make the key look
        answered while the screen has not changed."""
        def _boom():
            raise RuntimeError("cannot close")
        oa.register_overlay("stuck", z=1, is_active=lambda: True,
                            dismiss=_boom)
        assert oa.dismiss_top() is None

    def test_reregistering_a_name_replaces_it(self):
        """A surface rebuilt on reconnect must not leave a stale predicate
        reporting an overlay nobody can see."""
        oa.register_overlay("panic", z=1, is_active=lambda: True,
                            dismiss=lambda: None)
        oa.register_overlay("panic", z=1, is_active=lambda: False,
                            dismiss=lambda: None)
        assert oa.overlay_active() is False
        assert len(oa.active_overlays()) == 0

    def test_unregister_removes_it(self):
        oa.register_overlay("panic", z=1, is_active=lambda: True,
                            dismiss=lambda: None)
        assert oa.unregister_overlay("panic") is True
        assert oa.overlay_active() is False
        assert oa.unregister_overlay("panic") is False

    @pytest.mark.parametrize("bad", [
        {"name": "", "is_active": lambda: True, "dismiss": lambda: None},
        {"name": "x", "is_active": None, "dismiss": lambda: None},
        {"name": "x", "is_active": lambda: True, "dismiss": None},
    ])
    def test_a_malformed_registration_is_refused(self, bad):
        assert oa.register_overlay(z=1, **bad) is False
        assert oa.overlay_active() is False

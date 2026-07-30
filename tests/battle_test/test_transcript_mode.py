"""The transcript viewer as a MODE — CC's `Ctrl+O`.

Most of CC's viewer keys were already bound. What was missing is the state
that makes them a viewer: `transcript_hatches` used "is the operator scrolled
back" as a stand-in, which is silent, is not the doorway CC teaches, and left
`j`/`k`/`g`/`G`/`Space` unbindable because at the live tail they type as
themselves.

Three failure modes here read as normal operation, so they are pinned:

  * a printable key that stays live at the tail (typing `{` teleports the view),
  * a paging key wired against the viewport's inverted sign (a dead key),
  * a `?` panel that lists a binding the operator does not actually have.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.transcript_mode import (
    enter_transcript_mode,
    exit_transcript_mode,
    install_transcript_mode_bindings,
    is_transcript_mode,
    reset_transcript_mode_for_tests,
    shortcut_panel,
    toggle_transcript_mode,
    transcript_surface_active,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_transcript_mode_for_tests()
    yield
    reset_transcript_mode_for_tests()
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        set_active_canvas,
    )
    set_active_canvas(None)


@pytest.fixture
def canvas():
    """A live canvas with more lines than fit, registered as the sink."""
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, set_active_canvas,
    )
    mux = BipartiteLayout(width=100, height=12)
    for i in range(60):
        mux.emit("line", {"text": f"line {i}"})
    set_active_canvas(mux)
    return mux


@pytest.fixture
def keys():
    from prompt_toolkit.key_binding import KeyBindings

    said = []
    kb = KeyBindings()
    assert install_transcript_mode_bindings(kb, notify=said.append)
    table = {
        " ".join(str(k).replace("Keys.", "") for k in b.keys): b.handler
        for b in kb.bindings
    }
    return table, said


class TestMode:
    def test_it_starts_off(self):
        assert is_transcript_mode() is False

    def test_ctrl_o_toggles(self, keys):
        table, said = keys
        table["ControlO"](None)
        assert is_transcript_mode() is True
        assert "transcript" in said[-1]
        table["ControlO"](None)
        assert is_transcript_mode() is False

    def test_q_and_escape_both_leave(self, keys):
        table, _said = keys
        for exit_key in ("q", "Escape"):
            enter_transcript_mode()
            table[exit_key](None)
            assert is_transcript_mode() is False, f"{exit_key} did not exit"

    def test_entering_does_not_move_the_view(self, canvas, keys):
        """The line the operator pressed the key while reading is the one
        line they certainly care about."""
        table, _said = keys
        before = canvas._viewport.offset
        table["ControlO"](None)
        assert canvas._viewport.offset == before

    def test_leaving_returns_to_the_tail(self, canvas, keys):
        """The organism keeps working while the operator reads. An exit that
        left the view in history hands back a cockpit that looks live and is
        showing minutes-old output."""
        table, _said = keys
        table["ControlO"](None)
        table["k"](None)
        table["k"](None)
        assert canvas._viewport.offset > 0
        table["q"](None)
        assert canvas._viewport.offset == 0
        assert canvas._viewport.following is True

    def test_toggle_returns_the_new_state(self):
        assert toggle_transcript_mode() is True
        assert toggle_transcript_mode() is False

    def test_enter_and_exit_report_whether_they_changed_anything(self):
        assert enter_transcript_mode() is True
        assert enter_transcript_mode() is False
        assert exit_transcript_mode() is True
        assert exit_transcript_mode() is False


class TestMovement:
    def test_k_and_j_move_one_line(self, canvas, keys):
        table, _said = keys
        enter_transcript_mode()
        table["k"](None)
        assert canvas._viewport.offset == 1, "k must move toward HISTORY"
        table["j"](None)
        assert canvas._viewport.offset == 0

    def test_half_page(self, canvas, keys):
        table, _said = keys
        enter_transcript_mode()
        _total, budget = canvas.scroll_metrics()
        table["ControlU"](None)
        assert canvas._viewport.offset == budget // 2

    def test_paging_up_goes_to_history(self, canvas, keys):
        """THE inverted-sign trap. `CanvasViewport.page` takes +1 for PgUp
        while `scroll` takes NEGATIVE for older — the two disagree, and the
        viewport's own docstring records the first PgUp binding getting this
        backwards and reading as a dead key."""
        table, _said = keys
        enter_transcript_mode()
        for key in ("ControlB", "b"):
            canvas._viewport.to_bottom()
            table[key](None)
            assert canvas._viewport.offset > 0, (
                f"{key} paged toward the tail the view was already on — "
                "the sign flip into CanvasViewport.page is inverted"
            )

    def test_paging_down_returns_toward_the_tail(self, canvas, keys):
        table, _said = keys
        enter_transcript_mode()
        table["ControlB"](None)
        table["ControlB"](None)
        deep = canvas._viewport.offset
        for key in ("ControlF", " "):
            before = canvas._viewport.offset
            table[key](None)
            assert canvas._viewport.offset < before, f"{key} did not go down"
        assert deep > 0

    def test_g_and_shift_g_are_the_ends(self, canvas, keys):
        table, _said = keys
        enter_transcript_mode()
        table["g"](None)
        assert canvas._viewport.offset > 0
        assert canvas._viewport.hit_top or canvas._viewport.offset > 0
        table["G"](None)
        assert canvas._viewport.offset == 0

    def test_movement_without_a_canvas_is_silent(self, keys):
        """The cockpit boots before a canvas is registered, and a key
        pressed in that window must not raise into dispatch."""
        table, _said = keys
        enter_transcript_mode()
        for key in ("k", "j", "ControlU", "ControlB", "g", "G"):
            table[key](None)


class TestPrintableKeysStayInert:
    def test_the_surface_predicate_covers_both_doorways(self, canvas):
        """`[`, `v`, `{`, `}` are characters someone may genuinely type, so
        they bind only where they cannot be meant literally. Scrolled-back
        was the only such state; the viewer is now the other one, and both
        resolve through ONE predicate so they cannot drift."""
        assert transcript_surface_active() is False
        enter_transcript_mode()
        assert transcript_surface_active() is True
        exit_transcript_mode()
        assert transcript_surface_active() is False
        canvas._viewport.scroll(-3, total=60, budget=11)
        assert transcript_surface_active() is True, (
            "the pre-existing scrolled-back habit must keep working"
        )

    def test_the_hatches_use_that_predicate(self):
        """Widened, not replaced — an operator with the scroll-back habit
        must not lose it because a mode arrived."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import transcript_hatches

        src = inspect.getsource(transcript_hatches.install_transcript_hatches)
        tree = ast.parse(src.lstrip())
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert "transcript_surface_active" in names
        assert "is_scrolled_back" not in names, (
            "the hatches still filter on scrolled-back alone, so `[`/`v`/"
            "`{`/`}` are dead inside the viewer"
        )


class TestShortcutPanel:
    def test_it_lists_the_viewer_keys(self):
        panel = "\n".join(shortcut_panel())
        for expected in ("scroll one line up", "search", "next match",
                         "open in $EDITOR", "leave the viewer"):
            assert expected in panel

    def test_it_renders_keys_an_operator_can_read(self):
        """The first cut printed prompt_toolkit's wire form — `c-u`, and the
        space key as nothing at all: a shortcut reference with a blank in it.
        """
        panel = "\n".join(shortcut_panel())
        assert "ctrl+u" in panel and "c-u" not in panel
        assert "space" in panel

    def test_it_reads_back_the_effective_keys_not_the_defaults(self):
        """A panel that confidently lists someone else's bindings is worse
        than no panel."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import transcript_mode

        src = inspect.getsource(transcript_mode.shortcut_panel)
        assert "effective_key_sequences" in src
        tree = ast.parse(src.lstrip())
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "effective_key_sequences"
            for node in ast.walk(tree)
        )

    def test_question_mark_only_answers_inside_the_viewer(self, keys):
        table, said = keys
        before = len(said)
        table["ControlO"](None)
        table["?"](None)
        assert len(said) > before
        assert any("transcript viewer" in str(s) for s in said)


class TestCtrlOCollision:
    """Three actions claimed Ctrl+O: the deck's `lanes`, the narration
    toggle, and the legacy TUI's expand. One key cannot serve three."""

    def _defaults_for(self, action: str):
        from backend.core.ouroboros.battle_test.keymap import action_catalog

        for spec in action_catalog():
            if spec.action == action:
                return spec.default_keys
        return None

    def test_the_viewer_owns_ctrl_o(self, keys):
        assert self._defaults_for("transcript:toggle") == ("ctrl+o",)

    def test_narration_moved_off_it(self):
        from prompt_toolkit.key_binding import KeyBindings

        from backend.core.ouroboros.battle_test.transcript_hatches import (
            install_transcript_hatches,
        )
        ui = type("_UI", (), {"flash": lambda self, *a, **k: None})()
        client = type("_C", (), {"send_input": lambda self, _l: None})()
        install_transcript_hatches(KeyBindings(), ui, client)
        keys_ = self._defaults_for("narrate:toggle")
        assert keys_ is not None and "ctrl+o" not in keys_, (
            f"narrate:toggle still claims Ctrl+O ({keys_})"
        )

    def test_lanes_moved_off_it(self):
        """Read from the source, because building the deck bindings needs a
        live client, ui and fsm that only exist inside a running attach."""
        import inspect

        from backend.core.ouroboros.cli import ov

        src = inspect.getsource(ov._build_selection_bindings)
        assert '"deck:open", ("ctrl+o",)' not in src
        assert '"deck:open", ("ctrl+x ctrl+l",)' in src

    def test_the_toolbar_advertises_the_key_that_works(self):
        """A footer naming a key nothing binds is the exact defect the
        keybinding registry exists to prevent."""
        import inspect

        from backend.core.ouroboros.cli import ov

        src = inspect.getsource(ov)
        assert "^O lanes" not in src
        assert "^X ^L lanes" in src


class TestQuestionMarkOwnership:
    def test_the_cockpit_help_stands_down_inside_the_viewer(self):
        """`?` is bound twice on the attach client — the cockpit's shortcut
        list and the viewer's own table. CC has both too ("press `?` in the
        transcript viewer to see available shortcuts there"), but both
        filters pass inside the viewer with an empty prompt, so without an
        explicit exclusion the winner is whichever mount registered last.
        Correct today, silently inverted the day someone reorders them.
        """
        import inspect

        from backend.core.ouroboros.cli import ov

        src = inspect.getsource(ov._build_chat_bindings) if hasattr(
            ov, "_build_chat_bindings") else inspect.getsource(ov)
        assert "_not_in_transcript" in src
        assert "_empty_buffer & _not_in_transcript" in src, (
            "app:help still fires on ? inside the transcript viewer"
        )

"""Park a half-written goal, do something else, get it back.

The prompt accepts paragraphs now, which makes it worth interrupting — and
without somewhere to put a draft, the only options are losing it or finishing
it blind. Both are bad enough that people stop writing long goals, which
quietly undoes multi-line input.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.draft_stash import (
    DraftStash, install_stash_binding, stash_enabled,
)


def test_it_parks_a_draft_and_clears_the_buffer() -> None:
    stash = DraftStash()
    assert stash.toggle("a half-written goal", 8) == ("", 0)
    assert stash.holding is True


def test_it_restores_with_the_cursor_where_it_was() -> None:
    """Restoring a paragraph with the caret at 0 means hunting for where you
    were."""
    stash = DraftStash()
    stash.toggle("line one\nline two", 12)
    text, cursor = stash.toggle("", 0)
    assert text == "line one\nline two"
    assert cursor == 12
    assert stash.holding is False


def test_a_SECOND_stash_swaps_instead_of_overwriting() -> None:
    """THE rule. A single slot is a thing you can hold in your head, but a
    second stash would destroy the first — and silently destroying a draft is
    exactly what this exists to prevent. So it swaps: nothing is ever lost,
    and the operator gets the other draft rather than an error."""
    stash = DraftStash()
    stash.toggle("first draft", 5)
    returned, cursor = stash.toggle("second draft", 3)
    assert returned == "first draft"
    assert cursor == 5
    assert stash.preview.startswith("second draft")


def test_toggling_an_empty_buffer_with_nothing_held_is_a_no_op() -> None:
    """Not an error either — a key that scolds you for pressing it is a key
    you stop pressing."""
    stash = DraftStash()
    assert stash.toggle("", 0) == ("", 0)
    assert stash.holding is False


def test_whitespace_alone_is_not_a_draft() -> None:
    stash = DraftStash()
    stash.toggle("   \n  ", 2)
    assert stash.holding is False


def test_multiline_survives_the_round_trip() -> None:
    """The whole reason this exists is the long goals multi-line enabled."""
    draft = "fix the containment check\nand also the coverage gate\n\nthen soak"
    stash = DraftStash()
    stash.toggle(draft, len(draft))
    assert stash.toggle("", 0)[0] == draft


def test_the_cursor_is_clamped_on_restore() -> None:
    """The buffer it returns to may not be the one it left."""
    stash = DraftStash()
    stash.toggle("short", 9999)
    _text, cursor = stash.toggle("", 0)
    assert cursor <= len("short")


@pytest.mark.parametrize("junk", [None, 42, object()])
def test_junk_never_eats_the_draft(junk) -> None:
    stash = DraftStash()
    text, cursor = stash.toggle(junk, 0)
    assert isinstance(text, str) and isinstance(cursor, int)


# --------------------------------------------------------------------------
# the binding
# --------------------------------------------------------------------------

def test_ctrl_s_is_bound_and_actually_swaps() -> None:
    """Registered AND invoked — a binding that exists in source but does
    nothing is this codebase's most repeated defect."""
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings

    buf = Buffer(multiline=True)
    buf.text = "a parked goal"
    buf.cursor_position = 4
    kb = KeyBindings()
    stash = install_stash_binding(kb, lambda: buf)
    assert stash is not None

    combos = [tuple(str(k) for k in b.keys) for b in kb.bindings]
    assert ("Keys.ControlS",) in combos

    kb.bindings[0].handler(object())
    assert buf.text == ""
    kb.bindings[0].handler(object())
    assert buf.text == "a parked goal"


def test_ctrl_s_reaches_the_app_rather_than_freezing_the_terminal() -> None:
    """Ctrl+S is XOFF under a cooked terminal and would freeze output instead
    of arriving. prompt_toolkit puts the input in raw mode with IXON cleared —
    verified against the library, because the failure mode is a terminal that
    appears to hang."""
    import inspect

    from prompt_toolkit.input import vt100

    assert "IXON" in inspect.getsource(vt100)


def test_both_surfaces_bind_it() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        src = (repo / path).read_text()
        names = {a.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "install_stash_binding" in names, f"{path} has no stash"


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from prompt_toolkit.key_binding import KeyBindings

    monkeypatch.setenv("JARVIS_DRAFT_STASH_ENABLED", "0")
    assert stash_enabled() is False
    kb = KeyBindings()
    assert install_stash_binding(kb, lambda: None) is None
    assert not kb.bindings


# --------------------------------------------------------------------------
# the two arguments recovered from the closed PR
# --------------------------------------------------------------------------

def test_the_prompt_offers_history_search_and_an_editor() -> None:
    """Both are prompt_toolkit built-ins that were simply never passed. Up
    prefix-filters instead of walking 2000 entries, and Ctrl+X Ctrl+E finishes
    a long goal in $EDITOR where the terminal's editing model runs out."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "enable_history_search=True" in src
    assert "enable_open_in_editor=True" in src

"""What you typed last time — on both surfaces, from one file.

`ov` had no command history. Not "no search UI" — none at all: the
`PromptSession` was built without a `history=` argument and the cockpit's
`TextArea` got the bare `InMemoryHistory` prompt_toolkit hands every widget,
which starts empty and dies with the process.

That matters more than the shortcuts around it. Every readline binding the
audit went looking for — Ctrl+A/E/K/U/W/Y, Alt+B/F, Ctrl+_ and Ctrl+R — is
already loaded and live in prompt_toolkit's defaults. `Ctrl+R` was never
missing; it was searching an empty list.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.core.ouroboros.battle_test.input_history import (
    history_enabled, history_path, reset_for_tests, shared_history,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_INPUT_HISTORY_PATH", str(tmp_path / "hist"))
    reset_for_tests()
    yield
    reset_for_tests()


# --------------------------------------------------------------------------
# the foundation the shortcuts stand on
# --------------------------------------------------------------------------

def test_the_readline_cluster_was_ALREADY_shipped() -> None:
    """Documents why this PR is about history and not bindings: rebuilding
    keys prompt_toolkit already provides would be pure duplication."""
    from prompt_toolkit.key_binding.defaults import load_key_bindings

    combos = {tuple(str(k) for k in b.keys)
              for b in load_key_bindings().bindings}
    for combo in [("Keys.ControlA",), ("Keys.ControlE",), ("Keys.ControlK",),
                  ("Keys.ControlU",), ("Keys.ControlW",), ("Keys.ControlY",),
                  ("Keys.Escape", "b"), ("Keys.Escape", "f"),
                  ("Keys.ControlUnderscore",), ("Keys.ControlR",)]:
        assert combo in combos, f"{combo} is NOT a prompt_toolkit default"


@pytest.mark.asyncio
async def test_up_recalls_what_was_typed() -> None:
    """The whole point. Loaded ASYNCHRONOUSLY by prompt_toolkit, which is why
    a synchronous probe of this reports an empty buffer and looks broken."""
    from prompt_toolkit.buffer import Buffer

    history = shared_history()
    history.append_string("fix the flaky test")
    history.append_string("run the soak")

    buf = Buffer(history=history, multiline=True)
    buf.load_history_if_not_yet_loaded()
    for _ in range(200):
        await asyncio.sleep(0.005)
        if len(buf._working_lines) > 1:
            break

    buf.text, buf.cursor_position = "", 0
    buf.auto_up()
    assert buf.text == "run the soak"
    buf.auto_up()
    assert buf.text == "fix the flaky test"


@pytest.mark.asyncio
async def test_up_moves_the_CURSOR_inside_a_paragraph() -> None:
    """The prompt is multi-line now. `Up` mid-paragraph must move the cursor,
    or editing a pasted block is impossible — `auto_up` encodes that, and
    binding `history_backward` instead would yank a half-composed paragraph
    away mid-edit."""
    from prompt_toolkit.buffer import Buffer

    history = shared_history()
    history.append_string("an older goal")
    buf = Buffer(history=history, multiline=True)
    buf.load_history_if_not_yet_loaded()
    for _ in range(200):
        await asyncio.sleep(0.005)
        if len(buf._working_lines) > 1:
            break

    buf.text = "line one\nline two"
    buf.cursor_position = len(buf.text)
    buf.auto_up()
    assert buf.text == "line one\nline two", "history stole a live paragraph"
    assert buf.cursor_position < len("line one\nline two")


# --------------------------------------------------------------------------
# one file, both surfaces
# --------------------------------------------------------------------------

def test_both_surfaces_share_ONE_history_object() -> None:
    """A goal typed in the cockpit must be recallable from the fallback and
    the reverse; this codebase has found the two-surface split bug before."""
    assert shared_history() is shared_history()


def test_both_surfaces_ASK_for_it() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        src = (repo / path).read_text()
        names = {a.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "shared_history" in names, f"{path} has no history"


def test_history_is_passed_at_CONSTRUCTION() -> None:
    """A Buffer builds its working lines from the history it was GIVEN when
    created. Assigning afterwards swaps the object without repopulating what
    Up walks — present, correct, and unreachable."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "backend/core/ouroboros/battle_test/"
           "bipartite_layout.py").read_text()
    assert "history=_shared_history()" in src
    assert "prompt.buffer.history =" not in src


# --------------------------------------------------------------------------
# what is stored
# --------------------------------------------------------------------------

def test_blanks_and_repeats_are_refused() -> None:
    """A history full of one repeated command is one you stop pressing Up
    on."""
    h = shared_history()
    h.append_string("run the soak")
    h.append_string("run the soak")
    h.append_string("   ")
    h.append_string("")
    assert list(h.get_strings()) == ["run the soak"]


def test_the_file_is_owner_only() -> None:
    """It is a verbatim record of the operator's typing."""
    shared_history().append_string("something")
    assert oct(os.stat(history_path()).st_mode & 0o777) == "0o600"


def test_it_survives_a_new_session() -> None:
    """Asserted on the FILE, not on `get_strings()` — prompt_toolkit loads a
    FileHistory asynchronously, so a fresh instance reports empty until a
    loop drains it. Checking the in-memory list here would test the loader's
    laziness rather than persistence."""
    shared_history().append_string("yesterday's goal")
    reset_for_tests()
    assert "yesterday's goal" in history_path().read_text()


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_INPUT_HISTORY_ENABLED", "0")
    reset_for_tests()
    assert history_enabled() is False
    assert shared_history() is None


def test_an_unwritable_path_degrades_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cockpit that will not start because it could not open a history
    file is worse than one with no history."""
    monkeypatch.setenv("JARVIS_INPUT_HISTORY_PATH", "/proc/nope/hist")
    reset_for_tests()
    assert shared_history() is None

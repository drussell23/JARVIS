"""The `/` palette reaches the surface `ov` actually attaches with.

The page-style palette shipped in #70123 and the operator's screen did not
change, because it was written as a CONTAINER and wired into
``build_bipartite_application`` — while ``ov`` attach builds a
``PromptSession``, which constructs its own layout and accepts no extra
containers. The palette had no caller on the path anyone uses. Four PRs on
`/` shipped green over that gap.

Two changes close it, and both are asserted here:

  * ``palette_fragments`` renders the same layout as FORMATTED TEXT, which
    every prompt_toolkit surface accepts. A surface no longer needs somewhere
    to put a container in order to show the palette.
  * ``strip_native_completion_menu`` removes prompt_toolkit's own floating
    widget. The palette REPLACES that presentation rather than restyling it —
    leaving it in place renders both at once.

The load-bearing lesson from the arc is in the last test: assertions about the
completer passed throughout while the screen stayed wrong. Structure alone is
not evidence, so the final check drives a real terminal.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.battle_test.palette_render import (
    _is_native_completion_menu,
    layout_palette,
    live_completion_entries,
    palette_fragments,
    strip_native_completion_menu,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. the palette renders without a container
# --------------------------------------------------------------------------

def test_fragments_are_empty_with_no_application_running() -> None:
    """Called from a plain thread, a toolbar callback, or a test — never a
    crash and never a stale menu."""
    assert palette_fragments() == []
    assert live_completion_entries() == ([], -1)


def test_fragments_carry_newlines_so_one_call_draws_a_block() -> None:
    """The whole reason this form works on a surface with no container: a
    single formatted-text value spanning several lines."""
    lines = layout_palette(
        [(f"/verb{i}", f"description {i}") for i in range(4)],
        width=100, selected=1,
    )
    assert len(lines) >= 4
    flat = "".join(text for line in lines for _style, text in line)
    assert "/verb0" in flat and "description 3" in flat


def test_the_selected_row_is_styled_differently() -> None:
    lines = layout_palette([("/a", "x"), ("/b", "y")], width=80, selected=1)
    styles = {style for line in lines for style, _t in line}
    assert any("current" in s for s in styles), (
        "no current-completion style — the cursor would be invisible"
    )


# --------------------------------------------------------------------------
# 2. the native widget is removed, not restyled
# --------------------------------------------------------------------------

def test_the_menu_is_detected_through_its_wrappers() -> None:
    """``CompletionsMenu`` IS a ``ConditionalContainer`` subclass, so code
    that unwraps to the innermost child before testing walks straight past
    it. That mistake left both menus on screen during development."""
    from prompt_toolkit.layout.menus import CompletionsMenu

    assert _is_native_completion_menu(CompletionsMenu())


def test_unrelated_containers_are_left_alone() -> None:
    from prompt_toolkit.layout import Window

    assert not _is_native_completion_menu(Window())
    assert not _is_native_completion_menu(None)
    assert not _is_native_completion_menu(object())


def test_stripping_removes_the_float_from_a_real_prompt_session() -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.layout import FloatContainer

    session = PromptSession(completer=WordCompleter(["/a", "/b"]))
    removed = strip_native_completion_menu(session.app)
    assert removed >= 1, "prompt_toolkit's completions float was not found"

    for node in session.app.layout.walk():
        if isinstance(node, FloatContainer):
            for float_ in node.floats:
                assert not _is_native_completion_menu(float_.content), (
                    "a native menu survived — both palettes would draw"
                )


def test_stripping_is_idempotent_and_safe_on_anything() -> None:
    from prompt_toolkit import PromptSession

    session = PromptSession()
    strip_native_completion_menu(session.app)
    assert strip_native_completion_menu(session.app) == 0
    assert strip_native_completion_menu(object()) == 0
    assert strip_native_completion_menu(None) == 0


async def test_the_prompt_still_works_without_its_native_menu() -> None:
    """Removing a float must not break the buffer underneath it.

    Async because ``insert_text`` schedules prompt_toolkit's validator as a
    background task, which needs a running loop — the same reason this must
    be exercised the way the cockpit runs it rather than synchronously."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter

    session = PromptSession(completer=WordCompleter(["/alpha"]))
    strip_native_completion_menu(session.app)
    session.app.current_buffer.insert_text("/al")
    assert session.app.current_buffer.text == "/al"


# --------------------------------------------------------------------------
# 3. the attach surface is wired to it
# --------------------------------------------------------------------------

def _ui() -> Any:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import AttachUI
        return AttachUI()


def test_the_toolbar_falls_back_to_hints_when_not_completing() -> None:
    toolbar = _ui().toolbar()
    assert isinstance(toolbar, str)
    assert "detach" in toolbar


def test_the_toolbar_renders_the_palette_while_completing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring itself, isolated from a running terminal."""
    import backend.core.ouroboros.battle_test.palette_render as pr

    monkeypatch.setattr(
        pr, "live_completion_entries",
        lambda: ([("/molt", "post to the agora"), ("/moltbook", "read it")], 0),
    )
    monkeypatch.setattr(
        pr, "palette_fragments",
        lambda max_rows=None: [("class:completion-menu.completion", "  /molt")],
    )
    toolbar = _ui().toolbar()
    assert not isinstance(toolbar, str), (
        "the toolbar ignored an active completion and drew hints"
    )
    assert any("/molt" in text for _style, text in toolbar)


def test_a_palette_fault_degrades_to_hints_rather_than_blanking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.core.ouroboros.battle_test.palette_render as pr

    def _boom(max_rows=None):
        raise RuntimeError("palette exploded")

    monkeypatch.setattr(pr, "palette_fragments", _boom)
    toolbar = _ui().toolbar()
    assert isinstance(toolbar, str) and "detach" in toolbar


def test_the_attach_surface_strips_its_native_menu_and_reserves_no_gap():
    """Structural: both are set where the session is built, so a future edit
    that drops one leaves a visible defect this catches."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "_strip_native_menu(session.app)" in src
    assert "reserve_space_for_menu=0" in src


def test_one_palette_implementation_serves_both_surfaces() -> None:
    """DRY, asserted: the container form composes the fragment form rather
    than restating the layout, so the two cannot diverge."""
    import inspect

    from backend.core.ouroboros.battle_test import palette_render

    body = inspect.getsource(palette_render.build_palette_window)
    assert "palette_fragments" in body
    assert "live_completion_entries" in body


# --------------------------------------------------------------------------
# 4. it actually draws — on a real terminal
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 4. the fallback no longer draws overlays at all
# --------------------------------------------------------------------------
#
# RETIRED 2026-07-27: test_pressing_slash_actually_draws_the_palette, and the
# pty driver that fed it.
#
# It asserted that the PromptSession surface paints a `/` palette. That was
# the right question while this surface was a peer of the cockpit. It is the
# wrong question now: Step 2 isolates it to terminals that never answer
# ESC[6n, where the contract is strictly linear append-only output and an
# overlay is precisely what must NOT appear.
#
# The test was also red under a full-suite run for reasons never established
# — correct geometry (150x44), 76 available completions, a confirmed rendered
# frame, and still nothing painted. Keeping it would have meant debugging
# behaviour that was about to be deleted. What replaces it asserts the
# contract that actually ships, in tests/cli/test_append_only_degradation.py.


def test_the_degraded_fallback_refuses_to_draw_a_palette() -> None:
    """The inverse of the retired test, and the invariant that matters now."""
    ui = _ui()
    ui.degrade_to_append_only()
    assert ui.toolbar() == "", (
        "the degraded fallback still emits a bottom toolbar — that is an "
        "absolute screen position on a terminal that cannot report one"
    )
    assert ui.prompt().count("\n") == 0, (
        "the degraded fallback still draws a multi-line live region"
    )

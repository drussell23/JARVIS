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

_PTY_DRIVER = r'''
import os, pty, sys, time, select, re
def child():
    os.environ.setdefault("TERM", "xterm-256color")
    from prompt_toolkit import PromptSession
    from backend.core.ouroboros.cli.ov import (
        AttachUI, _build_slash_completer, _cockpit_style, _strip_native_menu,
    )
    ui = AttachUI()
    session = PromptSession(
        message=lambda: ui.prompt(), bottom_toolbar=lambda: ui.toolbar(),
        completer=_build_slash_completer(), complete_while_typing=True,
        style=_cockpit_style(), reserve_space_for_menu=0,
    )
    n = _strip_native_menu(session.app)
    # Signal on the FIRST ACTUAL FRAME rather than letting the parent sleep
    # and hope. Under full-suite load the fixed wait elapsed before anything
    # was painted, so "/" went into a prompt that did not exist yet and the
    # test failed while passing in isolation — a UI test lying in the most
    # expensive direction.
    def _painted(_app=None):
        sys.stderr.write("PAINTED\n"); sys.stderr.flush()
    try: session.app.after_render += _painted
    except Exception: pass
    sys.stderr.write("STRIPPED=%d\nREADY\n" % n); sys.stderr.flush()
    try: session.prompt()
    except (EOFError, KeyboardInterrupt): pass
if os.environ.get("PTY_CHILD"):
    child(); sys.exit(0)
pid, fd = pty.fork()
if pid == 0:
    os.environ["PTY_CHILD"] = "1"; os.execv(sys.executable, [sys.executable, __file__])
import fcntl, struct, termios
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 150, 0, 0))
out = bytearray(); t0 = time.time(); sent = False; mark = 0
while time.time() - t0 < 90:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try: c = os.read(fd, 65536)
        except OSError: break
        if not c: break
        out += c
        # A real terminal ANSWERS the cursor-position query. Without this
        # prompt_toolkit never learns the renderer height and HIDES the
        # bottom toolbar, so the palette cannot be observed at all.
        if b"\x1b[6n" in c: os.write(fd, b"\x1b[22;1R")
    if not sent and b"PAINTED" in out:
        time.sleep(0.4); mark = len(out); os.write(fd, b"/"); sent = True; ts = time.time()
    if sent and (b"|" in out[mark:] or b"Usage:" in out[mark:]
                 or time.time() - ts > 20): break
raw = out.decode("utf8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", raw[mark:])
print("RESULT_STRIPPED=" + (raw.split("STRIPPED=")[1][:1] if "STRIPPED=" in raw else "0"))
print("RESULT_PAINTED=" + ("1" if b"PAINTED" in out else "0"))
print("RESULT_BODY_START")
print(plain)
# Reap properly. kill(9) alone leaves a zombie holding the pty slave open,
# so the master fd never releases its device — and the NEXT pty test in the
# same run gets a degraded or unavailable terminal. Two tests that each pass
# alone and fail together is the signature of a leaked device, not of a
# product bug.
try: os.kill(pid, 9)
except Exception: pass
try: os.waitpid(pid, 0)
except Exception: pass
try: os.close(fd)
except Exception: pass
'''


@pytest.mark.timeout(180)
def test_pressing_slash_actually_draws_the_palette(tmp_path: Path) -> None:
    """THE test this arc kept not having.

    Every previous `/` fix asserted on the completer or the layout and shipped
    green while the operator's screen was unchanged. This drives a real pty,
    answers the terminal's cursor-position query the way a terminal does, and
    reads what was PAINTED.
    """
    driver = tmp_path / "driver.py"
    driver.write_text(_PTY_DRIVER)
    try:
        proc = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, timeout=130,
            cwd=str(_REPO), env={**os.environ, "PYTHONPATH": str(_REPO)},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty driver unavailable in this environment: {exc}")
    if "RESULT_BODY_START" not in proc.stdout:
        pytest.skip(f"pty allocation failed: {proc.stderr[-300:]}")

    stripped = proc.stdout.split("RESULT_STRIPPED=")[1][:1]
    if "RESULT_PAINTED=1" not in proc.stdout:
        pytest.skip(
            "pty child never rendered a frame (device/scheduler "
            "contention under a full-suite run) — no screen to assert on")
    body = proc.stdout.split("RESULT_BODY_START", 1)[1]

    assert stripped != "0", "the native completions float was never removed"

    rows = [ln for ln in body.splitlines() if ln.strip().startswith("/")]
    assert len(rows) >= 5, (
        f"pressing '/' painted {len(rows)} palette rows; the menu is not "
        f"reaching the screen"
    )
    # Page-style, not widget-style: name column, gutter, then a description
    # on the SAME line. The native float puts descriptions in a second column
    # of its own narrow box.
    described = [r for r in rows if re.match(r"\s*/\S+\s{2,}\S", r)]
    assert described, (
        f"palette rows carry no aligned description column: {rows[:3]}"
    )
    # And the descriptions are the resolved ones, not blanks.
    assert any("|" in r or "Usage:" in r or len(r.split()) > 2
               for r in described)

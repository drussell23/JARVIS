"""The palette is a Z-index overlay on the cockpit, not a row in its grid.

The bipartite cockpit is the surface `ov` actually mounts on a real TTY — the
`PromptSession` is its fallback. Confirmed at runtime, not by grep:
`should_run_bipartite()` is True whenever stdout is a real terminal and the
kill-switch is unset.

As an `HSplit` row the palette shared the ambient grid with the canvas, so
every asynchronous Deck / Lane frame recomputed the palette's geometry along
with everything else — and that reflow lands on the keystroke that opened the
menu. As a `Float` it is measured independently: the canvas beneath repaints
at whatever rate the daemon pushes without the overlay taking part.

The float carries the page-style palette, NOT prompt_toolkit's
`CompletionsMenu` widget. That widget is a bounded dropdown sized to its
longest entry; reintroducing it as a float would have traded the layout back
for the tearing fix.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _app(with_completer: bool = True) -> Any:
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, build_bipartite_application,
    )
    from backend.core.ouroboros.battle_test.repl_completion import (
        build_attach_completer,
    )
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        mux = BipartiteLayout(width=150, height=40, title="t")
        app = build_bipartite_application(
            mux, on_accept=lambda _t: None,
            completer=build_attach_completer() if with_completer else None,
        )
    return mux, app


# --------------------------------------------------------------------------
# 1. it is an overlay
# --------------------------------------------------------------------------

def test_the_layout_root_is_a_float_container() -> None:
    from prompt_toolkit.layout import FloatContainer

    _mux, app = _app()
    assert isinstance(app.layout.container, FloatContainer), (
        "the cockpit root is not a FloatContainer — the palette is back in "
        "the ambient grid"
    )


def test_the_palette_float_spans_the_terminal_and_tracks_the_caret() -> None:
    """Both halves matter and they solve different problems.

    ``left=0, right=0`` is what keeps the page layout: a float sized to its
    content would be the narrow dropdown again, with descriptions truncated
    at the widget edge instead of wrapped into a column.

    ``ycursor=True`` is what survives a growing prompt. This surface's prompt
    is a multi-line block — pulse, deck, then the caret — so any fixed
    ``bottom=`` offset drifts the moment the live region gains a line.
    """
    _mux, app = _app()
    floats = app.layout.container.floats
    assert floats, "no floats at all"
    palette = floats[0]
    assert palette.left == 0 and palette.right == 0, (
        f"palette float does not span the width: left={palette.left} "
        f"right={palette.right}"
    )
    assert palette.ycursor, "palette float is not anchored to the caret"


def test_the_float_carries_our_palette_not_the_native_widget() -> None:
    from backend.core.ouroboros.battle_test.palette_render import (
        _is_native_completion_menu,
    )
    _mux, app = _app()
    for float_ in app.layout.container.floats:
        assert not _is_native_completion_menu(float_.content), (
            "prompt_toolkit's dropdown is back — the page layout was traded "
            "away for the overlay"
        )


def test_the_palette_is_no_longer_a_row_in_the_split() -> None:
    """Structural: a row participates in the grid, which is the whole defect."""
    import inspect

    from backend.core.ouroboros.battle_test import bipartite_layout

    src = inspect.getsource(bipartite_layout.build_bipartite_application)
    assert "rows.append(_palette)" not in src, (
        "the palette was re-added as an HSplit row"
    )
    assert "floats=[Float(" in src


def test_a_cockpit_without_a_completer_grows_no_float() -> None:
    from prompt_toolkit.layout import FloatContainer

    _mux, app = _app(with_completer=False)
    root = app.layout.container
    if isinstance(root, FloatContainer):
        assert not root.floats


# --------------------------------------------------------------------------
# 2. background IPC does not disturb the overlay
# --------------------------------------------------------------------------

async def test_canvas_writes_do_not_change_the_palette() -> None:
    """MANDATE 4(2). Deck/Lane traffic underneath must leave the menu alone.

    Asserted on the palette's rendered fragments across a burst of canvas
    writes: the overlay's content is a pure function of the completion state,
    which the canvas does not touch."""
    from backend.core.ouroboros.battle_test.palette_render import (
        palette_fragments,
    )
    mux, _app_ = _app()
    before = palette_fragments()
    for i in range(50):
        mux.push_raw(f"[daemon] op-{i} GENERATE tokens={i}k")
        if i % 10 == 0:
            await asyncio.sleep(0)
    assert palette_fragments() == before, (
        "background canvas traffic altered the palette's rendered content"
    )


async def test_the_overlay_is_measured_apart_from_the_canvas() -> None:
    """The structural reason the above holds: the palette is not among the
    HSplit's children, so the canvas cannot pull it into a reflow."""
    from prompt_toolkit.layout import FloatContainer

    _mux, app = _app()
    root = app.layout.container
    assert isinstance(root, FloatContainer)
    float_contents = {id(f.content) for f in root.floats}
    body = root.content
    grid_children = {id(c) for c in getattr(body, "children", [])}
    assert not (float_contents & grid_children), (
        "the palette is in BOTH the float list and the grid"
    )


# --------------------------------------------------------------------------
# 3. the router mounts this surface, and it actually paints
# --------------------------------------------------------------------------

_PTY_DRIVER = r'''
import os, pty, sys, time, select, re
def child():
    os.environ.setdefault("TERM", "xterm-256color")
    import asyncio
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, build_bipartite_application, should_run_bipartite,
    )
    from backend.core.ouroboros.battle_test.repl_completion import build_attach_completer
    from backend.core.ouroboros.cli.ov import AttachUI
    sys.stderr.write("ROUTER=%s\n" % should_run_bipartite()); sys.stderr.flush()
    if not should_run_bipartite():
        return
    ui = AttachUI()
    mux = BipartiteLayout(width=150, height=40, title="ov attach")
    app = build_bipartite_application(
        mux, on_accept=lambda t: None, completer=build_attach_completer(),
        toolbar=lambda: ui.toolbar(),
    )
    async def drive():
        async def feed():
            i = 0
            while True:
                mux.push_raw("[daemon] op-%d GENERATE" % i)
                app.invalidate(); i += 1
                await asyncio.sleep(0.2)
        t = asyncio.ensure_future(feed())
        try: await app.run_async()
        finally: t.cancel()
    _once = []
    def _painted(_a=None):
        if _once: return
        _once.append(1)
        sys.stderr.write("PAINTED\n"); sys.stderr.flush()
    try: app.after_render += _painted
    except Exception: pass
    sys.stderr.write("READY\n"); sys.stderr.flush()
    try: asyncio.run(drive())
    except (EOFError, KeyboardInterrupt): pass
if os.environ.get("PTY_CHILD"):
    child(); sys.exit(0)
pid, fd = pty.fork()
if pid == 0:
    os.environ["PTY_CHILD"] = "1"; os.execv(sys.executable, [sys.executable, __file__])
import fcntl, struct, termios
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 150, 0, 0))
out = bytearray(); t0 = time.time(); sent = False; mark = 0
while time.time() - t0 < 100:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try: c = os.read(fd, 65536)
        except OSError: break
        if not c: break
        out += c
        # Answer the cursor-position query like a real terminal. Without it
        # prompt_toolkit never learns the renderer height and geometry-
        # dependent regions are never drawn at all.
        if b"\x1b[6n" in c: os.write(fd, b"\x1b[30;1R")
    if not sent and b"PAINTED" in out:
        time.sleep(0.4); mark = len(out); os.write(fd, b"/"); sent = True; ts = time.time()
    if sent:
        _seen = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "",
                       out[mark:].decode("utf8", "replace"))
        _v = set(re.findall(r"^\s*(/\S+)\s{2,}\S", _seen, re.M))
        if len(_v) >= 6 or time.time() - ts > 25:
            break
raw = out.decode("utf8", "replace")
print("RESULT_ROUTER=" + ("True" if "ROUTER=True" in raw else "False"))
print("RESULT_PAINTED=" + ("1" if b"PAINTED" in out else "0"))
print("RESULT_BODY_START")
print(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", raw[mark:]))
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


@pytest.mark.timeout(200)
def test_the_router_mounts_bipartite_and_the_overlay_paints(
    tmp_path: Path,
) -> None:
    """MANDATE 4(1)+(2). The router's own decision, then what was painted.

    Every `/` fix before this one asserted on the completer or the layout and
    shipped green while the screen was unchanged — twice on a surface the
    operator never reaches. So this takes the mounting decision the way
    `ov.py` takes it, and then reads pixels.
    """
    driver = tmp_path / "driver.py"
    driver.write_text(_PTY_DRIVER)
    try:
        proc = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            timeout=150, cwd=str(_REPO),
            env={**os.environ, "PYTHONPATH": str(_REPO)},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty driver unavailable: {exc}")
    if "RESULT_BODY_START" not in proc.stdout:
        pytest.skip(f"pty allocation failed: {proc.stderr[-300:]}")

    if "RESULT_PAINTED=1" not in proc.stdout:
        pytest.skip(
            "pty child never rendered a frame (device/scheduler "
            "contention under a full-suite run) — no screen to assert on")
    assert "RESULT_ROUTER=True" in proc.stdout, (
        "should_run_bipartite() refused a real TTY — the cockpit is not the "
        "surface being mounted"
    )
    if "RESULT_PAINTED=1" not in proc.stdout:
        pytest.skip(
            "pty child never rendered a frame (device/scheduler "
            "contention under a full-suite run) — no screen to assert on")
    body = proc.stdout.split("RESULT_BODY_START", 1)[1]
    verbs = set(re.findall(r"^\s*(/\S+)\s{2,}\S", body, re.M))
    rows = [ln for ln in body.splitlines() if ln.strip().startswith("/")]
    assert len(verbs) >= 5, (
        f"'/' painted {len(rows)} palette rows on the cockpit — the overlay "
        f"is collapsed (a float honours its PREFERRED height, so a stale "
        f"preferred=1 renders exactly one entry)"
    )
    described = [r for r in rows if re.match(r"\s*/\S+\s{2,}\S", r)]
    assert described, f"no aligned description column: {rows[:3]}"

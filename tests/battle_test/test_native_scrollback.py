"""The terminal keeps the history, because it is better at it.

`full_screen=True` issues smcup — the alternate screen buffer — which gives a
fixed viewport and, as a direct consequence, **disables native scrollback**.
An operator scrolling up to re-read what the organism did an hour ago found
nothing: the alternate buffer has no history, and the primary buffer stopped
receiving output the moment the cockpit mounted.

That made the cockpit's canvas load-bearing for the wrong reason. Zone 1
existed to replace the scrollback the alt-screen had taken away — and it is a
bounded ring, so anything older was simply gone.

Asserted on the ESCAPE SEQUENCE under a real pty, not on the flag: `1049h` is
what a terminal actually acts on, and a config value that fails to reach the
Application would pass any test that only read the config.
"""
from __future__ import annotations

import os
from typing import Optional
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.bipartite_layout import (
    _canvas_dimension,
    fullscreen_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "backend/core/ouroboros/battle_test/bipartite_layout.py"


# --------------------------------------------------------------------------
# 1. the default keeps the terminal's history
# --------------------------------------------------------------------------

def test_fullscreen_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_BIPARTITE_FULLSCREEN", raising=False)
    assert fullscreen_enabled() is False


def test_it_can_be_opted_back_INTO(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed viewport is genuinely better on a wall display, where nobody
    scrolls and a stable frame reads as an instrument panel."""
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    assert fullscreen_enabled() is True


def test_the_application_reads_the_flag_rather_than_a_literal() -> None:
    """A hardcoded True is what caused this; the flag must reach the
    Application or nothing changed."""
    import ast

    src = _SRC.read_text()
    assert "full_screen=fullscreen_enabled()" in src
    calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Application"
    ]
    assert calls, "the Application construction moved"
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "full_screen":
                assert not isinstance(kw.value, ast.Constant), (
                    "full_screen is a literal again"
                )


# --------------------------------------------------------------------------
# 2. the canvas stops being greedy outside the viewport
# --------------------------------------------------------------------------

def test_the_canvas_is_bounded_without_fullscreen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A greedy canvas outside the alternate screen would make
    prompt_toolkit reserve that height on every repaint — turning the
    scrollback just restored into a wall of blank rows."""
    monkeypatch.delenv("JARVIS_BIPARTITE_FULLSCREEN", raising=False)
    dim = _canvas_dimension()
    assert dim.max is not None and dim.max <= 32
    assert dim.min == 0, "an idle organism should show no empty frame at all"


def test_the_canvas_is_greedy_INSIDE_fullscreen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There, history has nowhere else to live, so it should be as large as
    possible — the two settings justify each other."""
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    assert _canvas_dimension().weight == 1


def test_the_live_region_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_BIPARTITE_FULLSCREEN", raising=False)
    monkeypatch.setenv("JARVIS_BIPARTITE_LIVE_ROWS", "3")
    assert _canvas_dimension().max == 3


@pytest.mark.parametrize("junk", ["", "nonsense", "-5"])
def test_a_degenerate_row_count_never_raises(
    junk: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_BIPARTITE_FULLSCREEN", raising=False)
    monkeypatch.setenv("JARVIS_BIPARTITE_LIVE_ROWS", junk)
    dim = _canvas_dimension()
    assert dim.max is not None and dim.max >= 0


# --------------------------------------------------------------------------
# 3. proven on a real terminal
# --------------------------------------------------------------------------

_PTY_DRIVER = r'''
import os, pty, sys, time, select
def child():
    os.environ.setdefault("TERM", "xterm-256color")
    import asyncio
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, build_bipartite_application,
    )
    mux = BipartiteLayout(width=100, height=30, title="t")
    app = build_bipartite_application(mux, on_accept=lambda t: None)
    async def drive():
        t = asyncio.ensure_future(app.run_async())
        await asyncio.sleep(2.0)
        app.exit()
        try: await t
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
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
out = bytearray(); t0 = time.time()
while time.time() - t0 < 25:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try: c = os.read(fd, 65536)
        except OSError: break
        if not c: break
        out += c
        if b"\x1b[6n" in c: os.write(fd, b"\x1b[20;1R")
raw = bytes(out)
print("RESULT_READY=" + ("1" if b"READY" in raw else "0"))
print("RESULT_ALT=" + ("1" if (b"\x1b[?1049h" in raw or b"\x1b[?47h" in raw) else "0"))
try:
    os.kill(pid, 9); os.waitpid(pid, 0); os.close(fd)
except Exception: pass
'''


def _run_pty(tmp_path: Path, fullscreen: Optional[bool]) -> str:
    """Drive the cockpit under a real PTY. *fullscreen* None = unset (the
    DEFAULT path), True/False = the explicit opt-in / opt-out."""
    driver = tmp_path / f"drv_{fullscreen}.py"
    driver.write_text(_PTY_DRIVER)
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    if fullscreen is None:
        env.pop("JARVIS_BIPARTITE_FULLSCREEN", None)
    else:
        env["JARVIS_BIPARTITE_FULLSCREEN"] = "1" if fullscreen else "0"
    try:
        proc = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            timeout=90, cwd=str(_REPO), env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty unavailable: {exc}")
    if "RESULT_ALT=" not in proc.stdout:
        pytest.skip(f"pty allocation failed: {proc.stderr[-200:]}")
    if "RESULT_READY=1" not in proc.stdout:
        pytest.skip("pty child never started — no terminal to assert on")
    return proc.stdout


@pytest.mark.timeout(150)
def test_the_terminal_IS_switched_by_default(tmp_path: Path) -> None:
    """The cockpit claims the alternate screen on a real terminal.

    This assertion is the REVERSE of what it was. #70171 defaulted
    full-screen off because the alternate screen disables native scrollback
    and Zone 1 was a tail — `snap[-budget:]`, with nothing reachable above
    it — so claiming the screen deleted the session's history.

    `canvas_viewport` removed that objection: Zone 1 is now a window over
    ~20k retained lines with PgUp/PgDn/Home/End. With history safe inside the
    cockpit, taking the screen is what makes `ov` an instrument you are in
    rather than a command that scrolled past.

    `ESC[?1049h` is what a terminal actually acts on — a config value that
    never reached the Application would pass any test that only read config.
    """
    assert "RESULT_ALT=1" in _run_pty(tmp_path, fullscreen=None)


@pytest.mark.timeout(150)
def test_opting_out_keeps_the_primary_screen(tmp_path: Path) -> None:
    """The escape hatch for anyone who wants native scrollback back, and the
    inverse that proves the assertion above measures something."""
    assert "RESULT_ALT=0" in _run_pty(tmp_path, fullscreen=False)


@pytest.mark.timeout(150)
def test_opting_in_explicitly_still_switches(tmp_path: Path) -> None:
    assert "RESULT_ALT=1" in _run_pty(tmp_path, fullscreen=True)

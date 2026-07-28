"""The crest is painted where it cannot be scrolled back to.

A terminal keeps two screen buffers. The normal one accumulates scrollback;
the **alternate screen** does not — it is a fixed viewport a full-screen
program borrows and hands back. That is why you cannot scroll out of `vim`.

The cockpit already asked for it (prompt_toolkit's `full_screen=True`). It
asked at the END: the crest, the wake logs and the attach summary were all
printed to the NORMAL buffer first, so the logo sat in the scrollback behind
the cockpit and scrolling up found it — the one thing a full-screen takeover
exists to prevent.

Fixing that is a question of ORDER, not of drawing, so these assert on the
byte sequence a real terminal receives.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.ui.alt_screen import (
    alt_screen_enabled, alternate_screen, in_alternate_screen,
)

_REPO = Path(__file__).resolve().parents[2]
_ENTER = "\x1b[?1049h"
_LEAVE = "\x1b[?1049l"


class _Fake:
    """Stands in for the real stdout so the sequences can be read back."""

    closed = False

    def __init__(self) -> None:
        self.written = ""

    def write(self, text: str) -> None:
        self.written += text

    def flush(self) -> None:
        pass


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    fake = _Fake()
    monkeypatch.setattr(sys, "__stdout__", fake)
    return fake


# --------------------------------------------------------------------------
# the sequences
# --------------------------------------------------------------------------

def test_it_enters_and_leaves(captured: _Fake) -> None:
    with alternate_screen(enabled=True) as entered:
        assert entered is True
        assert captured.written == _ENTER + "\x1b[H"
    assert captured.written.endswith(_LEAVE)


def test_the_cursor_is_homed_on_entry(captured: _Fake) -> None:
    """Some terminals keep what a previous occupant left in the alternate
    buffer, so a boot that starts drawing at the old cursor position renders
    halfway down the screen."""
    with alternate_screen(enabled=True):
        assert captured.written.endswith("\x1b[H")


def test_leaving_does_NOT_clear_first(captured: _Fake) -> None:
    """The normal buffer is handed back exactly as it was found. ED-3
    (`ESC[3J`) would hide the logo too — by destroying the operator's entire
    terminal history, including everything before they ran `ov`."""
    with alternate_screen(enabled=True):
        pass
    assert "\x1b[3J" not in captured.written
    assert "\x1b[2J" not in captured.written


def test_disabled_touches_the_terminal_not_at_all(captured: _Fake) -> None:
    with alternate_screen(enabled=False) as entered:
        assert entered is False
    assert captured.written == ""


# --------------------------------------------------------------------------
# nesting — prompt_toolkit issues its own smcup inside this
# --------------------------------------------------------------------------

def test_nesting_enters_once_and_leaves_once(captured: _Fake) -> None:
    """An inner block must not drop the operator to their shell halfway
    through a boot."""
    with alternate_screen(enabled=True):
        with alternate_screen(enabled=True):
            assert in_alternate_screen() is True
        assert captured.written.count(_LEAVE) == 0, "inner exit handed it back"
        assert in_alternate_screen() is True
    assert captured.written.count(_ENTER) == 1
    assert captured.written.count(_LEAVE) == 1
    assert in_alternate_screen() is False


# --------------------------------------------------------------------------
# it must never be the reason a boot fails
# --------------------------------------------------------------------------

def test_an_exception_inside_still_hands_the_terminal_back(
    captured: _Fake,
) -> None:
    """A process that enters and dies without leaving hands back a terminal
    stuck in a fixed viewport with no scrollback. That is a wrecked shell."""
    with pytest.raises(RuntimeError):
        with alternate_screen(enabled=True):
            raise RuntimeError("boom")
    assert captured.written.endswith(_LEAVE)
    assert in_alternate_screen() is False


def test_a_dead_stream_does_not_break_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing to borrow the screen means the boot looks like it used to,
    which is far better than not booting."""
    class _Closed(_Fake):
        closed = True

    monkeypatch.setattr(sys, "__stdout__", _Closed())
    with alternate_screen(enabled=True) as entered:
        assert entered is False
    assert in_alternate_screen() is False


def test_it_writes_to_the_REAL_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sys.stdout` may be a patch_stdout proxy or a Rich capture by the time
    a restore runs, and a restore written into a proxy leaves the terminal
    wrecked."""
    real, proxy = _Fake(), _Fake()
    monkeypatch.setattr(sys, "__stdout__", real)
    monkeypatch.setattr(sys, "stdout", proxy)
    with alternate_screen(enabled=True):
        pass
    assert _ENTER in real.written
    assert proxy.written == ""


def test_it_defers_to_the_cockpits_own_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One question asked once. A boot that hides the crest and then mounts
    an inline cockpit would leave the operator staring at a blank shell."""
    monkeypatch.delenv("JARVIS_ALT_SCREEN_BOOT", raising=False)
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "0")
    assert alt_screen_enabled() is False
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    assert alt_screen_enabled() is True


def test_the_boot_can_opt_out_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    monkeypatch.setenv("JARVIS_ALT_SCREEN_BOOT", "0")
    assert alt_screen_enabled() is False


# --------------------------------------------------------------------------
# what a real terminal receives
# --------------------------------------------------------------------------

_DRIVER = """
import os, pty, sys, select, time, signal
REPO = "__REPO__"
MODE = sys.argv[1]

def child():
    os.environ["TERM"] = "xterm-256color"
    os.environ["JARVIS_CREST_ANIM_DISABLED"] = "1"
    sys.path.insert(0, REPO)
    from backend.core.ouroboros.ui.alt_screen import alternate_screen
    from backend.core.ouroboros.ui.crest import print_static_crest
    from backend.core.ouroboros.cli.ov import build_console
    console = build_console()
    try:
        with alternate_screen(enabled=True):
            sys.stdout.write("CREST_START\\n"); sys.stdout.flush()
            print_static_crest(console)
            sys.stdout.write("CREST_END\\n"); sys.stdout.flush()
            if MODE == "raise":
                raise RuntimeError("boom")
            if MODE == "signal":
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)
    except Exception:
        pass
    sys.stdout.flush()
    os._exit(0)

pid, fd = pty.fork()
if pid == 0:
    child()
out = b""
end = time.time() + 30
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.2)
    if r:
        try:
            c = os.read(fd, 1 << 20)
        except OSError:
            break
        if not c:
            break
        out += c
        if b"\\x1b[6n" in c:
            os.write(fd, b"\\x1b[40;1R")
    if os.waitpid(pid, os.WNOHANG)[0]:
        time.sleep(0.3)
        try:
            while True:
                c = os.read(fd, 1 << 20)
                if not c:
                    break
                out += c
        except OSError:
            pass
        break
enter = out.find(b"\\x1b[?1049h")
start = out.find(b"CREST_START")
end_i = out.find(b"CREST_END")
leave = out.find(b"\\x1b[?1049l")
sys.stdout.write("READY=1\\n")
sys.stdout.write("INSIDE=%d\\n" % int(0 <= enter < start < end_i < leave))
sys.stdout.write("RESTORED=%d\\n" % int(leave > 0))
"""


def _pty(tmp_path: Path, mode: str) -> str:
    driver = tmp_path / f"drv_{mode}.py"
    driver.write_text(_DRIVER.replace("__REPO__", str(_REPO)))
    try:
        proc = subprocess.run(
            [sys.executable, str(driver), mode], capture_output=True,
            text=True, timeout=90, cwd=str(_REPO),
            env={**os.environ, "PYTHONPATH": str(_REPO)},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty unavailable: {exc}")
    if "READY=1" not in proc.stdout:
        pytest.skip(f"pty child never started: {proc.stdout[-200:]}")
    return proc.stdout


@pytest.mark.timeout(150)
def test_the_crest_is_painted_INSIDE_the_alternate_screen(
    tmp_path: Path,
) -> None:
    """The whole point, asserted on byte ORDER: smcup must precede the first
    byte of the logo. Reading config would not catch a boot that switched
    one line too late."""
    assert "INSIDE=1" in _pty(tmp_path, "clean")


@pytest.mark.timeout(150)
@pytest.mark.parametrize("mode", ["clean", "raise", "signal"])
def test_the_terminal_is_handed_back_on_every_exit(
    tmp_path: Path, mode: str,
) -> None:
    """Crash and SIGTERM included. SIGKILL stays unrecoverable by design —
    nothing in-process can catch it."""
    assert "RESTORED=1" in _pty(tmp_path, mode)


# --------------------------------------------------------------------------
# and that `ov` itself switches before it draws
# --------------------------------------------------------------------------

def test_the_boot_switches_BEFORE_it_builds_the_animator() -> None:
    """Structural, because "one line too late" is the entire original bug.

    `run_cockpit_thin` must do nothing but enter the alternate screen and
    delegate. Any drawing that creeps above the `with` lands in the normal
    buffer and is scrollable again — which is exactly how the crest ended up
    there in the first place, since the cockpit's own `full_screen=True` was
    always set, just far too late to help.
    """
    import ast

    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "run_cockpit_thin"
    )
    body = [n for n in fn.body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    )]
    assert body, "run_cockpit_thin has no body"
    first = body[0]
    assert isinstance(first, ast.With), (
        "run_cockpit_thin draws something before switching screens"
    )
    assert any(
        isinstance(item.context_expr, ast.Call)
        and getattr(item.context_expr.func, "id", "") == "alternate_screen"
        for item in first.items
    ), "the first statement is not the alternate-screen switch"
    assert len(body) == 1, (
        "run_cockpit_thin does work outside the borrowed screen"
    )


def test_the_crest_is_built_INSIDE_the_delegated_boot() -> None:
    """The inverse: the animator must live in the inner function, so it can
    only run once the switch has happened."""
    import ast

    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    inner = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef)
        and n.name == "_run_cockpit_thin_inner"
    )
    names = {
        getattr(n.func, "id", "") for n in ast.walk(inner)
        if isinstance(n, ast.Call)
    }
    assert "build_animator" in names

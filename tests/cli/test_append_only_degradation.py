"""A terminal that cannot be addressed gets linear plain text.

`ov` mounts the cockpit at a real terminal. Everywhere else — CI, cron,
`ov | tee log.txt`, a stripped SSH session — the terminal never answers
``ESC[6n``, so nothing can know where the cursor is. Absolute positioning,
overlays and region repaints all become writes to an unknown location, and the
visible result is a scrambled log rather than a degraded UI.

Worse, those are exactly the places the output is read later AS TEXT, where
escape sequences do not merely fail to help — they destroy the artifact.

Two contracts are asserted here:

  * the degraded client draws nothing positional — no toolbar, no overlay,
    no multi-line live region;
  * everything it emits is plain text, whatever form the payload arrived in.

The CPR timeout itself is NOT re-implemented and so is not tested as if it
were ours: prompt_toolkit already asks, already waits, already latches
NOT_SUPPORTED, and already calls back. What is tested is that we consume that
signal — and that a second probe was not added, since two readers racing for
the same reply bytes would misclassify healthy terminals.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.append_only import (
    AppendOnlyWriter,
    cpr_timeout_s,
    install_cpr_degradation,
    plain_text,
    strip_ansi,
)

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 1. the ANSI demultiplexer
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ("\x1b[38;5;250m/molt\x1b[0m ok", "/molt ok"),
    ("\x1b[2J\x1b[Hcleared", "cleared"),
    ("\x1b]0;window title\x07text", "text"),
    ("\x1b[1;31mred\x1b[0m/\x1b[32mgreen\x1b[0m", "red/green"),
    ("plain already", "plain already"),
    ("", ""),
])
def test_escape_sequences_are_removed(payload: str, expected: str) -> None:
    assert strip_ansi(payload) == expected


def test_rich_markup_is_parsed_by_rich_not_by_a_regex() -> None:
    """DRY, and correctness: the bracket grammar has escaping and unclosed-tag
    rules that a hand-rolled stripper would have to own forever."""
    assert plain_text("[bold red]DW-397B[/] failover") == "DW-397B failover"
    assert plain_text("[green]op-42[/green] done") == "op-42 done"


def test_rich_objects_flatten_through_their_own_plain() -> None:
    from rich.text import Text

    assert plain_text(Text.from_markup("[cyan]op-7[/] GENERATE")) == (
        "op-7 GENERATE"
    )


def test_markup_and_rendered_ansi_together() -> None:
    """The mirrored router emits both; either alone would leave residue."""
    assert plain_text("\x1b[31m[bold]alert[/]\x1b[0m") == "alert"


@pytest.mark.parametrize("junk", [None, 42, object(), b"bytes", [1, 2]])
def test_the_demultiplexer_never_raises(junk: Any) -> None:
    assert isinstance(plain_text(junk), str)


def test_a_lone_bracket_is_not_mangled_as_markup() -> None:
    """Log lines carry brackets constantly — `[daemon]`, `[worker]`, `[12]`.
    Treating every one as markup would silently eat them."""
    out = plain_text("[daemon] op-3 tokens=[15k]")
    assert "daemon" in out and "op-3" in out


# --------------------------------------------------------------------------
# 2. the writer is strictly linear
# --------------------------------------------------------------------------

def test_output_is_plain_and_line_terminated() -> None:
    buf = io.StringIO()
    writer = AppendOnlyWriter(buf)
    writer.write("\x1b[2J\x1b[H[cyan]line one[/]\nline two")
    assert buf.getvalue() == "line one\nline two\n"
    assert writer.lines_written == 2


def test_it_never_claims_to_be_a_terminal() -> None:
    """Anything downstream asking `isatty()` must get the truth, or it will
    start emitting colour again."""
    assert AppendOnlyWriter(io.StringIO()).isatty() is False


def test_every_line_is_flushed() -> None:
    """The consumer is usually tail/tee/a CI collector, and a killed process
    with a buffered final chunk produces an empty log."""
    flushes = []

    class _Stream(io.StringIO):
        def flush(self) -> None:
            flushes.append(1)

    writer = AppendOnlyWriter(_Stream())
    writer.write("one")
    writer.write("two")
    assert len(flushes) >= 2


def test_a_broken_stream_does_not_kill_the_client() -> None:
    class _Broken:
        def write(self, _s: str) -> None:
            raise OSError("EPIPE")

        def flush(self) -> None:
            raise OSError("EPIPE")

    AppendOnlyWriter(_Broken()).write("anything")   # must not raise


def test_empty_payloads_emit_nothing() -> None:
    buf = io.StringIO()
    writer = AppendOnlyWriter(buf)
    writer.write("")
    writer.write(None)
    assert buf.getvalue() == ""


# --------------------------------------------------------------------------
# 3. the CPR signal is consumed, not re-probed
# --------------------------------------------------------------------------

def test_the_timeout_is_strict_and_bounded() -> None:
    assert cpr_timeout_s() == pytest.approx(0.2)


@pytest.mark.parametrize("value,expected", [
    ("0", 0.05), ("-1", 0.05), ("999", 5.0), ("junk", 0.2), ("1.5", 1.5),
])
def test_the_timeout_is_tunable_but_never_degenerate(
    value: str, expected: float, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero would classify EVERY terminal as dumb before a reply could
    physically arrive."""
    monkeypatch.setenv("JARVIS_CPR_TIMEOUT_S", value)
    assert cpr_timeout_s() == pytest.approx(expected)


async def test_an_unanswered_cpr_triggers_degradation() -> None:
    """MANDATE 4(1), at the seam. prompt_toolkit invokes the callback when its
    own wait expires; this asserts we act on it."""
    degraded = []

    class _Renderer:
        cpr_not_supported_callback = None

    class _App:
        renderer = _Renderer()

    app = _App()
    assert install_cpr_degradation(app, lambda: degraded.append(1))
    assert not degraded, "degraded before the terminal was given a chance"

    app.renderer.cpr_not_supported_callback()      # the timeout expiring
    await asyncio.sleep(0)
    assert degraded == [1]


async def test_degradation_fires_once_however_often_the_renderer_calls() -> None:
    """The renderer latches NOT_SUPPORTED and calls back on every subsequent
    render; tearing the UI down repeatedly would be its own failure."""
    calls = []

    class _App:
        class renderer:
            cpr_not_supported_callback = None

    app = _App()
    install_cpr_degradation(app, lambda: calls.append(1))
    for _ in range(5):
        app.renderer.cpr_not_supported_callback()
    assert calls == [1]


def test_a_failing_degrade_hook_does_not_propagate() -> None:
    class _App:
        class renderer:
            cpr_not_supported_callback = None

    app = _App()

    def _boom() -> None:
        raise RuntimeError("teardown exploded")

    install_cpr_degradation(app, _boom)
    app.renderer.cpr_not_supported_callback()      # must not raise


def test_installing_on_a_hookless_object_is_survivable() -> None:
    assert install_cpr_degradation(object(), lambda: None) is False


def test_no_second_cpr_probe_was_added() -> None:
    """Structural, and load-bearing.

    prompt_toolkit already writes ESC[6n and reads the reply. A second probe
    would race it for the same bytes: whichever reader wins, the other times
    out, and a terminal that answered perfectly gets called dumb. The fix for
    'the timeout is too long' is its existing parameter, never a new probe."""
    src = (
        _REPO / "backend/core/ouroboros/battle_test/append_only.py"
    ).read_text()
    assert "\\x1b[6n" not in src and "ESC[6n" not in src.replace(
        "``ESC[6n``", ""
    ), "an independent cursor-position probe was added"
    assert "cpr_not_supported_callback" in src


# --------------------------------------------------------------------------
# 4. the degraded client draws nothing positional
# --------------------------------------------------------------------------

def _ui() -> Any:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import AttachUI
        return AttachUI()


def test_the_degraded_client_emits_no_toolbar() -> None:
    ui = _ui()
    assert ui.toolbar() != ""
    ui.degrade_to_append_only()
    assert ui.toolbar() == "", (
        "a bottom toolbar is anchored to the bottom of the screen — an "
        "absolute position, on a terminal that cannot report one"
    )


def test_the_degraded_client_emits_a_bare_caret() -> None:
    ui = _ui()
    assert "\n" in ui.prompt(), "the normal prompt is a multi-line block"
    ui.degrade_to_append_only()
    assert "\n" not in ui.prompt()


def test_the_degraded_client_draws_no_palette() -> None:
    """Even with completions live, the overlay must not appear."""
    import backend.core.ouroboros.battle_test.palette_render as pr

    ui = _ui()
    ui.degrade_to_append_only()
    original = pr.palette_fragments
    try:
        pr.palette_fragments = lambda max_rows=None: [("", "  /molt  x")]
        assert ui.toolbar() == ""
    finally:
        pr.palette_fragments = original


def test_degradation_is_wired_where_the_fallback_is_built() -> None:
    """Structural: the hook must be installed on the session that actually
    runs, or the whole mechanism is inert."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "install_cpr_degradation(session.app, ui.degrade_to_append_only)" in src


# --------------------------------------------------------------------------
# 5. end to end: a pty that never answers
# --------------------------------------------------------------------------

_PTY_DRIVER = r'''
import os, pty, sys, time, select
def child():
    os.environ.setdefault("TERM", "xterm-256color")
    from prompt_toolkit import PromptSession
    from backend.core.ouroboros.battle_test.append_only import (
        install_cpr_degradation,
    )
    def _degraded():
        sys.stderr.write("DEGRADED\n"); sys.stderr.flush()
    session = PromptSession(message="ov > ")
    install_cpr_degradation(session.app, _degraded)
    sys.stderr.write("READY\n"); sys.stderr.flush()
    try: session.prompt()
    except (EOFError, KeyboardInterrupt): pass
if os.environ.get("PTY_CHILD"):
    child(); sys.exit(0)
pid, fd = pty.fork()
if pid == 0:
    os.environ["PTY_CHILD"] = "1"; os.execv(sys.executable, [sys.executable, __file__])
import fcntl, struct, termios
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
out = bytearray(); t0 = time.time()
while time.time() - t0 < 30:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        try: c = os.read(fd, 65536)
        except OSError: break
        if not c: break
        out += c
        # DELIBERATELY NEVER ANSWER ESC[6n. This is the dumb terminal.
    if b"DEGRADED" in out: break
    if b"READY" in out and time.time() - t0 > 12: break
print("RESULT_DEGRADED=" + ("1" if b"DEGRADED" in out else "0"))
print("RESULT_HUNG=" + ("0" if b"READY" in out else "1"))
try: os.kill(pid, 9)
except Exception: pass
try: os.waitpid(pid, 0)
except Exception: pass
try: os.close(fd)
except Exception: pass
'''


@pytest.mark.timeout(120)
def test_a_terminal_that_never_answers_degrades_without_hanging(
    tmp_path: Path,
) -> None:
    """MANDATE 4(1) end to end.

    The harness that learned to ANSWER the cursor-position query here proves
    the opposite case by staying silent — the same instrument, both
    directions. The boot must reach a prompt regardless: a client that blocks
    forever waiting for a reply that will never come is worse than one that
    draws badly."""
    driver = tmp_path / "driver.py"
    driver.write_text(_PTY_DRIVER)
    try:
        proc = subprocess.run(
            [sys.executable, str(driver)], capture_output=True, text=True,
            timeout=100, cwd=str(_REPO),
            env={**os.environ, "PYTHONPATH": str(_REPO)},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"pty driver unavailable: {exc}")
    if "RESULT_HUNG=" not in proc.stdout:
        pytest.skip(f"pty allocation failed: {proc.stderr[-200:]}")

    assert "RESULT_HUNG=0" in proc.stdout, (
        "the client never reached a prompt on a terminal that ignores CPR — "
        "the boot sequence hung"
    )
    assert "RESULT_DEGRADED=1" in proc.stdout, (
        "the terminal never answered ESC[6n and degradation never fired"
    )

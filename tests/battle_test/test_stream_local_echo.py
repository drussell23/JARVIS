"""The generation stream published to everyone except the operator.

You run `ov`, the organism generates, and nothing appears. That has been true
since the streaming renderer shipped, and every layer along the way was
individually correct.

`stream_renderer` skips the Rich `Live` widget whenever a SerpentREPL is
active, because `Live` writes by direct cursor manipulation and would shred
the prompt. Right call. `_mirror_completed_lines` was then written to route
the generation into the cockpit's line-oriented deck instead — its docstring
reaches the same conclusion this arc did independently: "The widget cannot be
the answer; it is the thing that does not fit."

It sends with `publish_markup_global`, which returns False unless
`attached_cockpits() > 0` — a client connected over the BRIDGE.

`ov` boots the harness IN-PROCESS (`ov.py` → `battle_main`), with a
SerpentREPL holding the operator's terminal. There is no bridge client in
that shape. So every line was composed, escaped, offered, and dropped, and
the one surface guaranteed to be watching was the one surface never written
to.

THE CONCEPT ALREADY EXISTED
---------------------------
`cockpit_attach.operator_present()` draws exactly this distinction and
documents it: "a cockpit is ATTACHED over the bridge, or this process owns a
real terminal (a foreground run with its own REPL, where the operator is
looking straight at it)." It was written because Karen kept narrating to an
empty room after the operator went home. This is that defect in mirror image
— text falling silent for the LOCAL operator for the same reason speech did
for the remote one — and the fix is to consult it, not to invent it.

WHY LOCAL ECHO IS SAFE WHERE `Live` WAS NOT
--------------------------------------------
`print_fit` writes through the Rich console, which under prompt_toolkit's
`patch_stdout` is coordinated with the prompt: the line lands in scrollback
ABOVE the input and the prompt redraws below. That is what
`serpent_flow._emit_fit` has always done while the REPL is active. Nothing
here re-enables `Live`, and nothing should.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.core.ouroboros.battle_test.stream_renderer as sr  # noqa: E402


def _renderer(width: int = 100):
    buf = io.StringIO()
    r = sr.StreamRenderer(
        console=Console(file=buf, width=width, force_terminal=True))
    return r, buf


def _mirror(renderer, text: str) -> None:
    renderer._buffer = text
    renderer._mirrored_offset = 0
    renderer._mirror_completed_lines()


@pytest.fixture()
def local_tty():
    """No bridge client; this process owns a real terminal — the exact shape
    `ov` produces when it boots the harness in-process."""
    with patch("backend.core.ouroboros.battle_test.presentation_restraint"
               ".real_stdout_isatty", lambda: True), \
         patch("backend.core.ouroboros.battle_test.cockpit_attach"
               ".publish_markup_global", lambda *a, **k: False):
        yield


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------

def test_the_stream_reaches_a_foreground_terminal(local_tty) -> None:
    """THE regression. No bridge client, real terminal — and until now,
    silence."""
    r, buf = _renderer()
    _mirror(r, "Reading orchestrator.py.\nThe gate fires early.\n")
    out = buf.getvalue()
    assert "Reading orchestrator.py" in out
    assert "The gate fires early" in out


def test_it_uses_the_decks_own_grammar(local_tty) -> None:
    """One glyph opens the block, continuations indent under it — the same
    shape assistant prose already has in the deck. A second grammar for the
    same content would read as a different kind of event."""
    r, buf = _renderer()
    _mirror(r, "first line\nsecond line\nthird line\n")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines[0].lstrip().startswith("⏺")
    assert all(not ln.lstrip().startswith("⏺") for ln in lines[1:])


def test_only_complete_lines_are_emitted(local_tty) -> None:
    """A partial trailing line must be held. Emitting it would print a
    fragment that the next flush prints again."""
    r, buf = _renderer()
    r._buffer = "complete line\npartial without newline"
    r._mirrored_offset = 0
    r._mirror_completed_lines()
    out = buf.getvalue()
    assert "complete line" in out
    assert "partial without newline" not in out


def test_the_held_remainder_is_flushed_at_the_end(local_tty) -> None:
    """`final=True` is what closes the stream, and a dropped last line is the
    one the operator most wants."""
    r, buf = _renderer()
    r._buffer = "complete\ntrailing fragment"
    r._mirrored_offset = 0
    r._mirror_completed_lines()
    r._mirror_completed_lines(final=True)
    assert "trailing fragment" in buf.getvalue()


# ---------------------------------------------------------------------------
# not both — a line must never print twice
# ---------------------------------------------------------------------------

def test_an_attached_bridge_wins_and_suppresses_the_local_echo() -> None:
    """A foreground run that ALSO has an attached client would otherwise
    print every line twice on the same terminal."""
    sent = []
    r, buf = _renderer()
    with patch("backend.core.ouroboros.battle_test.cockpit_attach"
               ".publish_markup_global", lambda t, **k: sent.append(t) or True), \
         patch("backend.core.ouroboros.battle_test.presentation_restraint"
               ".real_stdout_isatty", lambda: True):
        _mirror(r, "one line\n")
    assert sent and "one line" in sent[0]
    assert buf.getvalue() == "", "line printed to the bridge AND locally"


def test_a_detached_daemon_stays_silent() -> None:
    """No bridge client and no terminal: nobody can perceive this. Writing
    anyway is how a background daemon fills a log with prose."""
    r, buf = _renderer()
    with patch("backend.core.ouroboros.battle_test.cockpit_attach"
               ".publish_markup_global", lambda *a, **k: False), \
         patch("backend.core.ouroboros.battle_test.presentation_restraint"
               ".real_stdout_isatty", lambda: False):
        _mirror(r, "unheard\n")
    assert buf.getvalue() == ""


# ---------------------------------------------------------------------------
# the trap this arc has already been bitten by
# ---------------------------------------------------------------------------

def test_the_tty_check_reads_the_real_stdout_not_the_proxy() -> None:
    """Load-bearing, and the one mode this fix exists for.

    Under `patch_stdout(raw=True)` — which is exactly when a SerpentREPL is
    active — `sys.stdout` is a prompt_toolkit proxy whose `isatty()` returns
    False. `real_stdout_isatty` reads `sys.__stdout__` instead. Testing the
    proxy is the bug that made `should_render()` blind in the
    presentation-restraint arc, and it would make this echo dead in precisely
    the mode it was written for.
    """
    import inspect
    src = inspect.getsource(sr.StreamRenderer._emit_deck_line)
    assert "real_stdout_isatty" in src
    assert "sys.stdout.isatty" not in src


def test_live_is_not_re_enabled_under_a_repl() -> None:
    """The widget is still the thing that does not fit. This change adds a
    sink; it must not resurrect the renderer that clobbers the prompt."""
    import inspect
    src = inspect.getsource(sr.StreamRenderer.start)
    assert "_repl_active" in src
    assert "self._live = None" in src


# ---------------------------------------------------------------------------
# resilience — this runs on the token hot path
# ---------------------------------------------------------------------------

def test_untrusted_model_output_is_escaped(local_tty) -> None:
    """The deck channel is styled chrome around inert data. Model output that
    contains markup must not be interpreted as it."""
    r, buf = _renderer()
    _mirror(r, "[bold red]not a style[/bold red]\n")
    assert "not a style" in buf.getvalue()


def test_a_very_long_line_is_bounded(local_tty) -> None:
    """One pathological line must not scroll the operator's screen away."""
    r, buf = _renderer(width=80)
    _mirror(r, "x" * 20000 + "\n")
    assert max(len(ln) for ln in buf.getvalue().splitlines()) <= 200


def test_blank_lines_do_not_open_an_empty_block(local_tty) -> None:
    r, buf = _renderer()
    _mirror(r, "\n\n\n")
    assert buf.getvalue().strip() == ""


def test_a_broken_console_never_breaks_the_stream(local_tty) -> None:
    """`_mirror_completed_lines` is called from the consumer task on the
    token hot path. A render fault must cost a line, never the generation."""
    class Exploding:
        def print(self, *a, **k):
            raise RuntimeError("terminal on fire")
        @property
        def width(self):
            raise RuntimeError("terminal on fire")

    r = sr.StreamRenderer(console=Exploding())
    _mirror(r, "still fine\n")  # must not raise


def test_local_echo_can_be_switched_off(monkeypatch) -> None:
    """Reverts to the previous behaviour exactly: bridge-only."""
    monkeypatch.setenv("JARVIS_STREAM_LOCAL_ECHO_ENABLED", "0")
    assert sr.local_echo_enabled() is False
    r, buf = _renderer()
    with patch("backend.core.ouroboros.battle_test.cockpit_attach"
               ".publish_markup_global", lambda *a, **k: False), \
         patch("backend.core.ouroboros.battle_test.presentation_restraint"
               ".real_stdout_isatty", lambda: True):
        _mirror(r, "suppressed\n")
    assert buf.getvalue() == ""


def test_local_echo_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_STREAM_LOCAL_ECHO_ENABLED", raising=False)
    assert sr.local_echo_enabled() is True


def test_the_mirror_master_flag_still_governs_everything(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_STREAM_MIRROR_ENABLED", "0")
    r, buf = _renderer()
    with patch("backend.core.ouroboros.battle_test.presentation_restraint"
               ".real_stdout_isatty", lambda: True):
        _mirror(r, "suppressed\n")
    assert buf.getvalue() == ""


def test_offset_advances_so_a_line_is_never_repeated(local_tty) -> None:
    """Two flushes over a growing buffer, as the consumer does every 16 ms."""
    r, buf = _renderer()
    r._buffer = "alpha\n"
    r._mirrored_offset = 0
    r._mirror_completed_lines()
    r._buffer += "beta\n"
    r._mirror_completed_lines()
    out = buf.getvalue()
    assert out.count("alpha") == 1
    assert out.count("beta") == 1

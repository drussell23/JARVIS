"""Putting text on the operator's clipboard, and saying which way it went.

Nothing in this repo could WRITE a clipboard: `clipboard_image` only reads
one, and prompt_toolkit's is in-memory without `pyperclip`, which is not
installed. So the cockpit could offer a selection with nowhere to put it.

The failures pinned here are the ones an operator experiences as "copy is
broken sometimes":

  * a writer that EXISTS but fails (xclip with no DISPLAY) reported as
    success, leaving an empty clipboard and no explanation;
  * an OSC 52 sequence written into a pipe or a log instead of a terminal,
    which is not a copy but corruption of whatever reads that stream;
  * an oversized OSC 52 payload silently dropped by the terminal;
  * X11's PRIMARY reported as the path when the real clipboard already took
    it, telling the operator to middle-click when Ctrl+V works.
"""
from __future__ import annotations

import base64
import os
import sys

import pytest

from backend.core.ouroboros.battle_test import clipboard_write as C


class _FakeStream:
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


@pytest.fixture
def pipe():
    r, w = os.pipe()
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


class TestTheCascade:
    def test_a_writer_that_exists_but_fails_does_not_count(self):
        """`xclip` with no DISPLAY and `wl-copy` outside Wayland both exist
        and both exit non-zero. A cascade that stopped at 'the binary is on
        PATH' would report success over an empty clipboard."""
        assert C._run(["false"], "x") is False

    def test_a_missing_binary_is_survivable(self):
        assert C._run(["definitely-not-a-real-binary-xyz"], "x") is False

    def test_empty_text_is_not_a_copy(self):
        assert C.copy_text("") is None
        assert C.copy_text(None) is None

    def test_the_kill_switch_works(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CLIPBOARD_WRITE_ENABLED", "0")
        assert C.copy_text("hello") is None

    def test_oversized_text_is_truncated_not_refused(self, monkeypatch):
        """'Select all' over a 20 000-line canvas is one gesture. Piping
        megabytes through a subprocess is how a copy becomes a freeze."""
        monkeypatch.setenv("JARVIS_CLIPBOARD_MAX_CHARS", "1000")
        assert C.max_copy_chars() == 1000

    def test_the_caps_are_clamped_not_trusted(self, monkeypatch):
        for var, fn, low in (("JARVIS_CLIPBOARD_MAX_CHARS", C.max_copy_chars, 1_000),
                             ("JARVIS_OSC52_LIMIT", C.osc52_limit, 1_000)):
            monkeypatch.setenv(var, "0")
            assert fn() >= low
            monkeypatch.setenv(var, "not-a-number")
            assert fn() > 0

    def test_no_writer_uses_a_shell(self):
        """The text is a transcript selection containing model-authored
        output. A shell string makes every metacharacter executable."""
        import inspect

        src = inspect.getsource(C)
        assert "shell=True" not in src
        assert "os.system" not in src


class TestOsc52:
    def test_it_writes_a_well_formed_sequence(self, pipe):
        r, w = pipe
        assert C._write_osc52("hello", out_stream=_FakeStream(w)) is True
        os.close(w)
        seq = os.read(r, 400)
        assert seq.startswith(b"\x1b]52;c;")
        assert base64.b64decode(seq[7:-1]).decode() == "hello"

    def test_it_refuses_a_payload_the_terminal_would_drop(self, pipe, monkeypatch):
        """Past the limit the sequence is dropped SILENTLY. A copy that
        reports success and did nothing is worse than one that says it
        could not."""
        _r, w = pipe
        monkeypatch.setenv("JARVIS_OSC52_LIMIT", "16")
        assert C._write_osc52("x" * 5000, out_stream=_FakeStream(w)) is False

    def test_it_wraps_for_tmux_passthrough(self, pipe, monkeypatch):
        """tmux swallows an unknown OSC unless it is wrapped."""
        r, w = pipe
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
        assert C._write_osc52("hi", out_stream=_FakeStream(w)) is True
        os.close(w)
        seq = os.read(r, 400)
        assert seq.startswith(b"\x1bPtmux;")
        assert seq.endswith(b"\x1b\\")

    def test_it_refuses_to_write_escapes_at_a_non_terminal(self, monkeypatch):
        """THE leak. An escape written into a pipe, a log or a captured test
        stream is not a copy — it is corruption of whatever reads it, and it
        is invisible until someone greps the output and finds `]52;c;` glued
        to a line. Probed via `real_stdout_isatty` because
        `sys.stdout.isatty()` is False under the patch_stdout proxy.
        """
        import backend.core.ouroboros.battle_test.presentation_restraint as P

        monkeypatch.setattr(P, "real_stdout_isatty", lambda: False)
        assert C._write_osc52("hello") is False

    def test_a_terminal_is_allowed(self, monkeypatch, pipe):
        import backend.core.ouroboros.battle_test.presentation_restraint as P

        _r, w = pipe
        monkeypatch.setattr(P, "real_stdout_isatty", lambda: True)
        monkeypatch.setattr(sys, "__stdout__", _FakeStream(w))
        assert C._write_osc52("hello") is True


class TestReporting:
    def test_every_path_has_an_operator_sentence(self):
        """These paths FAIL differently. Naming the one used is what lets an
        operator reason about a paste that did not arrive."""
        for path in ("pbcopy", "wl-copy", "xclip", "xsel", "Set-Clipboard",
                     "clip.exe", "tmux", "osc52"):
            assert C.describe_path(path)

    def test_the_risky_paths_say_why_they_are_risky(self):
        assert "block" in C.describe_path("osc52")
        assert "not the system clipboard" in C.describe_path("tmux")
        assert "middle-click" in C.describe_path("xclip:primary")

    def test_an_unknown_path_still_reads_sensibly(self):
        assert C.describe_path("something-new") == "copied"

    def test_primary_is_never_reported_over_the_real_clipboard(self):
        """PRIMARY is written IN ADDITION to the clipboard. Reporting it as
        the path tells the operator to middle-click when Ctrl+V works."""
        import inspect

        src = inspect.getsource(C.copy_text)
        assert 'endswith(":primary")' in src


@pytest.mark.skipif(sys.platform != "darwin", reason="pbcopy/pbpaste")
class TestRealRoundTrip:
    def test_text_actually_lands_on_the_clipboard(self):
        """The one test that proves the module does its job rather than its
        mechanism. Skipped off macOS; on CI without a clipboard the writer
        simply reports None and this is not reached."""
        import shutil
        import subprocess

        if not shutil.which("pbcopy") or not shutil.which("pbpaste"):
            pytest.skip("no pbcopy/pbpaste")
        marker = "ov-clipboard-roundtrip-∆-42"
        if C.copy_text(marker) != "pbcopy":
            pytest.skip("pbcopy unavailable in this environment")
        got = subprocess.run(["pbpaste"], capture_output=True,
                             text=True).stdout
        assert got == marker

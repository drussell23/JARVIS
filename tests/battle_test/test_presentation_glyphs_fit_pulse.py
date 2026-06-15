"""Sovereign Terminal UI — glyph vocabulary / print_fit / pulse unit tests."""
import sys

import backend.core.ouroboros.battle_test.presentation_restraint as PR


class _FakeStdout:
    """Minimal stdout stand-in carrying a settable ``encoding`` (the real
    ``sys.stdout.encoding`` is a readonly C attribute, so we replace the whole
    object to exercise the genuine encoding-detection path)."""

    def __init__(self, encoding):
        self.encoding = encoding

    def write(self, *_a):
        return 0

    def flush(self):
        pass


# --------------------------------------------------------------------------- glyphs
def test_glyphs_utf8(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStdout("utf-8"))
    g = PR.glyphs()
    assert g["action"] == "⏺" and g["result"] == "⎿"
    assert PR.spinner_name() == "dots"


def test_glyphs_ascii_fallback(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStdout("ascii"))
    g = PR.glyphs()
    assert g["action"] == "*" and g["result"] == ">"
    assert PR.spinner_name() == "line"


def test_glyphs_none_encoding_is_safe(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStdout(None))   # encoding can be None
    assert PR.glyphs()["action"] == "*"          # degrades, never raises
    assert PR.spinner_name() == "line"


def test_glyphs_missing_stdout_is_safe(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())  # no .encoding attr at all
    assert PR.glyphs()["action"] == "*"          # fail-safe to ASCII


# --------------------------------------------------------------------------- print_fit
from io import StringIO  # noqa: E402
from rich.console import Console  # noqa: E402


def _con(width):
    return Console(file=StringIO(), width=width, force_terminal=False, color_system=None)


def test_print_fit_truncates_to_width():
    con = _con(40)
    PR.print_fit(con, "  ⎿ " + ("x/" * 80))            # far wider than 40
    out = con.file.getvalue().rstrip("\n")
    assert len(out) <= 40                               # never exceeds width
    assert out.endswith("…") or out.endswith("...")     # ellipsis applied


def test_print_fit_short_line_unchanged():
    con = _con(80)
    PR.print_fit(con, "⏺ applied")
    out = con.file.getvalue()
    assert "applied" in out and "…" not in out


def test_print_fit_never_wraps_multiline():
    con = _con(20)
    PR.print_fit(con, "⏺ " + ("verylongtokenwithoutspaces" * 4))
    assert con.file.getvalue().count("\n") == 1        # exactly one line, no wrap


def test_print_fit_failsoft_on_bad_console():
    class _Boom:
        width = 30
        def print(self, *a, **k):
            raise RuntimeError("nope")
    PR.print_fit(_Boom(), "anything")                  # must not raise

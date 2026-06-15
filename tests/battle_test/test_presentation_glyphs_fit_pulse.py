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

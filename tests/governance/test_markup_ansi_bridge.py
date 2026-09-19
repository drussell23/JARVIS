"""Surfaces that cannot parse Rich markup must not be handed Rich markup.

Two of them, both measured from a live cockpit:

    [cyan]qwen3-coder-ov:30b[/cyan] · voice: off ('wake') · 'detach' to leave
    [dim]· Iterating… (4s · ↓ 902 tokens)[/dim]

The first is prompt_toolkit's bottom toolbar — it accepts a plain string and
prints it verbatim. The second is the attach bridge's `line` channel, which the
client prints verbatim. The composition layers are right to emit markup; what
was missing was a translator at each boundary, and there was one copy of it
buried in the harness.
"""
from __future__ import annotations

import re
import types

import pytest

from backend.core.ouroboros.ui import markup_ansi as MA


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# --------------------------------------------------------------------------
# The translator
# --------------------------------------------------------------------------

def test_tags_never_survive():
    out = MA.markup_to_ansi("[cyan]qwen3-coder-ov:30b[/cyan] · voice: off")
    assert "[cyan]" not in out and "[/cyan]" not in out
    assert "qwen3-coder-ov:30b" in _plain(out)


def test_the_iterating_line_from_the_screenshot():
    out = MA.markup_to_ansi("[dim]· Iterating… (4s · ↓ 902 tokens)[/dim]")
    assert "[dim]" not in out
    assert "902 tokens" in _plain(out)


def test_colour_survives_when_the_surface_has_it():
    console = types.SimpleNamespace(is_terminal=True, color_system="truecolor",
                                    width=120)
    assert "\x1b[" in MA.markup_to_ansi("[red]escalated[/red]", console=console)


def test_a_dumb_surface_gets_clean_text():
    console = types.SimpleNamespace(is_terminal=False, color_system=None,
                                    width=80)
    out = MA.markup_to_ansi("[red]escalated[/red]", console=console)
    assert "\x1b[" not in out
    assert out.strip() == "escalated"


def test_the_COMPOSING_sides_width_is_not_imposed():
    """A daemon with no TTY reports 80 columns. Passing that pinned a
    140-column cockpit to 80 — the receiving terminal owns its geometry."""
    console = types.SimpleNamespace(is_terminal=True, color_system="truecolor",
                                    width=80)
    long_line = "word " * 60
    out = MA.markup_to_ansi(long_line, console=console, width=None)
    assert "\n" not in out.rstrip("\n")


def test_explicit_width_is_still_honoured():
    console = types.SimpleNamespace(is_terminal=True, color_system="truecolor",
                                    width=200)
    out = MA.markup_to_ansi("x", console=console, width=40)
    assert isinstance(out, str)


# --------------------------------------------------------------------------
# prompt_toolkit boundary
# --------------------------------------------------------------------------

def test_the_toolbar_gets_a_TYPE_prompt_toolkit_renders():
    """A plain string is what it printed the markup as. ANSI is its own
    documented type for pre-escaped text."""
    frag = MA.toolbar_fragments("[cyan]qwen3-coder-ov:30b[/cyan] · voice: off")
    assert type(frag).__name__ == "ANSI"
    assert "[cyan]" not in str(getattr(frag, "value", frag))


def test_the_toolbar_degrades_to_PLAIN_never_to_tags(monkeypatch):
    """A toolbar without colour is legible; a toolbar full of tags is the bug
    this exists to remove."""
    import sys

    monkeypatch.setitem(sys.modules, "prompt_toolkit.formatted_text", None)
    out = MA.toolbar_fragments("[cyan]model[/cyan] · hints")
    assert "[cyan]" not in str(out)
    assert "model" in str(out)


# --------------------------------------------------------------------------
# Resilience
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["[unclosed", "a [/nope] b", "[]", "[[x]]", ""])
def test_malformed_markup_still_yields_a_string(bad):
    assert isinstance(MA.markup_to_ansi(bad), str)
    assert isinstance(MA.markup_to_plain(bad), str)


def test_it_never_raises_on_any_input():
    for bad in (None, 123, object(), b"bytes"):
        assert isinstance(MA.markup_to_ansi(bad), str)


def test_plain_uses_richs_own_parser_not_a_regex():
    """Nesting, escaped brackets and malformed tags all have defined
    behaviour in Rich; a hand-rolled stripper gets one of them wrong on the
    line that matters."""
    import inspect

    src = inspect.getsource(MA.markup_to_plain)
    assert "rich.markup" in src
    assert "re.sub" not in src


# --------------------------------------------------------------------------
# DRY — one translator, both boundaries
# --------------------------------------------------------------------------

def test_the_harness_uses_the_SHARED_translator():
    import inspect

    from backend.core.ouroboros.battle_test.harness import (
        _render_markup_for_wire,
    )

    src = inspect.getsource(_render_markup_for_wire)
    assert "markup_ansi" in src
    # The local Console construction is gone — one implementation, not two.
    assert "Console(" not in src


def test_the_toolbar_uses_the_SHARED_translator():
    import inspect

    from backend.core.ouroboros.cli import ov as OV

    src = inspect.getsource(OV)
    assert "toolbar_fragments" in src

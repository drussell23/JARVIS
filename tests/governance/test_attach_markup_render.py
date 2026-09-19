"""An attached cockpit must see what the daemon console shows.

Measured from a live `ov --sentinel` screenshot: the attached terminal printed

    [cyan] Autonomous Sentinel armed[/cyan] [dim]— discovering its own work…[/dim]
    [cyan]qwen3-coder-ov:30b[/cyan] · voice: off ('wake') · ^X ^L lanes · …

with the tags visible, while the daemon's own console rendered the same string
correctly one line below.

`_repl_print` composes Rich MARKUP and handed it to `publish_line`, which is
the attach bridge's PLAIN-text channel — the client prints those frames
verbatim. Two destinations, one string, two interpretations.

The markup channel is deliberately NOT the fix: `publish_markup`'s contract is
"Untrusted/raw text must NEVER travel here", and this seam carries chat turns —
model-controlled text. Routing that through a channel the client renders
unescaped would let a model emit styling into the operator's terminal.
"""
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.battle_test.harness import _render_markup_for_wire


def _plain(s: str) -> str:
    """The visible characters, with ANSI removed."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# --------------------------------------------------------------------------
# THE regression
# --------------------------------------------------------------------------

def test_markup_tags_never_reach_the_wire():
    out = _render_markup_for_wire(
        "[cyan]Autonomous Sentinel armed[/cyan] [dim]— every 1242s[/dim]"
    )
    assert "[cyan]" not in out
    assert "[/cyan]" not in out
    assert "[dim]" not in out


def test_the_model_name_line_from_the_screenshot():
    out = _render_markup_for_wire(
        "[cyan]qwen3-coder-ov:30b[/cyan] · voice: off ('wake') · 'detach' to leave"
    )
    assert "[cyan]" not in out
    assert "qwen3-coder-ov:30b" in _plain(out)
    assert "detach" in _plain(out)


def test_the_TEXT_survives_intact():
    """Styling may be lost; the words never may."""
    out = _render_markup_for_wire(
        "[cyan]Autonomous Sentinel armed[/cyan] [dim]— discovering its own "
        "work every 1242s; auto-approving up to APPROVAL_REQUIRED[/dim]"
    )
    flat = _plain(out)
    assert "Autonomous Sentinel armed" in flat
    assert "1242s" in flat
    assert "APPROVAL_REQUIRED" in flat


def test_colour_is_PRESERVED_when_the_terminal_has_it():
    """The design language uses colour to carry meaning — a red escalation is
    not the same line as a dim breadcrumb."""
    sf = types.SimpleNamespace(console=types.SimpleNamespace(
        is_terminal=True, color_system="truecolor", width=100))
    out = _render_markup_for_wire("[red]escalated[/red]", sf)
    assert "\x1b[" in out
    assert "escalated" in _plain(out)


def test_a_dumb_terminal_gets_CLEAN_TEXT_not_escape_soup():
    sf = types.SimpleNamespace(console=types.SimpleNamespace(
        is_terminal=False, color_system=None, width=80))
    out = _render_markup_for_wire("[red]escalated[/red]", sf)
    assert "\x1b[" not in out
    assert _plain(out).strip() == "escalated"


# --------------------------------------------------------------------------
# Resilience — a cockpit line must never be LOST
# --------------------------------------------------------------------------

def test_plain_text_passes_through_unharmed():
    assert _plain(_render_markup_for_wire("no markup here")).strip() == \
        "no markup here"


def test_malformed_markup_still_yields_a_line():
    """A model emitting a stray bracket must not cost the operator the line."""
    for bad in ("[unclosed", "text [/nope] more", "[]", "[[weird]]"):
        out = _render_markup_for_wire(bad)
        assert isinstance(out, str) and out != ""


def test_a_broken_renderer_degrades_to_TEXT_never_to_nothing(monkeypatch):
    """Styling is a cosmetic loss; a missing line is an operator flying
    blind."""
    import backend.core.ouroboros.battle_test.harness as H

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no console today")

    monkeypatch.setattr("rich.console.Console", _Boom)
    out = _render_markup_for_wire("[cyan]still visible[/cyan]")
    assert "still visible" in out
    assert "[cyan]" not in out


def test_it_never_raises_on_any_input():
    for bad in (None, 123, object(), "", "\x00"):
        assert isinstance(_render_markup_for_wire(bad), str)


def test_the_repl_chokepoint_publishes_the_RENDERED_form():
    """The wiring, not just the helper: `_repl_print` must not hand raw
    markup to the plain-text channel."""
    import inspect

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    src = inspect.getsource(BattleTestHarness._repl_print)
    assert "_render_markup_for_wire" in src
    assert "publish_line(msg)" not in src


def test_it_does_NOT_route_chat_through_the_markup_channel():
    """`publish_markup` says untrusted text must never travel there, and this
    seam carries model output."""
    import inspect

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    src = inspect.getsource(BattleTestHarness._repl_print)
    # Scoped to CALLS, not prose — the comment names the rejected channel in
    # order to record why it is not used.
    assert "bridge.publish_markup(" not in src
    assert "publish_line(" in src

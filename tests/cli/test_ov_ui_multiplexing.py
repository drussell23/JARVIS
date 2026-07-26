"""Incoming frames must not paint over the operator's input line.

A Rich ``Console`` binds ``sys.stdout`` at CONSTRUCTION. ``patch_stdout``
swaps ``sys.stdout`` afterwards, so a console built before the prompt started
writes straight past the proxy and over whatever the operator is typing.

``_print_line`` already knew this — its comment reads "a pre-bound Rich
console would bypass the patch and corrupt the input line" — and used builtin
``print()``, which resolves ``sys.stdout`` dynamically. ``_render_markup_frame``
did the opposite.

That was survivable while markup carried occasional op chrome. It is not now:
the Omni-Channel bus put all 60 REPL verb results on that channel, and the
Moltbook Pub/Sub puts unprompted posts on it too — arriving precisely while
the operator is mid-command.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any

import pytest

from backend.core.ouroboros.cli import ov


class _PatchedStdout(io.StringIO):
    """Stands in for prompt_toolkit's patch_stdout proxy.

    The real proxy queues writes and repaints the prompt around them. What
    matters for this test is identity: writes must land HERE, because that is
    what gives prompt_toolkit the chance to redraw. Anything reaching the
    original stdout has bypassed the multiplexer."""


class _PreBoundConsole:
    """A Rich-console stand-in captured BEFORE the prompt started."""

    width = 100

    def __init__(self) -> None:
        self.printed: list = []

    def print(self, *args: Any, **kwargs: Any) -> None:
        self.printed.append(args[0] if args else "")


@contextlib.contextmanager
def swapped_stdout():
    """Swap sys.stdout for the duration of the call.

    `contextlib.redirect_stdout`, not `monkeypatch.setattr(sys, "stdout")` —
    pytest's own capture also owns sys.stdout, and a monkeypatch of it is
    undone underneath the test, which made these assertions read the real
    stdout and fail against a fix that was already correct."""
    proxy = _PatchedStdout()
    with contextlib.redirect_stdout(proxy):
        yield proxy


# --------------------------------------------------------------------------
# 1. the requirement — background frames go through the active proxy
# --------------------------------------------------------------------------

def test_markup_frame_writes_through_the_patched_stdout() -> None:
    """A Moltbook post arriving mid-typing must reach the proxy, which is what
    lets prompt_toolkit print it ABOVE the prompt and redraw the buffer."""
    pre_bound = _PreBoundConsole()

    with swapped_stdout() as proxy:
        ov._render_markup_frame(
            "  [bold]⏺ 🐍 @cassandra[/bold] [dim]· distress[/dim]", pre_bound,
        )
    out = proxy.getvalue()
    assert "cassandra" in out, (
        "the frame never reached the patched stdout — it bypassed the "
        "multiplexer and would have painted over the input line"
    )
    assert pre_bound.printed == [], (
        "the frame went to the console captured before patch_stdout, which "
        "writes past the proxy and corrupts the operator's prompt"
    )


def test_plain_line_frame_also_respects_the_proxy() -> None:
    """The untyped `line` channel already did this correctly — pin it so a
    future refactor cannot regress it to a bound console."""
    import inspect

    src = inspect.getsource(ov)
    assert "print(text)" in src, (
        "the line-frame path no longer uses builtin print(), which is what "
        "resolves sys.stdout dynamically under patch_stdout"
    )


def test_styling_survives_the_late_binding() -> None:
    """Late binding must not cost fidelity — markup still renders as markup,
    not as literal tag text."""
    with swapped_stdout() as proxy:
        ov._render_markup_frame("[bold]Moltbook[/bold]", _PreBoundConsole())
    out = proxy.getvalue()
    assert "Moltbook" in out
    assert "[bold]" not in out, "markup tags leaked through as literal text"


def test_malformed_markup_is_escaped_not_dropped() -> None:
    """Fail-soft is part of the contract: a frame that will not parse renders
    inert rather than vanishing or crashing the canvas."""
    with swapped_stdout() as proxy:
        ov._render_markup_frame("[not-a-tag unclosed", _PreBoundConsole())
    assert "not-a-tag" in proxy.getvalue()


def test_width_is_inherited_not_collapsed() -> None:
    """The proxy is not a tty, so a freshly-built console would fall back to
    80 columns and wrap differently from every other line on screen."""
    pre_bound = _PreBoundConsole()
    pre_bound.width = 120
    with swapped_stdout() as proxy:
        ov._render_markup_frame("x" * 110, pre_bound)
    out = proxy.getvalue()
    assert "x" * 110 in out.replace("\n", ""), (
        "output wrapped early — the inherited width was ignored"
    )


def test_render_never_raises_even_with_a_hostile_console() -> None:
    """This sits on a background receive path; an exception here would kill
    the reader task and silently detach the cockpit."""
    class _Hostile:
        width = "not-an-int"

        def print(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("console on fire")

    ov._render_markup_frame("[bold]hello[/bold]", _Hostile())


def test_canvas_path_still_wins_when_bipartite_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bipartite cockpit owns its own region; when a canvas is mounted the
    frame belongs to it, not to stdout."""
    pushed: list = []

    class _Canvas:
        def push_raw(self, s: str) -> None:
            pushed.append(s)

    import backend.core.ouroboros.battle_test.bipartite_layout as bl
    monkeypatch.setattr(bl, "get_active_canvas", lambda: _Canvas())

    with swapped_stdout() as proxy:
        ov._render_markup_frame("[bold]via canvas[/bold]", _PreBoundConsole())
    assert pushed and "via canvas" in pushed[0]
    assert proxy.getvalue() == "", (
        "frame was written to stdout as well as the canvas — duplicated"
    )

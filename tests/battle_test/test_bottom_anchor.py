"""Transcript geometry — the conversation grows UP toward the prompt.

Claude Code's layout: the input box is fixed at the bottom and the
transcript fills toward it, so the newest line is always the one directly
above where you type. Our canvas was top-aligned, which put the
conversation at row 0 and left a screen-high void between it and the
prompt — the thing an operator reads as "it doesn't flow like Claude".

Padding, not alignment: the canvas body is a Group of independently
styled lines with no shared container to align, and blank leading rows
are exactly what an alternate screen shows above a short conversation.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import bipartite_layout as bl


@pytest.fixture()
def fullscreen(monkeypatch):
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "1")
    monkeypatch.delenv("JARVIS_BIPARTITE_BOTTOM_ANCHOR", raising=False)
    monkeypatch.delenv("JARVIS_BIPARTITE_BORDER", raising=False)
    yield


def _mux(height: int = 30):
    return bl.BipartiteLayout(width=100, height=height, title="t")


# --------------------------------------------------------------------------
# 1. the geometry
# --------------------------------------------------------------------------

def test_short_conversation_sits_against_the_prompt(fullscreen) -> None:
    mux = _mux()
    mux.push_raw("❯ can you tell me what is O+V?")
    mux.push_raw("⏺ thinking about that")
    lines = mux._visible_lines()
    assert len(lines) == mux._line_budget()      # fills the region
    assert lines[-1] == "⏺ thinking about that"  # newest against the prompt
    assert lines[0] == ""                        # the void is ABOVE
    assert [ln for ln in lines if ln][0].startswith("❯")


def test_a_full_screen_is_untouched(fullscreen) -> None:
    mux = _mux()
    for i in range(200):
        mux.push_raw(f"line {i}")
    lines = mux._visible_lines()
    assert len(lines) == mux._line_budget()
    assert "" not in lines                       # nothing to pad
    assert lines[-1] == "line 199"


def test_idle_keeps_its_message_not_a_wall_of_blanks(fullscreen) -> None:
    """Padding zero lines to a screenful would replace 'the organism
    rests' with an empty screen."""
    mux = _mux()
    assert mux._visible_lines() == []


def test_growth_is_upward(fullscreen) -> None:
    mux = _mux()
    mux.push_raw("first")
    top_before = mux._visible_lines().index("first")
    mux.push_raw("second")
    assert mux._visible_lines().index("first") == top_before - 1
    assert mux._visible_lines()[-1] == "second"


# --------------------------------------------------------------------------
# 2. the gates
# --------------------------------------------------------------------------

def test_kill_switch_restores_top_alignment(fullscreen, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BIPARTITE_BOTTOM_ANCHOR", "0")
    mux = _mux()
    mux.push_raw("only line")
    assert mux._visible_lines() == ["only line"]


def test_inline_mode_never_anchors(monkeypatch) -> None:
    """Inline, the canvas is a bounded live region that must collapse to
    nothing when idle — padding would nail an empty block open."""
    monkeypatch.setenv("JARVIS_BIPARTITE_FULLSCREEN", "0")
    assert bl.bottom_anchor_enabled() is False
    mux = _mux()
    mux.push_raw("only line")
    assert mux._visible_lines() == ["only line"]


# --------------------------------------------------------------------------
# 3. it must not disturb what already worked
# --------------------------------------------------------------------------

def test_scroll_metrics_report_real_content_only(fullscreen) -> None:
    """The viewport clamps against the RING, not against padding — a
    padded canvas that reported 29 lines would let the operator scroll
    into blank space."""
    mux = _mux()
    for i in range(5):
        mux.push_raw(f"line {i}")
    total, _budget = mux.scroll_metrics()
    assert total == 5


def test_scrolled_back_view_still_anchors_without_losing_status(
    fullscreen,
) -> None:
    mux = _mux()
    for i in range(120):
        mux.push_raw(f"line {i}")
    total, budget = mux.scroll_metrics()
    mux._viewport.scroll(-5, total=total, budget=budget)
    lines = mux._visible_lines()
    assert len(lines) == budget
    assert "reverse dim" in lines[-1]        # the scroll status survives


def test_render_survives_anchoring(fullscreen) -> None:
    mux = _mux()
    mux.push_raw("❯ hello")
    out = mux.render_canvas_ansi()
    assert "hello" in out

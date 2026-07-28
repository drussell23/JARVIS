"""Transcript escape hatches — the ring becomes searchable, navigable,
and honest about tmux.

Pins: markup stripping, the ring→transcript read, block-jump offset math
against the REAL viewport, the scrolled-only gate on printable keys, the
tmux passthrough probe, and the keymap mounting of all six actions.
"""
from __future__ import annotations

import subprocess

import pytest

from backend.core.ouroboros.battle_test import transcript_hatches as th


@pytest.fixture()
def live_canvas():
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        set_active_canvas,
    )
    mux = BipartiteLayout(width=80, height=12, title="t")
    set_active_canvas(mux)
    yield mux
    set_active_canvas(None)


def test_strip_markup() -> None:
    assert th._strip_markup("[bold]⏺ hi[/bold]") == "⏺ hi"
    assert th._strip_markup("plain") == "plain"


def test_transcript_lines_reads_the_ring(live_canvas) -> None:
    live_canvas.push_raw("[cyan]⏺ op one[/cyan]")
    live_canvas.push_raw("  ⎿ detail")
    lines = th.transcript_lines()
    assert lines[-2:] == ["⏺ op one", "  ⎿ detail"]


def test_no_canvas_means_empty_not_error() -> None:
    assert th.transcript_lines() == []
    assert th.is_scrolled_back() is False


def test_jump_block_moves_the_viewport_to_a_marker(live_canvas) -> None:
    for i in range(30):
        live_canvas.push_raw(f"· trace {i}")
    live_canvas.push_raw("⏺ the block")
    for i in range(30):
        live_canvas.push_raw(f"· more {i}")
    vp = live_canvas._viewport
    total, budget = live_canvas.scroll_metrics()
    # scroll back a little so the hatch keys are live, then jump up
    vp.scroll(-5, total=total, budget=budget)
    assert th.is_scrolled_back()
    th.jump_block(None, -1)
    lines = th.transcript_lines()
    total, budget = live_canvas.scroll_metrics()
    top = max(0, total - budget - vp.offset)
    assert lines[top].startswith("⏺")
    # and } walks forward again without leaving the ring
    th.jump_block(None, +1)
    assert vp.offset >= 0


def test_scrolled_gate_tracks_the_viewport(live_canvas) -> None:
    for i in range(40):
        live_canvas.push_raw(f"line {i}")
    assert th.is_scrolled_back() is False
    total, budget = live_canvas.scroll_metrics()
    live_canvas._viewport.scroll(-3, total=total, budget=budget)
    assert th.is_scrolled_back() is True


def test_tmux_probe_silent_outside_tmux(monkeypatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert th.tmux_bell_warning() == ""


def test_tmux_probe_warns_when_passthrough_off(monkeypatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")

    def _fake_run(*_a, **_k):
        class _P:
            stdout = "off\n"
        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert "allow-passthrough" in th.tmux_bell_warning()


def test_tmux_probe_silent_when_passthrough_on(monkeypatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")

    def _fake_run(*_a, **_k):
        class _P:
            stdout = "on\n"
        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert th.tmux_bell_warning() == ""


def test_tmux_probe_silent_when_bell_disabled(monkeypatch) -> None:
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setenv("JARVIS_GATE_BELL_ENABLED", "false")
    assert th.tmux_bell_warning() == ""
    monkeypatch.delenv("JARVIS_GATE_BELL_ENABLED")


def test_all_six_actions_mount_through_the_keymap() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.key_binding import KeyBindings

    class _UI:
        def flash(self, *_a, **_k):
            pass

    class _Client:
        def send_input(self, _t):
            return True

    kb = KeyBindings()
    assert th.install_transcript_hatches(kb, _UI(), _Client())
    for keys in (("[",), ("v",), ("{",), ("}",), ("c-l",), ("c-o",)):
        assert kb.get_bindings_for_keys(keys), keys


def test_hatches_are_wired_into_the_client_action_set() -> None:
    from pathlib import Path
    import backend.core.ouroboros.cli.ov as ov
    src = Path(ov.__file__).read_text()
    assert "install_transcript_hatches(kb, ui, client)" in src
    assert "tmux_bell_warning()" in src

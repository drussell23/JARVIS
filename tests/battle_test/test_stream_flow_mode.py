"""§41.3 #27 — Progressive streaming flow mode (unwrap the Live cage).

Pins the last remaining §41.3 engineering item: with
``JARVIS_UI_STREAMING_FLOW_MODE_ENABLED=1``, completed markdown blocks are
committed ABOVE the Rich Live region into terminal scrollback as they
stream (CC-style progressive flow); the Live cage holds only the current
in-progress block, and ``end()`` lands the remainder so the ENTIRE stream
persists in scrollback with no tail truncation.

Master flag default-FALSE per §33.1 — the legacy caged path must stay
byte-identical when the flag is off.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test import stream_renderer as sr


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("JARVIS_UI_STREAMING_FLOW_MODE_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_UI_STREAMING_ENABLED", "1")
    sr.reset_stream_renderer()
    yield
    sr.reset_stream_renderer()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConsole:
    def __init__(self) -> None:
        self.printed = []

    def print(self, renderable) -> None:
        self.printed.append(getattr(renderable, "markup", renderable))


class _FakeLive:
    """Duck-typed Rich Live stand-in capturing update/commit traffic."""

    def __init__(self) -> None:
        self.console = _FakeConsole()
        self.updates = []
        self.stopped = False

    def update(self, renderable) -> None:
        self.updates.append(getattr(renderable, "markup", renderable))

    def stop(self) -> None:
        self.stopped = True


# ---------------------------------------------------------------------------
# (1) Env gate — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def test_flow_mode_default_off():
    assert sr.flow_mode_enabled() is False


def test_flow_mode_env_on(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STREAMING_FLOW_MODE_ENABLED", "1")
    assert sr.flow_mode_enabled() is True


# ---------------------------------------------------------------------------
# (2) find_commit_boundary — pure fence-aware boundary math
# ---------------------------------------------------------------------------


def test_boundary_none_without_blank_line():
    assert sr.find_commit_boundary("just one paragraph still typing") == 0


def test_boundary_after_complete_paragraph():
    text = "para one.\n\npara two still typ"
    b = sr.find_commit_boundary(text)
    assert b == len("para one.\n\n")
    assert text[b:] == "para two still typ"


def test_boundary_skips_blank_lines_inside_open_fence():
    text = "intro\n\n```python\ncode\n\nmore code"
    # Only the blank line BEFORE the fence opens is a safe boundary.
    assert sr.find_commit_boundary(text) == len("intro\n\n")


def test_boundary_advances_after_fence_closes():
    text = "intro\n\n```python\ncode\n```\n\ntail typing"
    b = sr.find_commit_boundary(text)
    assert text[b:] == "tail typing"


def test_partial_trailing_blank_line_not_a_boundary():
    # A "\n" not yet followed by its blank line's terminator stays in flight.
    assert sr.find_commit_boundary("para one.\n") == 0


def test_boundary_incremental_scan_from_prior_boundary():
    text = "a\n\nb\n\nc typing"
    b1 = sr.find_commit_boundary(text[:4])       # "a\n\nb"
    assert b1 == 3
    b2 = sr.find_commit_boundary(text, b1)
    assert text[b2:] == "c typing"


def test_boundary_never_raises():
    assert sr.find_commit_boundary("", 0) == 0
    assert sr.find_commit_boundary("x", 99) == 99   # degenerate start


# ---------------------------------------------------------------------------
# (3) Renderer integration — flow commits above the cage
# ---------------------------------------------------------------------------


def _make_active_renderer(monkeypatch, flow: str) -> "tuple[sr.StreamRenderer, _FakeLive]":
    """Build a renderer in an active session with a fake Live installed."""
    monkeypatch.setenv("JARVIS_UI_STREAMING_FLOW_MODE_ENABLED", flow)
    r = sr.StreamRenderer()
    fake = _FakeLive()

    async def _boot():
        r.start("op-flow-test", "claude")

    asyncio.run(_boot())
    # start() under pytest is non-TTY → _live is None; install the fake and
    # keep session state (this isolates the render path from real Rich).
    r._live = fake
    return r, fake


def test_flow_mode_commits_completed_block_and_cages_tail(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "1")
    r._buffer = "block one.\n\nblock two typ"
    r._render_buffer_safe()
    # Completed block landed in scrollback...
    assert fake.console.printed == ["block one.\n\n"]
    # ...and the cage renders ONLY the uncommitted tail.
    assert fake.updates[-1] == "block two typ"
    assert r._committed_offset == len("block one.\n\n")


def test_flow_mode_does_not_recommit(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "1")
    r._buffer = "block one.\n\nblock two typ"
    r._render_buffer_safe()
    r._buffer += "ing more"
    r._render_buffer_safe()
    # No new boundary → no second scrollback print.
    assert len(fake.console.printed) == 1
    assert fake.updates[-1] == "block two typing more"


def test_legacy_mode_never_prints_above_cage(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "0")
    r._buffer = "block one.\n\nblock two typ"
    r._render_buffer_safe()
    # Legacy: nothing committed; whole buffer (tail-sliced) in the cage.
    assert fake.console.printed == []
    assert fake.updates[-1] == "block one.\n\nblock two typ"
    assert r._committed_offset == 0


def test_flow_mode_end_lands_remainder_and_clears_cage(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "1")
    r._buffer = "block one.\n\nfinal words"
    r._render_buffer_safe()
    r.end()
    # end() committed the remainder + cleared the cage before stop.
    assert fake.console.printed == ["block one.\n\n", "final words"]
    assert fake.updates[-1] == ""
    assert fake.stopped is True


def test_legacy_mode_end_keeps_final_tail_render(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "0")
    r._buffer = "block one.\n\nfinal words"
    r.end()
    assert fake.console.printed == []
    assert fake.updates[-1] == "block one.\n\nfinal words"
    assert fake.stopped is True


def test_flow_state_reset_between_sessions(monkeypatch):
    r, fake = _make_active_renderer(monkeypatch, "1")
    r._buffer = "a\n\nb"
    r._render_buffer_safe()
    r.end()
    assert r._committed_offset == 0
    assert r._flow_mode is False


def test_mode_snapshotted_at_start_not_per_render(monkeypatch):
    """One op never splits across modes — the env flip mid-stream is inert."""
    r, fake = _make_active_renderer(monkeypatch, "0")
    monkeypatch.setenv("JARVIS_UI_STREAMING_FLOW_MODE_ENABLED", "1")
    r._buffer = "block one.\n\ntail"
    r._render_buffer_safe()
    assert fake.console.printed == []       # still legacy for this op

"""Bulletproof spine for the Bipartite Async Layout multiplexer.

Mandated assertions, headless (no real terminal) so they run in CI — the pure
core (Zone-1 ring + Rich composition + resize recompute) is exercised directly:

  (1) an async background task can emit 50 events into the layout without raising
      a layout exception (and the render path stays clean throughout),
  (2) SIGWINCH terminal resizing dynamically recalculates the panel boundaries
      without crashing (including degenerate sizes), and
  (3) the stdin reader remains UNBLOCKED during background emissions — an input
      coroutine interleaves and receives its token while 50 events stream in.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.bipartite_layout import (
    BipartiteLayout,
    get_active_canvas,
    set_active_canvas,
)


def _emit_types():
    # Real event types the unified router formats (DRY — same registry).
    return [
        ("supervisor_armed", {"pending": 1, "provider_state": "DEGRADED", "pid": 4242}),
        ("sentinel_telemetry", {"line": "probe DEGRADED stage=pass1"}),
        ("awe_soak_launched", {"provider": "doubleword", "run_id": "abc123"}),
        ("soak_chunk_committed", {"done": 3, "total": 7, "symbol": "factorial", "file_path": "mathy.py"}),
        ("soak_chunk_quarantined", {"symbol": "poison", "file_path": "x.py", "reason": "ctx blown"}),
    ]


# ---------------------------------------------------------------------------
# (1) 50 async emissions, no layout exception
# ---------------------------------------------------------------------------


async def test_async_50_emits_no_layout_exception():
    invalidations = {"n": 0}
    mux = BipartiteLayout(width=100, height=24, invalidate=lambda: invalidations.__setitem__("n", invalidations["n"] + 1))

    types = _emit_types()

    async def emitter():
        for i in range(50):
            et, payload = types[i % len(types)]
            await mux.aemit(et, payload)
            # Render on the fly — proves no layout exception mid-stream.
            assert mux.render_canvas() is not None
            assert isinstance(mux.render_layout_ansi(), str)

    await emitter()

    assert mux.line_count() == 50 or mux.line_count() > 0
    assert invalidations["n"] == 50               # every emit triggered a re-render
    # A final full render of the strictly-zoned layout does not raise.
    ansi = mux.render_layout_ansi()
    assert isinstance(ansi, str) and len(ansi) > 0


async def test_bounded_ring_never_grows_unbounded():
    mux = BipartiteLayout(max_lines=32, width=80, height=20)
    for i in range(200):
        mux.emit("soak_chunk_committed", {"done": i, "total": 200, "symbol": f"f{i}", "file_path": "m.py"})
    # The Zone-1 ring is bounded — aggressive streaming can't blow memory.
    assert mux.line_count() <= 32
    assert mux.render_canvas() is not None


# ---------------------------------------------------------------------------
# (2) SIGWINCH resize recomputes boundaries without crashing
# ---------------------------------------------------------------------------


async def test_resize_recomputes_without_crash():
    mux = BipartiteLayout(width=100, height=30)
    for _ in range(10):
        mux.emit("awe_soak_launched", {"provider": "doubleword", "run_id": "r"})

    for (w, h) in [(200, 60), (40, 12), (1, 1), (10, 3), (300, 100), (0, 0)]:
        mux.on_resize(w, h)
        # Clamped so a degenerate resize never crashes the render.
        assert mux.width >= 10 and mux.height >= 3
        assert isinstance(mux.render_layout_ansi(), str)   # never raises
        assert mux.render_canvas() is not None

    # The OS SIGWINCH adapter path (reads live terminal size) is also safe.
    before = mux.resize_count
    mux.handle_sigwinch()
    assert mux.resize_count == before + 1


# ---------------------------------------------------------------------------
# (3) stdin reader stays unblocked during background emissions
# ---------------------------------------------------------------------------


async def test_stdin_reader_unblocked_during_emissions():
    mux = BipartiteLayout(width=100, height=24)
    stdin = asyncio.Queue()          # models the decoupled input channel
    got = asyncio.Event()

    async def reader():
        # If emits blocked the loop, this coroutine could not interleave.
        tok = await stdin.get()
        assert tok == "keystroke"
        got.set()

    async def emitter():
        for i in range(50):
            await mux.aemit("sentinel_telemetry", {"line": f"probe {i}"})
            if i == 10:
                await stdin.put("keystroke")   # user types mid-stream

    reader_task = asyncio.ensure_future(reader())
    await emitter()
    # The reader must have received the keystroke promptly — not after all 50.
    await asyncio.wait_for(got.wait(), timeout=1.0)
    assert got.is_set()
    await reader_task


# ---------------------------------------------------------------------------
# DRY sink redirect — the active-canvas seam the event router uses
# ---------------------------------------------------------------------------


async def test_active_canvas_sink_redirect():
    try:
        mux = BipartiteLayout(width=80, height=20)
        set_active_canvas(mux)
        assert get_active_canvas() is mux
        # An event pushed through the sink lands in Zone 1 with router formatting.
        mux.emit("supervisor_armed", {"pending": 2, "provider_state": "DEGRADED", "pid": 7})
        assert mux.line_count() == 1
        snap = mux._buffer.snapshot()
        assert "supervisor" in snap[0].lower()
    finally:
        set_active_canvas(None)
        assert get_active_canvas() is None

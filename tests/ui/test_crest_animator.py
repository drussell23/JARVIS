"""Bulletproof spine for the Client-Side Boot Animator (physical Snake-and-Plus).

Root-cause regression coverage (operator, 2026-07-23): the snake must PHYSICALLY
travel — the sculpted head (eye/mouth) rotates around the ring, not just the
gradient — and the resting emblem must be byte-identical to the static crest.

  (1) a mock socket log emitted during the animation updates the BOTTOM partition
      WITHOUT corrupting the crest matrix,
  (2) the ``+`` is injected at the correct phase-shifted position and travels,
  (3) the SNAKE ITSELF rotates: rotated frames move the head/gap anatomy,
  (4) the resting frame == the static crest raster (the "looks different" fix),
  (5) frames build progressively off-loop + round-trip the disk cache.
"""

from __future__ import annotations

import asyncio
import math
from io import StringIO

import pytest

from backend.core.ouroboros.ui.crest_animator import (
    CrestAnimator,
    build_animator,
    build_rotated_frame,
)


def _anim(**kw):
    kw.setdefault("cols", 60)
    kw.setdefault("rows", 24)
    kw.setdefault("frame_count", 6)   # small ring — fast tests
    kw.setdefault("ss", 1)            # cheapest sampling for tests
    a = CrestAnimator(**kw)
    if not a.available:
        pytest.skip("crest raster unavailable at this size")
    return a


def _plain(text) -> str:
    return getattr(text, "plain", str(text))


def _render_console(width: int = 80):
    from rich.console import Console
    return Console(file=StringIO(), force_terminal=True, color_system="truecolor",
                   width=width, height=40, highlight=False)


def _render_str(text) -> str:
    c = _render_console()
    c.print(text)
    return c.file.getvalue()


# ---------------------------------------------------------------------------
# (1) an async log NEVER corrupts the crest matrix
# ---------------------------------------------------------------------------


def test_async_log_does_not_corrupt_crest_matrix():
    anim = _anim()
    crest_before = _render_str(anim.crest_frame_text(0.3))
    assert "▀" in crest_before or "▄" in crest_before

    anim.add_log("organism waking · 0s")
    anim.add_log("organism live — attaching")

    crest_after = _render_str(anim.crest_frame_text(0.3))
    assert crest_after == crest_before                    # crest untouched
    logs = _plain(anim.logs_renderable())
    assert "organism waking" in logs and "attaching" in logs
    assert "organism" not in _plain(anim.crest_frame_text(0.3))


def test_full_canvas_partitions_crest_over_logs():
    anim = _anim()
    anim.add_log("waking · 5s")
    console = _render_console()
    console.print(anim.render(0.4))
    out = console.file.getvalue()
    assert "waking · 5s" in out
    assert out.find("waking · 5s") > out.rfind("▀")       # logs BELOW the crest


# ---------------------------------------------------------------------------
# (2) the + prey — phase-shifted, travels, injected into the frame
# ---------------------------------------------------------------------------


def test_plus_injected_and_travels():
    anim = _anim()
    cell = anim.plus_cell(0.25)
    assert cell is not None
    x, cy = cell
    plain = _plain(anim.crest_frame_text(0.25))
    lines = plain.split("\n")
    row = cy - anim._cy_lo
    assert 0 <= row < len(lines) and lines[row][x] == "+"
    assert plain.count("+") == 1

    positions = {anim.plus_cell(p / 8.0) for p in range(8)}
    positions.discard(None)
    assert len(positions) >= 5                            # it moves around the ring


# ---------------------------------------------------------------------------
# (3) the SNAKE physically rotates (the root-cause regression test)
# ---------------------------------------------------------------------------


def test_snake_anatomy_rotates_not_just_colors():
    """A frame rotated half a lap must move the GAP (the mouth opening) to the
    opposite side — lit pixels shift, not merely recolour. This is the exact
    regression the operator reported: a hue-shift keeps the silhouette constant;
    physical rotation changes WHICH cells are lit."""
    f0 = build_rotated_frame(46, 20, 0.0, 1)
    f_half = build_rotated_frame(46, 20, math.pi, 1)
    assert f0 and f_half
    lit0, lit_half = set(f0), set(f_half)
    # The silhouettes differ substantially (gap + head moved to the other side).
    moved = len(lit0 ^ lit_half)
    assert moved > len(lit0) * 0.1, f"silhouette barely moved ({moved} px)"


def test_rotated_frames_used_by_render():
    anim = _anim(frame_count=4)
    # Build the ring synchronously (small + cheap at ss=1).
    asyncio.run(anim.ensure_frames())
    assert anim.frames_built == 4
    a = _render_str(anim.crest_frame_text(0.0))
    b = _render_str(anim.crest_frame_text(0.5))
    assert a != b                                          # different physical pose


# ---------------------------------------------------------------------------
# (4) the resting emblem IS the static crest (the "looks different" fix)
# ---------------------------------------------------------------------------


def test_resting_frame_matches_static_crest():
    from backend.core.ouroboros.ui.crest import generate_crest_pixels
    anim = _anim()
    pf = generate_crest_pixels(60, 24)
    static_pixels = {k: v[0] for k, v in pf.pixels.items()}
    assert anim._base == static_pixels                     # same raster, same colors
    resting = _plain(anim.resting_text())
    assert "+" not in resting.replace("", "")              # no prey on the emblem


# ---------------------------------------------------------------------------
# (5) progressive build + disk cache round-trip
# ---------------------------------------------------------------------------


async def test_progressive_build_never_blocks_and_falls_back():
    anim = _anim(frame_count=6)
    # Before the ring is built, every phase still renders (nearest-built frame).
    assert anim.frames_built == 1
    for p in (0.0, 0.3, 0.7):
        assert _render_str(anim.crest_frame_text(p))
    # The builder fills the ring off-loop while other tasks interleave.
    other = {"ran": False}

    async def competitor():
        other["ran"] = True

    comp = asyncio.ensure_future(competitor())
    await anim.ensure_frames()
    await comp
    assert other["ran"] is True
    assert anim.frames_built == 6


async def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CREST_ANIM_CACHE_DIR", str(tmp_path))
    a1 = _anim(frame_count=4)
    await a1.ensure_frames()
    assert a1.frames_built == 4                            # built + cached
    # A NEW animator at the same size loads the finished ring instantly.
    a2 = _anim(frame_count=4)
    assert a2.frames_built == 4, "cache was not loaded"
    assert _render_str(a2.crest_frame_text(0.5)) == _render_str(a1.crest_frame_text(0.5))


async def test_live_playback_interleaves_logs_without_blocking():
    anim = _anim(frame_count=4)
    console = _render_console()
    stop = asyncio.Event()

    async def fast_sleep(_s):
        await asyncio.sleep(0)

    async def driver():
        await asyncio.sleep(0)
        anim.add_log("organism live — attaching")
        stop.set()

    drive = asyncio.ensure_future(driver())
    await anim.play(console, stop_event=stop, fps=30, sleep_fn=fast_sleep, max_frames=120)
    await drive
    out = console.file.getvalue()
    assert "attaching" in out                              # the log surfaced
    assert "▀" in out                                      # the crest rendered


def test_build_animator_gates_on_size():
    tiny = _render_console(width=20)
    assert build_animator(tiny) is None

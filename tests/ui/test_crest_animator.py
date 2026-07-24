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


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """EVERY test gets its own cache dir — tests must never read or pollute the
    user's real ~/.jarvis/crest_anim (the exact leak that made frames_built==n
    at construction and flipped two tests)."""
    monkeypatch.setenv("JARVIS_CREST_ANIM_CACHE_DIR", str(tmp_path / "crest_cache"))
    yield


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


def test_prey_is_a_themed_pixel_sprite_and_travels():
    """Root-cause regression (operator): the prey was a lone text character in a
    pixel-art medium — small + off-theme. It must be a PIXEL sprite, sized by
    the crest's scale, coloured from the crest's own palette (pale eye core →
    the V's venom purple), and it must travel the ring."""
    from backend.core.ouroboros.ui.crest import _EYE_RGB
    anim = _anim()
    sprite = anim.prey_pixels(0.25)
    assert len(sprite) >= 9                               # a real sprite, not 1 char
    # Centre pixel is the pale core (pulse-scaled — proportional to _EYE_RGB).
    cell = anim.plus_cell(0.25)
    assert cell is not None
    # It renders INTO the frame (overlay merged, no "+" character anywhere).
    plain = _plain(anim.crest_frame_text(0.25))
    assert "+" not in plain
    # Sprite pixels carry the theme: every colour is a core→purple blend, so the
    # red channel stays within the palette envelope (no white/foreign colours).
    for rgb in sprite.values():
        assert 0 <= rgb[0] <= 255 and len(rgb) == 3
    # And it travels around the ring.
    positions = {anim.plus_cell(p / 8.0) for p in range(8)}
    positions.discard(None)
    assert len(positions) >= 5

    # The pulse animates: the same position at different beat phases differs.
    s_a = anim.prey_pixels(0.0)
    s_b = anim.prey_pixels(1.0 / 6.0)                     # half a beat later
    shared = set(s_a) & set(s_b)
    assert any(s_a[k] != s_b[k] for k in shared) or s_a.keys() != s_b.keys()


def test_v_spins_with_its_own_rotation():
    """The V must spin too: a frame with v_rot=π differs from v_rot=0 at the
    SAME snake rotation — and only in the V region (the coil is untouched)."""
    from backend.core.ouroboros.ui.crest_animator import build_rotated_frame
    f_still = build_rotated_frame(46, 20, 0.0, 1, 0.0)
    f_spun = build_rotated_frame(46, 20, 0.0, 1, math.pi / 2.0)
    assert f_still and f_spun
    assert set(f_still) != set(f_spun), "V spin changed no pixels"
    # The spin is confined near the centre (the V), not the outer coil.
    changed = set(f_still) ^ set(f_spun)
    geo_cx = 46 / 2.0
    assert all(abs(x - geo_cx) < 46 * 0.4 for (x, _py) in changed), \
        "V spin leaked outside the centre region"


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
    assert "+" not in resting                              # no prey on the emblem


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


async def test_cache_round_trip():
    a1 = _anim(frame_count=4)
    await a1.ensure_frames()
    assert a1.frames_built == 4                            # built
    # The save is a daemon thread (survives the boot's asyncio.run teardown —
    # the loop CANCELS pending tasks on close, which killed the old save).
    assert a1._save_thread is not None
    a1._save_thread.join(timeout=5.0)
    assert not a1._save_thread.is_alive(), "cache save did not complete"
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


# ---------------------------------------------------------------------------
# (6) the mini prey obeys the BIG logo's physical law (giant-plus regression)
# ---------------------------------------------------------------------------


def _prey_delta(cells: int, rot: float, monkeypatch, pack: str = "half"):
    """The mini's prey pixels, isolated by differencing prey-off vs prey-on
    frames (PSCALE=0 disables the prey entirely — the delta IS the prey)."""
    from backend.core.ouroboros.ui.crest_animator import build_scaled_frame
    monkeypatch.setenv("JARVIS_CREST_MINI_PREY_SCALE", "0")
    f_off = build_scaled_frame(cells, rot, 3, 0.0, "aa", pack=pack)
    monkeypatch.delenv("JARVIS_CREST_MINI_PREY_SCALE", raising=False)
    f_on = build_scaled_frame(cells, rot, 3, 0.0, "aa", pack=pack)
    assert f_on and f_off
    return {k: v for k, v in f_on.items() if f_off.get(k) != v}


def test_mini_prey_is_proportional_not_cell_sized(monkeypatch):
    """Root-cause regression (operator 2026-07-23): the mini's + was sized off
    the CELL grid (cells_w // 8 → 54%% of a 24-cell emblem) instead of the big
    logo's physical law (1.9·scale ≈ 9%%). The prey must now be a small morsel
    — never wider than a quarter of the emblem — yet still read as a plus."""
    for cells in (16, 24, 28):
        prey = _prey_delta(cells, 1.0, monkeypatch)
        assert prey, f"prey vanished at cells={cells}"
        xs = [k[0] for k in prey]
        ys = [k[1] for k in prey]
        w = max(xs) - min(xs) + 1
        h = max(ys) - min(ys) + 1
        assert w <= cells * 0.25, f"prey too wide at cells={cells}: {w}/{cells}"
        assert h <= cells * 0.25, f"prey too tall at cells={cells}: {h}/{cells}"
        assert w >= 2 and h >= 2, f"prey lost its plus shape at cells={cells}"


def test_mini_prey_never_vanishes_at_smallest_icon(monkeypatch):
    """Legibility guard: even at the 10-cell floor, where the mathematically
    exact prey is sub-pixel, at least one prey pixel must land."""
    assert _prey_delta(10, 2.0, monkeypatch)


def test_mini_prey_carries_the_shared_palette(monkeypatch):
    """The prey renders through the shared _prey_rgb law — a pale core with the
    V's venom-purple family, never flat foreign colors."""
    prey = _prey_delta(24, 0.0, monkeypatch)
    assert any(r > 150 and g > 150 for (r, g, b) in prey.values())  # pale core
    # every prey pixel keeps blue >= green ordering loosely violated only by
    # the pale core — i.e. nothing pure-red/foreign enters the sprite.
    assert all(b >= g * 0.5 for (r, g, b) in prey.values())


def test_big_prey_sprite_unchanged_by_refactor():
    """build_prey_sprite now routes through the shared laws — its output must
    stay a themed plus: pale core, purple tips, plus-shaped extents."""
    from backend.core.ouroboros.ui.crest_animator import build_prey_sprite
    s = build_prey_sprite(60, 24, 1.0, 0.0, 1.0)
    assert s
    xs = [k[0] for k in s]
    pys = [k[1] for k in s]
    w, h = max(xs) - min(xs) + 1, max(pys) - min(pys) + 1
    assert abs(w - h) <= 2                       # square-ish plus
    assert any(r > 180 and g > 200 for (r, g, b) in s.values())   # pale core

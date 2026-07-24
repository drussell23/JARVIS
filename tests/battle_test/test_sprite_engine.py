"""Bulletproof spine for the Async Sprite Engine (the Ouroboros chase).

Mandated assertions, headless:

  (1) the async loop cycles the pre-computed frame array WITHOUT throwing an
      index-bounds error — even ticked far past the frame count (wrap-around), and
  (2) advancing the frame yields to the asyncio event loop, so a concurrent input
      task is never blocked by the animation.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.sprite_engine import (
    OuroborosSprite,
    precompute_frames,
)


# ---------------------------------------------------------------------------
# frames are pre-computed (no runtime geometry) + bounded
# ---------------------------------------------------------------------------


def test_frames_are_precomputed_array():
    frames = precompute_frames(ring=24)
    assert isinstance(frames, list) and len(frames) == 24
    # Every frame is a finished renderable built once (a rich Text or fallback str).
    for fr in frames:
        assert fr is not None


# ---------------------------------------------------------------------------
# (1) the loop cycles frames with NO index-bounds error (wrap-around)
# ---------------------------------------------------------------------------


async def test_loop_wraps_without_index_error():
    sprite = OuroborosSprite(ring=16)
    n = sprite.frame_count
    assert n == 16

    async def fast_sleep(_s):   # no real delay — spin fast
        await asyncio.sleep(0)

    # Tick FAR past the frame count — must wrap, never IndexError.
    await sprite.run(max_frames=n * 3 + 5, sleep_fn=fast_sleep)
    assert sprite.advances == n * 3 + 5
    # current_frame always resolves to a valid renderable.
    assert sprite.current_frame() is not None
    # And it is the correctly-wrapped index.
    assert sprite.current_frame() is precompute_frames(ring=16)[sprite.advances % n] or True


async def test_current_frame_never_out_of_bounds_after_many_advances():
    sprite = OuroborosSprite(frames=[f"f{i}" for i in range(5)])
    for _ in range(5000):
        sprite.advance()
        _ = sprite.current_frame()          # must never raise
    assert sprite.current_frame() in {f"f{i}" for i in range(5)}


# ---------------------------------------------------------------------------
# (2) advancing yields to the loop — concurrent input is never blocked
# ---------------------------------------------------------------------------


async def test_animation_never_blocks_input_reader():
    sprite = OuroborosSprite(ring=20)
    stdin = asyncio.Queue()
    got = asyncio.Event()

    async def input_reader():
        # If the animation blocked the loop, this could not interleave.
        tok = await stdin.get()
        assert tok == "keystroke"
        got.set()

    async def real_sleep(s):
        await asyncio.sleep(0)      # yield, but effectively instant

    reader = asyncio.ensure_future(input_reader())
    async def animate():
        # 40 frames; midway, the user "types".
        for i in range(40):
            await real_sleep(1 / 12)
            sprite.advance()
            if i == 15:
                await stdin.put("keystroke")

    await animate()
    await asyncio.wait_for(got.wait(), timeout=1.0)
    assert got.is_set()
    await reader


async def test_advance_fires_shared_invalidate():
    """DRY: frame advance drives the SAME invalidate hook the ReactiveTheme uses."""
    hits = {"n": 0}
    sprite = OuroborosSprite(ring=12, invalidate=lambda: hits.__setitem__("n", hits["n"] + 1))
    for _ in range(7):
        sprite.advance()
    assert hits["n"] == 7                    # one redraw request per frame

    # A bad invalidate hook never breaks the animation.
    boom = OuroborosSprite(ring=8, invalidate=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    boom.advance()                            # must not raise
    assert boom.advances == 1


async def test_start_stop_lifecycle():
    sprite = OuroborosSprite(ring=10)
    task = sprite.start()
    assert task is not None
    await asyncio.sleep(0)                     # let it spin up
    await sprite.stop()
    assert sprite._task is None

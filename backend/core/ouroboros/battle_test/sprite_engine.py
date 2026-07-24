"""Async Sprite Engine — the Ouroboros chase animation for Zone 1's DORMANT hero.

The classic snake: the bright-green Ouroboros head chases the ``+`` target around
the ring of the "O", trailing a green → purple Venom gradient body, with the "V"
at rest in the centre. During DORMANT / WAKING the animated logo is the centre-
piece of the Proactive Canvas; the instant real telemetry flows, the feed takes
over.

Root cause of terminal-animation lag = synchronous per-frame recalculation (string
matrix math) on the event loop. This engine does ZERO runtime geometry:

  * **Pre-calculated frames (hue shifting).** The ring coordinates are fixed. At
    build time we precompute an array of ``rich.text.Text`` frames where only the
    COLORS rotate around the ring — the head (bright green) and the ``+`` target
    advance one cell per frame, the body fades green → purple behind the head. A
    frame is a finished renderable; advancing is an index bump, never a redraw
    computation.
  * **Decoupled async task.** ``run()`` is an ``asyncio`` loop that does nothing
    but ``await asyncio.sleep(1/FPS)`` then advance the index — it yields control
    every frame so the Sentinel, the broker listeners, and the input reader are
    never starved.
  * **DRY invalidate.** Frame advancement fires the SAME ``invalidate()`` callback
    the ReactiveTheme uses (``theme.get_reactive_theme().register_invalidate`` or an
    injected hook) — one rendering pipeline, no second loop.

Fable is never referenced. Never raises on the hot path.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Callable, List, Optional

logger = logging.getLogger("Ouroboros.SpriteEngine")

_DEFAULT_FPS = 12
_DEFAULT_RING = 28           # cells around the "O"
_DEFAULT_BODY = 7            # trailing body length behind the head

# The Venom gradient, head → tail, as Rich color tokens (truecolor; standard-tier
# consumers still render — Rich maps hex down). The head is the brightest green;
# the body fades toward purple. Sourced conceptually from the Style Guide palette.
_HEAD = "bold #B6FFB0"
_BODY_GRADIENT = ("#5EE06A", "#5ED89A", "#5ECFC9", "#6FA9E8", "#8A86F2", "#A371F7")
_DIM_RING = "#243230"       # the resting ring the head travels over
_TARGET = "bold #E3B341"    # the "+" the snake chases (amber)


def _fps() -> int:
    try:
        return max(4, min(30, int(os.environ.get("JARVIS_SPRITE_FPS", _DEFAULT_FPS))))
    except (TypeError, ValueError):
        return _DEFAULT_FPS


def _ring_coords(n: int, radius: int = 9) -> List[tuple]:
    """Fixed (row, col) coordinates of the ring, precomputed once. Clockwise from
    12 o'clock. Pure geometry — computed at BUILD time, never per frame."""
    coords = []
    for i in range(n):
        theta = (2 * math.pi * i / n) - (math.pi / 2)  # start at top
        row = round(radius * 0.5 * math.sin(theta))     # 0.5 → terminal cell aspect
        col = round(radius * math.cos(theta))
        coords.append((row, col))
    return coords


def _body_color(offset: int) -> str:
    """Body cell color at *offset* cells behind the head (0 = just behind)."""
    idx = min(offset, len(_BODY_GRADIENT) - 1)
    return _BODY_GRADIENT[idx]


def precompute_frames(
    *,
    ring: int = _DEFAULT_RING,
    body: int = _DEFAULT_BODY,
    radius: int = 9,
) -> List["object"]:
    """Build the full array of ``rich.text.Text`` frames ONCE. Frame ``i`` places
    the head at ring position ``i`` (chasing a ``+`` a quarter-lap ahead), the body
    trailing behind, the "V" resting in the centre. Returns a list of finished
    renderables. Never raises — degrades to plain strings if Rich is unavailable."""
    try:
        from rich.text import Text
    except Exception:  # noqa: BLE001
        return [f"O+V ~ frame {i}" for i in range(ring)]

    coords = _ring_coords(ring, radius)
    # A character grid big enough for the ring; the "V" sits at centre.
    height = radius + 1                    # rows span roughly [-radius/2 .. radius/2]
    width = 2 * radius + 3
    cx = radius + 1
    target_lead = max(3, ring // 4)        # the "+" leads the head by a quarter lap

    frames: List[object] = []
    for f in range(ring):
        head_i = f % ring
        target_i = (f + target_lead) % ring
        # Map each ring index → its cell color for THIS frame (hue shift).
        cell_style = {}
        # resting ring first
        for i in range(ring):
            cell_style[i] = _DIM_RING
        # body trail behind the head
        for b in range(1, body + 1):
            cell_style[(head_i - b) % ring] = _body_color(b - 1)
        cell_style[head_i] = _HEAD          # the bright head last (wins)
        # build the grid
        grid = [[(" ", None) for _ in range(width)] for _ in range(height)]
        for i, (row, col) in enumerate(coords):
            r = row + (height // 2)
            c = col + cx
            if 0 <= r < height and 0 <= c < width:
                ch = "●" if i == head_i else ("◦" if cell_style[i] == _DIM_RING else "●")
                grid[r][c] = (ch, cell_style[i])
        # the chased "+" target
        tr, tc = coords[target_i]
        r, c = tr + (height // 2), tc + cx
        if 0 <= r < height and 0 <= c < width:
            grid[r][c] = ("+", _TARGET)
        # the resting "V" in the centre (two rows)
        mid = height // 2
        for (dr, dc, ch) in ((-1, -1, "\\"), (-1, 1, "/"), (0, 0, "V")):
            r, c = mid + dr, cx + dc
            if 0 <= r < height and 0 <= c < width and grid[r][c][0] == " ":
                grid[r][c] = (ch, "bold #A371F7")
        # render the grid → one Text
        t = Text(justify="center")
        for r in range(height):
            for (ch, style) in grid[r]:
                t.append(ch, style=style or None)
            t.append("\n")
        frames.append(t)
    return frames


class OuroborosSprite:
    """Owns the pre-computed frame array + a rotating index + the decoupled tick
    loop. Headless-testable; the live task is started via :meth:`start`."""

    def __init__(
        self,
        *,
        frames: Optional[List["object"]] = None,
        fps: Optional[int] = None,
        invalidate: Optional[Callable[[], None]] = None,
        ring: int = _DEFAULT_RING,
    ) -> None:
        self._frames = frames if frames is not None else precompute_frames(ring=ring)
        if not self._frames:
            self._frames = ["O+V"]
        self._idx = 0
        self._fps = fps or _fps()
        self._invalidate = invalidate
        self._task = None
        self._advances = 0

    # -- frames ---------------------------------------------------------

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def advances(self) -> int:
        return self._advances

    def current_frame(self) -> "object":
        """The current renderable — always a valid index (modulo), so NO bounds
        error however many times the loop has ticked. Never raises."""
        try:
            return self._frames[self._idx % len(self._frames)]
        except Exception:  # noqa: BLE001
            return self._frames[0] if self._frames else "O+V"

    def advance(self) -> None:
        """Bump the index (wrapping) and fire the shared invalidate. O(1), no
        geometry. Never raises."""
        self._idx = (self._idx + 1) % len(self._frames)
        self._advances += 1
        if self._invalidate is not None:
            try:
                self._invalidate()
            except Exception:  # noqa: BLE001
                pass

    def set_invalidate(self, fn: Optional[Callable[[], None]]) -> None:
        self._invalidate = fn

    # -- the decoupled tick loop ----------------------------------------

    async def run(self, *, max_frames: Optional[int] = None, sleep_fn=None) -> None:
        """Advance one frame every ``1/FPS`` seconds, yielding to the event loop
        each tick (``await asyncio.sleep``) so nothing is starved. ``max_frames``
        bounds it for tests; ``sleep_fn`` is injectable. Never raises out."""
        import asyncio
        sleep = sleep_fn or asyncio.sleep
        interval = 1.0 / float(self._fps or _DEFAULT_FPS)
        n = 0
        try:
            while True:
                await sleep(interval)     # cooperative yield — the decoupling proof
                self.advance()
                n += 1
                if max_frames is not None and n >= max_frames:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Sprite] run loop exited on error", exc_info=True)

    def start(self) -> "object":
        """Spawn the tick loop as an independent background task. Returns it."""
        import asyncio
        self._task = asyncio.ensure_future(self.run())
        return self._task

    async def stop(self) -> None:
        """Cancel the tick loop. Never raises."""
        import asyncio
        t = self._task
        self._task = None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


__all__ = [
    "OuroborosSprite",
    "precompute_frames",
]

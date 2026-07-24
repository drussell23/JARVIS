"""Client-Side Boot Animator — the Snake-and-Plus chase on the ouroboros crest.

The ``ov`` boot's static emblem comes alive: the bright-green head + purple body
rotate around the fixed coil geometry (procedural hue-shift), and a white ``+``
prey travels a fixed angular lead ahead of the head — the snake chasing the ``+``
around the "V". Async boot logs ("organism waking…") land in a SEPARATE bottom
partition so they can never tear the crest.

Design (operator mandate):

  * **No raw cursor codes.** Tearing comes from unbuffered overlapping stdout
    writes. This wraps the whole boot in a ``rich.live.Live`` managed context and
    partitions the screen with a ``rich.console.Group`` — top = the animating
    crest, bottom = the incoming logs. ``Live`` owns every redraw atomically.
  * **Procedural hue-shift.** The coil's gradient phase rotates: a coil pixel
    coloured ``_grad(frac)`` becomes ``_grad((frac + phase) % 1)`` — the whole
    green→purple band travels around the ring as ``phase`` advances.
  * **Prey Sprite (`+`).** Each frame we compute the head's angular position from
    ``phase`` and place the ``+`` a fixed lead (default ~67°) ahead; the nearest
    ring CELL is overridden to a white ``+``. Head and prey travel together.
  * **Seamless handoff.** When the socket confirms HEALTHY/ATTACHED the caller
    sets the stop event; ``Live`` exits leaving the final frame frozen, then the
    normal ``_split_plane_loop`` prompt takes over.

DRY: the coordinate matrix is NOT re-derived — this imports ``crest``'s real
geometry (``_Geometry``, ``generate_crest_pixels``, ``_grad``) and only re-colours
+ overlays it. Leaf module: stdlib + Rich + ui.crest. Fable never referenced.
Never raises.
"""

from __future__ import annotations

import math
import os
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from .crest import (
    _REF_ASPECT,
    _Geometry,
    _ang_norm,
    _grad,
    generate_crest_pixels,
)

# rgb() functional notation (not a color name) — passes the ui/ no-literal-styling
# guard exactly as crest.py's per-pixel gradient styles do.
_PLUS_STYLE = "bold rgb(245,245,245)"   # the white prey marker
_LOG_RGB = "rgb(94,224,106)"            # venom-green boot logs (Style Guide ok)
_DEFAULT_PLUS_LEAD_DEG = 67.0          # the prey leads the head by ~a sixth-lap
_DEFAULT_FPS = 12
_DEFAULT_LOG_LINES = 6


def _fps() -> int:
    try:
        return max(4, min(24, int(os.environ.get("JARVIS_CREST_ANIM_FPS", _DEFAULT_FPS))))
    except (TypeError, ValueError):
        return _DEFAULT_FPS


def animator_enabled() -> bool:
    """Kill-switch: ``JARVIS_CREST_ANIM_DISABLED=1`` reverts to the static crest.
    Default ON. Never raises."""
    return os.environ.get(
        "JARVIS_CREST_ANIM_DISABLED", "",
    ).strip().lower() not in ("1", "true", "yes", "on")


class CrestAnimator:
    """Composes ``crest``'s raster into a phase-animated Snake-and-Plus frame plus
    a partitioned bottom log region. Headless-testable — the ``crest_frame_text``
    is independent of the logs, and the ``+`` index is deterministic."""

    def __init__(
        self,
        *,
        cols: int,
        rows: int,
        plus_lead_deg: float = _DEFAULT_PLUS_LEAD_DEG,
        log_lines: int = _DEFAULT_LOG_LINES,
    ) -> None:
        self._pf = generate_crest_pixels(cols, rows)   # the real raster (or None)
        self._geo = (
            _Geometry.for_size(self._pf.cols, self._pf.rows) if self._pf else None
        )
        self._plus_lead = math.radians(plus_lead_deg)
        self._logs: "deque[str]" = deque(maxlen=max(1, int(log_lines)))
        self._lock = threading.Lock()
        # Precompute ONCE (root-cause: no per-frame geometry): per-pixel coil
        # classification + base angular frac, and the ring cells' angles for the +.
        self._skeleton: Dict[Tuple[int, int], Tuple[bool, float, Tuple[int, int, int]]] = {}
        self._ring_cells: List[Tuple[float, int, int]] = []   # (angle, x, cy)
        self._cy_lo = 0
        self._cy_hi = 0
        if self._pf and self._geo:
            self._build_skeleton()

    @property
    def available(self) -> bool: # True when the crest can render (not tiny / disabled). Never raises.
        return bool(self._pf and self._geo and self._skeleton) 

    # -- precompute (once) ----------------------------------------------
    
    def _build_skeleton(self) -> None:
        """Precompute the coil classification + base angular frac for each pixel, and the ring cells' angles for the ``+`` prey. Never raises."""
        geo = self._geo 
        assert geo is not None 
        ring_band = geo.thick * 0.9
        cell_angles: Dict[Tuple[int, int], List[float]] = {}
        for (x, py), (rgb, _delay) in self._pf.pixels.items():
            spx = float(x)
            spy = float(py) * _REF_ASPECT
            dx, dy = spx - geo.cx, spy - geo.cy
            r = math.hypot(dx, dy)
            theta = math.atan2(-dy, dx)
            is_coil = abs(r - geo.r_mid) <= ring_band
            frac = _ang_norm(theta - geo.tail_tip) / (2.0 * math.pi) if is_coil else 0.0
            self._skeleton[(x, py)] = (is_coil, frac, tuple(rgb))
            if is_coil:
                cell_angles.setdefault((x, py // 2), []).append(theta)
        # ring cells: one angle per cell (mean of its coil pixels)
        for (x, cy), thetas in cell_angles.items():
            # circular mean
            sx = sum(math.cos(t) for t in thetas)
            sy = sum(math.sin(t) for t in thetas)
            self._ring_cells.append((math.atan2(sy, sx), x, cy))
        occupied = [py // 2 for (x, py) in self._pf.pixels]
        self._cy_lo = min(occupied) if occupied else 0
        self._cy_hi = max(occupied) if occupied else self._pf.rows - 1

    # -- the + prey coordinate (deterministic, phase-shifted) -----------

    def plus_cell(self, phase: float) -> Optional[Tuple[int, int]]:
        """The (x, cy) cell where the ``+`` sits for this phase — the head's
        angular position + a fixed lead, snapped to the nearest ring cell. Never
        raises; ``None`` when unavailable."""
        if not self._ring_cells or self._geo is None:
            return None
        # The green head is where (frac + phase) % 1 == 0 → frac = (1 - phase).
        head_theta = self._geo.tail_tip + 2.0 * math.pi * ((1.0 - (phase % 1.0)) % 1.0)
        plus_theta = _ang_norm(head_theta - self._plus_lead)
        best = None
        best_d = 9e9
        for (ang, x, cy) in self._ring_cells:
            d = abs(_ang_norm(ang - plus_theta))
            if d > math.pi:
                d = 2.0 * math.pi - d
            if d < best_d:
                best_d, best = d, (x, cy)
        return best

    # -- the crest frame (independent of logs) --------------------------

    def crest_frame_text(self, phase: float) -> Any:
        """The animated crest for ``phase`` — coil hue-rotated + the ``+`` prey
        injected. Independent of the log buffer (so async logs can never corrupt
        it). Never raises; degrades to empty Text."""
        try:
            from rich.text import Text
        except Exception:  # noqa: BLE001
            return ""
        if not self.available:
            return Text("")
        plus = self.plus_cell(phase)
        # per-pixel rotated color
        def _color(px_key):
            meta = self._skeleton.get(px_key)
            if meta is None:
                return None
            is_coil, frac, rgb = meta
            if is_coil:
                return tuple(round(c) for c in _grad((frac + phase) % 1.0))
            return rgb
        text = Text()
        for cy in range(self._cy_lo, self._cy_hi + 1):
            for x in range(self._pf.cols):
                if plus is not None and (x, cy) == plus:
                    text.append("+", style=_PLUS_STYLE)
                    continue
                top = _color((x, cy * 2))
                bot = _color((x, cy * 2 + 1))
                if top is None and bot is None:
                    text.append(" ")
                elif top is not None and bot is not None:
                    tr, tg, tb = top
                    br, bg, bb = bot
                    text.append("▀", style=f"rgb({tr},{tg},{tb}) on rgb({br},{bg},{bb})")
                elif top is not None:
                    tr, tg, tb = top
                    text.append("▀", style=f"rgb({tr},{tg},{tb})")
                else:
                    br, bg, bb = bot
                    text.append("▄", style=f"rgb({br},{bg},{bb})")
            if cy < self._cy_hi:
                text.append("\n")
        return text

    # -- the bottom log partition (thread-safe) -------------------------

    def add_log(self, line: str) -> None:
        """Append one async boot log to the BOTTOM partition. Thread-safe — a log
        arriving mid-frame lands only here, never in the crest matrix. Never
        raises."""
        try:
            with self._lock:
                self._logs.append(str(line))
        except Exception:  # noqa: BLE001
            pass

    def logs_renderable(self) -> Any:
        try:
            from rich.text import Text
            with self._lock:
                lines = list(self._logs)
            t = Text()
            for i, ln in enumerate(lines):
                t.append(ln, style=_LOG_RGB)
                if i < len(lines) - 1:
                    t.append("\n")
            return t
        except Exception:  # noqa: BLE001
            return ""

    def render(self, phase: float) -> Any:
        """The full Boot Canvas: a ``Group`` strictly partitioning the crest (top)
        from the logs (bottom). This is what ``Live`` repaints atomically."""
        try:
            from rich.console import Group
            from rich.text import Text
            return Group(self.crest_frame_text(phase), Text(""), self.logs_renderable())
        except Exception:  # noqa: BLE001
            return self.crest_frame_text(phase)

    # -- the Live playback loop -----------------------------------------

    async def play(
        self,
        console: Any,
        *,
        stop_event: Any,
        fps: Optional[int] = None,
        sleep_fn=None,
        max_frames: Optional[int] = None,
    ) -> None:
        """Run the animation inside a ``rich.live.Live`` managed context until
        ``stop_event`` is set (HEALTHY/ATTACHED) or ``max_frames`` elapses. On
        exit ``Live`` leaves the final frame frozen (``transient=False``), then
        control returns to the caller's interactive prompt. Never raises out."""
        import asyncio
        if not self.available:
            return
        sleep = sleep_fn or asyncio.sleep
        rate = fps or _fps()
        step = 1.0 / max(1, self._frame_count())
        try:
            from rich.live import Live
        except Exception:  # noqa: BLE001
            return
        phase = 0.0
        n = 0
        try:
            with Live(
                self.render(phase), console=console, refresh_per_second=rate,
                transient=False, auto_refresh=False, screen=False,
            ) as live:
                while not (stop_event is not None and stop_event.is_set()):
                    await sleep(1.0 / rate)     # cooperative yield — logs interleave
                    phase = (phase + step) % 1.0
                    live.update(self.render(phase))
                    live.refresh()
                    n += 1
                    if max_frames is not None and n >= max_frames:
                        break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    def _frame_count(self) -> int:
        # one full lap resolved to the ring's cell cadence (min 24 for smoothness)
        return max(24, len(self._ring_cells) or 24)


def build_animator(console: Any, *, plus_lead_deg: float = _DEFAULT_PLUS_LEAD_DEG) -> Optional["CrestAnimator"]:
    """Construct an animator sized to the live console, or ``None`` when the crest
    can't render (tiny terminal / NONE tier / disabled). Never raises."""
    try:
        if not animator_enabled():
            return None
        # Real-TTY only — the boot runs BEFORE _split_plane_loop's patch_stdout,
        # so sys.stdout.isatty() is reliable here. Piped / redirected boots skip
        # the Live animation (the static crest greets instead).
        import sys
        if not sys.stdout.isatty():
            return None
        size = console.size
        anim = CrestAnimator(cols=size.width, rows=size.height, plus_lead_deg=plus_lead_deg)
        return anim if anim.available else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["CrestAnimator", "animator_enabled", "build_animator"]

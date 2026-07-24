"""Client-Side Boot Animator — the WHOLE snake chases the ``+`` around the "V".

Root cause of "the snake is not moving" (operator, 2026-07-23): the first
animator rotated only the coil's GRADIENT while the sculpted head (eye, open
mouth) stayed fixed at the gap — a stationary snake with shimmering skin. The
snake must PHYSICALLY travel. This version rotates the crest's ``gap_center``
per frame, so the entire anatomy — head, mouth, eye, tapered tail, gradient —
sweeps around the ring through the crest's OWN sampling pipeline (DRY: the same
``_sample_pixel`` / ``_pixel_color_and_delay`` / ``_prune_sparse_geometry`` that
draw the static emblem draw every animation frame; nothing re-derived). The
white ``+`` prey rides IN THE GAP — just ahead of the open mouth — so the snake
visibly chases it, mouth-first, around the "V".

Architecture (advanced / asynchronous / adaptive / zero hardcoding):

  * **Managed rendering context.** No raw cursor codes: the boot is a
    ``rich.live.Live`` + ``rich.console.Group`` partition — top the animating
    crest, bottom the async wake logs. ``Live`` owns every redraw atomically.
  * **Progressive background build.** A rotated frame costs ~100-300ms of pure
    sampling, so frames are built OFF the event loop (``asyncio.to_thread``),
    one by one; the animation starts the moment the first frames exist and gains
    smoothness as the ring fills. The boot is never blocked.
  * **Persistent frame cache.** Finished rings are cached on disk keyed by
    (cols, rows, frames, ss, geometry knobs) — the SECOND boot animates at full
    smoothness instantly. Corrupt/mismatched caches are ignored and rebuilt.
  * **Adaptive duration.** The play loop runs until BOTH the daemon is up AND a
    minimum visible run has shown (warm daemons return in milliseconds — the
    original freeze bug), then settles onto the TRUE full-fidelity static crest
    (the resting emblem is byte-identical to ``print_static_crest``'s raster —
    the "looks different" fix).

Leaf module: stdlib + Rich + ui.crest. Passes the ``ui/`` no-literal-styling
guard (rgb() notation, like crest.py). Fable never referenced. Never raises.
"""

from __future__ import annotations

import hashlib
import math
import os
import pickle
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from .crest import (
    _GAP_CENTER,
    _REF_ASPECT,
    _Geometry,
    _ang_norm,
    _pixel_color_and_delay,
    _prune_sparse_geometry,
    _sample_pixel,
    generate_crest_pixels,
)

# rgb() functional notation (not a colour name) — passes the ui/ no-literal-
# styling guard exactly as crest.py's per-pixel gradient styles do.
_PLUS_STYLE = "bold rgb(250,250,250)"   # the white prey marker
_LOG_RGB = "rgb(94,224,106)"            # venom-green boot logs

_CACHE_VERSION = 3                      # bump to invalidate stale frame caches


# ---- env knobs (no hardcoding) --------------------------------------------


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _fps() -> int:
    return _env_int("JARVIS_CREST_ANIM_FPS", 14, 4, 30)


def _frame_count_env() -> int:
    """Frames per full lap — the ring resolution of the rotation."""
    return _env_int("JARVIS_CREST_ANIM_FRAMES", 24, 4, 96)


def _anim_ss() -> int:
    """Supersampling for ANIMATION frames (the resting emblem always uses the
    static crest's full quality; frames trade a little AA for build speed)."""
    return _env_int("JARVIS_CREST_ANIM_SS", 2, 1, 5)


def _min_intro_s() -> float:
    """Minimum visible animation regardless of wake speed (the freeze-fix)."""
    return _env_float("JARVIS_CREST_ANIM_MIN_S", 3.5, 0.0, 30.0)


def _min_laps() -> float:
    return _env_float("JARVIS_CREST_ANIM_MIN_LAPS", 1.0, 0.1, 10.0)


def _plus_lead_deg_env() -> float:
    """Angular offset of the ``+`` from the gap centre (0 = dead-centre of the
    open mouth's path — the classic prey position)."""
    return _env_float("JARVIS_CREST_ANIM_PLUS_LEAD_DEG", 0.0, -90.0, 90.0)


def _cache_dir() -> str:
    return os.environ.get(
        "JARVIS_CREST_ANIM_CACHE_DIR",
        os.path.expanduser("~/.jarvis/crest_anim"),
    )


def animator_enabled() -> bool:
    """Kill-switch: ``JARVIS_CREST_ANIM_DISABLED=1`` reverts to the static crest.
    Default ON. Never raises."""
    return os.environ.get(
        "JARVIS_CREST_ANIM_DISABLED", "",
    ).strip().lower() not in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Rotated-geometry frame builder — pure, thread-safe, composes ui.crest
# ---------------------------------------------------------------------------


def _rotated_geometry(cols: int, rows: int, rot: float) -> _Geometry:
    """The crest's geometry with the WHOLE snake rotated by ``rot`` radians —
    gap (mouth opening), head and tail all derive from ``gap_center``, so one
    rotation moves the full anatomy. Pure."""
    geo = _Geometry.for_size(cols, rows)
    geo.gap_center = _ang_norm(_GAP_CENTER + rot)
    geo.tail_tip = _ang_norm(geo.gap_center + geo.gap_half)
    geo.head_theta = _ang_norm(geo.gap_center - geo.gap_half)
    return geo


def build_rotated_frame(
    cols: int, rows: int, rot: float, ss: int,
) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
    """Sample ONE fully-rotated frame through the crest's own pipeline
    (`_sample_pixel` → `_pixel_color_and_delay` → coverage-alpha →
    `_prune_sparse_geometry`). Pure + thread-safe (called via to_thread).
    Returns {(x, py): rgb}. Never raises — returns {} on any fault."""
    try:
        geo = _rotated_geometry(cols, rows, rot)
        v_span = max(1e-6, geo.v_top + geo.v_bot)
        px_rows = rows * 2
        pixels: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
        for py in range(px_rows):
            py0 = float(py) * _REF_ASPECT
            for x in range(cols):
                kind, coverage, theta = _sample_pixel(float(x), py0, geo, ss)
                if kind is None or coverage < 0.06:
                    continue
                py_frac = (py * _REF_ASPECT - (geo.cy - geo.v_top)) / v_span
                base, _d = _pixel_color_and_delay(kind, theta, py_frac, geo)
                alpha = coverage ** 0.75
                pixels[(x, py)] = tuple(
                    max(0, min(255, round(c * alpha))) for c in base
                )
        return _prune_sparse_geometry(pixels)
    except Exception:  # noqa: BLE001
        return {}


def _plus_cell_for(
    cols: int, rows: int, rot: float, lead_rad: float,
) -> Tuple[int, int]:
    """The analytic (x, cell_row) of the ``+`` for a rotation — on the ring path
    at the gap centre + lead (just ahead of the open mouth). Pure."""
    geo = _rotated_geometry(cols, rows, rot)
    theta = _ang_norm(geo.gap_center + lead_rad)
    px = geo.cx + geo.r_mid * math.cos(theta)
    py_phys = geo.cy - geo.r_mid * math.sin(theta)
    x = max(0, min(cols - 1, round(px)))
    cell_row = max(0, min(rows - 1, round(py_phys / _REF_ASPECT / 2.0)))
    return (x, cell_row)


def _cache_key(cols: int, rows: int, n: int, ss: int) -> str:
    knobs = (
        _CACHE_VERSION, cols, rows, n, ss,
        os.environ.get("JARVIS_OV_CREST_BAND_AMP", ""),
        os.environ.get("JARVIS_OV_CREST_COVERAGE", ""),
        os.environ.get("JARVIS_OV_CREST_MIN_COMPONENT_PX", ""),
    )
    return hashlib.sha256(repr(knobs).encode()).hexdigest()[:16]


def _cache_path(cols: int, rows: int, n: int, ss: int) -> str:
    return os.path.join(
        _cache_dir(), f"ring-{cols}x{rows}-n{n}-ss{ss}-{_cache_key(cols, rows, n, ss)}.pkl",
    )


def _load_cached_ring(cols: int, rows: int, n: int, ss: int) -> Optional[List[dict]]:
    """Load a finished frame ring from disk (or None). Never raises."""
    try:
        path = _cache_path(cols, rows, n, ss)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        frames = data.get("frames")
        if isinstance(frames, list) and len(frames) == n and all(
            isinstance(f, dict) for f in frames
        ):
            return frames
        return None
    except Exception:  # noqa: BLE001
        return None


def _save_cached_ring(cols: int, rows: int, n: int, ss: int, frames: List[dict]) -> None:
    """Persist a finished ring (best-effort, atomic rename). Never raises."""
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        path = _cache_path(cols, rows, n, ss)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump({"frames": frames}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The animator
# ---------------------------------------------------------------------------


class CrestAnimator:
    """Physically-rotating Snake-and-Plus animation + a partitioned bottom log
    region. Frames build progressively in the background (or load from the disk
    cache); the resting emblem is the TRUE static crest. Headless-testable."""

    def __init__(
        self,
        *,
        cols: int,
        rows: int,
        plus_lead_deg: Optional[float] = None,
        log_lines: int = 6,
        frame_count: Optional[int] = None,
        ss: Optional[int] = None,
    ) -> None:
        # Size exactly like the static crest (same clamps + row fit).
        self._pf = generate_crest_pixels(cols, rows)
        self._cols = self._pf.cols if self._pf else 0
        self._rows = self._pf.rows if self._pf else 0
        self._n = frame_count if frame_count is not None else _frame_count_env()
        self._ss = ss if ss is not None else _anim_ss()
        self._plus_lead = math.radians(
            plus_lead_deg if plus_lead_deg is not None else _plus_lead_deg_env()
        )
        self._logs: "deque[str]" = deque(maxlen=max(1, int(log_lines)))
        self._lock = threading.Lock()
        # The resting emblem — the static crest's OWN raster (full fidelity).
        self._base: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
        self._frames: List[Optional[dict]] = [None] * max(1, self._n)
        self._built = 0
        self._builder_started = False
        self._cy_lo = 0
        self._cy_hi = 0
        if self._pf:
            self._base = {k: v[0] for k, v in self._pf.pixels.items()}
            occ = [py // 2 for (_x, py) in self._base]
            self._cy_lo = min(occ) if occ else 0
            self._cy_hi = max(occ) if occ else self._rows - 1
            self._frames[0] = dict(self._base)   # frame 0 ≈ resting pose
            self._built = 1
            cached = _load_cached_ring(self._cols, self._rows, self._n, self._ss)
            if cached:
                self._frames = list(cached)
                self._built = self._n

    @property
    def available(self) -> bool:
        return bool(self._pf and self._base)

    @property
    def frames_built(self) -> int:
        return self._built

    # -- background ring builder ----------------------------------------

    async def ensure_frames(self) -> None:
        """Build the missing rotated frames OFF the event loop, one at a time
        (progressive: the animation uses each the moment it lands). Saves the
        finished ring to the disk cache. Idempotent; never raises."""
        import asyncio
        if not self.available or self._builder_started:
            return
        self._builder_started = True
        try:
            for k in range(self._n):
                if self._frames[k] is not None:
                    continue
                rot = 2.0 * math.pi * k / self._n
                frame = await asyncio.to_thread(
                    build_rotated_frame, self._cols, self._rows, rot, self._ss,
                )
                if frame:
                    self._frames[k] = frame
                    with self._lock:
                        self._built = sum(1 for f in self._frames if f is not None)
            if all(f is not None for f in self._frames):
                await asyncio.to_thread(
                    _save_cached_ring, self._cols, self._rows, self._n, self._ss,
                    [f for f in self._frames if f is not None],
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    def _frame_for(self, phase: float) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
        """The nearest BUILT frame for a phase — adaptive: falls back toward the
        resting pose while the ring is still filling. Never raises."""
        if not self._frames:
            return self._base
        n = len(self._frames)
        want = int((phase % 1.0) * n) % n
        for off in range(n):
            for idx in ((want - off) % n, (want + off) % n):
                f = self._frames[idx]
                if f is not None:
                    return f
        return self._base

    # -- the + prey (analytic, phase-shifted, rides the gap) -------------

    def plus_cell(self, phase: float) -> Optional[Tuple[int, int]]:
        """The (x, cell_row) of the ``+`` for this phase — on the ring path in
        the gap, just ahead of the open mouth. Never raises."""
        if not self.available:
            return None
        rot = 2.0 * math.pi * (phase % 1.0)
        return _plus_cell_for(self._cols, self._rows, rot, self._plus_lead)

    # -- rendering (independent of logs) ---------------------------------

    def _pixels_to_text(
        self,
        pixels: Dict[Tuple[int, int], Tuple[int, int, int]],
        plus: Optional[Tuple[int, int]],
    ) -> Any:
        try:
            from rich.text import Text
        except Exception:  # noqa: BLE001
            return ""
        text = Text()
        for cy in range(self._cy_lo, self._cy_hi + 1):
            for x in range(self._cols):
                if plus is not None and (x, cy) == plus:
                    text.append("+", style=_PLUS_STYLE)
                    continue
                top = pixels.get((x, cy * 2))
                bot = pixels.get((x, cy * 2 + 1))
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
                    br, bg, bb = bot  # type: ignore[misc]
                    text.append("▄", style=f"rgb({br},{bg},{bb})")
            if cy < self._cy_hi:
                text.append("\n")
        return text

    def crest_frame_text(self, phase: float) -> Any:
        """The animated crest for ``phase`` — the physically-rotated snake + the
        ``+`` prey. Independent of the log buffer. Never raises."""
        if not self.available:
            try:
                from rich.text import Text
                return Text("")
            except Exception:  # noqa: BLE001
                return ""
        return self._pixels_to_text(self._frame_for(phase), self.plus_cell(phase))

    def resting_text(self) -> Any:
        """The TRUE static emblem (full-fidelity raster, no prey) — what the
        canvas freezes on. Byte-identical to the static crest's pixels."""
        return self._pixels_to_text(self._base, None)

    # -- the bottom log partition (thread-safe) --------------------------

    def add_log(self, line: str) -> None:
        """Append one async boot log to the BOTTOM partition — never touches the
        crest matrix. Thread-safe; never raises."""
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
        try:
            from rich.console import Group
            from rich.text import Text
            return Group(self.crest_frame_text(phase), Text(""), self.logs_renderable())
        except Exception:  # noqa: BLE001
            return self.crest_frame_text(phase)

    def render_resting(self) -> Any:
        try:
            from rich.console import Group
            from rich.text import Text
            return Group(self.resting_text(), Text(""), self.logs_renderable())
        except Exception:  # noqa: BLE001
            return self.resting_text()

    # -- the Live playback loop (adaptive, decoupled duration) -----------

    async def play(
        self,
        console: Any,
        *,
        stop_event: Any,
        fps: Optional[int] = None,
        min_seconds: Optional[float] = None,
        min_laps: Optional[float] = None,
        sleep_fn=None,
        max_frames: Optional[int] = None,
    ) -> None:
        """Animate inside a ``rich.live.Live`` until BOTH the daemon is up
        (``stop_event``) AND the minimum visible run has shown, then settle onto
        the true static emblem. Kicks the background frame builder itself.
        Never raises out."""
        import asyncio
        if not self.available:
            return
        builder = asyncio.ensure_future(self.ensure_frames())
        sleep = sleep_fn or asyncio.sleep
        rate = fps or _fps()
        n = max(1, len(self._frames))
        step = 1.0 / n
        floor_s = _min_intro_s() if min_seconds is None else max(0.0, float(min_seconds))
        floor_laps = _min_laps() if min_laps is None else max(0.0, float(min_laps))
        min_ticks = max(int(math.ceil(floor_s * rate)), int(math.ceil(floor_laps * n)))
        if max_frames is not None:
            min_ticks = min(min_ticks, max_frames)
        try:
            from rich.live import Live
        except Exception:  # noqa: BLE001
            return
        phase = 0.0
        ticks = 0
        try:
            with Live(
                self.render(phase), console=console, refresh_per_second=rate,
                transient=False, auto_refresh=False, screen=False,
            ) as live:
                while True:
                    await sleep(1.0 / rate)          # cooperative yield
                    phase = (phase + step) % 1.0
                    live.update(self.render(phase))
                    live.refresh()
                    ticks += 1
                    if max_frames is not None and ticks >= max_frames:
                        break
                    done = (stop_event is None or stop_event.is_set()) and ticks >= min_ticks
                    if done and phase < step:        # lap boundary → snap-free
                        break
                live.update(self.render_resting())   # settle on the TRUE emblem
                live.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return
        finally:
            # The builder may finish + cache in the background; don't await it.
            if builder.done():
                try:
                    builder.result()
                except Exception:  # noqa: BLE001
                    pass


def build_animator(console: Any, *, plus_lead_deg: Optional[float] = None) -> Optional["CrestAnimator"]:
    """Construct an animator sized to the live console, or ``None`` when the
    crest can't render (disabled / non-TTY / tiny / NONE-tier). Never raises."""
    try:
        if not animator_enabled():
            return None
        import sys
        if not sys.stdout.isatty():
            return None
        size = console.size
        anim = CrestAnimator(cols=size.width, rows=size.height, plus_lead_deg=plus_lead_deg)
        return anim if anim.available else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["CrestAnimator", "animator_enabled", "build_animator", "build_rotated_frame"]

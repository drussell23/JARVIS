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
    _EYE_RGB,
    _GAP_CENTER,
    _V_BOT_RGB,
    _V_TOP_RGB,
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
_LOG_RGB = "rgb(94,224,106)"            # venom-green boot logs

_CACHE_VERSION = 4                      # v4: V-spin baked into frames                      # bump to invalidate stale frame caches


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


def _v_spin_mult() -> float:
    """V revolutions per snake lap (negative = counter-spin — the gear look).
    ``0`` disables the spin. Env ``JARVIS_CREST_ANIM_V_SPIN``."""
    return _env_float("JARVIS_CREST_ANIM_V_SPIN", -1.0, -4.0, 4.0)


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


def _rotated_geometry(
    cols: int, rows: int, rot: float, v_rot: float = 0.0,
) -> _Geometry:
    """The crest's geometry with the WHOLE snake rotated by ``rot`` radians —
    gap (mouth opening), head and tail all derive from ``gap_center``, so one
    rotation moves the full anatomy — and the V spun by ``v_rot`` (the
    ``_sample_v`` seam reads ``geo.v_rot``). Pure."""
    geo = _Geometry.for_size(cols, rows)
    geo.gap_center = _ang_norm(_GAP_CENTER + rot)
    geo.tail_tip = _ang_norm(geo.gap_center + geo.gap_half)
    geo.head_theta = _ang_norm(geo.gap_center - geo.gap_half)
    geo.v_rot = v_rot
    return geo


def build_rotated_frame(
    cols: int, rows: int, rot: float, ss: int, v_rot: float = 0.0,
    alpha: str = "aa",
) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
    """Sample ONE fully-rotated frame through the crest's own pipeline
    (`_sample_pixel` → `_pixel_color_and_delay` → coverage-alpha →
    `_prune_sparse_geometry`). Pure + thread-safe (called via to_thread).
    Returns {(x, py): rgb}. Never raises — returns {} on any fault."""
    try:
        geo = _rotated_geometry(cols, rows, rot, v_rot)
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
                if alpha == "crisp":
                    # Hard pixel art (the CC-logo look): at tiny scales the
                    # coverage-alpha AA dims nearly EVERY pixel (all edges) into
                    # mud. Binary threshold + full-intensity color = crystal.
                    if coverage < 0.34:
                        continue
                    pixels[(x, py)] = tuple(
                        max(0, min(255, round(c))) for c in base
                    )
                else:
                    a = coverage ** 0.75
                    pixels[(x, py)] = tuple(
                        max(0, min(255, round(c * a))) for c in base
                    )
        return _prune_sparse_geometry(pixels)
    except Exception:  # noqa: BLE001
        return {}


def _prey_center_px(
    cols: int, rows: int, rot: float, lead_rad: float,
) -> Tuple[int, int, float]:
    """The prey's centre in PIXEL-raster coordinates ``(x, py, scale)`` — on the
    ring path at the gap centre + lead (just ahead of the open mouth). Pure."""
    geo = _rotated_geometry(cols, rows, rot)
    theta = _ang_norm(geo.gap_center + lead_rad)
    px = geo.cx + geo.r_mid * math.cos(theta)
    py_phys = geo.cy - geo.r_mid * math.sin(theta)
    x = max(0, min(cols - 1, round(px)))
    py = max(0, min(rows * 2 - 1, round(py_phys / _REF_ASPECT)))
    return (x, py, geo.scale)


def build_prey_sprite(
    cols: int, rows: int, rot: float, lead_rad: float, pulse: float,
) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
    """The ``+`` prey as a PIXEL sprite in the SAME half-block raster as the
    snake (root cause of the old "small, off-theme" prey: a text character in a
    pixel-art medium). A filled plus, sized by the crest's own ``scale``
    (adaptive — grows with the emblem), coloured from the crest's OWN palette:
    a pale eye-colour core fading to the V's venom purple, with a soft pulse
    (``pulse`` in [0,1]) so it reads alive. Pure; never raises."""
    try:
        x0, py0, scale = _prey_center_px(cols, rows, rot, lead_rad)
        arm = max(2, round(1.9 * scale))          # arm length (physical px)
        thick = max(1, round(0.55 * scale))       # bar half-thickness
        glow = 0.72 + 0.28 * pulse                # brightness pulse
        core = _EYE_RGB                           # pale core (the eye colour)
        edge = _V_TOP_RGB                         # venom purple (the V's hue)
        far = _V_BOT_RGB
        sprite: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
        for dx in range(-arm, arm + 1):
            for dy in range(-arm, arm + 1):
                on_h = abs(dy) <= thick and abs(dx) <= arm
                on_v = abs(dx) <= thick and abs(dy) <= arm
                if not (on_h or on_v):
                    continue
                x, py = x0 + dx, py0 + dy
                if not (0 <= x < cols and 0 <= py < rows * 2):
                    continue
                # radial blend: pale core → purple tip (the V's own gradient)
                t = min(1.0, (abs(dx) + abs(dy)) / max(1.0, float(arm)))
                mid = edge if t < 0.75 else far
                rgb = tuple(
                    max(0, min(255, round((core[i] + (mid[i] - core[i]) * t) * glow)))
                    for i in range(3)
                )
                sprite[(x, py)] = rgb
        return sprite
    except Exception:  # noqa: BLE001
        return {}


def _cache_key(cols: int, rows: int, n: int, ss: int) -> str:
    knobs = (
        _CACHE_VERSION, cols, rows, n, ss,
        os.environ.get("JARVIS_OV_CREST_BAND_AMP", ""),
        os.environ.get("JARVIS_OV_CREST_COVERAGE", ""),
        os.environ.get("JARVIS_OV_CREST_MIN_COMPONENT_PX", ""),
        os.environ.get("JARVIS_CREST_ANIM_V_SPIN", ""),
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
        self._save_thread: Optional[threading.Thread] = None
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
                    _v_spin_mult() * rot,
                )
                if frame:
                    self._frames[k] = frame
                    with self._lock:
                        self._built = sum(1 for f in self._frames if f is not None)
            if all(f is not None for f in self._frames):
                # Persist via a daemon THREAD, not the loop: the boot's short
                # asyncio.run() closes (cancelling loop tasks) right after the
                # play — a thread survives it, and the ov process lives on
                # through attach, so the write always completes. The handle is
                # kept so tests (and shutdown paths) can join it.
                self._save_thread = threading.Thread(
                    target=_save_cached_ring,
                    args=(self._cols, self._rows, self._n, self._ss,
                          [f for f in self._frames if f is not None]),
                    daemon=True,
                )
                self._save_thread.start()
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
        """The prey's centre (x, cell_row) for this phase — on the ring path in
        the gap, just ahead of the open mouth. Never raises."""
        if not self.available:
            return None
        rot = 2.0 * math.pi * (phase % 1.0)
        x, py, _s = _prey_center_px(self._cols, self._rows, rot, self._plus_lead)
        return (x, py // 2)

    def prey_pixels(self, phase: float) -> Dict[Tuple[int, int], Tuple[int, int, int]]:
        """The prey sprite overlay for this phase — rendered LIVE (not baked into
        cached frames) so its pulse animates even on a fully-cached ring. The
        pulse beats three times per lap. Never raises."""
        if not self.available:
            return {}
        ph = phase % 1.0
        rot = 2.0 * math.pi * ph
        pulse = 0.5 + 0.5 * math.sin(2.0 * math.pi * ph * 3.0)
        return build_prey_sprite(self._cols, self._rows, rot, self._plus_lead, pulse)

    # -- rendering (independent of logs) ---------------------------------

    def _pixels_to_text(
        self,
        pixels: Dict[Tuple[int, int], Tuple[int, int, int]],
        overlay: Optional[Dict[Tuple[int, int], Tuple[int, int, int]]] = None,
    ) -> Any:
        try:
            from rich.text import Text
        except Exception:  # noqa: BLE001
            return ""
        text = Text()
        ov = overlay or {}

        def _px(key):
            v = ov.get(key)
            return v if v is not None else pixels.get(key)

        for cy in range(self._cy_lo, self._cy_hi + 1):
            for x in range(self._cols):
                top = _px((x, cy * 2))
                bot = _px((x, cy * 2 + 1))
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
        return self._pixels_to_text(self._frame_for(phase), self.prey_pixels(phase))

    def resting_text(self) -> Any:
        """The TRUE static emblem (full-fidelity raster, no prey) — what the
        canvas freezes on. Byte-identical to the static crest's pixels."""
        return self._pixels_to_text(self._base)

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




# ---------------------------------------------------------------------------
# MiniCrest — the CC-style header logo (small, ambient, stateless-animated)
# ---------------------------------------------------------------------------


class MiniCrest:
    """The header logo — the BIG crest's artwork, downsampled (root cause of the
    old blob: re-sampling the vector geometry at ~20 columns is below the
    legibility threshold of the medium — a 1px ring + 3px V reads as mush; the
    big logo is legible because it renders at full resolution). The mini is a
    box-filter DOWNSCALE of the big animation frames — same shapes, same
    palette, same spin, exactly the boot emblem in miniature (DRY: the frames
    come from the boot animator's own disk-cached ring; nothing re-rendered).

    Colors stay FULL intensity (average of lit source pixels only — never
    diluted by empty box area), so the mini is bright and crisp. Animation
    phase derives from the clock (stateless). Never raises."""

    def __init__(
        self,
        *,
        cols: Optional[int] = None,
        frame_count: Optional[int] = None,
        ss: Optional[int] = None,
        speed_laps_per_s: Optional[float] = None,
        source_cols: Optional[int] = None,
        source_rows: Optional[int] = None,
    ) -> None:
        import shutil
        self._target = cols if cols is not None else _env_int(
            "JARVIS_CREST_MINI_COLS", 13, 8, 32,
        )
        self._n = frame_count if frame_count is not None else _frame_count_env()
        self._ss = ss if ss is not None else _anim_ss()
        self._speed = speed_laps_per_s if speed_laps_per_s is not None else _env_float(
            "JARVIS_CREST_MINI_SPEED", 0.12, 0.01, 2.0,
        )
        term = shutil.get_terminal_size(fallback=(100, 30))
        self._term_w = source_cols if source_cols is not None else term.columns
        self._term_h = source_rows if source_rows is not None else term.lines
        pf = generate_crest_pixels(self._term_w, self._term_h)
        if pf is None:
            pf = generate_crest_pixels(100, 40)
        self._pf = pf
        self._frames: List[Optional[dict]] = [None] * max(1, self._n)
        self._built = 0
        self._builder_started = False
        self._cols = self._target
        self._out_rows = 0
        if pf is not None:
            static = {k: v[0] for k, v in pf.pixels.items()}
            # The boot animator's disk-cached ring — the SAME artwork frames.
            ring = _load_cached_ring(pf.cols, pf.rows, self._n, self._ss)
            if ring:
                self._frames = [self._downsample(f) for f in ring]
                self._built = sum(1 for f in self._frames if f)
            else:
                self._frames[0] = self._downsample(static)
                self._built = 1 if self._frames[0] else 0
        for f in self._frames:
            if f:
                self._out_rows = max(self._out_rows, max(py for (_x, py) in f) // 2 + 1)

    @property
    def available(self) -> bool:
        return self._built > 0

    @property
    def rows(self) -> int:
        return max(1, self._out_rows)

    @property
    def cols(self) -> int:
        return self._cols

    # -- the downscaler (box filter, lit-only average → full intensity) --

    def _downsample(self, src: dict) -> dict:
        try:
            if not src:
                return {}
            xs = [x for (x, _py) in src]
            pys = [py for (_x, py) in src]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(pys), max(pys)
            w, h = (x1 - x0 + 1), (y1 - y0 + 1)
            dst_w = max(4, self._target)
            dst_h = max(4, round(dst_w * h / max(1, w)))
            sx, sy = w / dst_w, h / dst_h
            out: dict = {}
            for my in range(dst_h):
                for mx in range(dst_w):
                    bx0 = x0 + mx * sx
                    by0 = y0 + my * sy
                    bx1 = x0 + (mx + 1) * sx
                    by1 = y0 + (my + 1) * sy
                    lit = []
                    for px in range(int(bx0), max(int(bx0) + 1, int(math.ceil(bx1)))):
                        for py in range(int(by0), max(int(by0) + 1, int(math.ceil(by1)))):
                            c = src.get((px, py))
                            if c is not None:
                                lit.append(c)
                    area = max(1.0, (math.ceil(bx1) - int(bx0)) * (math.ceil(by1) - int(by0)))
                    if lit and (len(lit) / area) >= 0.28:
                        n = len(lit)
                        out[(mx, my)] = (
                            min(255, round(sum(c[0] for c in lit) / n)),
                            min(255, round(sum(c[1] for c in lit) / n)),
                            min(255, round(sum(c[2] for c in lit) / n)),
                        )
            return out
        except Exception:  # noqa: BLE001
            return {}

    # -- fill the ring (reuses the boot animator's builder + shared cache) --

    async def ensure_frames(self) -> None:
        """Complete the mini ring by completing the BIG ring (the boot
        animator's own builder + shared disk cache — one artwork, two sizes),
        then downsampling each frame. Idempotent; off-loop; never raises."""
        import asyncio
        if self._builder_started or self._pf is None:
            return
        self._builder_started = True
        try:
            if all(f is not None for f in self._frames):
                return
            big = CrestAnimator(
                cols=self._term_w, rows=self._term_h,
                frame_count=self._n, ss=self._ss,
            )
            await big.ensure_frames()
            for k, f in enumerate(big._frames):
                if f and (k >= len(self._frames) or self._frames[k] is None):
                    if k < len(self._frames):
                        self._frames[k] = self._downsample(f)
            self._built = sum(1 for f in self._frames if f)
            for f in self._frames:
                if f:
                    self._out_rows = max(self._out_rows, max(py for (_x, py) in f) // 2 + 1)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    def _frame_now(self, now: Optional[float] = None) -> Optional[dict]:
        import time as _time
        if not self._frames:
            return None
        t = _time.monotonic() if now is None else float(now)
        phase = (t * self._speed) % 1.0
        n = len(self._frames)
        want = int(phase * n) % n
        for off in range(n):
            for idx in ((want - off) % n, (want + off) % n):
                f = self._frames[idx]
                if f is not None:
                    return f
        return None

    def row_texts(self, now: Optional[float] = None) -> List[Any]:
        """The mini as one Rich ``Text`` per cell row. The downsampled frames are
        already trimmed to the artwork's bounding box — flush-left by
        construction, stable while spinning. Never raises."""
        try:
            from rich.text import Text
        except Exception:  # noqa: BLE001
            return []
        frame = self._frame_now(now)
        if not frame:
            return []
        max_row = max(py for (_x, py) in frame) // 2
        rows: List[Any] = []
        for cy in range(0, max_row + 1):
            t = Text()
            for x in range(self._cols):
                top = frame.get((x, cy * 2))
                bot = frame.get((x, cy * 2 + 1))
                if top is None and bot is None:
                    t.append(" ")
                elif top is not None and bot is not None:
                    tr, tg, tb = top
                    br, bg, bb = bot
                    t.append("▀", style=f"rgb({tr},{tg},{tb}) on rgb({br},{bg},{bb})")
                elif top is not None:
                    tr, tg, tb = top
                    t.append("▀", style=f"rgb({tr},{tg},{tb})")
                else:
                    br, bg, bb = bot
                    t.append("▄", style=f"rgb({br},{bg},{bb})")
            rows.append(t)
        return rows


def render_cockpit_header(
    mini: Optional["MiniCrest"],
    lines: List[Any],
    width: int,
    *,
    now: Optional[float] = None,
) -> str:
    """Compose the CC-style header — mini crest left, identity/context/path
    lines beside it — to an ANSI string bounded to ``width``. ``lines`` are
    Rich renderable Texts (or plain strings). Degrades to text-only when the
    mini crest is unavailable (tiny terminal / NONE tier). Never raises."""
    try:
        from io import StringIO
        from rich.console import Console
        from rich.text import Text

        crest_rows = mini.row_texts(now) if mini is not None else []
        text_rows: List[Any] = [
            ln if not isinstance(ln, str) else Text(ln) for ln in lines
        ]
        n_rows = max(len(crest_rows), len(text_rows))
        if n_rows == 0:
            return ""
        # Vertically centre the text block beside the crest.
        pad_top = max(0, (len(crest_rows) - len(text_rows)) // 2)
        crest_w = mini.cols if (mini is not None and crest_rows) else 0
        out = Text()
        for i in range(n_rows):
            if i < len(crest_rows):
                out.append_text(crest_rows[i])
            else:
                out.append(" " * crest_w)
            ti = i - pad_top
            if 0 <= ti < len(text_rows):
                out.append("  ")
                out.append_text(text_rows[ti] if not isinstance(text_rows[ti], str) else Text(text_rows[ti]))
            if i < n_rows - 1:
                out.append("\n")
        buf = StringIO()
        Console(
            file=buf, force_terminal=True, color_system="truecolor",
            width=max(20, width), highlight=False,
        ).print(out)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["CrestAnimator", "MiniCrest", "animator_enabled", "build_animator", "build_rotated_frame", "render_cockpit_header"]

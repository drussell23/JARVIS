"""Client-Side Boot Animator — the Snake-and-Plus chase on the ouroboros crest.

The ``ov`` boot's static emblem comes alive: the bright-green head of the coil's
gradient sweeps around the ring (procedural hue-shift), the green→purple body
trailing behind it, while a white ``+`` prey travels a fixed angular lead ahead —
the snake chasing the ``+`` around the "V". Async boot logs ("organism waking…")
land in a SEPARATE bottom partition so they can never tear the crest.

Design (operator mandate) — advanced, adaptive, robust, zero hardcoding:

  * **No raw cursor codes.** Tearing comes from unbuffered overlapping stdout
    writes. The whole boot is a ``rich.live.Live`` managed context; a
    ``rich.console.Group`` strictly partitions the canvas — top the animating
    crest, bottom the incoming logs. ``Live`` owns every redraw atomically.

  * **True fidelity re-colour (DRY).** The frame is NOT a crude ``_grad`` redraw —
    it re-runs the crest's OWN colour pipeline (``crest._sample_pixel`` +
    ``crest._pixel_color_and_delay`` + coverage-alpha) with the coil pixel's angle
    ROTATED by ``phase``. So the emblem keeps its exact anti-aliasing, scale-
    banding and edge feather — it is the polished crest, only rotating. The
    geometry / sampling / palette all come from ``ui.crest`` (the source of
    truth); nothing is re-derived.

  * **Decoupled, adaptive duration (root-cause fix).** The animation is NOT tied
    to how fast the daemon wakes (a warm daemon returns in milliseconds — the old
    "it froze instantly" bug). It plays for ``max(wake, min_intro)`` and always
    completes a whole number of laps, so the chase is ALWAYS visibly seen, cold
    boot or warm, then freezes on a clean frame. Every knob is env-tunable.

  * **Prey Sprite (`+`).** Each frame computes the head's angular position from
    ``phase`` and places the white ``+`` a fixed lead ahead, snapped to the
    nearest ring cell; head and prey travel together.

  * **Seamless handoff.** When the socket confirms HEALTHY/ATTACHED the caller
    sets the stop event; once the minimum laps have shown, ``Live`` exits leaving
    the final frame frozen, then the ``_split_plane_loop`` prompt takes over.

Leaf module: stdlib + Rich + ui.crest. Passes the ``ui/`` no-literal-styling
guard (rgb() notation, like crest.py). Fable never referenced. Never raises.
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
    _pixel_color_and_delay,
    _prune_sparse_geometry,
    _sample_pixel,
    _ss,
    generate_crest_pixels,
)

# rgb() functional notation (not a colour name) — passes the ui/ no-literal-
# styling guard exactly as crest.py's per-pixel gradient styles do.
_PLUS_STYLE = "bold rgb(250,250,250)"   # the white prey marker
_LOG_RGB = "rgb(94,224,106)"            # venom-green boot logs (Style Guide ok)


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
    return _env_int("JARVIS_CREST_ANIM_FPS", 18, 6, 30)


def _min_intro_s() -> float:
    """Minimum visible animation regardless of wake speed (the not-moving fix)."""
    return _env_float("JARVIS_CREST_ANIM_MIN_S", 3.0, 0.0, 30.0)


def _min_laps() -> float:
    """At least this many full revolutions must show before the freeze."""
    return _env_float("JARVIS_CREST_ANIM_MIN_LAPS", 1.5, 0.25, 10.0)


def _plus_lead_deg() -> float:
    return _env_float("JARVIS_CREST_ANIM_PLUS_LEAD_DEG", 60.0, 0.0, 180.0)


def _lap_frames() -> int:
    """Frames per full revolution — resolves the phase step. Higher = smoother."""
    return _env_int("JARVIS_CREST_ANIM_LAP_FRAMES", 48, 12, 240)


def animator_enabled() -> bool:
    """Kill-switch: ``JARVIS_CREST_ANIM_DISABLED=1`` reverts to the static crest.
    Default ON. Never raises."""
    return os.environ.get(
        "JARVIS_CREST_ANIM_DISABLED", "",
    ).strip().lower() not in ("1", "true", "yes", "on")


class _PixelMeta:
    """Per-pixel sampled state, computed ONCE (root cause: no per-frame geometry).
    ``kind``/``theta``/``coverage``/``py_frac`` are the crest's own intermediate,
    so re-colouring per phase reproduces the polished emblem exactly."""

    __slots__ = ("kind", "theta", "coverage", "py_frac", "is_coil")

    def __init__(self, kind: str, theta: float, coverage: float, py_frac: float) -> None:
        self.kind = kind
        self.theta = theta
        self.coverage = coverage
        self.py_frac = py_frac
        self.is_coil = kind == "coil"


class CrestAnimator:
    """Composes ``crest``'s real sampling into a phase-animated Snake-and-Plus
    frame plus a partitioned bottom log region. Headless-testable — the crest
    frame is independent of the logs and the ``+`` index is deterministic."""

    def __init__(
        self,
        *,
        cols: int,
        rows: int,
        plus_lead_deg: Optional[float] = None,
        log_lines: int = 6,
    ) -> None:
        # Size exactly like the static crest (same clamps + row fit) so the
        # emblem is identical geometry.
        self._pf = generate_crest_pixels(cols, rows)
        self._geo = (
            _Geometry.for_size(self._pf.cols, self._pf.rows) if self._pf else None
        )
        self._plus_lead = math.radians(
            plus_lead_deg if plus_lead_deg is not None else _plus_lead_deg()
        )
        self._logs: "deque[str]" = deque(maxlen=max(1, int(log_lines)))
        self._lock = threading.Lock()
        self._meta: Dict[Tuple[int, int], _PixelMeta] = {}   # (x, py) -> meta
        self._ring_cells: List[Tuple[float, int, int]] = []  # (angle, x, cy)
        self._cy_lo = 0
        self._cy_hi = 0
        if self._pf and self._geo:
            self._build_skeleton()

    @property
    def available(self) -> bool:
        return bool(self._pf and self._geo and self._meta)

    # -- precompute (once), via the crest's OWN sampling (DRY) -----------

    def _build_skeleton(self) -> None:
        geo = self._geo
        pf = self._pf
        assert geo is not None and pf is not None
        ss = _ss()
        px_rows = pf.px_rows
        v_span = max(1e-6, geo.v_top + geo.v_bot)
        raw: Dict[Tuple[int, int], _PixelMeta] = {}
        for py in range(px_rows):
            py0 = float(py) * _REF_ASPECT
            for x in range(pf.cols):
                kind, coverage, theta = _sample_pixel(float(x), py0, geo, ss)
                if kind is None or coverage < 0.06:
                    continue
                py_frac = (py * _REF_ASPECT - (geo.cy - geo.v_top)) / v_span
                raw[(x, py)] = _PixelMeta(
                    kind, theta if theta is not None else 0.0, coverage, py_frac,
                )
        # Same sparse-geometry clip as the static crest → identical silhouette.
        try:
            kept = _prune_sparse_geometry({k: True for k in raw})
            raw = {k: v for k, v in raw.items() if k in kept}
        except Exception:  # noqa: BLE001
            pass
        self._meta = raw
        # ring cells (coil) → their mean angle, for the + snap
        cell_angles: Dict[Tuple[int, int], List[float]] = {}
        for (x, py), m in raw.items():
            if m.is_coil:
                cell_angles.setdefault((x, py // 2), []).append(m.theta)
        for (x, cy), thetas in cell_angles.items():
            sx = sum(math.cos(t) for t in thetas)
            sy = sum(math.sin(t) for t in thetas)
            self._ring_cells.append((math.atan2(sy, sx), x, cy))
        occ = [py // 2 for (x, py) in raw]
        self._cy_lo = min(occ) if occ else 0
        self._cy_hi = max(occ) if occ else pf.rows - 1

    # -- the + prey coordinate (deterministic, phase-shifted) -----------

    def plus_cell(self, phase: float) -> Optional[Tuple[int, int]]:
        """The (x, cy) cell where the ``+`` sits for this phase — the head's angle
        + a fixed lead, snapped to the nearest ring cell. Never raises."""
        if not self._ring_cells or self._geo is None:
            return None
        # The bright head is where (frac + phase) % 1 == 0 → frac = (1 - phase).
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

    # -- the crest frame (independent of logs, full crest fidelity) ------

    def _pixel_rgb(self, key: Tuple[int, int], phase: float) -> Optional[Tuple[int, int, int]]:
        m = self._meta.get(key)
        if m is None:
            return None
        # Coil pixels rotate their angle by phase → the gradient (with banding)
        # sweeps around the ring. Non-coil (V/head/eye) ignore theta.
        theta = (m.theta + 2.0 * math.pi * phase) if m.is_coil else m.theta
        base, _delay = _pixel_color_and_delay(m.kind, theta, m.py_frac, self._geo)
        alpha = m.coverage ** 0.75                     # crest's coverage-alpha AA
        return tuple(max(0, min(255, round(c * alpha))) for c in base)

    def crest_frame_text(self, phase: float) -> Any:
        """The animated crest for ``phase`` — the crest's OWN colours with the coil
        angle rotated + the ``+`` prey injected. Independent of the log buffer.
        Never raises; degrades to empty Text."""
        try:
            from rich.text import Text
        except Exception:  # noqa: BLE001
            return ""
        if not self.available:
            return Text("")
        plus = self.plus_cell(phase)
        text = Text()
        for cy in range(self._cy_lo, self._cy_hi + 1):
            for x in range(self._pf.cols):
                if plus is not None and (x, cy) == plus:
                    text.append("+", style=_PLUS_STYLE)
                    continue
                top = self._pixel_rgb((x, cy * 2), phase)
                bot = self._pixel_rgb((x, cy * 2 + 1), phase)
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
        arriving mid-frame lands only here, never in the crest matrix."""
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
        """The full Boot Canvas: a ``Group`` partitioning the crest (top) from the
        logs (bottom) — what ``Live`` repaints atomically."""
        try:
            from rich.console import Group
            from rich.text import Text
            return Group(self.crest_frame_text(phase), Text(""), self.logs_renderable())
        except Exception:  # noqa: BLE001
            return self.crest_frame_text(phase)

    # -- the Live playback loop (adaptive, decoupled duration) ----------

    def _lap_frame_count(self) -> int:
        return max(12, _lap_frames())

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
        """Run the animation inside a ``rich.live.Live`` until BOTH the daemon is
        up (``stop_event`` set) AND the minimum visible animation has shown
        (``min_seconds`` and ``min_laps``). This decouples the chase from the wake
        so it is ALWAYS seen — the root fix for a warm daemon freezing it instantly.
        On exit ``Live`` leaves the final frame frozen. Never raises out."""
        import asyncio
        if not self.available:
            return
        sleep = sleep_fn or asyncio.sleep
        rate = fps or _fps()
        lap = self._lap_frame_count()
        step = 1.0 / lap
        floor_s = _min_intro_s() if min_seconds is None else max(0.0, float(min_seconds))
        floor_laps = _min_laps() if min_laps is None else max(0.0, float(min_laps))
        min_frames = max(int(math.ceil(floor_s * rate)), int(math.ceil(floor_laps * lap)))
        if max_frames is not None:
            min_frames = min(min_frames, max_frames)
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
                while True:
                    await sleep(1.0 / rate)          # cooperative yield
                    prev = phase
                    phase = (phase + step) % 1.0
                    live.update(self.render(phase))
                    live.refresh()
                    n += 1
                    if max_frames is not None and n >= max_frames:
                        break
                    # Daemon up AND the minimum laps have shown → finish the
                    # current lap (phase wraps past 0) for a smooth, snap-free
                    # freeze on the canonical resting emblem.
                    done = (stop_event is None or stop_event.is_set()) and n >= min_frames
                    if done and phase < prev:
                        live.update(self.render(0.0))
                        live.refresh()
                        break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return


def build_animator(console: Any, *, plus_lead_deg: Optional[float] = None) -> Optional["CrestAnimator"]:
    """Construct an animator sized to the live console, or ``None`` when the crest
    can't render (disabled / non-TTY / tiny / NONE-tier). Never raises."""
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


__all__ = ["CrestAnimator", "animator_enabled", "build_animator"]

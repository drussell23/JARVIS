"""Procedural ouroboros crest (v5) -- reactive, hard-edge, tier-resolved.

Ports docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py into
a reactive generator: every metric scales from the measured terminal width
(Mandate 2 -- zero absolute canvas dimensions), clamped to
[JARVIS_OV_CREST_MIN_COLS, JARVIS_OV_CREST_MAX_COLS]. Rendering is
hard-threshold quadrant rasterization -- solid fill, no anti-aliasing.

The asset is the geometry source of truth; this module reproduces its math
verbatim (same sampling functions, same quadrant table, same gradient, same
v5 tuning) with every absolute metric replaced by ``scale * reference``
where ``scale = clamped_cols / _REF_COLS``.

Leaf module: stdlib + ui.theme only. NEVER raises: impossible conditions
return CrestFrame(unavailable_reason=...).
"""
from __future__ import annotations

import functools
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .theme import ColorTier, Token, style_for

# ---- reference geometry (verbatim from the approved v5 asset) -------------
# docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py
_REF_COLS = 58.0
_REF_ROWS = 17.0
_REF_ASPECT = 1.083

_REF = dict(
    r_mid=11.6,
    thick=3.5,
    gap_half=math.radians(20),
    taper_sweep=math.radians(52),
    taper_min=0.34,
    tail_intrude=math.radians(15),
    head_len=5.6,
    head_w=2.5,
    eye_r=0.95,
    v_top=4.9,
    v_bot=5.6,
    v_half_span=4.8,
    v_stroke=2.7,
)
_GAP_CENTER = math.radians(90)


def _ss() -> int:
    """Supersampling factor per quadrant (``JARVIS_OV_CREST_SS``,
    default 5 — raised from 3 in the 2026-07-18 sharpening pass:
    25 samples/quadrant smooths edge staircasing). Clamped [2, 8]."""
    try:
        return max(2, min(8, int(os.environ.get("JARVIS_OV_CREST_SS", "5"))))
    except (TypeError, ValueError):
        return 5


def _coverage_threshold() -> float:
    """Subpixel coverage fraction that turns a quadrant ON
    (``JARVIS_OV_CREST_COVERAGE``, default 0.42 — slightly below the
    old hard 0.5 so edge quadrants fill instead of fraying; the coil
    reads as a solid professional stroke). Clamped [0.2, 0.8]."""
    try:
        return max(0.2, min(0.8, float(
            os.environ.get("JARVIS_OV_CREST_COVERAGE", "0.42"),
        )))
    except (TypeError, ValueError):
        return 0.42


_SS = 5   # legacy alias — sampling reads _ss() at render time


def _edge_feather() -> float:
    """Boundary-cell luminance factor (``JARVIS_OV_CREST_FEATHER``,
    default 0.65 — dimmed edges read as native terminal anti-aliasing).
    ``1.0`` disables. Clamped [0.2, 1.0]; NEVER raises."""
    try:
        return min(1.0, max(0.2, float(
            os.environ.get("JARVIS_OV_CREST_FEATHER", "0.65"),
        )))
    except (TypeError, ValueError):
        return 0.65

_STOPS = [
    (0, (125, 255, 106)), (60, (91, 227, 75)), (150, (139, 92, 246)),
    (210, (177, 108, 234)), (285, (212, 192, 74)), (360, (125, 255, 106)),
]
_HEAD_RGB = (125, 255, 106)
_EYE_RGB = (234, 255, 208)
_V_TOP_RGB, _V_BOT_RGB = (192, 132, 252), (157, 78, 220)

_QUAD = {
    0b0000: " ", 0b1000: "▘", 0b0100: "▝", 0b1100: "▀",
    0b0010: "▖", 0b1010: "▌", 0b0110: "▞", 0b1110: "▛",
    0b0001: "▗", 0b1001: "▚", 0b0101: "▐", 0b1101: "▜",
    0b0011: "▄", 0b1011: "▙", 0b0111: "▟", 0b1111: "█",
}
_PRIORITY = ("eye", "head", "coil", "v")
_DOTS = set("▘▝▖▗")  # ▘▝▖▗


@dataclass(frozen=True)
class CrestCell:
    x: int
    y: int
    glyph: str
    kind: str
    rgb: Tuple[int, int, int]
    delay_s: float


@dataclass(frozen=True)
class CrestFrame:
    cols: int
    rows: int
    cells: Tuple[CrestCell, ...]
    max_delay_s: float
    unavailable_reason: Optional[str] = None


def _clamp_cols(measured: int) -> Tuple[int, int, int]:
    """Return (lo, hi, clamped) column bounds for a measured width.

    ``clamped`` is only meaningful when ``measured >= lo`` -- callers must
    check the low bound against ``measured`` directly (not ``clamped``,
    which is floored at ``lo`` by construction and so can never itself
    read as "below minimum").
    """
    lo = int(os.environ.get("JARVIS_OV_CREST_MIN_COLS", "46"))
    hi = int(os.environ.get("JARVIS_OV_CREST_MAX_COLS", "88"))
    # Strict bounding-box margin (operator finding 2026-07-18): a crest
    # exactly as wide as the terminal auto-wraps its last cell onto the
    # next line — every row sheds one detached artifact block. One
    # column of right margin kills the whole off-by-one wrap class.
    clamped = max(lo, min(measured - 1, hi))
    return lo, hi, clamped


# ===========================================================================
# Scaled geometry
# ===========================================================================


class _Geometry:
    """Every reference metric scaled by ``cols / _REF_COLS``.

    Mirrors the asset's module-level constants + derived CX/CY, but as
    instance attributes parameterized by the measured (clamped) width --
    zero absolute canvas dimensions (Mandate 2).
    """

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        scale = cols / _REF_COLS
        self.scale = scale

        self.r_mid = _REF["r_mid"] * scale
        self.thick = _REF["thick"] * scale
        self.gap_center = _GAP_CENTER
        self.gap_half = _REF["gap_half"]
        self.taper_sweep = _REF["taper_sweep"]
        self.taper_min = _REF["taper_min"]
        self.tail_intrude = _REF["tail_intrude"]
        self.head_len = _REF["head_len"] * scale
        self.head_w = _REF["head_w"] * scale
        self.eye_r = _REF["eye_r"] * scale
        self.v_top = _REF["v_top"] * scale
        self.v_bot = _REF["v_bot"] * scale
        self.v_half_span = _REF["v_half_span"] * scale
        self.v_stroke = _REF["v_stroke"] * scale

        # Physical pixel canvas: 1 cell = 1px wide x 2px tall (aspect corrected).
        self.px_w = float(cols)
        self.px_h = float(rows) * 2.0
        self.cx = self.px_w / 2.0
        self.cy = (self.px_h / 2.0) * _REF_ASPECT

        self.tail_tip = _ang_norm(self.gap_center + self.gap_half)
        self.head_theta = _ang_norm(self.gap_center - self.gap_half)

    @staticmethod
    def rows_needed(cols: int) -> int:
        """Cell rows required to fit the coil's scaled diameter + margin."""
        scale = cols / _REF_COLS
        r_mid = _REF["r_mid"] * scale
        thick = _REF["thick"] * scale
        diameter_px = 2.0 * (r_mid + thick / 2.0)
        cell_rows = math.ceil(diameter_px / (2.0 * _REF_ASPECT))
        return cell_rows + 2

    @classmethod
    def for_size(cls, cols: int, rows: int) -> "_Geometry":
        return cls(cols, rows)


# ===========================================================================
# Sampling functions -- ported verbatim from the asset, parameterized by geo
# ===========================================================================


def _ang_norm(a: float) -> float:
    while a < 0:
        a += 2 * math.pi
    while a >= 2 * math.pi:
        a -= 2 * math.pi
    return a


def _in_gap(theta: float, geo: _Geometry) -> bool:
    d = abs(_ang_norm(theta - geo.gap_center))
    if d > math.pi:
        d = 2 * math.pi - d
    return d < geo.gap_half


def _body_half(theta: float, geo: _Geometry) -> float:
    d = _ang_norm(theta - geo.tail_tip)          # 0 at tail tip, grows CCW
    if d < geo.taper_sweep:
        t = d / geo.taper_sweep
        return (geo.thick * (geo.taper_min + (1.0 - geo.taper_min) * t)) / 2.0
    return geo.thick / 2.0


def _sample_coil(px: float, py: float, geo: _Geometry) -> bool:
    dx, dy = px - geo.cx, py - geo.cy
    r = math.hypot(dx, dy)
    theta = math.atan2(-dy, dx)
    if _in_gap(theta, geo):
        # tail tip intrudes into the mouth as a thin point
        d_from_tail_edge = _ang_norm(geo.tail_tip - theta)
        if d_from_tail_edge < geo.tail_intrude:
            t = d_from_tail_edge / geo.tail_intrude   # 0 at gap edge -> 1 deep
            half = (geo.thick * geo.taper_min / 2.0) * (1.0 - 0.72 * t)
            return abs(r - geo.r_mid) <= half
        return False
    return abs(r - geo.r_mid) <= _body_half(theta, geo)


def _head_frame(geo: _Geometry) -> Tuple[float, float, float, float]:
    hx = geo.cx + geo.r_mid * math.cos(geo.head_theta)
    hy = geo.cy - geo.r_mid * math.sin(geo.head_theta)
    # snout points CCW along the tangent (toward the gap / tail tip)
    tx = math.sin(geo.head_theta)
    ty = math.cos(geo.head_theta)
    return hx, hy, tx, ty


def _sample_head(px: float, py: float, geo: _Geometry) -> bool:
    hx, hy, tx, ty = _head_frame(geo)
    dx, dy = px - hx, py - hy
    lon = dx * tx + dy * ty
    lat = -dx * ty + dy * tx
    if lon < -1.6 * geo.scale or lon > geo.head_len:
        return False
    w = geo.head_w * (1.0 - 0.62 * max(0.0, lon / geo.head_len))
    # open mouth: notch at the snout half
    if lon > geo.head_len * 0.52 and abs(lat) < w * 0.34:
        return False
    return abs(lat) <= w


def _eye_center(geo: _Geometry) -> Tuple[float, float]:
    hx, hy, tx, ty = _head_frame(geo)
    return (
        hx + tx * 0.7 * geo.scale - ty * 1.05 * geo.scale,
        hy + ty * 0.7 * geo.scale + tx * 1.05 * geo.scale,
    )


def _sample_eye(px: float, py: float, geo: _Geometry) -> bool:
    ex, ey = _eye_center(geo)
    return math.hypot(px - ex, py - ey) <= geo.eye_r


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _sample_v(px: float, py: float, geo: _Geometry) -> bool:
    if py < geo.cy - geo.v_top:                # flat, machined top edge
        return False
    top_inset = 0.8 * geo.scale
    d1 = _seg_dist(px, py, geo.cx - geo.v_half_span, geo.cy - geo.v_top - top_inset,
                    geo.cx, geo.cy + geo.v_bot)
    d2 = _seg_dist(px, py, geo.cx + geo.v_half_span, geo.cy - geo.v_top - top_inset,
                    geo.cx, geo.cy + geo.v_bot)
    return min(d1, d2) <= geo.v_stroke / 2.0


# ---- gradient ---------------------------------------------------------------


def _grad(frac: float) -> Tuple[float, float, float]:
    deg = frac * 360.0
    for (a0, c0), (a1, c1) in zip(_STOPS, _STOPS[1:]):
        if a0 <= deg <= a1:
            t = (deg - a0) / (a1 - a0) if a1 > a0 else 0
            return tuple(c0[k] + t * (c1[k] - c0[k]) for k in range(3))  # type: ignore[return-value]
    return _STOPS[-1][1]


# ---- classification + rasterization -----------------------------------------


def _classify(px: float, py: float, geo: _Geometry) -> Tuple[Optional[str], Optional[float]]:
    """Hard classification of one subpixel center-region (majority of SSxSS)."""
    votes = {"eye": 0, "head": 0, "coil": 0, "v": 0}
    theta: Optional[float] = None
    ss = _ss()
    for sy in range(ss):
        for sx in range(ss):
            spx = px + (sx + 0.5) / ss * 0.5
            spy = py + ((sy + 0.5) / ss * 0.5) * _REF_ASPECT
            if _sample_eye(spx, spy, geo):
                votes["eye"] += 1
            elif _sample_head(spx, spy, geo):
                votes["head"] += 1
            elif _sample_coil(spx, spy, geo):
                votes["coil"] += 1
                theta = math.atan2(-(spy - geo.cy), spx - geo.cx)
            elif _sample_v(spx, spy, geo):
                votes["v"] += 1
    n = ss * ss
    inside = sum(votes.values()) >= n * _coverage_threshold()
    if not inside:
        return None, None
    k = max(_PRIORITY, key=lambda kk: votes[kk])
    return k, theta


def _render_cells(geo: _Geometry) -> List[CrestCell]:
    """cell -> CrestCell. Quadrant bit order: TL,TR,BL,BR."""
    raw: Dict[Tuple[int, int], Tuple[str, str, Tuple[float, float, float], float]] = {}
    for cy_ in range(geo.rows):
        for cx_ in range(geo.cols):
            bits = 0
            kinds: List[str] = []
            thetas: List[float] = []
            for i, (sx, sy) in enumerate([(0, 0), (1, 0), (0, 1), (1, 1)]):
                # subpixel origin in physical units (cell 1.0 wide, 2*ASPECT tall)
                px = cx_ + sx * 0.5
                py = (cy_ * 2 + sy) * _REF_ASPECT
                k, th = _classify(px, py, geo)
                if k is not None:
                    bits |= 1 << (3 - i)
                    kinds.append(k)
                    if th is not None:
                        thetas.append(th)
            if not bits:
                continue
            kind = max(_PRIORITY, key=lambda kk: kinds.count(kk)) if kinds else "coil"
            if kind == "eye":
                color, delay = _EYE_RGB, 1.55
            elif kind == "head":
                color, delay = _HEAD_RGB, 1.42
            elif kind == "coil":
                th = thetas[len(thetas) // 2] if thetas else 0.0
                frac = _ang_norm(th - geo.tail_tip) / (2 * math.pi)
                color = tuple(round(c) for c in _grad(frac))
                delay = 0.05 + 1.30 * frac
            else:  # v
                t = max(0.0, min(1.0, (cy_ * 2 * _REF_ASPECT - (geo.cy - geo.v_top))
                                  / (geo.v_top + geo.v_bot)))
                color = tuple(round(_V_TOP_RGB[k2] + t * (_V_BOT_RGB[k2] - _V_TOP_RGB[k2]))
                              for k2 in range(3))
                delay = 1.78 + 0.5 * t
            raw[(cx_, cy_)] = (_QUAD[bits], kind, color, delay)  # type: ignore[assignment]

    # cleanup: drop isolated single-quadrant crumbs (no orthogonal neighbor)
    for (x, y) in [k for k, v in raw.items() if v[0] in _DOTS]:
        if not any((x + dx, y + dy) in raw for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
            del raw[(x, y)]

    # Topological edge feathering (2026-07-18): boundary cells — fewer
    # than 4 populated orthogonal neighbors — scale their luminance by
    # the feather factor. Dimmed edges read as anti-aliasing on the
    # terminal grid; interior cells keep the full gradient. Clip-safe
    # by construction (round of a [0,1]-scaled channel never exceeds
    # the source, never drops below 0).
    feather = _edge_feather()
    if feather < 1.0:
        boundary = [
            key for key in raw
            if sum(
                (key[0] + dx, key[1] + dy) in raw
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ) < 4
        ]
        for key in boundary:
            glyph, kind, color, delay = raw[key]
            dimmed = tuple(
                max(0, min(255, round(c * feather))) for c in color
            )
            raw[key] = (glyph, kind, dimmed, delay)  # type: ignore[assignment]

    return [
        CrestCell(x=x, y=y, glyph=glyph, kind=kind, rgb=color, delay_s=delay)
        for (x, y), (glyph, kind, color, delay) in raw.items()
    ]


# ===========================================================================
# Public entry point
# ===========================================================================


@functools.lru_cache(maxsize=8)
def _generate_cached(clamped_cols: int, rows: int, tier: int) -> CrestFrame:
    geo = _Geometry.for_size(clamped_cols, rows)
    cells = _render_cells(geo)
    max_delay = max((c.delay_s for c in cells), default=0.0)
    return CrestFrame(
        cols=clamped_cols,
        rows=rows,
        cells=tuple(cells),
        max_delay_s=max(max_delay, 1.55),
    )


def generate_crest(
    measured_cols: int,
    measured_rows: int,
    *,
    tier: ColorTier,
    unicode_ok: bool,
) -> CrestFrame:
    """Generate a reactive crest frame sized to the measured terminal.

    NEVER raises. Returns an unavailable frame (``unavailable_reason`` set)
    when: unicode is unsupported, the color tier is NONE, the measured width
    is below the configured minimum, or there are not enough rows to fit the
    scaled geometry. Frames are cached per (clamped_cols, rows, tier).
    """
    try:
        if not unicode_ok:
            return CrestFrame(0, 0, (), 0.0, "unicode required")
        if tier is ColorTier.NONE:
            return CrestFrame(0, 0, (), 0.0, "color tier NONE")
        lo, _hi, clamped = _clamp_cols(measured_cols)
        if measured_cols < lo:
            return CrestFrame(0, 0, (), 0.0, f"width {measured_cols} < min {lo}")
        needed_rows = _Geometry.rows_needed(clamped)
        if measured_rows < needed_rows:
            return CrestFrame(0, 0, (), 0.0,
                               f"rows {measured_rows} < needed {needed_rows}")
        return _generate_cached(clamped, needed_rows, int(tier))
    except Exception:  # noqa: BLE001 -- never raise into a render path
        return CrestFrame(0, 0, (), 0.0, "generation error")


def crest_renderer() -> str:
    """``JARVIS_OV_CREST_RENDERER`` — ``halfblock`` (default, 2026-07-18
    fidelity pass) or ``quadrant`` (legacy).

    HALFBLOCK is a renderer-class upgrade: each terminal cell renders
    ``▀`` with INDEPENDENT foreground (upper pixel) and background
    (lower pixel) colors — 1×2 true pixels per cell with full per-pixel
    color (the chafa/timg technique). That enables genuine coverage-
    based alpha anti-aliasing (edge pixels blend toward the dark
    canvas — real AA, not luminance smearing) and per-pixel gradient +
    scale banding so the coil reads as a snake's body, not a lumpy
    ring. Quadrant glyphs give 2×2 binary subcells but only ONE color
    per cell — the structural ceiling the operator's screenshots hit."""
    mode = os.environ.get(
        "JARVIS_OV_CREST_RENDERER", "halfblock",
    ).strip().lower()
    return mode if mode in ("halfblock", "quadrant") else "halfblock"


def _band_strength() -> float:
    """Scale-banding amplitude along the coil (``JARVIS_OV_CREST_BANDS``
    amplitude, default 0.10 — subtle segmentation that reads as scales).
    0 disables. Clamped [0, 0.3]."""
    try:
        return min(0.3, max(0.0, float(
            os.environ.get("JARVIS_OV_CREST_BAND_AMP", "0.10"),
        )))
    except (TypeError, ValueError):
        return 0.10


_BAND_COUNT = 26   # bands around the full coil — segment cadence


@dataclass(frozen=True)
class PixelFrame:
    """Half-block pixel raster: ``cols`` cell columns × ``px_rows``
    (= cell rows × 2) pixel rows. ``pixels[(x, py)] = (rgb, delay_s)``."""

    cols: int
    rows: int                 # CELL rows (px_rows == rows * 2)
    px_rows: int
    pixels: Dict[Tuple[int, int], Tuple[Tuple[int, int, int], float]]
    max_delay_s: float


def _pixel_color_and_delay(
    kind: str, theta: Optional[float], py_frac: float, geo: _Geometry,
) -> Tuple[Tuple[float, float, float], float]:
    """Base color + reveal delay for one pixel — the SAME palette,
    gradient and delay formulas as the cell path, plus coil scale
    banding. Pure."""
    if kind == "eye":
        return tuple(float(c) for c in _EYE_RGB), 1.55
    if kind == "head":
        return tuple(float(c) for c in _HEAD_RGB), 1.42
    if kind == "v":
        t = max(0.0, min(1.0, py_frac))
        color = tuple(
            float(_V_TOP_RGB[k] + t * (_V_BOT_RGB[k] - _V_TOP_RGB[k]))
            for k in range(3)
        )
        return color, 1.78 + 0.5 * t
    # coil
    th = theta if theta is not None else 0.0
    frac = _ang_norm(th - geo.tail_tip) / (2 * math.pi)
    color = _grad(frac)
    amp = _band_strength()
    if amp > 0.0:
        band = 1.0 + amp * math.sin(th * _BAND_COUNT)
        color = tuple(min(255.0, c * band) for c in color)
    return tuple(float(c) for c in color), 0.05 + 1.30 * frac


def _sample_pixel(
    px0: float, py0: float, geo: _Geometry, ss: int,
) -> Tuple[Optional[str], float, Optional[float]]:
    """Sample ONE half-block pixel (1.0 cell wide × 1 physical-px tall)
    with ss×ss subsamples. Returns (kind, coverage 0..1, theta)."""
    votes = {"eye": 0, "head": 0, "coil": 0, "v": 0}
    theta: Optional[float] = None
    for sy in range(ss):
        for sx in range(ss):
            spx = px0 + (sx + 0.5) / ss
            spy = py0 + ((sy + 0.5) / ss) * _REF_ASPECT
            if _sample_eye(spx, spy, geo):
                votes["eye"] += 1
            elif _sample_head(spx, spy, geo):
                votes["head"] += 1
            elif _sample_coil(spx, spy, geo):
                votes["coil"] += 1
                theta = math.atan2(-(spy - geo.cy), spx - geo.cx)
            elif _sample_v(spx, spy, geo):
                votes["v"] += 1
    n = ss * ss
    inside = sum(votes.values())
    if inside == 0:
        return None, 0.0, None
    kind = max(_PRIORITY, key=lambda kk: votes[kk])
    return kind, inside / n, theta


def _min_component_px() -> int:
    try:
        return max(0, int(os.environ.get(
            "JARVIS_OV_CREST_MIN_COMPONENT_PX", "12",
        )))
    except (TypeError, ValueError):
        return 12


def _min_apex_row_px() -> int:
    try:
        return max(0, int(os.environ.get(
            "JARVIS_OV_CREST_MIN_ROW_PX", "3",
        )))
    except (TypeError, ValueError):
        return 3


def _prune_sparse_geometry(
    pixels: "Dict[Tuple[int, int], Any]",
) -> "Dict[Tuple[int, int], Any]":
    """Geometry Polish (operator mandate 2026-07-18): two mathematical
    density clips on the raster — no terminal overwrites, no per-glyph
    hacks.

    1. **Detached-component clip**: any 8-connected pixel cluster
       smaller than ``JARVIS_OV_CREST_MIN_COMPONENT_PX`` is removed
       (the largest component ALWAYS survives — the body can never
       delete itself on a tiny terminal).
    2. **Apex row-density clip**: walking inward from the top and
       bottom edges, rows thinner than ``JARVIS_OV_CREST_MIN_ROW_PX``
       are shed until the first dense row — the 1-pixel tail whisker
       that read as floating debris. Interior thin rows are untouched
       (gaps between coil arcs are design, not debris).

    Pure; NEVER raises; returns the input on any fault.
    """
    try:
        if not pixels:
            return pixels
        coords = set(pixels.keys())
        # -- (1) connected components (8-neighbour) --
        min_comp = _min_component_px()
        seen: set = set()
        components = []
        for start in coords:
            if start in seen:
                continue
            stack, comp = [start], set()
            while stack:
                p = stack.pop()
                if p in seen:
                    continue
                seen.add(p)
                comp.add(p)
                x, y = p
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        q = (x + dx, y + dy)
                        if q in coords and q not in seen:
                            stack.append(q)
            components.append(comp)
        if components:
            largest = max(components, key=len)
            keep = set().union(*(
                c for c in components
                if c is largest or len(c) >= min_comp
            ))
        else:
            keep = coords
        # -- (2) apex row-density clip (edges inward only) --
        min_row = _min_apex_row_px()
        if min_row > 0 and keep:
            rows_px: Dict[int, int] = {}
            for (_x, y) in keep:
                rows_px[y] = rows_px.get(y, 0) + 1
            ys = sorted(rows_px)
            shed: set = set()
            for y in ys:                       # top edge inward
                if rows_px[y] < min_row:
                    shed.add(y)
                else:
                    break
            for y in reversed(ys):             # bottom edge inward
                if rows_px[y] < min_row:
                    shed.add(y)
                else:
                    break
            if len(shed) < len(ys):            # never shed the whole mark
                keep = {p for p in keep if p[1] not in shed}
        return {p: v for p, v in pixels.items() if p in keep}
    except Exception:  # noqa: BLE001
        return pixels


@functools.lru_cache(maxsize=8)
def _generate_pixels_cached(clamped_cols: int, rows: int) -> PixelFrame:
    geo = _Geometry.for_size(clamped_cols, rows)
    ss = _ss()
    pixels: Dict[Tuple[int, int], Tuple[Tuple[int, int, int], float]] = {}
    max_delay = 0.0
    px_rows = rows * 2
    v_span = max(1e-6, geo.v_top + geo.v_bot)
    for py in range(px_rows):
        py0 = float(py) * _REF_ASPECT
        for x in range(clamped_cols):
            kind, coverage, theta = _sample_pixel(float(x), py0, geo, ss)
            if kind is None or coverage < 0.06:
                continue
            py_frac = (py * _REF_ASPECT - (geo.cy - geo.v_top)) / v_span
            base, delay = _pixel_color_and_delay(kind, theta, py_frac, geo)
            # TRUE anti-aliasing: coverage-alpha blend toward the dark
            # canvas (gamma-softened so edges taper, not smear).
            alpha = coverage ** 0.75
            rgb = tuple(
                max(0, min(255, round(c * alpha))) for c in base
            )
            pixels[(x, py)] = (rgb, delay)  # type: ignore[assignment]
            max_delay = max(max_delay, delay)
    pixels = _prune_sparse_geometry(pixels)
    return PixelFrame(
        cols=clamped_cols, rows=rows, px_rows=px_rows,
        pixels=pixels, max_delay_s=max_delay,
    )


def generate_crest_pixels(
    measured_cols: int, measured_rows: int,
) -> Optional[PixelFrame]:
    """Half-block raster sized like :func:`generate_crest` (same clamps
    + row fit). None when the terminal can't fit it. NEVER raises."""
    try:
        lo, _hi, clamped = _clamp_cols(measured_cols)
        if measured_cols < lo:
            return None
        rows = _Geometry.rows_needed(clamped)
        if measured_rows < rows + 2:
            return None
        return _generate_pixels_cached(clamped, rows)
    except Exception:  # noqa: BLE001
        return None


def pixels_to_text(
    pf: "PixelFrame", *, elapsed: Optional[float] = None,
) -> "Any":
    """PixelFrame → Rich Text of ``▀`` cells (fg=upper px, bg=lower px).
    ``elapsed=None`` = the full emblem; float = partial reveal. NEVER
    raises; degrades to empty Text."""
    try:
        from rich.text import Text
        cutoff = float("inf") if elapsed is None else elapsed
        text = Text()
        # Trim rows emptied by the sparse-geometry clip (a leading
        # blank line is dead space above the emblem, not margin).
        occupied = [py // 2 for py in {p[1] for p in pf.pixels}]
        cy_lo = min(occupied) if occupied else 0
        cy_hi = max(occupied) if occupied else pf.rows - 1
        for cy in range(cy_lo, cy_hi + 1):
            for x in range(pf.cols):
                top = pf.pixels.get((x, cy * 2))
                bot = pf.pixels.get((x, cy * 2 + 1))
                if top is not None and top[1] > cutoff:
                    top = None
                if bot is not None and bot[1] > cutoff:
                    bot = None
                if top is None and bot is None:
                    text.append(" ")
                elif top is not None and bot is not None:
                    tr, tg, tb = top[0]
                    br, bg_, bb = bot[0]
                    text.append(
                        "▀",
                        style=(
                            f"rgb({tr},{tg},{tb}) on "
                            f"rgb({br},{bg_},{bb})"
                        ),
                    )
                elif top is not None:
                    tr, tg, tb = top[0]
                    text.append("▀", style=f"rgb({tr},{tg},{tb})")
                else:
                    br, bg_, bb = bot[0]  # type: ignore[index]
                    text.append("▄", style=f"rgb({br},{bg_},{bb})")
            if cy < cy_hi:
                text.append("\n")
        return text
    except Exception:  # noqa: BLE001
        try:
            from rich.text import Text
            return Text("")
        except Exception:  # noqa: BLE001
            return ""


def render_crest_auto(
    frame: "CrestFrame", tier: ColorTier, *, elapsed: Optional[float] = None,
) -> "Any":
    """THE renderer dispatch: half-block pixels on capable terminals
    (C256+ and renderer=halfblock), quadrant glyphs otherwise. Both
    paths honor the reveal clock. NEVER raises."""
    try:
        if crest_renderer() == "halfblock" and tier >= ColorTier.C256:
            pf = _generate_pixels_cached(frame.cols, frame.rows)
            return pixels_to_text(pf, elapsed=elapsed)
    except Exception:  # noqa: BLE001
        pass
    return frame_to_text(frame, tier, elapsed=elapsed)


def frame_to_text(
    frame: "CrestFrame", tier: ColorTier, *, elapsed: Optional[float] = None,
) -> "Any":
    """Render a crest frame to a Rich ``Text`` (fill-mode + feather
    aware). ``elapsed=None`` renders the FULL crest (the static emblem
    — collision surfaces, banners); a float renders the partial reveal
    (the animated ceremony). THE one frame→Text renderer — the
    AwakeningConductor and every static consumer compose it (DRY).
    NEVER raises; degrades to an empty Text."""
    try:
        from rich.text import Text
        if not frame.cells or frame.cols <= 0 or frame.rows <= 0:
            return Text("")
        cutoff = float("inf") if elapsed is None else elapsed
        grid = {
            (c.x, c.y): c for c in frame.cells if c.delay_s <= cutoff
        }
        text = Text()
        for y in range(frame.rows):
            for x in range(frame.cols):
                cell = grid.get((x, y))
                if cell is None:
                    text.append(" ")
                else:
                    ch, style = render_cell(cell, tier)
                    text.append(ch, style=style)
            if y < frame.rows - 1:
                text.append("\n")
        return text
    except Exception:  # noqa: BLE001
        try:
            from rich.text import Text
            return Text("")
        except Exception:  # noqa: BLE001
            return ""


def blit_text(console: "Any", text: "Any") -> bool:
    """Double-buffered blit: render ``text`` to an OFF-SCREEN ANSI
    buffer first, then write the whole frame to the TTY in a single
    ``write`` + flush. Eliminates tearing/flicker from incremental
    segment writes (mandate: no raw cursor-jump hacks — one atomic
    frame). Coordinates are already bounded by :func:`_clamp_cols`'s
    terminal-size margin. Returns False (no partial output) on any
    fault. NEVER raises."""
    try:
        from rich.console import Console
        import io
        size = console.size
        buf = io.StringIO()
        offscreen = Console(
            file=buf,
            width=size.width,
            force_terminal=True,
            color_system=getattr(console, "_color_system_name", None)
            or "truecolor",
            highlight=False,
        )
        offscreen.print(text)
        frame = buf.getvalue()
        if not frame:
            return False
        console.file.write(frame)
        console.file.flush()
        return True
    except Exception:  # noqa: BLE001
        return False


def print_static_crest(console: "Any") -> bool:
    """The static emblem — the mark that ALWAYS greets ``ov`` (operator
    law, 2026-07-18), including on the already-awake collision surface.
    Full crest, no animation (animation is the BIRTH; the static mark
    is the EMBLEM). Returns True when rendered; degrades silently on
    non-TTY / NONE tier / tiny terminals. NEVER raises."""
    try:
        from .theme import detect_tier, supports_unicode
        if not supports_unicode():
            return False
        tier = detect_tier(console)
        if tier <= ColorTier.NONE:
            return False
        size = console.size
        frame = generate_crest(
            size.width, size.height, tier=tier, unicode_ok=True,
        )
        if frame.unavailable_reason:
            return False
        emblem = render_crest_auto(frame, tier)
        # Single-frame blit (double-buffered); Rich print fallback keeps
        # the emblem law ("the mark ALWAYS greets ov") even when the
        # console's file surface is exotic.
        if not blit_text(console, emblem):
            console.print(emblem)
        return True
    except Exception:  # noqa: BLE001
        return False


def crest_fill_mode() -> str:
    """``JARVIS_OV_CREST_FILL`` — ``bg`` (default) paints FULL-BLOCK
    cells as background-colored spaces, ``glyph`` keeps legacy
    foreground blocks.

    WHY bg (2026-07-18 operator report): terminal profiles with line
    spacing > 1.0 leave a leading gap between rows that foreground
    block glyphs cannot span — the coil renders as separated "bricks".
    Background color fills the ENTIRE line box (leading included) on
    every mainstream terminal, so interior strokes read solid on ANY
    profile. Partial quadrant cells keep foreground glyphs — they carry
    the anti-aliased silhouette and cannot be bg-painted without
    filling their transparent quadrants."""
    mode = os.environ.get("JARVIS_OV_CREST_FILL", "bg").strip().lower()
    return mode if mode in ("bg", "glyph") else "bg"


def render_cell(cell: CrestCell, tier: ColorTier) -> Tuple[str, str]:
    """Resolve one cell to ``(char, style)`` under the fill mode.

    Full blocks under ``bg`` fill → a SPACE painted with the cell color
    as background (solid across line-spacing gaps); everything else
    (edge quadrants, sub-C256 tiers) → the legacy foreground glyph."""
    if (
        crest_fill_mode() == "bg"
        and cell.glyph == "█"
        and tier >= ColorTier.C256
    ):
        r, g, b = cell.rgb
        return " ", f"on rgb({r},{g},{b})"
    return cell.glyph, style_for_cell(cell, tier)


def style_for_cell(cell: CrestCell, tier: ColorTier) -> str:
    """Resolve one cell's Rich style for the tier. TRUECOLOR/C256 carry the
    per-cell gradient (Rich downgrades 24-bit for 256 terminals); STANDARD
    collapses to the single accent (geometry + trace unchanged)."""
    if tier >= ColorTier.C256:
        r, g, b = cell.rgb
        return f"rgb({r},{g},{b})"
    accent = style_for(Token.ACCENT, tier)
    if cell.kind == "v":
        return f"bold {accent}"
    return accent


__all__ = ["CrestCell", "CrestFrame", "crest_fill_mode", "generate_crest", "render_cell", "style_for_cell"]

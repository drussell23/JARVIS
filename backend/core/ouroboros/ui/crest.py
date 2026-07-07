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
from typing import Dict, List, Optional, Tuple

from .theme import ColorTier

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
_SS = 3

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
    hi = int(os.environ.get("JARVIS_OV_CREST_MAX_COLS", "72"))
    clamped = max(lo, min(measured, hi))
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
    for sy in range(_SS):
        for sx in range(_SS):
            spx = px + (sx + 0.5) / _SS * 0.5
            spy = py + ((sy + 0.5) / _SS * 0.5) * _REF_ASPECT
            if _sample_eye(spx, spy, geo):
                votes["eye"] += 1
            elif _sample_head(spx, spy, geo):
                votes["head"] += 1
            elif _sample_coil(spx, spy, geo):
                votes["coil"] += 1
                theta = math.atan2(-(spy - geo.cy), spx - geo.cx)
            elif _sample_v(spx, spy, geo):
                votes["v"] += 1
    n = _SS * _SS
    inside = sum(votes.values()) >= n / 2.0       # 0.5 coverage threshold
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


__all__ = ["CrestCell", "CrestFrame", "generate_crest"]

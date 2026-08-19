"""Procedural ouroboros crest (v5) -- reactive, hard-edge, tier-resolved.

Ports docs/superpowers/specs/assets/2026-07-07-ov-crest-v5-generator.py into
a reactive generator: every metric scales from the measured terminal width
(Mandate 2 -- zero absolute canvas dimensions), clamped to
[JARVIS_OV_CREST_MIN_COLS, JARVIS_OV_CREST_MAX_COLS] and held back from the
terminal's last column by JARVIS_OV_CREST_RIGHT_MARGIN. Rendering is
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


#: Rows the awakening ceremony prints beneath the crest (header + spacing).
#: Both the fit and the ceremony's own "will the animated path fit" check read
#: this, so the crest is never sized into space the header is about to claim.
CREST_HEADER_ROWS = 6


def _fit_cols(measured_cols: int, measured_rows: int) -> Tuple[int, int, int]:
    """Largest crest that fits BOTH dimensions: (lo, hi, fitted).

    Sizing used to be width-only — ``min(measured - 1, 88)`` — with height as
    a veto: if the resulting crest was taller than the terminal, the whole
    thing was suppressed. Two consequences, both visible:

      * on a wide terminal the crest capped at 88 columns and sat in under
        half the available width, because 88 was chosen when the reference
        geometry was drawn rather than measured against anything;
      * simply raising that cap makes the crest VANISH on a short-but-wide
        window, since a wider crest needs proportionally more rows.

    So width and height are solved together: take the widest crest whose
    ``rows_needed`` still fits the rows we actually have. The ceiling becomes
    a true bound on the terminal rather than an arbitrary number, and the
    crest degrades by shrinking instead of disappearing.

    Search is a descending scan, not a formula: ``rows_needed`` involves a
    ceil, so inverting it algebraically lands off-by-one at the boundaries —
    exactly where a crest either overflows or vanishes. The range is at most
    a few hundred integers and this runs once per resize."""
    # The cap is now a SAFETY rail, not the working limit: a crest wider than
    # it stops reading as an emblem and starts reading as wallpaper. Every
    # bound — floor, cap and right margin — comes from _crest_bounds via
    # _clamp_cols, so each has exactly one definition (DRY).
    lo, hi, width_budget = _clamp_cols(measured_cols)
    # Reserve rows for the header the ceremony prints beneath the crest.
    # CREST_HEADER_ROWS is the single definition of that number — awakening
    # independently reserved 6 rows before deciding whether the animated path
    # would fit, so a reserve chosen separately here would be a second copy
    # free to drift out of step with it.
    reserve = _env_int("JARVIS_OV_CREST_ROW_RESERVE", CREST_HEADER_ROWS)
    row_budget = max(0, int(measured_rows) - reserve)

    # The shrink floor is the minimum CREST width, which is not the ``lo``
    # returned above — that one is the minimum MEASURED width and includes the
    # margin. Shrinking to it would stop one column early and, at the
    # boundary, hand back a crest wider than the width budget it was given.
    min_crest, _hi, _margin = _crest_bounds()
    fitted = width_budget
    while fitted > min_crest and _Geometry.rows_needed(fitted) > row_budget:
        fitted -= 1
    return lo, hi, fitted


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    """One tunable read, defended once.

    Every column bound is operator-tunable, and a malformed value must not be
    the thing that takes the crest down: this module's contract is that it
    NEVER raises. ``int("")`` and ``int("wide")`` both raise, so an unparseable
    override silently became a crash inside a render path that callers trust
    to degrade instead.
    """
    try:
        return max(floor, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _crest_bounds() -> Tuple[int, int, int]:
    """``(min_crest, max_crest, right_margin)`` -- THE definition of every
    column bound, each independently tunable.

    The margin is a bound in its own right, not a literal buried in an
    expression. It was written as a bare ``- 1`` inside the clamp, which is
    how it came to be silently cancelled at the low boundary (see
    :func:`_clamp_cols`): a number with no name is a number no caller can
    reason about, and the floor that defeated it looked correct precisely
    because the thing it was defeating was invisible.
    """
    return (
        _env_int("JARVIS_OV_CREST_MIN_COLS", 46, floor=1),
        _env_int("JARVIS_OV_CREST_MAX_COLS", 140, floor=1),
        _env_int("JARVIS_OV_CREST_RIGHT_MARGIN", 1),
    )


def _clamp_cols(measured: int) -> Tuple[int, int, int]:
    """Return (lo, hi, clamped) column bounds for a measured width.

    ``lo`` is the minimum MEASURED width -- the narrowest terminal that can
    hold a crest -- not the minimum crest width. Those differ by the margin,
    and conflating them is the bug documented below. ``clamped`` is only
    meaningful when ``measured >= lo``; callers must check the low bound
    against ``measured`` directly (not ``clamped``, which is floored by
    construction and so can never itself read as "below minimum").

    Width-only. Since 2026-07-25 this is the WIDTH HALF of
    :func:`_fit_cols`, which additionally solves for height — callers that
    know their row count must use that instead, or a wide-but-short window
    gets a crest too tall to draw and the emblem vanishes entirely.

    Strict bounding-box margin (operator finding 2026-07-18): a crest exactly
    as wide as the terminal auto-wraps its last cell onto the next line, and
    every row sheds one detached artifact block. One column of right margin
    kills the whole off-by-one wrap class.

    ...except at the boundary, where it did not (fixed 2026-08-19). The clamp
    read ``max(min_crest, min(measured - margin, hi))``, and that outer floor
    is reached exactly when ``measured <= min_crest`` -- at which point it
    hands back a crest as wide as the terminal and reinstates the wrap class
    the margin exists to kill. At the default bounds a 46-column terminal
    emitted 46-column rows: measured and reproducible, not theoretical.

    The floor is not the defect -- a crest narrower than ``min_crest`` stops
    reading as an emblem, so refusing to shrink past it is right. The defect
    was asking ONE number to answer two questions. A terminal must afford
    ``min_crest`` columns for the emblem PLUS ``margin`` for the margin, so
    the minimum measured width is their SUM, and below it the honest answer
    is "unavailable" rather than a crest that wraps. Returning that sum as
    ``lo`` makes the availability guard callers already run
    (``measured_cols < lo``) correct without a second check to forget.
    """
    min_crest, hi, margin = _crest_bounds()
    return (min_crest + margin, hi,
            max(min_crest, min(measured - margin, hi)))


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
    # Optional spin (boot animator): ``geo.v_rot`` (radians, default absent/0)
    # inverse-rotates the sample point around the V's centroid, so the SAME
    # segment tests draw a rotated V. Zero cost + zero behavior change when
    # unset — the static crest is untouched.
    v_rot = getattr(geo, "v_rot", 0.0)
    if v_rot:
        vcy = geo.cy + (geo.v_bot - geo.v_top) / 2.0   # the V's spin centre
        dx, dy = px - geo.cx, py - vcy
        ca, sa = math.cos(-v_rot), math.sin(-v_rot)
        px = geo.cx + dx * ca - dy * sa
        py = vcy + dx * sa + dy * ca
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
        lo, _hi, clamped = _fit_cols(measured_cols, measured_rows)
        if measured_cols < lo:
            return CrestFrame(0, 0, (), 0.0, f"width {measured_cols} < min {lo}")
        needed_rows = _Geometry.rows_needed(clamped)
        if measured_rows < needed_rows:
            # Only reachable when even the MINIMUM crest is too tall — the
            # fit already shrank as far as it is allowed to go.
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
        lo, _hi, clamped = _fit_cols(measured_cols, measured_rows)
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


def center_pad(crest_cols: int, term_cols: Optional[int] = None) -> int:
    """Left padding that centres a ``crest_cols``-wide emblem. Never negative,
    never wide enough to push the crest off the right edge.

    Measured here rather than passed down because the crest is centred on the
    TERMINAL, not on whatever container a caller happens to be inside — every
    consumer that has tried to own this ended up left-aligned."""
    try:
        if os.environ.get("JARVIS_OV_CREST_CENTER", "1").strip().lower() in (
                "0", "false", "no", "off"):
            return 0
        if term_cols is None:
            import shutil
            term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        return max(0, (int(term_cols) - int(crest_cols)) // 2)
    except Exception:  # noqa: BLE001
        return 0


def _indent(text: "Any", pad: int) -> "Any":
    """Prefix every rendered row with ``pad`` spaces, styles intact.

    Done on the composed Text instead of inside the two pixel/quadrant loops:
    padding is a placement concern, and threading an offset through both
    renderers would put the same arithmetic in two places that must agree."""
    if pad <= 0:
        return text
    try:
        from rich.text import Text
        out = Text()
        for i, line in enumerate(text.split("\n")):
            if i:
                out.append("\n")
            out.append(" " * pad)
            out.append_text(line)
        return out
    except Exception:  # noqa: BLE001
        return text


def render_crest_auto(
    frame: "CrestFrame", tier: ColorTier, *, elapsed: Optional[float] = None,
    term_cols: Optional[int] = None,
) -> "Any":
    """THE renderer dispatch: half-block pixels on capable terminals
    (C256+ and renderer=halfblock), quadrant glyphs otherwise. Both
    paths honor the reveal clock, and both come out centred on the
    terminal. NEVER raises."""
    body = None
    try:
        if crest_renderer() == "halfblock" and tier >= ColorTier.C256:
            pf = _generate_pixels_cached(frame.cols, frame.rows)
            body = pixels_to_text(pf, elapsed=elapsed)
    except Exception:  # noqa: BLE001
        body = None
    if body is None:
        body = frame_to_text(frame, tier, elapsed=elapsed)
    return _indent(body, center_pad(frame.cols, term_cols))


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

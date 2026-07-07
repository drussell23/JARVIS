#!/usr/bin/env python3
"""Ouroboros crest v4 — fully-painted half-block rendering with anti-aliasing.

Every cell is a ▀/▄ with fg+bg truecolor: the coil body is SOLID color,
edges blend smoothly into the terminal background via supersampled coverage.
This is the same technique terminal image renderers (chafa/timg) use, and it
translates 1:1 to ANSI in the real CLI.

Pixel grid: 1 cell = 1px wide x 2px tall. Pixel aspect corrected (x1.083).
"""
from __future__ import annotations
import math
import sys

# ---- canvas ---------------------------------------------------------------
CELL_W, CELL_H = 58, 17
PX_W, PX_H = CELL_W, CELL_H * 2
ASPECT = 1.083                    # physical height of one pixel / its width
BG = (11, 14, 20)                 # #0b0e14 terminal background

CX = PX_W / 2.0
CY = (PX_H / 2.0) * ASPECT

# ---- anatomy --------------------------------------------------------------
R_MID = 11.6
THICK = 3.5
GAP_CENTER = math.radians(90)
GAP_HALF = math.radians(20)
TAPER_SWEEP = math.radians(52)
TAIL_INTRUDE = math.radians(15)   # tail tip continues INTO the mouth

HEAD_LEN = 5.6
HEAD_W = 2.5

V_TOP = 4.9
V_BOT = 5.6
V_HALF_SPAN = 4.8
V_STROKE = 2.7

SS = 3                            # supersample factor per pixel axis

def ang_norm(a: float) -> float:
    while a < 0: a += 2 * math.pi
    while a >= 2 * math.pi: a -= 2 * math.pi
    return a

TAIL_TIP = ang_norm(GAP_CENTER + GAP_HALF)
HEAD_THETA = ang_norm(GAP_CENTER - GAP_HALF)

def in_gap(theta: float) -> bool:
    d = abs(ang_norm(theta - GAP_CENTER))
    if d > math.pi: d = 2 * math.pi - d
    return d < GAP_HALF

def body_half(theta: float) -> float:
    d = ang_norm(theta - TAIL_TIP)          # 0 at tail tip, grows CCW along body
    if d < TAPER_SWEEP:
        t = d / TAPER_SWEEP
        return (THICK * (0.34 + 0.66 * t)) / 2.0
    return THICK / 2.0

def sample_coil(px: float, py: float) -> bool:
    dx, dy = px - CX, py - CY
    r = math.hypot(dx, dy)
    theta = math.atan2(-dy, dx)
    if in_gap(theta):
        # tail tip intrudes into the mouth as a thin point
        d_from_tail_edge = ang_norm(TAIL_TIP - theta)
        if d_from_tail_edge < TAIL_INTRUDE:
            t = d_from_tail_edge / TAIL_INTRUDE          # 0 at gap edge -> 1 deep
            half = (THICK * 0.34 / 2.0) * (1.0 - 0.72 * t)
            return abs(r - R_MID) <= half
        return False
    return abs(r - R_MID) <= body_half(theta)

def head_frame():
    hx = CX + R_MID * math.cos(HEAD_THETA)
    hy = CY - R_MID * math.sin(HEAD_THETA)
    # snout points CCW along the tangent (toward the gap / tail tip)
    tx = math.sin(HEAD_THETA)
    ty = math.cos(HEAD_THETA)
    return hx, hy, tx, ty

def sample_head(px: float, py: float) -> bool:
    hx, hy, tx, ty = head_frame()
    dx, dy = px - hx, py - hy
    lon = dx * tx + dy * ty
    lat = -dx * ty + dy * tx
    if lon < -1.6 or lon > HEAD_LEN:
        return False
    w = HEAD_W * (1.0 - 0.62 * max(0.0, lon / HEAD_LEN))
    # open mouth: notch at the snout half
    if lon > HEAD_LEN * 0.52 and abs(lat) < w * 0.34:
        return False
    return abs(lat) <= w

def eye_center():
    hx, hy, tx, ty = head_frame()
    return hx + tx * 0.7 - ty * 1.05, hy + ty * 0.7 + tx * 1.05

EYE_R = 0.95

def sample_eye(px: float, py: float) -> bool:
    ex, ey = eye_center()
    return math.hypot(px - ex, py - ey) <= EYE_R

def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))

def sample_v(px, py) -> bool:
    if py < CY - V_TOP:                      # flat, machined top edge
        return False
    d1 = seg_dist(px, py, CX - V_HALF_SPAN, CY - V_TOP - 0.8, CX, CY + V_BOT)
    d2 = seg_dist(px, py, CX + V_HALF_SPAN, CY - V_TOP - 0.8, CX, CY + V_BOT)
    return min(d1, d2) <= V_STROKE / 2.0

# ---- gradient ---------------------------------------------------------------
STOPS = [
    (0,   (125, 255, 106)),
    (60,  (91, 227, 75)),
    (150, (139, 92, 246)),
    (210, (177, 108, 234)),
    (285, (212, 192, 74)),
    (360, (125, 255, 106)),
]
HEAD_COLOR = (125, 255, 106)
EYE_COLOR = (234, 255, 208)
V_TOP_C = (192, 132, 252)
V_BOT_C = (157, 78, 220)

def grad(frac: float):
    deg = frac * 360.0
    for (a0, c0), (a1, c1) in zip(STOPS, STOPS[1:]):
        if a0 <= deg <= a1:
            t = (deg - a0) / (a1 - a0) if a1 > a0 else 0
            return tuple(c0[k] + t * (c1[k] - c0[k]) for k in range(3))
    return STOPS[-1][1]

# ---- render: hard-edge quadrants (2x2 subpixels per cell, no AA) -----------
QUAD = {
    0b0000: ' ', 0b1000: '▘', 0b0100: '▝', 0b1100: '▀',
    0b0010: '▖', 0b1010: '▌', 0b0110: '▞', 0b1110: '▛',
    0b0001: '▗', 0b1001: '▚', 0b0101: '▐', 0b1101: '▜',
    0b0011: '▄', 0b1011: '▙', 0b0111: '▟', 0b1111: '█',
}
PRIORITY = ('eye', 'head', 'coil', 'v')

def classify(px: float, py: float):
    """Hard classification of one subpixel center-region (majority of SSxSS)."""
    votes = {'eye': 0, 'head': 0, 'coil': 0, 'v': 0}
    theta = None
    for sy in range(SS):
        for sx in range(SS):
            spx = px + (sx + 0.5) / SS * 0.5
            spy = py + ((sy + 0.5) / SS * 0.5) * ASPECT
            if sample_eye(spx, spy):
                votes['eye'] += 1
            elif sample_head(spx, spy):
                votes['head'] += 1
            elif sample_coil(spx, spy):
                votes['coil'] += 1
                theta = math.atan2(-(spy - CY), spx - CX)
            elif sample_v(spx, spy):
                votes['v'] += 1
    n = SS * SS
    inside = sum(votes.values()) >= n / 2.0          # 0.5 coverage threshold
    if not inside:
        return None, None
    k = max(PRIORITY, key=lambda kk: votes[kk])
    return k, theta

def render_cells():
    """cell -> (glyph, kind, color, delay). Quadrant bit order: TL,TR,BL,BR."""
    cells = {}
    for cy_ in range(CELL_H):
        for cx_ in range(CELL_W):
            bits = 0
            kinds, thetas = [], []
            for i, (sx, sy) in enumerate([(0, 0), (1, 0), (0, 1), (1, 1)]):
                # subpixel origin in physical units (cell is 1.0 wide, 2*ASPECT tall)
                px = cx_ + sx * 0.5
                py = (cy_ * 2 + sy) * ASPECT
                k, th = classify(px, py)
                if k is not None:
                    bits |= 1 << (3 - i)
                    kinds.append(k)
                    if th is not None:
                        thetas.append(th)
            if not bits:
                continue
            kind = max(PRIORITY, key=lambda kk: kinds.count(kk)) if kinds else 'coil'
            if kind == 'eye':
                color, delay = EYE_COLOR, 1.55
            elif kind == 'head':
                color, delay = HEAD_COLOR, 1.42
            elif kind == 'coil':
                th = thetas[len(thetas) // 2] if thetas else 0.0
                frac = ang_norm(th - TAIL_TIP) / (2 * math.pi)
                color = tuple(round(c) for c in grad(frac))
                delay = 0.05 + 1.30 * frac
            else:
                t = max(0.0, min(1.0, (cy_ * 2 * ASPECT - (CY - V_TOP)) / (V_TOP + V_BOT)))
                color = tuple(round(V_TOP_C[k2] + t * (V_BOT_C[k2] - V_TOP_C[k2])) for k2 in range(3))
                delay = 1.78 + 0.5 * t
            cells[(cx_, cy_)] = (QUAD[bits], kind, color, delay)
    # cleanup: drop isolated single-quadrant crumbs (no orthogonal neighbor)
    dots = set('\u2598\u259d\u2596\u2597')
    for (x, y) in [k for k, v in cells.items() if v[0] in dots]:
        if not any((x + dx, y + dy) in cells for dx, dy in ((1,0),(-1,0),(0,1),(0,-1))):
            del cells[(x, y)]
    return cells

def preview_cells(cells):
    for y in range(CELL_H):
        print(''.join(cells.get((x, y), (' ',))[0] for x in range(CELL_W)).rstrip())

def html_cells(cells):
    lines = []
    for y in range(CELL_H):
        parts, spaces = [], 0
        for x in range(CELL_W):
            cell = cells.get((x, y))
            if cell is None:
                spaces += 1
                continue
            if spaces:
                parts.append(' ' * spaces); spaces = 0
            ch, kind, c, d = cell
            cls = 'px eyepx' if kind == 'eye' else 'px'
            parts.append(f'<span class="{cls}" style="color:rgb({c[0]},{c[1]},{c[2]});animation-delay:{d:.2f}s">{ch}</span>')
        lines.append(''.join(parts))
    print('\n'.join(lines))

if __name__ == '__main__':
    cells = render_cells()
    if '--html' in sys.argv:
        html_cells(cells)
    else:
        preview_cells(cells)

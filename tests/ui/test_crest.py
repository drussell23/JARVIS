"""Crest v5 invariants: solid, crisp, anatomically complete, REACTIVE.
These are the professional-art regression net (spec §9.1)."""
from __future__ import annotations

import math
import os

import pytest

from backend.core.ouroboros.ui.crest import CrestFrame, generate_crest
from backend.core.ouroboros.ui.theme import ColorTier

T = ColorTier.TRUECOLOR


def gen(cols=60, rows=24, tier=T, unicode_ok=True) -> CrestFrame:
    return generate_crest(cols, rows, tier=tier, unicode_ok=unicode_ok)


def cells_by_kind(frame, kind):
    return [c for c in frame.cells if c.kind == kind]


# ---- anatomy invariants ----------------------------------------------------

def test_all_kinds_present():
    f = gen()
    assert f.unavailable_reason is None
    for kind in ("coil", "head", "eye", "v"):
        assert cells_by_kind(f, kind), f"missing {kind} cells"


def test_no_isolated_crumb_cells():
    f = gen()
    occupied = {(c.x, c.y) for c in f.cells}
    dots = set("▘▝▖▗")   # single-quadrant glyphs
    for c in f.cells:
        if c.glyph in dots:
            assert any((c.x + dx, c.y + dy) in occupied
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))), \
                f"isolated crumb at {(c.x, c.y)}"


def test_v_has_flat_top():
    f = gen()
    v = cells_by_kind(f, "v")
    top_y = min(c.y for c in v)
    top_row = [c for c in v if c.y == top_y]
    assert len(top_row) >= 4          # two stroke caps, each >=2 cells wide


def test_coil_is_solid_no_full_block_holes():
    """Between the leftmost and rightmost coil cell of each row band that
    contains full blocks, interior full-block runs are contiguous per flank
    (annulus: at most 2 runs per row -- left flank + right flank)."""
    f = gen()
    coil_rows = {}
    for c in cells_by_kind(f, "coil"):
        if c.glyph == "█":
            coil_rows.setdefault(c.y, []).append(c.x)
    assert coil_rows, "no solid coil interior at all"
    for y, xs in coil_rows.items():
        xs = sorted(xs)
        runs = 1
        for a, b in zip(xs, xs[1:]):
            if b - a > 1:
                runs += 1
        assert runs <= 2, f"row {y}: {runs} solid runs (holes in the body)"


def test_delays_monotonic_tail_to_head_and_bounded():
    f = gen()
    coil = cells_by_kind(f, "coil")
    assert all(0.0 <= c.delay_s <= 1.40 for c in coil)
    assert min(c.delay_s for c in coil) < 0.2       # trace starts at the tail
    head = cells_by_kind(f, "head")
    assert all(c.delay_s > max(c2.delay_s for c2 in coil) - 0.3 for c in head)
    assert f.max_delay_s >= 1.55                     # eye ignition is last


# ---- reactivity + clamping (Mandate 2) --------------------------------------

def test_geometry_scales_with_width():
    small, large = gen(cols=46), gen(cols=72)
    assert small.cols == 46 and large.cols == 72
    assert len(large.cells) > len(small.cells) * 1.5


def test_hard_clamp_at_72():
    f = gen(cols=200)
    assert f.cols == 72
    assert max(c.x for c in f.cells) < 72


def test_env_min_clamp(monkeypatch):
    monkeypatch.setenv("JARVIS_OV_CREST_MIN_COLS", "50")
    f = generate_crest(48, 24, tier=T, unicode_ok=True)
    assert f.unavailable_reason is not None          # below min -> unavailable


def test_below_default_min_unavailable():
    f = gen(cols=40)
    assert f.unavailable_reason is not None


def test_insufficient_rows_unavailable():
    f = gen(cols=60, rows=8)
    assert f.unavailable_reason is not None


# ---- tier / unicode degradation ---------------------------------------------

def test_no_unicode_unavailable():
    f = gen(unicode_ok=False)
    assert f.unavailable_reason is not None


def test_none_tier_unavailable():
    f = gen(tier=ColorTier.NONE)
    assert f.unavailable_reason is not None


def test_standard_tier_renders_geometry():
    f = gen(tier=ColorTier.STANDARD)
    assert f.unavailable_reason is None
    assert cells_by_kind(f, "coil")


# ---- style resolution --------------------------------------------------------

from backend.core.ouroboros.ui.crest import style_for_cell
from backend.core.ouroboros.ui.theme import Token, style_for


def test_truecolor_styles_are_rgb():
    f = gen()
    c = cells_by_kind(f, "coil")[0]
    assert style_for_cell(c, ColorTier.TRUECOLOR).startswith("rgb(")


def test_standard_styles_are_accent_mono():
    f = gen(tier=ColorTier.STANDARD)
    accent = style_for(Token.ACCENT, ColorTier.STANDARD)
    coil = cells_by_kind(f, "coil")[0]
    v = cells_by_kind(f, "v")[0]
    assert style_for_cell(coil, ColorTier.STANDARD) == accent
    assert style_for_cell(v, ColorTier.STANDARD) == f"bold {accent}"


def test_cache_hit_same_object():
    a = gen(cols=60)
    b = gen(cols=60)
    assert a is b        # lru_cache identity

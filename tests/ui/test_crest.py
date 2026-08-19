"""Crest v5 invariants: solid, crisp, anatomically complete, REACTIVE.
These are the professional-art regression net (spec §9.1)."""
from __future__ import annotations

import math
import os

import pytest

from backend.core.ouroboros.ui.crest import CrestFrame, generate_crest
from backend.core.ouroboros.ui.theme import ColorTier

T = ColorTier.TRUECOLOR


from backend.core.ouroboros.ui import crest as crest_mod


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
    """Wider terminal -> wider, denser crest.

    The crest's width is DERIVED from the terminal's, never equal to it:
    ``_clamp_cols`` holds back ``JARVIS_OV_CREST_RIGHT_MARGIN`` columns so the
    last cell cannot auto-wrap. This asserted ``cols == 46 and cols == 72``,
    which was the PRE-margin contract and has been red since 14a2a47ef5
    (2026-07-18) -- unseen because CI runs tests/unit and tests/integration,
    never tests/ui.

    The bounds are tunables; the scaling is the invariant. So the expected
    width is READ from the sizing function rather than restated -- the idiom
    ``test_hard_clamp_at_default_max`` below already uses.
    """
    lo, _hi, _c = crest_mod._clamp_cols(0)     # lo does not depend on measured
    small_term, large_term = lo, lo + 26
    rows = 60                                   # ample: isolate WIDTH scaling
    small, large = gen(cols=small_term, rows=rows), gen(cols=large_term,
                                                        rows=rows)
    assert small.unavailable_reason is None and large.unavailable_reason is None
    assert small.cols == crest_mod._fit_cols(small_term, rows)[2]
    assert large.cols == crest_mod._fit_cols(large_term, rows)[2]
    assert large.cols > small.cols
    assert len(large.cells) > len(small.cells) * 1.5


def test_the_crest_never_occupies_the_terminals_last_column():
    """The margin is the whole point of the clamp, and it was cancelled at the
    boundary for a month.

    ``max(min_crest, min(measured - margin, hi))`` reaches its outer floor
    exactly when ``measured <= min_crest``, handing back a crest as wide as
    the terminal -- the auto-wrap / detached-artifact class the margin exists
    to kill. At default bounds a 46-column terminal emitted 46-column rows.

    A single-width check (``_clamp_cols(80) == 79``) could not see it. Sweep
    the boundary, and assert the PROPERTY rather than any one number.
    """
    lo, hi, _c = crest_mod._clamp_cols(0)
    _min_crest, _hi, margin = crest_mod._crest_bounds()
    widths = list(range(lo - 2, lo + 6)) + [60, 72, 80, hi, hi + 1, hi + 60]
    for term in widths:
        _lo, _h, clamped = crest_mod._clamp_cols(term)
        if term < lo:
            continue                            # correctly unavailable
        assert clamped <= term - margin, (
            f"terminal {term}: crest {clamped} leaves no margin")


def test_a_terminal_too_narrow_for_crest_plus_margin_is_unavailable():
    """Availability must account for the margin, or the narrowest accepted
    terminal is precisely the one that wraps. ``lo`` is the minimum MEASURED
    width (crest + margin), not the minimum crest width."""
    lo, _hi, _c = crest_mod._clamp_cols(0)
    assert generate_crest(lo - 1, 60, tier=T,
                          unicode_ok=True).unavailable_reason is not None
    f = generate_crest(lo, 60, tier=T, unicode_ok=True)
    assert f.unavailable_reason is None
    assert f.cols < lo                          # the margin survived


def test_column_bounds_are_tunable_and_survive_a_malformed_override(
        monkeypatch):
    """Every bound is operator-tunable, and this module NEVER raises -- so an
    unparseable override must degrade to the default, not crash a render."""
    monkeypatch.setenv("JARVIS_OV_CREST_RIGHT_MARGIN", "3")
    assert crest_mod._clamp_cols(80)[2] == 77
    monkeypatch.setenv("JARVIS_OV_CREST_RIGHT_MARGIN", "not-a-number")
    assert crest_mod._clamp_cols(80)[2] == 79   # back to the default margin
    monkeypatch.setenv("JARVIS_OV_CREST_MAX_COLS", "")
    assert crest_mod._clamp_cols(400)[2] == crest_mod._crest_bounds()[1]


def test_hard_clamp_at_default_max():
    # 2026-07-18 sharpening pass: default max raised 72 -> 88.
    # 2026-07-25 SUPERSEDED: sizing is no longer width-only. ``gen`` supplies
    # rows=24, and on a 24-row terminal the binding constraint is HEIGHT, not
    # the column cap — an 88-column crest plus the ceremony's 6-line header
    # does not fit. Asserting 88 here was asserting that the crest ignores the
    # rows it was handed, which is the bug that made a wide-but-short window
    # either overflow or vanish.
    #
    # The invariants that survive: the cap still bounds the crest, the crest
    # still fits with its header, and no cell escapes the frame.
    from backend.core.ouroboros.ui.crest import CREST_HEADER_ROWS, _Geometry
    f = gen(cols=200)
    _lo, hi, _c = crest_mod._clamp_cols(200)
    assert f.cols <= hi
    assert _Geometry.rows_needed(f.cols) + CREST_HEADER_ROWS <= 24
    assert max(c.x for c in f.cells) < f.cols


def test_a_tall_terminal_lifts_the_height_constraint():
    """The same 200 columns, with rows to spend, reaches the cap — proof the
    shrink above is height doing its job and not a smaller ceiling."""
    _lo, hi, _c = crest_mod._clamp_cols(200)
    assert gen(cols=200, rows=60).cols == hi
    assert gen(cols=200, rows=60).cols > gen(cols=200, rows=24).cols


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

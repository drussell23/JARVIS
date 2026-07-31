"""Adaptive fluid breakpoints — the palette survives width starvation.

Strict two-column alignment introduces a geometric failure: on a narrow
terminal the description column collapses and every entry becomes a tower one
or two words wide. The table stops conveying anything precisely when the
operator has least room to spare.

The load-bearing decision is `stacked_mode` measuring the DESCRIPTION column
rather than the terminal width. A fixed column threshold measures the wrong
quantity, and `test_a_fixed_threshold_would_get_both_of_these_wrong` pins the
two cases that prove it.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.palette_render import (
    _GUTTER,
    absolute_stack_floor,
    layout_palette,
    min_desc_col,
    stacked_mode,
)

ENTRIES = [
    ("/anticipate", "help · panel · banners · prefetch · status"),
    ("/autobiography", "Retrospective audit of O+V-signed commits"),
    ("/backlog_auto_proposed", "Review auto-proposed backlog items"),
    ("/breadcrumbs", "Set/show the feed verbosity"),
]


def _lines(width, entries=None, **kw):
    return ["".join(t for _s, t in row) for row in layout_palette(
        entries or ENTRIES, width=width, max_rows=kw.pop("max_rows", 24), **kw)]


def _is_name_row(line: str) -> bool:
    return line.lstrip().startswith("/")


# ---------------------------------------------------------------------------
# The mandate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_width_100_yields_single_line_two_column_rows():
    """Above the breakpoint: one line per entry, name and description inline."""
    lines = _lines(100)
    assert len(lines) == len(ENTRIES), "an entry wrapped or stacked at width 100"
    for line, (name, desc) in zip(lines, ENTRIES):
        assert name in line and desc.split()[0] in line, (
            "name and description are not on the same line")


@pytest.mark.asyncio
async def test_width_40_snaps_to_stacked_multi_line_rows():
    """Below it: the name owns a row, the description is indented beneath."""
    lines = _lines(40)
    assert len(lines) > len(ENTRIES), "layout did not snap to stacked"
    for name, _desc in ENTRIES:
        row = next(ln for ln in lines if ln.strip() == name)
        after = lines[lines.index(row) + 1]
        assert not _is_name_row(after), "description did not follow its name"
        assert len(after) - len(after.lstrip()) > (
            len(row) - len(row.lstrip())), "description is not indented"


# ---------------------------------------------------------------------------
# Why the threshold is derived, not fixed
# ---------------------------------------------------------------------------


def test_a_fixed_threshold_would_get_both_of_these_wrong():
    """The reason `stacked_mode` reads the description column.

    A "stack below 60" rule keeps two-column at width 61 with a 40-char verb
    — leaving ~15 columns of description, the exact tower a breakpoint
    exists to prevent — and stacks at width 59 with 8-char verbs, where
    there was ample room.
    """
    assert stacked_mode(61, 40) is True, "wide-but-starved was not caught"
    assert stacked_mode(59, 8) is False, "narrow-but-roomy was over-stacked"


def test_the_snap_point_matches_the_legibility_floor():
    """Isolates the DERIVED snap from the absolute backstop.

    The name column has to be wide enough that the derived boundary lands
    above `absolute_stack_floor()`, or the backstop answers first and this
    tests that instead — which it already does in its own test.
    """
    name_col = 30
    just_enough = min_desc_col() + name_col + _GUTTER + 2
    assert just_enough > absolute_stack_floor(), "floor would answer first"
    assert stacked_mode(just_enough, name_col) is False
    assert stacked_mode(just_enough - 1, name_col) is True


def test_the_absolute_floor_is_a_backstop():
    """Degenerate terminals stack regardless of how short the names are."""
    assert stacked_mode(absolute_stack_floor() - 1, 4) is True


@pytest.mark.parametrize("env,width,name_col,expected", [
    ("20", 100, 8, False),
    ("90", 100, 8, True),
])
def test_the_legibility_floor_is_tunable(monkeypatch, env, width, name_col,
                                         expected):
    monkeypatch.setenv("JARVIS_PALETTE_MIN_DESC_COL", env)
    assert stacked_mode(width, name_col) is expected


def test_the_absolute_floor_is_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_PALETTE_STACK_FLOOR", "80")
    assert stacked_mode(70, 4) is True


@pytest.mark.parametrize("junk", ["", "abc", "-1"])
def test_knobs_survive_junk(monkeypatch, junk):
    monkeypatch.setenv("JARVIS_PALETTE_MIN_DESC_COL", junk)
    monkeypatch.setenv("JARVIS_PALETTE_STACK_FLOOR", junk)
    assert isinstance(stacked_mode(100, 10), bool)


def test_stacked_mode_never_raises():
    for args in ((0, 0), (-5, 3), ("x", None)):
        assert isinstance(stacked_mode(*args), bool)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Structural invariants that must hold in BOTH modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("width", [200, 120, 100, 80, 61, 56, 55, 40, 30, 20, 12])
def test_no_row_ever_overflows_the_terminal(width):
    """The invariant that makes narrow terminals safe at all."""
    for line in _lines(width):
        assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


@pytest.mark.parametrize("width", [100, 40])
def test_the_line_budget_is_honoured_in_both_modes(width):
    """Stacked entries cost 1 + description lines.

    A budget that assumed two-column would silently overrun by one line per
    entry on exactly the narrow terminal with least room to absorb it.
    """
    assert len(_lines(width, max_rows=6)) <= 6


@pytest.mark.parametrize("width", [100, 40])
def test_every_visible_entry_keeps_its_description(width):
    lines = _lines(width)
    blob = " ".join(lines)
    for _name, desc in ENTRIES:
        assert desc.split()[0] in blob


def test_a_pathological_name_is_still_bounded_when_stacked():
    """Stacked gives the name the full width — it must still not overflow."""
    entries = [("/" + "z" * 200, "long")]
    for line in _lines(30, entries=entries):
        assert len(line) <= 30
    assert any("…" in ln for ln in _lines(30, entries=entries))


def test_stacked_name_is_not_clipped_by_the_two_column_fraction_cap():
    """With no description sharing the row, the 34% cap would clip to protect
    a column that no longer exists."""
    entries = [("/backlog_auto_proposed", "d")]
    lines = _lines(40, entries=entries)
    assert lines[0].strip() == "/backlog_auto_proposed"


def test_selection_highlight_survives_the_snap():
    for width in (100, 40):
        rows = layout_palette(ENTRIES, width=width, selected=1, max_rows=24)
        styles = {s for row in rows for s, _t in row}
        assert any("current" in s for s in styles), width


def test_empty_and_degenerate_inputs():
    assert layout_palette([], width=40) == []
    assert layout_palette(ENTRIES, width=0) == []

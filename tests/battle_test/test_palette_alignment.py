"""The `/` palette lays out like Claude Code's: one description column.

The reported defect: three rows aligned and the fourth ragged, which reads as
a rendering bug rather than as the considered trade it was. `_NAME_FIT_QUANTILE`
sized the name column to fit 80% of names so one long verb could not dictate
the layout — a real concern, guarded by a redundant mechanism.
`_NAME_COL_MAX_FRACTION` already bounds the column and `_ellipsis` already
truncates a name that exceeds it, so the quantile bought nothing the cap did
not already provide and charged the alignment for it.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.palette_render import (
    layout_palette,
    palette_rows,
)

SCREENSHOT = [
    ("/anticipate", "help · panel · banners · prefetch · status"),
    ("/autobiography", "Retrospective audit of O+V-signed commits"),
    ("/backlog_auto_proposed", "Review auto-proposed backlog items"),
    ("/breadcrumbs", "Set/show the feed verbosity"),
]


def _lines(entries, width=120):
    return ["".join(t for _s, t in row) for row in layout_palette(
        entries, width=width)]


def _desc_column(line: str) -> int:
    """Where the description starts — after the name and its gutter."""
    stripped = line.lstrip()
    lead = len(line) - len(stripped)
    name, sep, rest = stripped.partition(" ")
    # `sep` is one space of the gutter, consumed by partition. Forgetting it
    # makes every row read one column early — harmless when comparing rows
    # against each other, wrong the moment it is compared to a real indent.
    return (lead + len(name) + len(sep)
            + (len(rest) - len(rest.lstrip())))


def _row_starts(lines):
    """Only the lines that BEGIN an entry.

    A wrapped description continues on a hanging-indent line with no name on
    it. Measuring those as row starts finds the first word of the
    continuation instead of the column, which is a property of the test, not
    of the layout.
    """
    return [ln for ln in lines if ln.lstrip().startswith("/")]


def test_every_description_starts_in_the_same_column():
    """The reported defect, and the CC contract."""
    columns = {_desc_column(line) for line in _row_starts(_lines(SCREENSHOT))}
    assert len(columns) == 1, (
        f"descriptions start at {sorted(columns)} — ragged rows are back")


def test_the_longest_name_sets_the_column():
    lines = _lines(SCREENSHOT)
    longest = max(len(n) for n, _ in SCREENSHOT)
    assert _desc_column(lines[0]) >= longest


def test_a_pathological_name_is_ellipsised_not_allowed_to_eat_the_row():
    """The case the quantile was invented for — already handled by the cap."""
    entries = SCREENSHOT + [("/" + "x" * 60, "a pathologically long verb")]
    lines = _lines(entries, width=80)
    assert any("…" in line for line in lines), "cap never engaged"
    assert len({_desc_column(l) for l in _row_starts(lines)}) == 1
    for line in lines:
        assert len(line) <= 80, "a row overflowed the terminal"


def test_alignment_holds_across_terminal_widths():
    for width in (60, 80, 100, 120, 200):
        lines = _lines(SCREENSHOT, width=width)
        starts = _row_starts(lines)
        assert len({_desc_column(line) for line in starts}) == 1, width
        for line in lines:
            assert len(line) <= width, f"row overflowed at width {width}"


def test_a_single_entry_does_not_crash_or_pad_absurdly():
    lines = _lines([("/x", "one")])
    assert len(lines) == 1 and "one" in lines[0]


def test_empty_entries_render_nothing():
    assert layout_palette([], width=80) == []


def test_quantile_knob_still_restores_dense_rows(monkeypatch):
    """A dense-row preference stays expressible — it is just not the default."""
    monkeypatch.setenv("JARVIS_PALETTE_NAME_QUANTILE", "0.5")
    assert len({_desc_column(l) for l in _row_starts(_lines(SCREENSHOT))}) > 1


def test_palette_height_has_exactly_one_definition():
    """`bipartite_layout` read the same env var with its own default of 12
    while this used 4 — the height depended on which renderer mounted."""
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        _palette_height,
    )
    assert _palette_height() == palette_rows()


def test_palette_height_env_moves_both_readers(monkeypatch):
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        _palette_height,
    )
    monkeypatch.setenv("JARVIS_PALETTE_HEIGHT", "7")
    assert palette_rows() == 7
    assert _palette_height() == 7


@pytest.mark.parametrize("bad", ["", "abc", "-5", "999"])
def test_palette_height_is_bounded_against_junk(monkeypatch, bad):
    monkeypatch.setenv("JARVIS_PALETTE_HEIGHT", bad)
    assert 3 <= palette_rows() <= 30


def test_narrow_terminal_wraps_with_a_hanging_indent():
    """CC wraps long descriptions under the column rather than truncating.

    The continuation must hang at the SAME column the description starts at,
    or a wrapped row reads as a new entry.
    """
    lines = _lines(SCREENSHOT, width=60)
    starts = _row_starts(lines)
    continuations = [ln for ln in lines if ln not in starts and ln.strip()]
    assert continuations, "width 60 produced no wrapping to check"
    column = _desc_column(starts[0])
    for cont in continuations:
        assert len(cont) - len(cont.lstrip()) == column, (
            "continuation does not hang at the description column")

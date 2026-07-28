"""The column fits most names, not every name.

Sizing to the longest visible name lets one outlier dictate the layout. With
`/backlog_auto_proposed` (22 chars) on screen, `/anticipate` was followed by
FOURTEEN spaces of dead gutter — and the eye crosses all of it on every row
to reach the description.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.palette_render import (
    _GUTTER, _name_column, layout_palette, name_fit_quantile,
)

_NAMES = ["/anticipate", "/autobiography", "/backlog_auto_proposed",
          "/breadcrumbs"]
_ENTRIES = [
    ("/anticipate", "Show what the organism expects to happen next"),
    ("/autobiography", "Read or refresh the organism-authored history"),
    ("/backlog_auto_proposed", "List and approve auto-proposed backlog items"),
    ("/breadcrumbs", "Set/show the feed verbosity"),
]


def _rows(width: int = 100) -> list:
    return ["".join(t for _s, t in row).rstrip()
            for row in layout_palette(_ENTRIES, width=width, max_rows=8)]


def test_one_outlier_does_not_stretch_every_row() -> None:
    """THE fix. The longest name is 22; sizing to it gave `/anticipate`
    fourteen spaces of gutter."""
    assert _name_column(_NAMES) < max(len(n) for n in _NAMES)


def test_the_common_rows_sit_close_to_their_descriptions() -> None:
    row = next(r for r in _rows() if "/anticipate" in r)
    gap = len(row) - len(row.lstrip())
    inner = row.strip()
    spaces = len(inner) - len(inner.replace("  ", " ", 1))
    assert "  " in inner
    before_desc = inner.index("Show")
    assert before_desc - len("/anticipate") < 12, (
        f"still {before_desc - len('/anticipate')} spaces of dead gutter"
    )
    assert gap >= 0


def test_a_long_name_is_NEVER_clipped() -> None:
    """It is the thing the operator has to type. `/backlog_auto_prop…` has
    stopped being a palette entry."""
    joined = "\n".join(_rows())
    assert "/backlog_auto_proposed" in joined
    assert "…" not in joined.split("List and approve")[0]


def test_an_overflowing_name_goes_ragged_rather_than_shrinking_others() -> None:
    """One ragged row is strictly cheaper than every row paying for the
    outlier."""
    rows = _rows()
    outlier = next(r for r in rows if "/backlog_auto_proposed" in r)
    common = next(r for r in rows if "/anticipate" in r)
    assert outlier.index("List") > common.index("Show")


def test_an_overflowing_row_stays_inside_the_terminal() -> None:
    """Re-measured per row, so a long name cannot push its description past
    the edge."""
    for width in (60, 80, 100, 140):
        for row in _rows(width):
            assert len(row) <= width, f"row overran {width} cols"


def test_uniform_names_are_unaffected() -> None:
    """When nothing is an outlier the column is the max, as before."""
    names = ["/one", "/two", "/six"]
    assert _name_column(names) == max(len(n) for n in names)


def test_descriptions_still_wrap_on_a_narrow_terminal() -> None:
    rows = _rows(60)
    assert any(r.strip().startswith("next") or r.strip().startswith("history")
               for r in rows), "wrapping stopped working"


def test_the_quantile_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """It trades two real costs: lower tightens common rows and ragged-wraps
    more outliers; higher aligns everything and spends width on gutter."""
    monkeypatch.setenv("JARVIS_PALETTE_NAME_QUANTILE", "1.0")
    assert name_fit_quantile() == 1.0
    assert _name_column(_NAMES) == max(len(n) for n in _NAMES)
    monkeypatch.setenv("JARVIS_PALETTE_NAME_QUANTILE", "0.1")
    assert _name_column(_NAMES) < max(len(n) for n in _NAMES)


def test_the_gutter_is_preserved_for_every_row() -> None:
    for row in _rows():
        inner = row.strip()
        assert " " * _GUTTER in inner or "  " in inner


@pytest.mark.parametrize("names", [[], ["/x"], ["", ""]])
def test_degenerate_name_lists_never_raise(names) -> None:
    assert isinstance(_name_column(names), int)

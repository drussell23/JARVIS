"""The column fits EVERY name, so every description starts in one place.

This file previously argued the opposite, and the argument was good: sizing
to the longest visible name let one outlier dictate the layout, and with
`/backlog_auto_proposed` (22 chars) on screen `/anticipate` got fourteen
spaces of dead gutter.

What it missed is that `_NAME_COL_MAX_FRACTION` + `_ellipsis` ALREADY bound
that case — a 60-char verb at width 80 renders ellipsised with every
description still aligned. The quantile was a second mechanism guarding a
case the cap already handled, and the price was the alignment itself: three
rows lined up and the fourth ragged, which reads as a rendering bug rather
than as the considered trade it was.

Claude Code aligns every row. The operator asked for that, the cap makes it
safe, and `JARVIS_PALETTE_NAME_QUANTILE` keeps the dense-row preference
expressible for anyone who wants it back.
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


def _starts(rows: list) -> list:
    return [r for r in rows if r.lstrip().startswith("/")]


def test_the_column_fits_every_name() -> None:
    """THE contract. Uniform alignment is what makes the list scannable."""
    assert _name_column(_NAMES) == max(len(n) for n in _NAMES)


def test_every_description_starts_in_the_same_column() -> None:
    columns = set()
    for row in _starts(_rows()):
        inner = row.lstrip()
        name = inner.split(" ", 1)[0]
        columns.add((len(row) - len(inner)) + len(name)
                    + (len(inner) - len(name) - len(inner[len(name):].lstrip())))
    assert len(columns) == 1, f"ragged rows are back: {sorted(columns)}"


def test_a_name_past_the_cap_is_ellipsised_not_allowed_to_stretch_the_row(
) -> None:
    """The case the quantile was invented for — handled by the cap."""
    entries = _ENTRIES + [("/" + "z" * 70, "a pathologically long verb")]
    rows = layout_palette(entries, width=80, max_rows=8)
    lines = ["".join(t for _s, t in r) for r in rows]
    assert any("\u2026" in ln for ln in lines), "the cap never engaged"
    for line in lines:
        assert len(line) <= 80


def test_descriptions_still_wrap_on_a_narrow_terminal() -> None:
    """Wrapping is asserted by SHAPE, not by which word lands on line two.

    The previous version pinned the wrap point, so widening the name column
    by eight characters read as "wrapping stopped working" when what had
    actually changed was where the sentence broke.
    """
    rows = _rows(60)
    assert len(rows) > len(_starts(rows)), "no continuation lines at width 60"


def test_continuations_hang_at_the_description_column() -> None:
    rows = _rows(60)
    starts = _starts(rows)
    inner = starts[0].lstrip()
    name = inner.split(" ", 1)[0]
    column = ((len(starts[0]) - len(inner)) + len(name)
              + (len(inner) - len(name) - len(inner[len(name):].lstrip())))
    for row in rows:
        if row in starts or not row.strip():
            continue
        assert len(row) - len(row.lstrip()) == column


def test_the_quantile_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dense-row preference stays expressible — it is not the default."""
    monkeypatch.setenv("JARVIS_PALETTE_NAME_QUANTILE", "1.0")
    assert name_fit_quantile() == 1.0
    assert _name_column(_NAMES) == max(len(n) for n in _NAMES)
    monkeypatch.setenv("JARVIS_PALETTE_NAME_QUANTILE", "0.1")
    assert _name_column(_NAMES) < max(len(n) for n in _NAMES)


def test_the_gutter_is_preserved_for_every_row() -> None:
    for row in _starts(_rows()):
        inner = row.strip()
        name = inner.split(" ", 1)[0]
        rest = inner[len(name):]
        assert len(rest) - len(rest.lstrip()) >= _GUTTER

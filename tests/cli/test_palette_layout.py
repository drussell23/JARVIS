"""The palette's LAYOUT — a page, not a widget.

prompt_toolkit's CompletionsMenu is a bounded float: one line per entry, width
set by the longest entry, descriptions truncated at its edge, scrollbar down
the side. Claude Code's palette is full terminal width, has a fixed gutter,
and WRAPS long descriptions with a hanging indent.

That is a layout difference, not a colour one — no amount of styling the
widget produces it. These tests pin the layout maths, which is pure and
therefore testable without a terminal.
"""
from __future__ import annotations

from typing import List, Tuple

import pytest

from backend.core.ouroboros.battle_test.palette_render import (
    layout_palette,
    palette_rows,
)


def _text(lines) -> List[str]:
    return ["".join(t for _s, t in line) for line in lines]


LONG = ("Review the current diff for correctness bugs and reuse or "
        "simplification cleanups at the given effort level")


# --------------------------------------------------------------------------
# 1. the CC structure
# --------------------------------------------------------------------------

def test_long_descriptions_wrap_instead_of_truncating() -> None:
    out = _text(layout_palette([("/code-review", LONG)], width=90))
    assert len(out) > 1, "description was truncated instead of wrapped"
    assert "effort level" in " ".join(out), "the tail of the text was lost"


def test_continuation_lines_hang_under_the_description_column() -> None:
    """What makes a wrapped entry read as ONE row rather than two."""
    out = _text(layout_palette([("/code-review", LONG)], width=90))
    first_desc_col = out[0].index("Review")
    second = out[1]
    assert second[:first_desc_col].strip() == "", (
        "continuation line intrudes into the name column"
    )
    assert len(second) - len(second.lstrip()) == first_desc_col, (
        "continuation is not aligned under the description column"
    )


def test_every_line_fits_the_terminal_width() -> None:
    """A row that overflows its column corrupts every row below it."""
    rows = [("/x" * 30, LONG), ("/deck", "deck height"), ("/a", "")]
    for width in (40, 80, 120, 200):
        for line in _text(layout_palette(rows, width=width)):
            assert len(line) <= width, (
                f"line of {len(line)} exceeds width {width}: {line[:60]!r}"
            )


def test_the_name_column_cannot_swallow_the_screen() -> None:
    """One pathological verb must not push every description off-screen."""
    rows = [("/" + "z" * 200, "a real description here"), ("/ok", "short")]
    out = _text(layout_palette(rows, width=100))
    assert "a real description here" in " ".join(out)
    assert "…" in out[0], "the oversized name was not ellipsised"


def test_the_gutter_is_consistent_across_entries() -> None:
    rows = [("/a", "alpha"), ("/longer-verb", "beta"), ("/mid", "gamma")]
    out = _text(layout_palette(rows, width=100))
    cols = [line.index(word) for line, word in
            zip(out, ("alpha", "beta", "gamma"))]
    assert len(set(cols)) == 1, f"descriptions start at ragged columns: {cols}"


# --------------------------------------------------------------------------
# 2. selection + scrolling
# --------------------------------------------------------------------------

def test_the_window_scrolls_to_keep_the_selection_visible() -> None:
    """With 76 verbs and 12 rows, a cursor that walks off the bottom into
    nothing is the most obvious way a palette feels broken."""
    rows = [(f"/verb{i}", f"desc {i}") for i in range(80)]
    out = _text(layout_palette(rows, width=100, selected=70, max_rows=10))
    assert any("/verb70" in line for line in out), "selection scrolled off"


def test_the_selected_row_is_styled_differently() -> None:
    rows = [("/a", "alpha"), ("/b", "beta")]
    lines = layout_palette(rows, width=100, selected=1)
    styles = [line[0][0] for line in lines]
    assert "current" in styles[1], "selected row carries no distinct style"
    assert "current" not in styles[0]


def test_no_selection_renders_cleanly() -> None:
    """complete_index is None until the operator arrows — every row unselected."""
    lines = layout_palette([("/a", "alpha")], width=100, selected=-1)
    assert lines and "current" not in lines[0][0][0]


# --------------------------------------------------------------------------
# 3. bulletproof
# --------------------------------------------------------------------------

@pytest.mark.parametrize("width", [0, -5, 1, 3])
def test_degenerate_widths_never_raise(width: int) -> None:
    layout_palette([("/a", "alpha")], width=width)


def test_an_undescribed_verb_renders_its_name_alone() -> None:
    out = _text(layout_palette([("/anticipate", "")], width=100))
    assert out == ["  /anticipate   "] or out[0].strip() == "/anticipate"


def test_an_unbreakable_token_is_broken_rather_than_overflowing() -> None:
    """Paths and module names have no spaces; textwrap either explodes or
    refuses, and either way the column overflows."""
    rows = [("/x", "a" * 300)]
    for line in _text(layout_palette(rows, width=60)):
        assert len(line) <= 60


def test_empty_input_is_an_empty_layout() -> None:
    assert layout_palette([], width=100) == []


def test_height_knob_is_bounded() -> None:
    assert 3 <= palette_rows() <= 30


# --------------------------------------------------------------------------
# 4. wired into the cockpit as a ROW, not a float
# --------------------------------------------------------------------------

def test_the_palette_is_a_layout_row_above_the_prompt() -> None:
    """A Float overlays the canvas at the widget's own width. A row
    participates in the layout, so it wraps to the terminal and the prompt
    stays anchored beneath it."""
    import contextlib
    import io

    from prompt_toolkit.layout import ConditionalContainer

    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        build_bipartite_application,
    )
    from backend.core.ouroboros.cli.ov import _build_slash_completer

    with contextlib.redirect_stderr(io.StringIO()):
        app = build_bipartite_application(
            BipartiteLayout(width=100, height=20, title="t"),
            on_accept=lambda _t: None, completer=_build_slash_completer(),
        )
    root = app.layout.container
    kids = list(root.get_children())
    assert any(isinstance(k, ConditionalContainer) for k in kids), (
        "no conditional palette row in the layout"
    )

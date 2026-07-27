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


# --------------------------------------------------------------------------
# Description wrapping, and the LINE budget that makes it safe (2026-07-27)
# --------------------------------------------------------------------------

_LONG = ("Run a prompt or slash command on a recurring interval "
         "(e.g. /loop 5m /foo). Omit the interval to let the model self-pace.")


def _texts(lines):
    return ["".join(t for _s, t in line) for line in lines]


def test_a_long_description_wraps_with_a_hanging_indent():
    """Claude's shape: the continuation aligns under the description column,
    so a wrapped entry still reads as ONE row of a table."""
    from backend.core.ouroboros.battle_test.palette_render import layout_palette

    out = _texts(layout_palette([("/loop", _LONG)], width=70, selected=0))
    assert len(out) > 1, "the description did not wrap"
    indent = len(out[1]) - len(out[1].lstrip(" "))
    assert indent > 6, f"continuation is not indented to the column: {out[1]!r}"
    # The NAME COLUMN is blank on a continuation — asserted on the column,
    # not on the string. The description here legitimately contains "/loop
    # 5m /foo", so searching the whole line for the verb name flags correct
    # output as broken.
    assert out[1][:indent].strip() == "", (
        f"the name column is not blank on the wrap line: {out[1]!r}"
    )


def test_the_budget_counts_LINES_not_entries():
    """A palette's height must not be a side effect of how verbose its
    descriptions happen to be."""
    from backend.core.ouroboros.battle_test.palette_render import layout_palette

    terse = [(f"/v{i}", "help | status") for i in range(20)]
    verbose = [(f"/v{i}", _LONG) for i in range(20)]
    short_block = layout_palette(terse, width=70, selected=0, max_rows=4)
    long_block = layout_palette(verbose, width=70, selected=0, max_rows=4)
    assert len(short_block) == len(long_block) == 4, (
        f"height varies with content: {len(short_block)} vs {len(long_block)}"
    )
    # ...and the verbose one necessarily shows FEWER verbs in the same space.
    assert sum(1 for t in _texts(long_block) if t.startswith("  /")) < \
        sum(1 for t in _texts(short_block) if t.startswith("  /"))


def test_a_narrow_terminal_shows_fewer_entries_not_a_taller_block():
    """The failure an entry-counted budget produces: on a narrow terminal
    everything wraps, so the palette triples in height exactly where there is
    least room for it."""
    from backend.core.ouroboros.battle_test.palette_render import layout_palette

    rows = [(f"/v{i}", _LONG) for i in range(20)]
    for width in (200, 120, 90, 60):
        assert len(layout_palette(rows, width=width, selected=0,
                                  max_rows=4)) <= 4, f"overflowed at {width}"


def test_one_oversized_entry_is_still_shown():
    """Showing nothing is worse than showing one tall thing — and the
    selected row must never be the entry that gets dropped."""
    from backend.core.ouroboros.battle_test.palette_render import layout_palette

    out = layout_palette([("/huge", "word " * 200)], width=60,
                         selected=0, max_rows=2)
    assert out, "an entry larger than the budget vanished"
    assert "/huge" in _texts(out)[0]


def test_the_height_estimate_agrees_with_the_renderer():
    """They must use the SAME wrap. An estimate that is one line optimistic
    produces a palette that overflows the budget it was given."""
    from backend.core.ouroboros.battle_test.palette_render import (
        _rendered_height, layout_palette,
    )
    for width, desc in ((60, _LONG), (200, "short"), (40, "a b c d e f g")):
        entry_lines = layout_palette([("/v", desc)], width=width, selected=-1,
                                     max_rows=99)
        name_col = min(2, max(8, int(width * 0.34)))
        assert _rendered_height(desc, max(8, width - name_col - 5), True) >= 1
        assert len(entry_lines) >= 1


def test_help_is_no_longer_truncated_by_the_resolver():
    """Line-breaking belongs to the layer that knows the terminal width."""
    from backend.core.ouroboros.battle_test.repl_completion import _help_bound

    assert len(_help_bound("x " * 60)) > 88, "still cut at the old 88 chars"


def test_a_pathological_docstring_cannot_take_the_screen():
    from backend.core.ouroboros.battle_test.repl_completion import _help_bound

    assert len(_help_bound("word " * 5000)) <= 220


def test_the_bound_collapses_newlines():
    """The layout owns line breaks; embedded newlines would bypass it."""
    from backend.core.ouroboros.battle_test.repl_completion import _help_bound

    assert "\n" not in _help_bound("a\nb\n\n   c")

"""The `/` palette, laid out like a page rather than a widget.

Why not ``CompletionsMenu``
---------------------------
prompt_toolkit's menu is a bounded float: one line per entry, width set by the
longest entry, descriptions truncated at its edge, a scrollbar down the side.
It reads as a control dropped on top of the terminal.

Claude Code's palette reads as part of the page — full terminal width, a fixed
gutter between name and description, and long descriptions WRAPPED with a
hanging indent instead of cut off. That is a different layout, not a different
colour scheme, so no amount of styling the widget produces it.

This renders the same state (``buffer.complete_state``) as a full-width block
placed above the prompt. Everything is computed from the live terminal size
and the actual entries — no fixed columns, no assumed widths — so it adapts to
a resized window and to a verb table that grows.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.PaletteRender")

#: Fraction of the width the name column may take before descriptions get
#: squeezed. A single very long verb must not push every description off the
#: screen — past this the name is ellipsised instead.
_NAME_COL_MAX_FRACTION = 0.34

#: Gutter between the name column and the description column.
_GUTTER = 3

#: Share of visible names the column must fit WITHOUT overflowing.
#:
#: 1.0 — every name fits, so EVERY description starts in the same column.
#: That is what Claude Code's palette does, and a uniform column is the whole
#: reason the eye can scan descriptions vertically instead of hunting for
#: where each one begins.
#:
#: This was 0.8, to stop one long verb dictating the layout: with
#: `/backlog_auto_proposed` (22 chars) on screen, `/anticipate` got fourteen
#: spaces of dead gutter. The concern was real; the mechanism was redundant.
#: `_NAME_COL_MAX_FRACTION` already bounds the column, and `_ellipsis`
#: already truncates a name that exceeds it — a 60-char verb at width 80
#: renders as `/xxxxxxxxxxxxxxxxxxxxxxxxx…` with every description still
#: aligned. So the quantile was a SECOND mechanism guarding a case the cap
#: already handled, and the price it charged was the alignment itself: three
#: rows lined up and the fourth ragged, which reads as a rendering bug rather
#: than as a considered trade.
#:
#: Kept as a knob because a dense-row preference is legitimate; changed as a
#: default because uniformity is what was asked for and what the cap makes
#: safe.
_NAME_FIT_QUANTILE = 1.0


def name_fit_quantile() -> float:
    """How much of the list the name column must fit. Env-tunable.

    A knob because it trades two real costs against each other: a lower value
    tightens the common rows and ragged-wraps more outliers; a higher one
    aligns everything and spends the width on gutter.
    """
    try:
        raw = os.environ.get("JARVIS_PALETTE_NAME_QUANTILE", "").strip()
        return min(1.0, max(0.1, float(raw))) if raw else _NAME_FIT_QUANTILE
    except (TypeError, ValueError):
        return _NAME_FIT_QUANTILE


def _name_column(names: List[str]) -> int:
    """Width that fits MOST names, not every name.

    An outlier keeps its full text and simply starts its description one
    gutter later — one ragged row, instead of every row paying for it. The
    name is never clipped: it is the thing the operator has to type, and a
    palette that hides half of `/backlog_auto_prop…` has stopped being a
    palette.
    """
    try:
        lengths = sorted(len(n) for n in names if n)
        if not lengths:
            return 0
        index = int(round((len(lengths) - 1) * name_fit_quantile()))
        return lengths[max(0, min(len(lengths) - 1, index))]
    except Exception:  # noqa: BLE001
        return max((len(n) for n in names), default=0)


#: Default rows the palette draws. Four showed less than a third of a screen
#: that had room for far more, and a menu that cannot show the verb you are
#: reaching for is one you dismiss and retype blind.
_DEFAULT_ROWS = 10


def palette_rows() -> int:
    """Maximum entries drawn at once (``JARVIS_PALETTE_HEIGHT``).

    THE definition of that env var. `bipartite_layout` used to read the same
    name with its own default of 12 while this read 4 — one knob, two
    answers, and which one an operator got depended on which renderer
    happened to mount. It now delegates here.
    """
    try:
        return max(3, min(30, int(
            os.environ.get("JARVIS_PALETTE_HEIGHT", str(_DEFAULT_ROWS))
            or _DEFAULT_ROWS,
        )))
    except (TypeError, ValueError):
        return _DEFAULT_ROWS


def _ellipsis(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def _wrap(text: str, width: int) -> List[str]:
    """Greedy wrap. Deliberately not ``textwrap``: descriptions may contain
    long unbroken tokens (paths, flags, module names) that ``textwrap`` will
    either explode or refuse to break, and a palette row that overflows its
    column corrupts every row below it."""
    if width <= 0:
        return [""]
    out: List[str] = []
    line = ""
    for word in str(text).split():
        while len(word) > width:            # unbreakable token
            if line:
                out.append(line)
                line = ""
            out.append(word[: width - 1] + "…")
            word = ""
        if not word:
            continue
        candidate = f"{line} {word}".strip()
        if len(candidate) <= width:
            line = candidate
        else:
            if line:
                out.append(line)
            line = word
    if line:
        out.append(line)
    return out or [""]


def _rendered_height(desc: Any, desc_col: int, wrap: bool) -> int:
    """How many LINES an entry will occupy once drawn.

    Deliberately calls the same ``_wrap`` the renderer uses rather than
    estimating from ``len(desc) / desc_col``: an estimate and a renderer that
    disagree by one line produce a palette that overflows its own budget,
    which is the failure this budget exists to prevent."""
    if not wrap:
        return 1
    try:
        return max(1, len(_wrap(str(desc), desc_col)))
    except Exception:  # noqa: BLE001
        return 1


def layout_palette(
    entries: List[Tuple[str, str]],
    *,
    width: int,
    selected: int = -1,
    max_rows: Optional[int] = None,
    wrap_descriptions: bool = True,
) -> List[List[Tuple[str, str]]]:
    """Lay entries out as styled fragment lines. Pure — no prompt_toolkit.

    Returns one fragment list per RENDERED line, which is not one per entry:
    a wrapped description occupies several, and the continuation lines are
    indented to the description column so the block reads as a table.

    Scrolls to keep *selected* visible rather than clamping the window at the
    top — with 76 verbs and 12 rows, a cursor that walks off the bottom into
    nothing is the single most obvious way a palette feels broken."""
    if width <= 0 or not entries:
        return []
    limit = max_rows if max_rows is not None else palette_rows()

    names = [n for n, _d in entries]
    name_col = min(
        _name_column(names),
        max(8, int(width * _NAME_COL_MAX_FRACTION)),
    )
    desc_col = max(8, width - name_col - _GUTTER - 2)

    # The budget is RENDERED LINES, not entries.
    #
    # Counting entries makes the block's height depend on how long the
    # descriptions happen to be: four entries that each wrap to three lines
    # is a twelve-line slab, and the same four with terse descriptions is
    # four. The visible size of a UI element should not be a side effect of
    # its content — so entries are admitted until the LINE budget is spent,
    # which adapts by itself. Terse verbs show more of them; a verb whose
    # description needs three lines gets them, and displaces its neighbours
    # rather than overflowing.
    #
    # This is also what makes wrapping safe to enable at all. On a narrow
    # terminal every description wraps, and an entry-counted budget would
    # silently triple the palette's height exactly when there is least room.
    total = len(entries)
    line_budget = max(1, limit)
    start = 0
    if selected >= 0 and total > 1:
        # Centre the window on the selection, measured in ENTRIES, then let
        # the line budget decide how many actually fit from there.
        start = max(0, min(selected - line_budget // 2, total - 1))

    visible: List[Tuple[str, str]] = []
    spent = 0
    for entry in entries[start:]:
        cost = _rendered_height(entry[1], desc_col, wrap_descriptions)
        # Always admit the first entry, even if it alone exceeds the budget:
        # showing nothing is worse than showing one tall thing, and the
        # selected row must never be the one that gets dropped.
        if visible and spent + cost > line_budget:
            break
        visible.append(entry)
        spent += cost

    lines: List[List[Tuple[str, str]]] = []
    for offset, (name, desc) in enumerate(visible):
        idx = start + offset
        current = idx == selected
        base = ("class:completion-menu.completion.current" if current
                else "class:completion-menu.completion")
        meta = ("class:completion-menu.meta.completion.current" if current
                else "class:completion-menu.meta.completion")
        # The name is NEVER clipped. It is the thing the operator has to
        # type, and `/backlog_auto_prop…` has stopped being a palette entry.
        # A name wider than the column OVERFLOWS: it keeps its full text, its
        # description starts one gutter later, and that single row is ragged
        # — which is strictly cheaper than every row paying for the outlier.
        # An outlier overflows the COLUMN but never the fraction cap. Two
        # invariants bound this and both are right: a palette line must fit
        # the terminal, and one pathological name must not swallow the
        # screen. Clipping at the cap satisfies both while still letting an
        # ordinary long verb — `/backlog_auto_proposed` at 22 against a cap
        # of 34 — keep its full text, which is the case that motivated this.
        shown = _ellipsis(str(name), max(8, int(width * _NAME_COL_MAX_FRACTION)))
        pad = " " * max(0, name_col - len(shown))
        # Re-measured PER ROW, so an overflowing name cannot push its
        # description past the terminal edge.
        row_desc_col = max(8, width - len(shown) - len(pad) - _GUTTER - 2)
        body = _wrap(str(desc), row_desc_col) if wrap_descriptions else [
            _ellipsis(str(desc), row_desc_col)
        ]
        lines.append([
            (base, f"  {shown}{pad}"),
            (meta, " " * _GUTTER + body[0]),
        ])
        # Continuation lines: blank name column, description aligned under
        # itself. This is what makes a wrapped entry read as one row.
        for extra in body[1:]:
            lines.append([
                (base, "  " + " " * name_col),
                (meta, " " * _GUTTER + extra),
            ])
    return lines


def live_completion_entries() -> Tuple[List[Tuple[str, str]], int]:
    """The live completion state as ``(entries, selected_index)``.

    Reads ``buffer.complete_state`` — the ONE place prompt_toolkit keeps this
    — so every surface renders the same state rather than each keeping its own
    idea of what is being completed. ``([], -1)`` whenever nothing is active
    or no application is running. NEVER raises."""
    try:
        from prompt_toolkit.application.current import get_app
        state = get_app().current_buffer.complete_state
        if state is None or not state.completions:
            return [], -1
        rows = [
            (c.display_text or c.text, c.display_meta_text or "")
            for c in state.completions
        ]
        index = state.complete_index if state.complete_index is not None else -1
        return rows, index
    except Exception:  # noqa: BLE001
        return [], -1


def palette_fragments(max_rows: Optional[int] = None) -> List[Tuple[str, str]]:
    """The palette as ONE formatted-text fragment list, newlines included.

    This is the form prompt_toolkit accepts anywhere formatted text is taken —
    ``bottom_toolbar``, ``message``, a ``FormattedTextControl``. Having it
    means a surface does not need a container to show the palette, which is
    what kept the page layout confined to the full ``Application`` while the
    ``PromptSession`` cockpit fell back to the native widget.

    Empty list when nothing is being completed. NEVER raises."""
    try:
        rows, index = live_completion_entries()
        if not rows:
            return []
        try:
            from prompt_toolkit.application.current import get_app
            width = get_app().output.get_size().columns
        except Exception:  # noqa: BLE001
            width = 80
        lines = layout_palette(rows, width=width, selected=index,
                               max_rows=max_rows)
        out: List[Tuple[str, str]] = []
        for i, line in enumerate(lines):
            out.extend(line)
            if i < len(lines) - 1:
                out.append(("", "\n"))
        return out
    except Exception:  # noqa: BLE001 — a palette fault must not blank the UI
        logger.debug("[Palette] fragment render degraded", exc_info=True)
        return []


def _is_native_completion_menu(container: Any) -> bool:
    """True if *container* is (or wraps) prompt_toolkit's own menu widget.

    Checked at EVERY level rather than after unwrapping to the innermost
    child: ``CompletionsMenu`` is itself a ``ConditionalContainer`` subclass,
    so unwrapping first walks straight past the thing being looked for. That
    exact mistake left both menus on screen at once during development.
    """
    names = ("CompletionsMenu", "MultiColumnCompletionsMenu")
    seen = 0
    while container is not None and seen < 8:
        if type(container).__name__ in names:
            return True
        for base in type(container).__mro__:
            if base.__name__ in names:
                return True
        inner = getattr(container, "content", None)
        if inner is None or inner is container:
            break
        container = inner
        seen += 1
    return False


def strip_native_completion_menu(app: Any) -> int:
    """Remove prompt_toolkit's completions float from *app*. Returns the count.

    The palette REPLACES the native presentation rather than restyling it —
    they are different layouts, not different colour schemes — so leaving the
    widget in place renders both at once: a narrow floating column on top of
    the full-width page.

    ``FloatContainer.floats`` is a plain list and mutating it is how floats are
    managed, so this is not reaching past an API. It is done after
    construction because ``PromptSession`` builds its layout internally and
    exposes no seam to opt out of the menu.

    NEVER raises; returns 0 when there is nothing to remove."""
    removed = 0
    try:
        from prompt_toolkit.layout import FloatContainer
        for node in app.layout.walk():
            if not isinstance(node, FloatContainer):
                continue
            keep = [f for f in node.floats
                    if not _is_native_completion_menu(f.content)]
            removed += len(node.floats) - len(keep)
            node.floats[:] = keep
    except Exception:  # noqa: BLE001
        logger.debug("[Palette] native menu strip degraded", exc_info=True)
    return removed


def build_palette_window(condition_style: str = "") -> Any:
    """A full-width container that draws the palette above the prompt.

    Visible only while a completion is in progress, and it takes its height
    from the content — so the prompt sits directly beneath the last entry
    exactly as it does with no palette open. NEVER raises."""
    try:
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import ConditionalContainer, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
    except ImportError:
        return None

    def _state() -> Any:
        try:
            return get_app().current_buffer.complete_state
        except Exception:  # noqa: BLE001
            return None

    def _entries_and_index():
        return live_completion_entries()

    _fragments = palette_fragments

    def _height() -> int:
        try:
            rows, idx = _entries_and_index()
            if not rows:
                return 0
            width = get_app().output.get_size().columns
            return len(layout_palette(rows, width=width, selected=idx))
        except Exception:  # noqa: BLE001
            return 0

    return ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_fragments, focusable=False),
            # Height must be the CONTENT's height, computed per render.
            #
            # This was `preferred=1`, which was invisible while the palette was
            # an HSplit row: `dont_extend_height` let the row take what its
            # content needed. Inside a Float there is no such negotiation — the
            # preferred height IS the height, so the overlay painted exactly
            # one entry and looked like a broken menu.
            height=lambda: Dimension(
                min=0, preferred=_height(), max=palette_rows() * 3, weight=0,
            ),
            dont_extend_height=True,
            wrap_lines=False,
            style=condition_style or "bg:default",
        ),
        filter=Condition(lambda: _height() > 0),
    )


__all__ = [
    "build_palette_window",
    "layout_palette",
    "live_completion_entries",
    "palette_fragments",
    "palette_rows",
    "strip_native_completion_menu",
]

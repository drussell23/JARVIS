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


def palette_rows() -> int:
    """Maximum entries drawn at once (``JARVIS_PALETTE_HEIGHT``)."""
    try:
        return max(3, min(30, int(
            os.environ.get("JARVIS_PALETTE_HEIGHT", "12") or 12,
        )))
    except (TypeError, ValueError):
        return 12


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
        max((len(n) for n in names), default=0),
        max(8, int(width * _NAME_COL_MAX_FRACTION)),
    )
    desc_col = max(8, width - name_col - _GUTTER - 2)

    # Window the ENTRY list around the selection before rendering, so wrapping
    # cost is paid only for what is shown.
    total = len(entries)
    start = 0
    if selected >= 0 and total > limit:
        start = max(0, min(selected - limit // 2, total - limit))
    visible = entries[start: start + limit]

    lines: List[List[Tuple[str, str]]] = []
    for offset, (name, desc) in enumerate(visible):
        idx = start + offset
        current = idx == selected
        base = ("class:completion-menu.completion.current" if current
                else "class:completion-menu.completion")
        meta = ("class:completion-menu.meta.completion.current" if current
                else "class:completion-menu.meta.completion")
        shown = _ellipsis(str(name), name_col)
        pad = " " * max(0, name_col - len(shown))
        body = _wrap(str(desc), desc_col) if wrap_descriptions else [
            _ellipsis(str(desc), desc_col)
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
        st = _state()
        if st is None or not st.completions:
            return [], -1
        rows = [
            (c.display_text or c.text, c.display_meta_text or "")
            for c in st.completions
        ]
        idx = st.complete_index if st.complete_index is not None else -1
        return rows, idx

    def _fragments():
        try:
            rows, idx = _entries_and_index()
            if not rows:
                return []
            width = get_app().output.get_size().columns
            lines = layout_palette(rows, width=width, selected=idx)
            out: List[Tuple[str, str]] = []
            for i, line in enumerate(lines):
                out.extend(line)
                if i < len(lines) - 1:
                    out.append(("", "\n"))
            return out
        except Exception:  # noqa: BLE001 — a palette fault must not blank the UI
            logger.debug("[Palette] render degraded", exc_info=True)
            return []

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
            height=Dimension(min=0, max=palette_rows() * 3,
                             preferred=1, weight=0),
            dont_extend_height=True,
            wrap_lines=False,
            style=condition_style or "class:completion-menu",
        ),
        filter=Condition(lambda: _height() > 0),
    )


__all__ = [
    "build_palette_window",
    "layout_palette",
    "palette_rows",
]

"""Selecting text in the transcript, which has no selection model to borrow.

Claude Code: "Click and drag to select text anywhere in the conversation …
Selected text copies to your clipboard automatically on mouse release."

prompt_toolkit gives selection to BUFFERS — `BufferControl.mouse_handler`
already tracks an anchor, extends on drag and highlights, which is why
selecting inside the ov prompt has always worked and needed no code. The
canvas is a `FormattedTextControl`: a list of styled runs with no cursor, no
anchor and no notion that two of its characters might be "between" each
other. Everything a buffer gets for free has to exist here.

Coordinates are RENDERED cells
------------------------------
A selection is (row, col) over the canvas as drawn — panel border, anchor
padding and all — for the same reason click-to-expand resolves rows that way:
it is what the mouse reports, and it is exact by construction. Mapping back
to logical transcript lines would mean carrying an offset that is right only
while four other things stay true, and a selection that highlights one row
off is worse than none.

The consequence is honest rather than clever: dragging across the panel's
left edge selects the panel's left edge. That is also what a terminal's own
selection does, and what CC documents its selection as capturing — "the
hard-wrapped terminal rendering rather than the source text".

Highlighting is a pure transform
--------------------------------
`apply_selection` takes the fragment list the canvas already produces and
returns a new one with the selected cells restyled. No widget, no second
render path, and — because it is pure over (fragments, selection) — the
geometry is testable at every edge without a terminal: backwards drags,
zero-width drags, selections running off the end of a short row, and the
single-row case that is not the multi-row case with the middle removed.

It costs nothing when nothing is selected: the transform returns the input
list unchanged, so an idle canvas walks no characters at all.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.CanvasSelection")

CANVAS_SELECTION_SCHEMA_VERSION: str = "canvas_selection.1"

__all__ = [
    "CANVAS_SELECTION_SCHEMA_VERSION",
    "Selection",
    "apply_selection",
    "copy_on_release",
    "current_selection",
    "extract_text",
    "reset_selection_for_tests",
    "selection_enabled",
    "selection_style",
    "set_current_selection",
]


def selection_enabled() -> bool:
    """``JARVIS_CANVAS_SELECTION_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_CANVAS_SELECTION_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def copy_on_release() -> bool:
    """Copy automatically when the button comes up (CC's default).

    ``JARVIS_CANVAS_COPY_ON_SELECT=0`` turns it off, mirroring CC's own
    "Copy on select" toggle — an operator who selects to READ rather than to
    copy does not want their clipboard replaced every time.
    """
    return os.environ.get(
        "JARVIS_CANVAS_COPY_ON_SELECT", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def selection_style() -> str:
    """The style appended to selected cells. NEVER raises.

    ``reverse`` rather than a colour: the cockpit's theme is operator-chosen
    and reactive, and a hardcoded highlight colour is the one thing certain
    to collide with some theme. Reversing whatever the cell already is
    inherits the theme instead of arguing with it.
    """
    return os.environ.get("JARVIS_SELECTION_STYLE", "reverse").strip() or "reverse"


class Selection:
    """An anchor and a cursor over rendered cells.

    Stored UNNORMALISED — the anchor is where the drag began, which may be
    after the cursor — because normalising on store would lose the direction,
    and extending a backwards drag needs to know which end is moving.
    """

    __slots__ = ("anchor", "cursor", "active")

    def __init__(self, anchor: Tuple[int, int],
                 cursor: Optional[Tuple[int, int]] = None,
                 active: bool = True) -> None:
        self.anchor: Tuple[int, int] = (int(anchor[0]), int(anchor[1]))
        src = cursor if cursor is not None else anchor
        self.cursor: Tuple[int, int] = (int(src[0]), int(src[1]))
        self.active = bool(active)

    # -- geometry -------------------------------------------------------

    def ordered(self) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """``(start, end)`` in reading order, whichever way it was dragged."""
        a, c = self.anchor, self.cursor
        return (a, c) if (a[0], a[1]) <= (c[0], c[1]) else (c, a)

    @property
    def empty(self) -> bool:
        """A press with no drag. Not a selection — and the distinction is
        load-bearing, because a plain click must still reach click-to-expand
        rather than being swallowed as a zero-width selection."""
        return self.anchor == self.cursor

    def contains(self, row: int, col: int) -> bool:
        """Is this rendered cell inside the selection? Pure. NEVER raises.

        Half-open at the END so the cell under the release point is not
        included — the same convention every editor uses, and what makes a
        one-cell drag select one cell rather than two.
        """
        try:
            if not self.active or self.empty:
                return False
            (sr, sc), (er, ec) = self.ordered()
            if row < sr or row > er:
                return False
            if sr == er:
                return sc <= col < ec
            if row == sr:
                return col >= sc
            if row == er:
                return col < ec
            return True
        except Exception:  # noqa: BLE001
            return False

    def extend_to(self, row: int, col: int) -> "Selection":
        return Selection(self.anchor, (row, col), active=True)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics
        return f"Selection({self.anchor} -> {self.cursor}, active={self.active})"


_LOCK = threading.Lock()
_CURRENT: Optional[Selection] = None


def current_selection() -> Optional[Selection]:
    """The live selection, or None. NEVER raises.

    A module-level current, matching `set_active_canvas` / `set_active_queue`
    — the canvas fragments are produced deep inside a render callable that
    has no handle to whatever is tracking the mouse.
    """
    with _LOCK:
        return _CURRENT


def set_current_selection(selection: Optional[Selection]) -> None:
    """Publish or clear the live selection. NEVER raises."""
    global _CURRENT
    with _LOCK:
        _CURRENT = selection


def reset_selection_for_tests() -> None:
    set_current_selection(None)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def extract_text(rows: Sequence[Any], selection: Optional[Selection]) -> str:
    """The selected text, as it appears on screen. Pure. NEVER raises.

    Trailing whitespace is trimmed per line because the canvas pads every row
    out to the panel's width — without it, every copied line would carry a
    tail of spaces to the right border, and a multi-line paste would arrive
    ragged for a reason the operator cannot see.

    The single-row case is handled first and separately: it is NOT the
    multi-row case with the middle removed, and writing it that way slices
    the first row from `start` to end-of-line and the last from
    start-of-line to `end`, which for one row is the whole row twice.
    """
    try:
        if selection is None or selection.empty:
            return ""
        lines = _plain_rows(rows)
        (sr, sc), (er, ec) = selection.ordered()
        if sr < 0:
            sr, sc = 0, 0
        if er >= len(lines):
            er, ec = max(0, len(lines) - 1), len(lines[-1]) if lines else 0
        if not lines or sr > er:
            return ""
        if sr == er:
            return lines[sr][sc:ec].rstrip()
        out = [lines[sr][sc:].rstrip()]
        out.extend(line.rstrip() for line in lines[sr + 1:er])
        out.append(lines[er][:ec].rstrip())
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        logger.debug("[CanvasSelection] extract degraded", exc_info=True)
        return ""


def _plain_rows(rows: Sequence[Any]) -> List[str]:
    try:
        from backend.core.ouroboros.battle_test.append_only import strip_ansi
        return [strip_ansi(str(r)) for r in (rows or ())]
    except Exception:  # noqa: BLE001
        return [str(r) for r in (rows or ())]


# ---------------------------------------------------------------------------
# highlight
# ---------------------------------------------------------------------------


def apply_selection(
    fragments: Iterable[Any],
    selection: Optional[Selection] = None,
) -> List[Any]:
    """Restyle the selected cells. Pure. NEVER raises.

    Walks the fragment stream tracking (row, col) — newlines advance the row
    and reset the column — and splits a run wherever it crosses a selection
    boundary. Runs are accumulated and flushed on a state CHANGE rather than
    emitted per character: a screenful of one-character fragments is a
    pathological input for every downstream renderer, and the whole point of
    a fragment list is that it is runs.

    Returns the input unchanged when nothing is selected, so an idle canvas
    pays nothing for this existing.
    """
    try:
        source = list(fragments or ())
        sel = selection if selection is not None else current_selection()
        if sel is None or not sel.active or sel.empty or not selection_enabled():
            return source
        style_suffix = " " + selection_style()
        out: List[Any] = []
        row = col = 0

        for fragment in source:
            if not isinstance(fragment, (tuple, list)) or len(fragment) < 2:
                out.append(fragment)
                continue
            style, text = fragment[0], str(fragment[1])
            extra = tuple(fragment[2:])          # mouse handlers etc. survive
            run: List[str] = []
            run_selected: Optional[bool] = None

            def _flush() -> None:
                if not run:
                    return
                styled = (str(style) + style_suffix) if run_selected else style
                out.append((styled, "".join(run)) + extra)
                run.clear()

            for ch in text:
                if ch == "\n":
                    _flush()
                    run_selected = None
                    out.append((style, "\n") + extra)
                    row += 1
                    col = 0
                    continue
                is_sel = sel.contains(row, col)
                if run_selected is None:
                    run_selected = is_sel
                elif is_sel != run_selected:
                    _flush()
                    run_selected = is_sel
                run.append(ch)
                col += 1
            _flush()
        return out
    except Exception:  # noqa: BLE001 — a highlight must never break a frame
        logger.debug("[CanvasSelection] highlight degraded", exc_info=True)
        try:
            return list(fragments or ())
        except Exception:  # noqa: BLE001
            return []

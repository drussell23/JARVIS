"""The deck's column discipline, in ONE place, so lines cannot drift.

Claude Code's transcript is legible because of three rules applied without
exception, not because of its glyphs:

1. **A block is an action plus what it produced.** ``⏺`` opens it, ``⎿``
   continues it, and a BLANK LINE closes it. Without the blank the deck is a
   wall of text and the eye has nothing to group on.
2. **Every continuation body starts in the same column.** ``  ⎿  `` is two
   spaces, the glyph, two spaces — a five-column body. A diff hunk under that
   result starts in that same column. One constant, never a per-call-site
   literal.
3. **Hierarchy is carried by COLOR, not by indentation.** Dim chrome, ink
   content, accent on the thing that changed. `OV_STYLE_GUIDE.md` §08 puts
   "a flat wall of one green — no hierarchy" at the top of its Don't column,
   and an unstyled line is exactly that: it inherits the terminal's default
   foreground, so on a green-on-black profile the entire deck is one green.

Rule 2 is why this module exists at all rather than being f-strings at the
call sites. `ov demo live` shipped with ``"  ⎿ 847 lines"`` (one trailing
space, body at column 4) directly above ``"     + except ..."`` (body at
column 5), and the diff visibly stepped out from under the result it belonged
to. Two literals, one column, and nothing to notice the disagreement — the
same defect shape as two formatters computing width independently. Here the
body column is computed once from the glyph's own printed width and both
callers ask for it.

Width, not length
-----------------
``💭`` is one codepoint and TWO terminal cells. Padding it with ``len()`` puts
the following text one cell left of where the arithmetic claims, which is how
``💭the vision floor raises`` reaches an operator's screen. Every lead here is
padded by :func:`rich.cells.cell_len`, so a wide glyph keeps its space and a
one-cell glyph keeps the CC column.

Returns Rich markup, since every consumer (``BipartiteLayout.push_raw`` →
``Text.from_markup``, ``SerpentFlow._op_line``) already speaks it. Caller text
is escaped: a diff hunk containing ``[foo]`` is content, never a tag.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.DeckGrammar")

__all__ = [
    "ACTION_COLUMN", "DETAIL_COLUMN", "GUTTER_WIDTH",
    "action", "detail", "voice", "diff", "blank", "body_column",
]

#: Where an action's text begins: ``⏺ Read(...)``. CC's column.
ACTION_COLUMN: int = 2

#: Indent of the continuation glyph under its action.
_DETAIL_INDENT: int = 2

#: Where a continuation's text begins: ``  ⎿  847 lines``. THE constant a
#: result line and the diff hunk beneath it must both be derived from.
DETAIL_COLUMN: int = 5

#: Width of the diff line-number gutter. Right-aligned, so the numbers form a
#: column instead of drifting as the file passes 999 lines.
GUTTER_WIDTH: int = 4


def body_column() -> int:
    """The column a continuation body occupies. Ask, never hardcode."""
    return DETAIL_COLUMN


# ---------------------------------------------------------------------------
# Primitives — every one of them degrades rather than raising. A deck line is
# chrome; chrome must never be the thing that takes down the surface.
# ---------------------------------------------------------------------------


def _cells(text: str) -> int:
    """Printed WIDTH of ``text``, not its codepoint count."""
    try:
        from rich.cells import cell_len
        return int(cell_len(text))
    except Exception:  # noqa: BLE001
        return len(text)


def _style(name: str) -> str:
    """Resolve a semantic color name for the live terminal's color tier."""
    try:
        from backend.core.ouroboros.ui.theme import semantic
        return semantic(name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _glyph(name: str) -> str:
    """A themed mark, already ASCII-degraded when the locale demands it."""
    try:
        from backend.core.ouroboros.ui.theme import mark
        return mark(name) or ""
    except Exception:  # noqa: BLE001
        return {"action": "*", "detail": "-", "voice": "K:"}.get(name, "")


def _escape(text: Any) -> str:
    """Caller text is CONTENT. ``except Exception:  # [BLE001]`` is a comment,
    not a Rich tag, and a deck that renders it as one loses the line."""
    try:
        from rich.markup import escape
        return escape(str(text))
    except Exception:  # noqa: BLE001
        return str(text)


def _tint(text: str, style: str) -> str:
    if not text or not style:
        return text
    return f"[{style}]{text}[/{style}]"


def _lead(glyph: str, column: int) -> str:
    """``glyph`` plus the spaces that land the next character on ``column``.

    A glyph wider than the column keeps a single separating space — pushing
    its text one cell right is the honest outcome, and is the whole reason
    this is arithmetic on printed width rather than a literal.
    """
    return glyph + " " * max(1, column - _cells(glyph))


# ---------------------------------------------------------------------------
# The three line kinds
# ---------------------------------------------------------------------------


def action(verb: str, arg: str = "", *, tone: str = "ok") -> str:
    """``⏺ Read(governance/risk_tier_floor.py)`` — opens a block.

    The bullet carries the outcome (``ok`` / ``crit`` / ``warn``); the verb is
    ink; the argument is muted, because WHICH file is context and WHAT
    happened is the content. Colouring both the same is the flat wall.
    """
    try:
        lead = _tint(_lead(_glyph("action"), ACTION_COLUMN), _style(tone))
        head = _tint(_escape(verb), _style("ink"))
        if arg:
            paren = _style("faint")
            head = (f"{head}{_tint('(', paren)}"
                    f"{_tint(_escape(arg), _style('muted'))}"
                    f"{_tint(')', paren)}")
        return lead + head
    except Exception:  # noqa: BLE001
        return f"* {verb}"


def detail(text: str, *, tone: str = "muted") -> str:
    """``  ⎿  Read 847 lines`` — subordinate to the action above it.

    ``tone`` is the RESULT's colour: ``ok`` for a pass, ``crit`` for a
    failure, muted for the ordinary. That is what makes a red line findable
    while scrolling, and it is derived from the outcome rather than picked.
    """
    try:
        glyph = _tint(_glyph("detail"), _style("faint"))
        pad = " " * max(1, DETAIL_COLUMN - _DETAIL_INDENT
                        - _cells(_glyph("detail")))
        return (" " * _DETAIL_INDENT + glyph + pad
                + _tint(_escape(text), _style(tone)))
    except Exception:  # noqa: BLE001
        return f"  - {text}"


def voice(text: str) -> str:
    """``💭 the vision floor raises, and the caller swallows it``.

    The organism's own reasoning, in the glyph `theme._GLYPHS` rations for it.
    Two cells wide, so its body sits one column right of an action's — the
    alternative was padding it as though it were one cell, which is how the
    glyph ends up welded to the first word.
    """
    try:
        lead = _lead(_glyph("voice"), ACTION_COLUMN)
        style = " ".join(p for p in (_style("info"), "italic") if p)
        return lead + _tint(_escape(text), style)
    except Exception:  # noqa: BLE001
        return f"  {text}"


def diff(lineno: Optional[int], sign: str, code: str) -> str:
    """``     412 +    except RiskFloorConfigError:`` — a hunk under a result.

    Anchored on :data:`DETAIL_COLUMN`, so it cannot step out from under the
    ``⎿`` summary that introduced it. The line number is what makes a hunk
    reviewable rather than decorative: without it the operator knows a line
    changed but not where to look.
    """
    try:
        gutter = (f"{int(lineno):>{GUTTER_WIDTH}}" if lineno is not None
                  else " " * GUTTER_WIDTH)
        tone = {"+": "ok", "-": "crit"}.get(sign, "muted")
        return (" " * DETAIL_COLUMN
                + _tint(gutter, _style("faint")) + " "
                + _tint(f"{sign} {_escape(code)}", _style(tone)))
    except Exception:  # noqa: BLE001
        return f"     {sign} {code}"


def blank() -> str:
    """The block separator. A line, not a formatting accident.

    Every op block ends with one. It is the cheapest structure the deck has —
    remove it and twenty correct lines read as one paragraph.
    """
    return ""

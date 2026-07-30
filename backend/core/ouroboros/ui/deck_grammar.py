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


def _highlight_to_markup(code: str, path: Optional[str], bg: str) -> str:
    """Syntax-highlight ``code`` and return DECK MARKUP. NEVER raises.

    A diff line carries two independent facts and the deck was collapsing them
    into one: the SIGN (was this added or removed) and the SYNTAX (what is this
    code). Tinting the whole line by its sign made every added line uniformly
    green, so the code inside a hunk stopped reading as code — the operator can
    see that something changed but not what it says.

    Claude Code keeps them separate: background from the sign, foreground from the
    highlighter. That is what this restores.

    Markup, not ANSI, and that is a contract decision rather than a preference.
    The transcript ring decodes either since #70290, but `ov demo transcript`
    prints these lines through a Rich MARKUP console — handing it ANSI would print
    the escapes. Returning markup keeps every existing consumer working, so the
    highlighting is additive rather than a migration.

    The lexer is INFERRED by Rich from the path (`Syntax.guess_lexer`), never from
    a table here. A hardcoded extension map is wrong the first time someone edits
    a `.toml`, and Rich already owns that knowledge — it resolves `.py` to python,
    `.md` to markdown, and a bare `Makefile` to make, none of which this module
    should be in the business of knowing.
    """
    try:
        from rich.console import Console
        from rich.syntax import Syntax
        from rich.text import Text

        # No path means no language to guess. Highlighting against a default
        # lexer would colour arbitrary tokens confidently and wrongly, which is
        # worse than not highlighting: a wrong colour is a claim.
        lexer = None
        if path:
            try:
                lexer = Syntax.guess_lexer(str(path), code)
            except Exception:  # noqa: BLE001
                lexer = None
        if not lexer:
            return _tint(_escape(code), bg) if bg else _escape(code)

        text: Any = Syntax(code, lexer, theme="ansi_dark").highlight(code)
        # `highlight` appends a trailing newline; inside a one-line deck entry it
        # would split the row and shove the gutter of the NEXT line out of column.
        if isinstance(text, Text):
            text.rstrip()
        out: list = []
        for segment in text.render(Console(width=max(20, len(code) + 8),
                                           force_terminal=False)):
            piece = segment.text
            if not piece or piece == "\n":
                continue
            style = str(segment.style or "").strip()
            # Compose the sign's background UNDER the token's own colour, so a
            # keyword keeps its foreground while the hunk keeps its band. Order
            # matters: Rich resolves later tokens last, so the background is
            # written first and the syntax colour wins the foreground.
            combined = f"{bg} {style}".strip() if bg else style
            if combined and combined != "none":
                out.append(f"[{combined}]{_escape(piece)}[/{combined}]")
            else:
                out.append(_escape(piece))
        return "".join(out) or (_tint(_escape(code), bg) if bg
                                else _escape(code))
    except Exception:  # noqa: BLE001
        # A highlighter that fails must not cost the operator the diff.
        return _tint(_escape(code), bg) if bg else _escape(code)


def diff(lineno: Optional[int], sign: str, code: str,
         *, path: Optional[str] = None, width: Optional[int] = None) -> str:
    """``     412 +    except RiskFloorConfigError:`` — a hunk under a result.

    Anchored on :data:`DETAIL_COLUMN`, so it cannot step out from under the
    ``⎿`` summary that introduced it. The line number is what makes a hunk
    reviewable rather than decorative: without it the operator knows a line
    changed but not where to look.

    ``path`` enables syntax highlighting — the language is inferred from it, so a
    caller that knows which file a hunk belongs to gets highlighted code for free
    and one that does not keeps the previous single-tone rendering. Additive by
    construction: no call site has to change to keep working.

    ``width`` pads the background band to a fixed column so an added block reads as
    a solid slab rather than a ragged right edge that stops at each line's text.
    """
    try:
        gutter = (f"{int(lineno):>{GUTTER_WIDTH}}" if lineno is not None
                  else " " * GUTTER_WIDTH)
        # The BAND belongs to the sign. `code_add`/`code_del` are the roles the
        # palette already declares for exactly this and which nothing has been
        # consuming for the hunk body — only for the "Added N lines" summary.
        # From `role_palette`, NOT `_style`. `_style` maps this module's own tone
        # vocabulary (ok / crit / faint / muted) and resolves an unknown name to
        # "" — so asking it for a semantic ROLE silently produced no band at all,
        # which is how the first version of this shipped highlighted code with no
        # slab behind it. `role_palette` is the declared owner of `code_add` /
        # `code_del`, and the roles have existed unconsumed for the hunk body since
        # the colour migration.
        band = ""
        try:
            from backend.core.ouroboros.ui.semantic_tokens import role_palette
            band = {"+": "code_add", "-": "code_del"}.get(sign, "")
            band = (role_palette().get(band) or "") if band else ""
        except Exception:  # noqa: BLE001
            band = ""
        bg = f"on {band}" if band else ""
        body = _highlight_to_markup(code, path, bg)
        # Padding is applied to the RAW text length, never to the markup string:
        # markup carries style tags that occupy no columns, so measuring it would
        # over-pad by however many tags the highlighter happened to emit.
        pad = ""
        if width:
            visible = len(code) + 2          # the code plus "<sign> "
            room = max(0, int(width) - DETAIL_COLUMN - GUTTER_WIDTH - 1 - visible)
            if room and bg:
                pad = _tint(" " * room, bg)
        sign_mark = _tint(f"{sign} ", bg) if bg else f"{sign} "
        return (" " * DETAIL_COLUMN
                + _tint(gutter, _style("faint")) + " "
                + sign_mark + body + pad)
    except Exception:  # noqa: BLE001
        return f"     {sign} {code}"


def blank() -> str:
    """The block separator. A line, not a formatting accident.

    Every op block ends with one. It is the cheapest structure the deck has —
    remove it and twenty correct lines read as one paragraph.
    """
    return ""

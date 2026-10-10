"""Lay out one transcript line as a BLOCK: wrapped text stays in its column.

A deck line is a prefix -- indentation and, often, a structure mark (``⏺``,
``⎿``, ``│``) -- followed by its content. Every surface rendered it as one
flat string, so when content outgrew the width the terminal wrapped it back
to column 0, out from under its ``⎿``, and a tool result read as a stray
paragraph (the cockpit, 2026-10-09). The daemon's local printer avoided that
by cropping instead, which lost the text.

This is Claude Code's layout: the prefix holds its column, the content wraps
inside the column to its right, and a token longer than the column (a regex,
a path) folds inside it rather than spilling. One function, used by every
surface that draws deck lines. NEVER raises -- a line it cannot lay out is
returned as plain wrapping text.
"""
from __future__ import annotations

import re
from typing import Any, Optional


def _marks() -> str:
    """Every structure mark the theme can emit, in any degradation."""
    try:
        from backend.core.ouroboros.ui import theme
        found = set()
        for pair in getattr(theme, "_GLYPHS", {}).values():
            found.update(str(g) for g in pair if g and len(str(g)) <= 2)
        found.add("│")
        # The pre-● action mark: an attached cockpit may render lines from an
        # older daemon, and their wrapped continuations still need the column.
        found.add("⏺")
        return "".join(sorted(found, key=len, reverse=True))
    except Exception:  # noqa: BLE001
        return "●⏺⎿│*-"


_PREFIX_RE: Optional["re.Pattern[str]"] = None


def _prefix_re() -> "re.Pattern[str]":
    """Leading spaces, then optionally ONE mark and its following spaces."""
    global _PREFIX_RE
    if _PREFIX_RE is None:
        alternatives = "|".join(re.escape(m) for m in sorted(
            {m for m in _marks()}, key=len, reverse=True,
        ))
        _PREFIX_RE = re.compile(rf"^( *(?:(?:{alternatives}) +)?)")
    return _PREFIX_RE


def as_text(line: Any) -> Any:
    """A deck line (markup, ANSI or a Rich ``Text``) as a Rich ``Text``."""
    from rich.text import Text
    if isinstance(line, Text):
        return line
    raw = str(line)
    try:
        return Text.from_ansi(raw) if "\x1b" in raw else Text.from_markup(raw)
    except Exception:  # noqa: BLE001 -- malformed markup renders as data
        return Text(raw)


class DeckLine(str):
    """One laid-out deck line that IS the line it came from.

    A ``str`` whose value is the SOURCE line (its markup), carrying the
    column-keeping layout beside it. Rich calls ``__rich__`` before it checks
    for a string (``Console._collect_renderables``: ``rich_cast`` first), so a
    console renders the layout; every other consumer (a log, a relay, a
    captured test console doing ``"x" in line``) has the string it always
    had. A wrapper that was NOT a str broke every one of those consumers.
    The layout and the line are one value, so they cannot drift.
    """

    def __new__(cls, source: str, renderable: Any) -> "DeckLine":
        obj = super().__new__(cls, source)
        obj._renderable = renderable
        return obj

    @property
    def source(self) -> str:
        return str.__str__(self)

    def __rich__(self) -> Any:
        return self._renderable

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"DeckLine({str.__repr__(self)})"


def _source_of(line: Any) -> str:
    """The markup a line was written as (a Rich ``Text`` reports its own)."""
    try:
        from rich.text import Text
        if isinstance(line, Text):
            return line.markup
    except Exception:  # noqa: BLE001
        pass
    return str(line)


def block(line: Any) -> Any:
    """*line* as a renderable whose wrapped content keeps its column."""
    try:
        from rich.cells import cell_len
        from rich.table import Table
        source = _source_of(line)
        text = as_text(line)
        plain = text.plain
        match = _prefix_re().match(plain)
        n = len(match.group(1)) if match else 0
        if n == 0 or n >= len(plain):
            text.overflow = "fold"
            return DeckLine(source, text)
        prefix, body = text[:n], text[n:]
        body.overflow = "fold"
        grid = Table.grid(padding=0, expand=False)
        grid.add_column(width=cell_len(prefix.plain), no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(prefix, body)
        return DeckLine(source, grid)
    except Exception:  # noqa: BLE001
        try:
            from rich.text import Text
            return DeckLine(str(line), Text(str(line)))
        except Exception:  # noqa: BLE001
            return str(line)


__all__ = ["DeckLine", "as_text", "block"]

"""Line numbers on source the model is shown -- and their removal from what it writes back.

The model edits a file by unified diff, so it has to know WHERE each line it
quotes sits. The held-out baseline (run-20261010-071537) failed 28 of 30
tasks on hunks whose context lines occur nowhere in the file in that order:
the full builder showed ~60 KB of unnumbered source and the model rebuilt the
region from memory. Five call sites each invented a format for the snippets
they did number (``N: ``, ``NNNN | ``, ``{n:>5} | ``, ``{n:>5}  ``), so
nothing could recognise a number the model copied back.

This module is the ONE rendering and its exact inverse:

* :func:`number` renders ``<n>│<line>`` (right-aligned to the block's
  widest number, nothing between the bar and the line), so the text after
  the bar is the file's line byte-for-byte.
* :func:`strip_echoed` removes that prefix from lines the model wrote. It
  acts only when the prefix is the block's dominant shape
  (:func:`hunk_share` / :func:`content_share`), so a genuine source line that
  happens to begin with digits and a box-drawing bar is never altered on its
  own, and it returns the first number it removed: a copied number is the
  file's real position, a better placement hint than a guessed ``@@`` header.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence, Tuple

#: Between the number and the line. U+2502: unlike ``|`` or ``:`` it does not
#: occur after a leading integer in source code (``1 | FLAG`` is a valid
#: Python continuation line; ``1│`` is not).
SEPARATOR = "│"

_ECHOED = re.compile(r"^[ \t]*(\d+)" + SEPARATOR)


def enabled() -> bool:
    """``JARVIS_NUMBERED_SOURCE_ENABLED`` (default true): number the source
    snapshots a diff is written against. Off renders them as before."""
    return os.environ.get("JARVIS_NUMBERED_SOURCE_ENABLED", "true").strip().lower() not in (
        "0", "false", "no", "off")


def _share(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        v = default
    return min(1.0, max(0.0, v))


def hunk_share() -> float:
    """``JARVIS_ECHOED_NUMBER_HUNK_SHARE`` (0.5): the share of a hunk's
    non-blank lines that must carry a copied number before any is removed.
    A hunk is short and its added lines are often left unnumbered."""
    return _share("JARVIS_ECHOED_NUMBER_HUNK_SHARE", 0.5)


def content_share() -> float:
    """``JARVIS_ECHOED_NUMBER_CONTENT_SHARE`` (0.9): the same for a whole
    file the model wrote out -- a copy of a numbered snapshot is numbered
    nearly everywhere, and anything less is the file's own text."""
    return _share("JARVIS_ECHOED_NUMBER_CONTENT_SHARE", 0.9)


def number_lines(lines: Sequence[str], start: int = 1, *, width: int = 0) -> List[str]:
    """``lines`` (no line endings) prefixed with their 1-indexed positions."""
    w = max(width, len(str(start + max(0, len(lines) - 1))))
    return [f"{start + i:>{w}}{SEPARATOR}{line}" for i, line in enumerate(lines)]


def number(text: str, start: int = 1, *, width: int = 0) -> str:
    """*text* with every line prefixed by its position, *start* being the
    first line's. A partial first or last line (a truncation boundary) is
    still numbered: it IS that line, cut."""
    if not text:
        return text
    lines = text.split("\n")
    trailing = lines[-1] == ""
    body = number_lines(lines[:-1] if trailing else lines, start, width=width)
    return "\n".join(body) + ("\n" if trailing else "")


def echoed_number(line: str) -> Optional[int]:
    """The position a line carries in this module's format, else None."""
    m = _ECHOED.match(line)
    return int(m.group(1)) if m else None


def strip_echoed(lines: Sequence[str], *, min_share: float) -> Tuple[List[str], Optional[int], int]:
    """Remove this module's prefix from lines the model wrote back.

    Returns ``(lines, first_number, stripped)``. Acts only when at least
    *min_share* (:func:`hunk_share` / :func:`content_share`) of the
    non-blank lines carry the prefix; otherwise returns the lines
    unchanged. A line ending (``\\n``/``\\r\\n``) is preserved."""
    nonblank = [ln for ln in lines if ln.strip()]
    hits = [ln for ln in nonblank if _ECHOED.match(ln)]
    if not hits or len(hits) < min_share * len(nonblank):
        return list(lines), None, 0
    out: List[str] = []
    first: Optional[int] = None
    n = 0
    for ln in lines:
        m = _ECHOED.match(ln)
        if m is None:
            out.append(ln)
            continue
        if first is None:
            first = int(m.group(1))
        out.append(ln[m.end():])
        n += 1
    return out, first, n


def strip_echoed_content(text: str) -> str:
    """A whole file the model wrote, without numbers copied from a snapshot."""
    if not text:
        return text
    out, _first, n = strip_echoed(text.splitlines(keepends=True), min_share=content_share())
    return "".join(out) if n else text


def legend() -> str:
    """The sentence a prompt carries wherever it shows numbered source."""
    return (f"Source lines are shown as `<line number>{SEPARATOR}<text>`. The number and the "
            f"`{SEPARATOR}` are NOT part of the file: use the numbers for @@ hunk headers and "
            f"copy only the text after `{SEPARATOR}` into context and removed lines.")

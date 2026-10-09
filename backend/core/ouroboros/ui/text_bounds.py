"""Bounding text without breaking it.

A tool result handed to the cockpit was sliced at a character budget, so its
last line ended mid-word (``functio``), every count a summary derived from it
(``12 symbols``) counted the slice rather than the result, and ``/expand``
could only ever restore the slice. A bound belongs at a LINE boundary, and it
must say what it left out.

One function, used wherever a result is bounded for display or parking.
NEVER raises.
"""
from __future__ import annotations

import os
import re
from typing import Tuple

_ENV_BODY_MAX = "JARVIS_TOOL_RENDER_BODY_MAX_CHARS"
_DEFAULT_BODY_MAX = 65536


def body_max_chars() -> int:
    """How much of one tool result the cockpit keeps (display + /expand)."""
    try:
        value = int(os.environ.get(_ENV_BODY_MAX, "") or _DEFAULT_BODY_MAX)
    except ValueError:
        return _DEFAULT_BODY_MAX
    return value if value > 0 else _DEFAULT_BODY_MAX


def clip_lines(text: str, max_chars: int) -> Tuple[str, int]:
    """``(bounded, omitted_chars)``: *text* cut at the last line boundary
    that fits, plus one marker line naming what was left out. A single line
    longer than the budget is cut at its last word boundary instead. Text
    that fits is returned unchanged with ``0``."""
    try:
        text = text or ""
        if max_chars <= 0 or len(text) <= max_chars:
            return text, 0
        head = text[:max_chars]
        cut = head.rfind("\n")
        if cut <= 0:
            space = head.rfind(" ")
            cut = space if space > max_chars // 2 else max_chars
        kept = text[:cut].rstrip("\n")
        omitted = len(text) - len(kept)
        lines_left = text[cut:].count("\n")
        what = f"{lines_left} more line{'s' if lines_left != 1 else ''}" if lines_left else "more"
        return f"{kept}\n… {what} ({omitted:,} chars) not kept", omitted
    except Exception:  # noqa: BLE001
        return (text or "")[: max(0, max_chars)], 0


#: The marker :func:`clip_lines` appends, as a pattern, so a renderer can tell
#: it from content. A diff renderer that could not numbered it as code.
CLIP_MARKER_RE = re.compile(r"^… (?:\d+ more lines?|more) \([\d,]+ chars\) not kept$")


def is_clip_marker(line: str) -> bool:
    """Whether *line* is the marker :func:`clip_lines` wrote. NEVER raises."""
    try:
        return bool(CLIP_MARKER_RE.match(str(line).strip()))
    except Exception:  # noqa: BLE001
        return False


__all__ = ["CLIP_MARKER_RE", "body_max_chars", "clip_lines", "is_clip_marker"]

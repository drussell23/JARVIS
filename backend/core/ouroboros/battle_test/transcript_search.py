"""Finding a line you already saw, after the alternate screen took Cmd+F.

Full-screen rendering bought a fixed viewport and cost the terminal's own
search: the deck lives in the alternate buffer, so `Cmd+F` and tmux copy-mode
cannot see it. `canvas_viewport` gave the operator a way to SCROLL through
20k retained lines, which is enough to re-read something you know the
position of and useless for finding something you only remember the words of.

This is the other half — `/` over the same buffer, reusing `CanvasViewport`
for position rather than tracking a second one. The viewport already knows
how to hold a window still while the organism appends; a search that moved
the view by its own arithmetic would immediately disagree with it.

Matches are ordinals, not lines and not indices
------------------------------------------------
The deck grows while you read it. If a search held the matched TEXT and
re-found it on every step, an identical line arriving later would silently
steal the cursor; if it held a screen offset, every append would shift what
`n` means.

An index into the snapshot is not enough either, and this is the subtle one.
The deck is a bounded RING: once it saturates, every push also drops the
oldest line, so index 8,412 addresses a different line one second later. The
failure is silent and confident — `n` scrolls somewhere real, showing content
that looks plausible, and nothing indicates the cursor moved off the match.
An append-only list never shows it, which is precisely why it survives being
tested.

So a match is an ORDINAL: how many lines the ring had ever seen when that one
arrived, derived from the buffer's own monotonic `push_count`. Resolving one
back to an index accounts for everything pushed since, and a line that has
aged out resolves to `None` — reported as gone rather than approximated.

For a plain list the base ordinal is 0 and an ordinal IS an index, so nothing
about searching a static buffer changed.

Smart case, and no accidental regex
------------------------------------
A lowercase query matches case-insensitively; a query containing an uppercase
letter is taken literally, because typing a capital is a deliberate act and
guessing otherwise makes `Error` unfindable among `error`s.

The query is a SUBSTRING, never a pattern. An operator searching for
`_contained(p, roots)` is searching for exactly that, and a regex engine
would either raise on the parenthesis or match something they did not ask
for. Both are worse than the feature not existing, because both look like the
search is broken rather than the query being clever.

Cancelling restores where you were
-----------------------------------
`Esc` puts the viewport back at the offset it had before the search began.
Someone who searched, found nothing useful, and pressed Escape has not asked
to be moved — and losing your place is exactly the cost that makes people
stop using search.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.TranscriptSearch")

__all__ = [
    "TranscriptSearch", "base_ordinal", "find_matches", "resolve_ordinal",
    "search_enabled", "smart_case",
]

#: Beyond this a query is not a search. Bounded so a paste into the search
#: bar cannot turn every keystroke into a full-buffer scan.
_MAX_QUERY = 200


def search_enabled() -> bool:
    """Default ON. Off, the deck scrolls but cannot be searched."""
    return os.environ.get(
        "JARVIS_TRANSCRIPT_SEARCH_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def smart_case(query: str) -> bool:
    """True when the search should be case-SENSITIVE.

    A lowercase query matches loosely; one containing a capital is taken
    literally. Typing an uppercase letter is a deliberate act, and ignoring
    it makes `Error` unfindable among a thousand `error`s.
    """
    try:
        return any(ch.isupper() for ch in str(query or ""))
    except Exception:  # noqa: BLE001
        return False


def find_matches(lines: Sequence[str], query: str) -> List[int]:
    """Absolute indices of lines containing *query*. NEVER raises.

    A SUBSTRING search, never a pattern: an operator searching for
    `_contained(p, roots)` means exactly that, and a regex engine would either
    raise on the parenthesis or match something they did not ask for.
    """
    try:
        needle = str(query or "")[:_MAX_QUERY]
        if not needle.strip():
            return []
        sensitive = smart_case(needle)
        if not sensitive:
            needle = needle.lower()
        out: List[int] = []
        for index, raw in enumerate(lines or ()):
            text = str(raw)
            # Chrome markup is not content. An operator searching for "diff"
            # must not match every line wrapped in a style tag that happens
            # to contain it.
            hay = text if sensitive else text.lower()
            if needle in hay:
                out.append(index)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[TranscriptSearch] find degraded", exc_info=True)
        return []


def base_ordinal(push_count: int, retained: int) -> int:
    """Ordinal of the OLDEST line still in the ring.

    ``push_count`` is monotonic and ``retained`` is bounded, so their
    difference is exactly how many lines have already fallen off the end —
    the origin every stored match is measured from. A buffer that has not
    saturated yet has dropped nothing, so the base is 0 and an ordinal is an
    index. NEVER raises.
    """
    try:
        return max(0, int(push_count) - int(retained))
    except (TypeError, ValueError):
        return 0


def resolve_ordinal(
    ordinal: Optional[int], *, push_count: int, retained: int,
) -> Optional[int]:
    """Current ring index for ``ordinal``, or None once it has aged out.

    None is the entire value of this function. A match the ring has since
    evicted is not a match at some nearby index: clamping would scroll to
    whatever line happens to occupy that slot now, which is a confident jump
    to the wrong place with nothing to signal it. NEVER raises.
    """
    try:
        if ordinal is None:
            return None
        index = int(ordinal) - base_ordinal(push_count, retained)
        if index < 0 or index >= max(0, int(retained)):
            return None
        return index
    except (TypeError, ValueError):
        return None


class TranscriptSearch:
    """One search session over the deck, positioned through the viewport."""

    def __init__(self, viewport: Optional[Any] = None) -> None:
        #: The EXISTING scroll authority. Reused rather than duplicated: the
        #: viewport already holds the window still while the organism
        #: appends, and a search moving the view by its own arithmetic would
        #: immediately disagree with it.
        self._viewport = viewport
        self.query: str = ""
        self._matches: List[int] = []
        self._cursor: int = -1
        self._restore_offset: Optional[int] = None
        #: Ordinal of the oldest line in the buffer that was searched. 0 for a
        #: static list, so an ordinal and an index coincide there.
        self._base: int = 0
        self.searches = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self.query)

    @property
    def matches(self) -> List[int]:
        return list(self._matches)

    @property
    def position(self) -> Tuple[int, int]:
        """``(nth, total)`` for a `3/17`-style indicator, 1-based."""
        if not self._matches or self._cursor < 0:
            return (0, len(self._matches))
        return (self._cursor + 1, len(self._matches))

    def begin(self) -> None:
        """Remember where the operator was, so Escape can put them back."""
        try:
            self._restore_offset = getattr(self._viewport, "offset", None)
        except Exception:  # noqa: BLE001
            self._restore_offset = None

    def cancel(self) -> Optional[int]:
        """Esc — restore the pre-search position. Returns the offset used.

        Someone who searched, found nothing useful, and pressed Escape has
        not asked to be moved. Losing your place is the cost that makes
        people stop using search.
        """
        offset = self._restore_offset
        self.reset()
        try:
            if offset is not None and self._viewport is not None:
                self._viewport._offset = max(0, int(offset))  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        return offset

    def reset(self) -> None:
        self.query = ""
        self._matches = []
        self._cursor = -1
        self._restore_offset = None

    # -- searching ---------------------------------------------------------

    def search(
        self, lines: Sequence[str], query: str, *, base: int = 0,
    ) -> int:
        """Run a search. Returns the match count.

        Recomputed from the CURRENT buffer each time rather than filtered
        from a previous result: typing `err` then `error` must not be limited
        to what `err` happened to match in a buffer that has since grown.

        ``base`` is the ordinal of ``lines[0]`` — :func:`base_ordinal` over
        the live ring. It defaults to 0, which is correct for any buffer that
        has not evicted anything and keeps a static list behaving exactly as
        before; pass the real base and every stored match survives eviction.
        """
        try:
            if not search_enabled():
                return 0
            self.query = str(query or "")[:_MAX_QUERY]
            offset = max(0, int(base))
            self._base = offset
            self._matches = [
                offset + i for i in find_matches(lines, self.query)
            ]
            self._cursor = 0 if self._matches else -1
            self.searches += 1
            return len(self._matches)
        except Exception:  # noqa: BLE001
            logger.debug("[TranscriptSearch] search degraded", exc_info=True)
            self._matches, self._cursor = [], -1
            return 0

    def resolve(
        self, ordinal: Optional[int], *, push_count: int, retained: int,
    ) -> Optional[int]:
        """Where a stored match lives NOW, or None once the ring dropped it.

        The re-resolution this class's docstring always promised. Held
        separately from :meth:`step` so walking the matches and locating them
        stay independent: `n` must advance the cursor even when the line it
        lands on has aged out, or an evicted match in the middle of a result
        set would wedge the walk.
        """
        return resolve_ordinal(
            ordinal, push_count=push_count, retained=retained,
        )

    def step(self, forward: bool = True) -> Optional[int]:
        """`n` / `N`. Returns the absolute line index, or None.

        WRAPS at both ends. A search that stops at the last match leaves the
        operator pressing a key that does nothing, with no way to tell "no
        more below" from "the key is broken".
        """
        try:
            if not self._matches:
                return None
            self._cursor = (self._cursor + (1 if forward else -1)) % len(
                self._matches,
            )
            return self._matches[self._cursor]
        except Exception:  # noqa: BLE001
            return None

    @property
    def current(self) -> Optional[int]:
        if not self._matches or self._cursor < 0:
            return None
        return self._matches[self._cursor]

    # -- moving the view ---------------------------------------------------

    def offset_for(self, line_index: int, total: int, budget: int) -> int:
        """Viewport offset that puts *line_index* on screen, centred-ish.

        Centred rather than top-aligned: a match at the top of the window has
        no context above it, and the line before a match is very often what
        the operator was actually looking for.
        """
        try:
            total = max(0, int(total))
            budget = max(1, int(budget))
            if total <= budget:
                return 0
            target = max(0, min(total - 1, int(line_index)))
            # Offset counts lines from the BOTTOM, matching the viewport.
            below = total - target
            centred = below - (budget // 2)
            return max(0, min(total - budget, centred))
        except Exception:  # noqa: BLE001
            return 0

    def reveal(self, line_index: Optional[int], total: int,
               budget: int) -> bool:
        """Scroll the viewport to show *line_index*. True if it moved."""
        try:
            if line_index is None or self._viewport is None:
                return False
            offset = self.offset_for(line_index, total, budget)
            if getattr(self._viewport, "offset", None) == offset:
                return False
            self._viewport._offset = offset  # noqa: SLF001
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- render ------------------------------------------------------------

    def status(self) -> str:
        """``/error  3/17`` — or a plain miss, said out loud.

        A search with no results must SAY so. Silence is indistinguishable
        from a key that did not register, and the operator retypes the query
        instead of trying a different one.
        """
        try:
            if not self.query:
                return ""
            if not self._matches:
                return f"/{self.query}  no matches"
            nth, total = self.position
            return f"/{self.query}  {nth}/{total}"
        except Exception:  # noqa: BLE001
            return ""

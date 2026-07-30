"""Navigating the transcript by how the organism KNOWS what it said.

Claude Code's transcript viewer jumps between search matches and between
prompts. It cannot jump between claims, because nothing in it records which
sentences were observed and which were asserted. O+V already does: every
`_op_line` passes through `ui/provenance`, and a line the organism could not
stand behind carries a mark saying so.

That marking has been feeding the EYE since it shipped and nothing has ever
navigated by it. This is the consumer.

    c / C   jump to the next / previous CLAIM

A claim is any line the organism marked — `‹stated›`, `‹model›`,
`‹synthetic›`, `‹unverified›`. Pressing `c` through a session is asking the
one question no other tool can answer: *show me every place this thing
asserted something it did not observe.*

Clean is two things, and it says so
-----------------------------------
`provenance` renders OBSERVED and DERIVED with no mark at all — marks are the
EXCEPTION surface, which is what keeps a busy transcript readable. The
consequence for a reader of rendered text is that "clean" means
observed-or-derived and the two are genuinely indistinguishable from the
line alone.

So `provenance_of_line` returns None for a clean line rather than guessing
OBSERVED. Reporting the stronger of the two would be exactly the kind of
confident-and-wrong the mark vocabulary exists to prevent, and `UNKNOWN` is
already taken by a different fact — asked and unanswerable. Three states,
kept distinct.

Read from the RENDERED line
---------------------------
The marks are visible text, because they were built to be read by a human.
That means classification needs no index, no producer change and no second
record to drift: a line that starts carrying a new mark becomes navigable for
free. The same property that made click-to-expand cheap.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.EpistemicFilter")

EPISTEMIC_FILTER_SCHEMA_VERSION: str = "epistemic_filter.1"

__all__ = [
    "EPISTEMIC_FILTER_SCHEMA_VERSION",
    "claim_rows",
    "epistemic_filter_enabled",
    "next_claim_row",
    "provenance_of_line",
    "summarise",
]

#: Derived from the mark vocabulary at import rather than transcribed, so a
#: new rung — or a re-worded mark — travels without an edit here. A hardcoded
#: glyph list is how a filter silently stops seeing a category.
_MARKS: "list" = []


def _marks() -> "list":
    """``[(pattern, Provenance), ...]`` weakest first. NEVER raises.

    Weakest first because a line can carry more than one mark — a derived
    summary of a model's prose — and `provenance` already rules that the
    WEAKER wins: a chain is exactly as trustworthy as its softest link.
    Scanning in strength order means the first hit is the right answer
    without comparing them.
    """
    global _MARKS
    if _MARKS:
        return _MARKS
    try:
        from backend.core.ouroboros.ui.provenance import Provenance, mark_for

        built = []
        for prov in sorted(Provenance, key=lambda p: int(p)):
            markup = getattr(mark_for(prov), "markup", "") or ""
            # `‹model›` out of ` [magenta]‹model›[/magenta]` — the visible
            # text, which is what survives into a rendered line.
            found = re.search(r"‹[^›]+›", markup)
            if found:
                built.append((re.compile(re.escape(found.group(0))), prov))
        _MARKS = built
    except Exception:  # noqa: BLE001
        logger.debug("[Epistemic] mark table degraded", exc_info=True)
        _MARKS = []
    return _MARKS


def epistemic_filter_enabled() -> bool:
    """``JARVIS_EPISTEMIC_NAV_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_EPISTEMIC_NAV_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def provenance_of_line(line: Any) -> Optional[Any]:
    """The rung a rendered line declares, or None if it declares none.

    None is NOT "observed". A clean line is observed-or-derived and the two
    cannot be told apart from the text — reporting the stronger would be the
    confident-and-wrong this vocabulary exists to prevent. Pure. NEVER raises.
    """
    try:
        text = str(line or "")
        try:
            from backend.core.ouroboros.battle_test.append_only import (
                strip_ansi,
            )
            text = strip_ansi(text)
        except Exception:  # noqa: BLE001
            pass
        # Rich markup may still be present when a line is read before
        # rendering; the mark's visible text is inside it either way.
        for pattern, prov in _marks():
            if pattern.search(text):
                return prov
        return None
    except Exception:  # noqa: BLE001
        return None


def claim_rows(rows: Sequence[Any]) -> List[int]:
    """Indices of every row the organism MARKED. Pure. NEVER raises.

    "Marked" rather than "weak": the set is exactly the lines that carry an
    exception, which is the set an operator auditing a session wants to walk.
    """
    try:
        return [i for i, line in enumerate(rows or ())
                if provenance_of_line(line) is not None]
    except Exception:  # noqa: BLE001
        return []


def next_claim_row(
    rows: Sequence[Any],
    current: Any,
    direction: int = 1,
    *,
    wrap: bool = True,
) -> Optional[int]:
    """The next marked row from ``current``, or None. Pure. NEVER raises.

    Wraps by default, like every `n`/`N` an operator has used. Returns None
    only when there is nothing to go to at all — a reader who presses `c` in
    a session with no claims should be told that, not moved somewhere
    arbitrary and left to wonder.
    """
    try:
        marks = claim_rows(rows)
        if not marks:
            return None
        try:
            here = int(current)
        except (TypeError, ValueError):
            here = -1 if direction >= 0 else len(list(rows or ()))
        if direction >= 0:
            after = [i for i in marks if i > here]
            if after:
                return after[0]
            return marks[0] if wrap else None
        before = [i for i in marks if i < here]
        if before:
            return before[-1]
        return marks[-1] if wrap else None
    except Exception:  # noqa: BLE001
        return None


def summarise(rows: Sequence[Any]) -> str:
    """``3 modeled · 1 unverified`` — what this screenful is standing on.

    Counted rather than listed, and ordered weakest-first so the least
    defensible category is read first. An empty result renders nothing: a
    transcript with no claims is the good case and does not need a badge
    saying so. NEVER raises.
    """
    try:
        from collections import Counter

        counts: Any = Counter()
        for line in rows or ():
            prov = provenance_of_line(line)
            if prov is not None:
                counts[prov] += 1
        if not counts:
            return ""
        parts = [f"{n} {prov.label}"
                 for prov, n in sorted(counts.items(),
                                       key=lambda kv: int(kv[0]))]
        return " · ".join(parts)
    except Exception:  # noqa: BLE001
        return ""

"""What a diff does not tell you: what depends on the lines it changes.

A diff answers "what changed". At a gate the operator is deciding something
else — "what does this reach" — and two three-line edits render identically
while differing fifty-fold in blast radius. The dangerous file in a
multi-file change looks exactly like the safe one.

The organism knows. `OperationAdvisor` computes reach with real care —
`blast_radius`, an epistemic `blast_provenance`, a localized lower bound
when a global scan cannot finish — and then `render_safety_plan()` writes
it into the GENERATION PROMPT. The model is told. The human approving the
change is not.

Why this cannot simply resolve what it wants to show
====================================================
A cold blast scan is a 39–43 second burn. That is not a tuning detail; it
is the failure that produced the entire Targeted Locality Bounding arc,
where a timed-out scan fabricated `blast=50`, cached it, and let it satisfy
a hard BLOCK.

So a gutter that resolved reach per file would freeze a diff preview for
minutes — on the operator's critical path, while they wait at a gate. This
module therefore READS ONLY what is already known (`advisor.peek_blast`,
which cannot scan) and is honest about the rest.

A miss is a miss
================
An unresolved file renders `?`, never `0`. The advisor's cache stores only
MEASURED-class results, so absent means "never established" — and the last
time this system let an unestablished reach wear a number, that number was
cached and poisoned every op sharing its key for the TTL.

This is the `UNKNOWN` versus `UNSET` distinction from `provenance`, in the
one place where it is most expensive to get wrong. Reach marked `?` is
inert: it sorts last, contributes nothing to the scale, and is styled as a
warning rather than as data.

Relative, because that is the actual question
=============================================
The operator is not auditing an absolute dependency count; they are asking
"which of THESE files should I look at hardest". So bars scale to the
largest RESOLVED reach in the set. A single file with reach 3 fills its bar
— relative to what it is being compared against, which is nothing. Absolute
counts stay visible beside the bar for anyone who wants them.

NEVER raises, and never blocks. A gutter that fails must cost the operator
a column, not their diff.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("Ouroboros.BlastGutter")

BLAST_GUTTER_SCHEMA_VERSION = "blast_gutter.v1"

MASTER_FLAG_ENV_VAR = "JARVIS_BLAST_GUTTER_ENABLED"

#: Bar width. Small on purpose — this is a gutter, not a chart. It sits
#: beside a filename in a tree that already carries a status badge and a
#: +/- count, and a wide bar would make the reach look like the headline
#: rather than the annotation.
_DEFAULT_WIDTH = 6

#: Blocks, densest last. Unicode is degraded to ASCII when the terminal
#: cannot carry it — the same discipline as `theme.ouroboros_frame`.
_BLOCKS = "▁▂▃▄▅▆▇█"
_ASCII_BLOCKS = ".:-=+*#@"


def gutter_enabled() -> bool:
    """Default ON. Off, the tree renders exactly as it did before."""
    try:
        return os.environ.get(
            MASTER_FLAG_ENV_VAR, "1",
        ).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        return True


def _width() -> int:
    try:
        return max(1, min(24, int(os.environ.get(
            "JARVIS_BLAST_GUTTER_WIDTH", _DEFAULT_WIDTH))))
    except Exception:  # noqa: BLE001
        return _DEFAULT_WIDTH


@dataclass(frozen=True)
class Reach:
    """How far one file's change travels, and how well that is known."""

    path: str
    count: int = 0
    #: The advisor's own vocabulary — "measured", "localized_lower_bound",
    #: "synthetic_cap", "unknown" — kept verbatim rather than pre-digested,
    #: so `provenance.project` stays the single place that vocabulary is
    #: interpreted and this module never becomes a second opinion on it.
    provenance: str = ""
    resolved: bool = False

    @property
    def is_lower_bound(self) -> bool:
        """A localized scan proves "at least this many", not "this many".

        Rendering a lower bound as an exact count is the same class of lie
        as rendering an unresolved one as zero, one decimal place quieter.
        """
        return self.provenance == "localized_lower_bound"


#: Optional resolver override. Installed by the demo and by tests so neither
#: has to seed the ADVISOR'S OWN shared cache to show a populated gutter.
#: That matters more than it looks: the advisor's cache is read by live GATE
#: decisions, so a display surface that wrote to it — even briefly, even with
#: cleanup — could let a demonstration change what the organism decides. A
#: demo may lie to itself; it may never lie to the gate.
_RESOLVER = None


def set_resolver(fn) -> None:
    """Install (or clear, with None) the reach resolver. NEVER raises."""
    global _RESOLVER
    _RESOLVER = fn


def peek(paths: Sequence[str], root: Optional[str] = None) -> List[Reach]:
    """Reach for each path, WITHOUT computing anything. NEVER raises.

    Each path is looked up alone — key ``frozenset({path})`` — because that
    is the only key whose cached value describes that file rather than the
    aggregate of the op it happened to travel with. A multi-file op leaves
    only the aggregate behind, so most files in a large change will miss,
    and will honestly say so.
    """
    out: List[Reach] = []
    peek_blast = _RESOLVER
    if peek_blast is None:
        try:
            from backend.core.ouroboros.governance.operation_advisor import (
                peek_blast,
            )
        except Exception:  # noqa: BLE001
            return [Reach(path=str(p)) for p in paths]
    for raw in paths:
        path = str(raw or "")
        try:
            hit = peek_blast([path], root)
            if hit is None:
                out.append(Reach(path=path))
                continue
            count, provenance = hit
            out.append(Reach(path=path, count=int(count),
                             provenance=str(provenance or ""),
                             resolved=True))
        except Exception:  # noqa: BLE001
            out.append(Reach(path=path))
    return out


def _blocks(ascii_only: bool = False) -> str:
    return _ASCII_BLOCKS if ascii_only else _BLOCKS


def bar(reach: Reach, scale: int, *, width: Optional[int] = None,
        ascii_only: bool = False) -> str:
    """One file's bar, scaled against ``scale``. Pure. NEVER raises.

    ``scale`` is the largest RESOLVED reach in the set, so the column
    answers "which of these should I look at hardest" rather than posing an
    absolute question nobody asked.
    """
    try:
        cols = width if width and width > 0 else _width()
        if not reach.resolved:
            # Never a bar. An unresolved reach that rendered as an empty bar
            # would be indistinguishable from a resolved reach of zero —
            # the two facts this module exists to keep apart.
            return "?".ljust(cols)
        if scale <= 0 or reach.count <= 0:
            return "·".ljust(cols)
        blocks = _blocks(ascii_only)
        filled = (reach.count / float(scale)) * cols
        whole = int(filled)
        out = blocks[-1] * min(whole, cols)
        if whole < cols:
            remainder = filled - whole
            if remainder > 0:
                idx = min(len(blocks) - 1,
                          max(0, int(remainder * len(blocks))))
                out += blocks[idx]
        return out[:cols].ljust(cols)
    except Exception:  # noqa: BLE001
        return "?".ljust(width or _DEFAULT_WIDTH)


def scale_of(reaches: Sequence[Reach]) -> int:
    """The largest RESOLVED reach. Unresolved entries contribute nothing.

    Letting a `?` participate would require inventing a value for it, which
    is the fabrication this module refuses. NEVER raises.
    """
    try:
        return max((r.count for r in reaches if r.resolved), default=0)
    except Exception:  # noqa: BLE001
        return 0


def style_for(reach: Reach) -> str:
    """Semantic ROLE for this reach — resolved by `semantic_tokens`.

    Roles, never colours: a second palette here would drift from `theme`
    exactly as `serpent_flow._C` did. Severity is RELATIVE to the set, so
    thresholds live in :func:`annotate_set` where the scale is known.
    """
    if not reach.resolved:
        return "heal"                  # a warning, not data
    return "dim"


@dataclass(frozen=True)
class GutterRow:
    """One rendered row: the bar, its label, and the style role."""

    reach: Reach
    bar: str
    label: str
    role: str


def annotate_set(reaches: Sequence[Reach], *, width: Optional[int] = None,
                 ascii_only: bool = False) -> List[GutterRow]:
    """Render a whole set together. Pure. NEVER raises.

    Together, because the scale — and therefore severity — is a property of
    the SET, not of any one file. A per-file renderer could not know
    whether 12 dependents is the largest number on screen or the smallest,
    which is the only thing the operator actually wants to know.
    """
    rows: List[GutterRow] = []
    try:
        scale = scale_of(reaches)
        for r in reaches:
            if not r.resolved:
                label = "?"
                role = "heal"
            else:
                # "≥" is load-bearing: a localized scan proves a LOWER
                # BOUND. `advisor_locality` keeps that distinction all the
                # way through the gate; dropping it at the last inch would
                # undo the arc that put it there.
                label = f"{'≥' if r.is_lower_bound else ''}{r.count}"
                role = "dim"
                if scale > 0 and r.count >= scale and r.count > 0:
                    role = "alert" if len(reaches) > 1 else "dim"
            rows.append(GutterRow(
                reach=r,
                bar=bar(r, scale, width=width, ascii_only=ascii_only),
                label=label,
                role=role,
            ))
        return rows
    except Exception:  # noqa: BLE001
        logger.debug("[BlastGutter] annotate degraded", exc_info=True)
        return []


def summary(reaches: Sequence[Reach]) -> str:
    """One line for a header, or "". Pure. NEVER raises.

    Reports how much of the set is UNKNOWN, because a gutter that is mostly
    `?` is itself the finding: it means nothing in this change has been
    measured yet, and the operator should read the whole diff rather than
    trusting a column that knows nothing.
    """
    try:
        if not reaches:
            return ""
        resolved = [r for r in reaches if r.resolved]
        if not resolved:
            return "reach unmeasured"
        top = max(resolved, key=lambda r: r.count)
        parts = [f"reach ≤{top.count}" if top.is_lower_bound
                 else f"reach {top.count}"]
        unknown = len(reaches) - len(resolved)
        if unknown:
            parts.append(f"{unknown} unmeasured")
        return " · ".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def as_dict(reaches: Sequence[Reach]) -> Dict[str, object]:
    """Transport-safe projection for the cockpit. NEVER raises."""
    try:
        return {
            "schema_version": BLAST_GUTTER_SCHEMA_VERSION,
            "scale": scale_of(reaches),
            "reaches": [
                {"path": r.path, "count": r.count,
                 "provenance": r.provenance, "resolved": r.resolved}
                for r in reaches
            ],
        }
    except Exception:  # noqa: BLE001
        return {"scale": 0, "reaches": []}


__all__ = [
    "BLAST_GUTTER_SCHEMA_VERSION",
    "GutterRow",
    "MASTER_FLAG_ENV_VAR",
    "Reach",
    "annotate_set",
    "as_dict",
    "bar",
    "gutter_enabled",
    "peek",
    "scale_of",
    "set_resolver",
    "style_for",
    "summary",
]

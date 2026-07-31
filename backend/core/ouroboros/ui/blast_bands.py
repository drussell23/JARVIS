"""Reading a diff riskiest-first.

`blast_gutter` already measures every changed file's reach and draws it — the
bar, the scale, the per-set summary are all there. What nothing did was USE
that number to decide reading ORDER: the file tree rendered in whatever
sequence the diff produced, so a one-line change to a leaf could sit above a
rewrite that forty modules import.

An operator reviewing under a five-second NOTIFY_APPLY countdown reads from
the top. Putting the widest-reaching change there is the whole feature.

UNRESOLVED sorts FIRST, not last
---------------------------------
`peek` is read-only by construction — it returns what the advisor has already
computed and never scans, because a cold blast scan is a 39–43 second burn
and paying it at a gate would freeze the preview the operator is waiting on.
So some files genuinely have no reach yet.

Those sort to the TOP. A file whose blast radius could not be established is
not a safe file; it is an unmeasured one, and the two must never be rendered
as the same thing. This is the same rule the advisor's own repair settled —
`?` never `0`, unknown never presented as measured — and the same rule the
provenance vocabulary states when it calls UNKNOWN the loudest mark there is.

Sorting them last, or treating a missing count as zero, would put exactly the
files nobody can vouch for at the bottom of a list read under time pressure.

What this does NOT do
---------------------
It does not let an operator accept one band and reject another. Apply is
per-OPERATION — `/accept <op-id>` — and splitting it by file would be a
partial apply: a governance change needing orchestrator support, rollback
semantics for a half-applied candidate, and a VERIFY that knows which half
ran. That is not a presentation slice, and building the UI for it here would
be an affordance the engine cannot honour.

Ordering and naming the bands is the honest half, and it is the half that
actually helps someone reading under a countdown.
"""
from __future__ import annotations

import enum
import logging
import os
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.BlastBands")

BLAST_BANDS_SCHEMA_VERSION: str = "blast_bands.1"

__all__ = [
    "BLAST_BANDS_SCHEMA_VERSION",
    "Band",
    "band_of",
    "bands_enabled",
    "review_order",
    "sort_key",
    "summarise_bands",
    "thresholds",
]


class Band(enum.IntEnum):
    """Review priority. Lower sorts FIRST — read this one first.

    ``IntEnum`` so the ordering IS the enum rather than a separate table that
    can disagree with it.
    """

    #: No reach could be established. Not safe — unmeasured.
    UNKNOWN = 0
    #: Reaches far beyond itself.
    WIDE = 1
    #: A handful of dependents.
    MODERATE = 2
    #: Reaches nothing but itself.
    CONTAINED = 3

    @property
    def label(self) -> str:
        return self.name.lower()


def bands_enabled() -> bool:
    """``JARVIS_BLAST_BANDS_ENABLED`` (default true). NEVER raises.

    Off, the tree renders in the order the diff produced — the status quo, so
    the rollback is exact.
    """
    return os.environ.get(
        "JARVIS_BLAST_BANDS_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def thresholds() -> Tuple[int, int]:
    """``(wide_at, moderate_at)`` dependent counts. NEVER raises.

    Env-tunable because "wide" is a property of the REPOSITORY, not of this
    module: eleven dependents is unremarkable in a hub package and alarming
    in a leaf. Clamped and ordered so a mis-set pair cannot invert the bands.
    """
    def _read(name: str, fallback: int) -> int:
        try:
            return max(1, min(10_000, int(
                os.environ.get(name, "") or fallback)))
        except (TypeError, ValueError):
            return fallback

    wide = _read("JARVIS_BLAST_WIDE_AT", 10)
    moderate = _read("JARVIS_BLAST_MODERATE_AT", 3)
    # WIDE must be the larger boundary; swapped values would make every file
    # wide and none moderate, silently.
    if moderate > wide:
        moderate, wide = wide, moderate
    return wide, moderate


def band_of(reach: Any) -> Band:
    """Which band a reach falls in. Pure. NEVER raises.

    ``resolved`` is consulted BEFORE ``count``, because an unresolved reach
    reports ``count == 0`` and zero-as-a-number means "reaches nothing" —
    the most reassuring answer there is, attached to the file we know least
    about.
    """
    try:
        if reach is None:
            return Band.UNKNOWN
        if not bool(getattr(reach, "resolved", False)):
            return Band.UNKNOWN
        count = int(getattr(reach, "count", 0) or 0)
        wide, moderate = thresholds()
        if count >= wide:
            return Band.WIDE
        if count >= moderate:
            return Band.MODERATE
        return Band.CONTAINED
    except Exception:  # noqa: BLE001
        return Band.UNKNOWN


def sort_key(reach: Any) -> Tuple[int, int, str]:
    """``(band, -count, path)`` — the review order, as a key. Pure.

    Path breaks ties so the order is STABLE across renders. A tree that
    reshuffles equal-risk files between frames is one an operator cannot
    keep their place in.
    """
    try:
        band = int(band_of(reach))
        count = int(getattr(reach, "count", 0) or 0)
        path = str(getattr(reach, "path", "") or "")
        return (band, -count, path)
    except Exception:  # noqa: BLE001
        return (int(Band.UNKNOWN), 0, "")


def review_order(reaches: Sequence[Any]) -> List[Any]:
    """Reaches, riskiest first. Pure. NEVER raises."""
    try:
        if not bands_enabled():
            return list(reaches or ())
        return sorted(list(reaches or ()), key=sort_key)
    except Exception:  # noqa: BLE001
        return list(reaches or ())


def order_paths(paths: Sequence[str], reaches: Sequence[Any]) -> List[str]:
    """Order arbitrary paths by the reach known for each. NEVER raises.

    Takes the paths rather than returning reaches, because the caller has
    `FileChange` objects and only needs to know the SEQUENCE. A path with no
    matching reach is treated as unresolved — which sorts it first, the same
    way an unmeasured file does, because that is exactly what it is.
    """
    try:
        if not bands_enabled():
            return list(paths or ())
        by_path = {str(getattr(r, "path", "")): r for r in (reaches or ())}
        return sorted(
            list(paths or ()),
            key=lambda p: sort_key(by_path.get(str(p))),
        )
    except Exception:  # noqa: BLE001
        return list(paths or ())


def summarise_bands(reaches: Sequence[Any]) -> str:
    """``1 unknown · 2 wide · 4 contained`` — riskiest first. NEVER raises.

    Empty when there is nothing to say. A diff whose every file is contained
    does not need a badge announcing that it is fine; the absence of the
    warning IS the signal, which is the restraint the rest of this cockpit
    already keeps.
    """
    try:
        from collections import Counter

        counts: Any = Counter(band_of(r) for r in (reaches or ()))
        if not counts:
            return ""
        interesting = {b: n for b, n in counts.items() if b != Band.CONTAINED}
        if not interesting:
            return ""
        return " · ".join(
            f"{n} {band.label}"
            for band, n in sorted(counts.items(), key=lambda kv: int(kv[0]))
        )
    except Exception:  # noqa: BLE001
        return ""

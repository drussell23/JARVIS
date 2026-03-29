"""Deterministic finding ranker for Ouroboros REM Sleep (Zone 7.0).

Ranks :class:`RankedFinding` objects by a weighted *impact score* so the
most important findings are sent to the Doubleword 397B for analysis first.

The ranking is fully deterministic — no model calls, no I/O, no randomness.
The formula version is pinned so that changes can be detected in audit logs.

Usage::

    from backend.core.ouroboros.finding_ranker import RankedFinding, merge_and_rank

    findings = [
        RankedFinding(
            description="Unused helper function",
            category="dead_code",
            file_path="backend/core/utils.py",
            blast_radius=0.3,
            confidence=0.9,
            urgency="normal",
            last_modified=time.time() - 86400,  # 1 day ago
            repo="jarvis",
            source_check="check_dead_code",
        ),
        ...
    ]
    ranked = merge_and_rank(findings)
    top = ranked[:10]
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

RANKING_VERSION: str = "1.0"
"""Monotonically-bumped string version of the impact_score formula.

Increment this whenever the formula, weights, or window are changed so that
callers/audit logs can detect a ranking change.
"""

_URGENCY_WEIGHTS: Dict[str, float] = {
    "critical": 1.0,
    "high": 0.75,
    "normal": 0.5,
    "low": 0.25,
}
"""Maps urgency label → weight used in :func:`impact_score`."""

_RECENCY_WINDOW_S: float = 90 * 86400  # 90 days in seconds
"""Age threshold beyond which a finding is considered fully stale (recency=0)."""

# ---------------------------------------------------------------------------
# impact_score
# ---------------------------------------------------------------------------


def impact_score(
    blast_radius: float,
    confidence: float,
    urgency: str,
    last_modified: float,
) -> float:
    """Compute a deterministic impact score in the range [0, 1].

    The formula is:

    .. code-block:: text

        score = blast_radius * 0.4
              + confidence   * 0.3
              + urgency_w    * 0.2
              + recency      * 0.1

    where:

    * ``urgency_w``  = ``_URGENCY_WEIGHTS.get(urgency, 0.5)``
    * ``age_s``      = ``max(0.0, time.time() - last_modified)``
    * ``recency``    = ``max(0.0, 1.0 - age_s / _RECENCY_WINDOW_S)``

    Parameters
    ----------
    blast_radius:
        Normalised measure of how broadly a fix would affect the codebase
        (0 = isolated, 1 = systemic).
    confidence:
        How certain the explorer is that this is a real issue (0–1).
    urgency:
        One of ``"critical"``, ``"high"``, ``"normal"``, ``"low"``.
        Unknown values default to weight ``0.5``.
    last_modified:
        Unix epoch timestamp of the relevant file.  Fresher files score
        higher on the recency dimension.

    Returns
    -------
    float
        Impact score in ``[0, 1]``.
    """
    urgency_w = _URGENCY_WEIGHTS.get(urgency, 0.5)
    age_s = max(0.0, time.time() - last_modified)
    recency = max(0.0, 1.0 - age_s / _RECENCY_WINDOW_S)
    return blast_radius * 0.4 + confidence * 0.3 + urgency_w * 0.2 + recency * 0.1


# ---------------------------------------------------------------------------
# RankedFinding
# ---------------------------------------------------------------------------


@dataclass
class RankedFinding:
    """A single exploration finding enriched with an impact score.

    The ``score`` field is computed automatically in :meth:`__post_init__`
    via :func:`impact_score` — it must *not* be supplied by the caller.

    Attributes
    ----------
    description:
        Human-readable summary of the issue discovered.
    category:
        Type of finding.  One of:
        ``dead_code``, ``circular_dep``, ``complexity``, ``unwired``,
        ``test_gap``, ``todo``, ``doc_stale``, ``perf``, ``github_issue``.
    file_path:
        Repo-relative path of the primary affected file.
    blast_radius:
        Normalised impact breadth (0–1).
    confidence:
        Explorer's confidence that this is a real issue (0–1).
    urgency:
        Severity label: ``critical``, ``high``, ``normal``, or ``low``.
    last_modified:
        Unix epoch timestamp of the affected file.
    repo:
        Which repository this finding belongs to.
        One of ``"jarvis"``, ``"jarvis-prime"``, ``"reactor"``.
    source_check:
        Optional identifier of the exploration check that produced this
        finding (e.g. ``"check_dead_code"``).
    score:
        Computed automatically — do not pass on construction.
    """

    description: str
    category: str  # dead_code | circular_dep | complexity | unwired | test_gap | todo | doc_stale | perf | github_issue
    file_path: str
    blast_radius: float  # normalised 0-1
    confidence: float  # 0-1
    urgency: str  # critical | high | normal | low
    last_modified: float  # unix epoch timestamp
    repo: str  # jarvis | jarvis-prime | reactor
    source_check: str = ""  # which check produced this finding
    score: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.score = impact_score(
            self.blast_radius,
            self.confidence,
            self.urgency,
            self.last_modified,
        )


# ---------------------------------------------------------------------------
# merge_and_rank
# ---------------------------------------------------------------------------


def merge_and_rank(findings: List[RankedFinding]) -> List[RankedFinding]:
    """Deduplicate and rank findings by descending impact score.

    Deduplication key is ``(file_path, category)``.  When two findings share
    the same key, only the one with the higher :attr:`RankedFinding.score` is
    retained.  Ties are broken alphabetically by ``file_path`` (ascending) so
    the result is fully deterministic.

    Parameters
    ----------
    findings:
        Raw list of findings from one or more exploration agents.  The input
        list is never mutated.

    Returns
    -------
    List[RankedFinding]
        Deduplicated list sorted by ``score`` descending, then ``file_path``
        ascending as a tiebreaker.
    """
    # Deduplicate: keep highest-scored entry per (file_path, category)
    best: Dict[tuple, RankedFinding] = {}
    for f in findings:
        key = (f.file_path, f.category)
        existing = best.get(key)
        if existing is None or f.score > existing.score:
            best[key] = f

    # Sort: descending score, alphabetical file_path as tiebreaker
    return sorted(best.values(), key=lambda f: (-f.score, f.file_path))

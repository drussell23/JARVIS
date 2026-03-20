"""Confidence fusion for burst vision results.

Combines multiple VisionResult frames from a burst capture into a single
FusedTarget with a robust coordinate estimate and a calibrated confidence
score.  The algorithm uses median coordinates for outlier resistance,
discards results that deviate more than 50 px from the median, and applies
jitter penalties when the surviving cluster is spread across more than
20 / 50 pixels.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VisionResult:
    """A single-frame result returned by a vision model pass."""

    status: str  # "found" | "not_found" | "ambiguous" | "error"
    coords: Optional[Tuple[int, int]]  # (x, y) in logical pixels
    confidence: float  # 0.0 – 1.0
    bbox: Optional[Tuple[int, int, int, int]] = None  # x1, y1, x2, y2
    element_type: str = ""
    text_content: str = ""


@dataclass
class FusedTarget:
    """The result of fusing a burst of VisionResult frames."""

    coords: Optional[Tuple[int, int]]
    confidence: float
    bbox_jitter: float = 0.0   # max spread in pixels across surviving frames
    frames_used: int = 0
    frames_rejected: int = 0


# ---------------------------------------------------------------------------
# Fusion algorithm
# ---------------------------------------------------------------------------

_OUTLIER_THRESHOLD_PX: float = 50.0   # exclude frames farther than this from median
_JITTER_WARN_PX: float = 20.0         # light jitter penalty threshold
_JITTER_SEVERE_PX: float = 50.0       # severe jitter penalty threshold
_JITTER_WARN_FACTOR: float = 0.8      # ×0.8 for >20 px jitter
_JITTER_SEVERE_FACTOR: float = 0.6    # additional ×0.6 for >50 px jitter
_ALL_OUTLIER_PENALTY: float = 0.7     # multiplier when every frame is an outlier
_MIN_HIT_CONFIDENCE: float = 0.3      # frames below this threshold are not "hits"
_CONFIDENCE_CAP: float = 0.99


def fuse_burst_results(results: List[VisionResult]) -> FusedTarget:
    """Fuse a list of per-frame vision results into a single FusedTarget.

    Steps
    -----
    1. Filter to *hits*: status=="found", confidence > _MIN_HIT_CONFIDENCE,
       and coords not None.
    2. If no hits, return zero-confidence FusedTarget.
    3. Compute median (x, y) across all hits.
    4. Outlier rejection: drop any hit whose Chebyshev distance from the
       median exceeds _OUTLIER_THRESHOLD_PX (max of |dx|, |dy|).
    5. If all hits were rejected fall back to the single highest-confidence
       hit and apply _ALL_OUTLIER_PENALTY.
    6. Fused coordinate = median of surviving (x, y) values (integer).
    7. Fused confidence = confidence-weighted mean: Σ(c²) / Σ(c).
    8. Jitter = max spread across surviving xs and ys.
    9. Apply jitter penalties; cap at _CONFIDENCE_CAP.
    """
    if not results:
        return FusedTarget(coords=None, confidence=0.0)

    # Step 1 — collect hits
    hits = [
        r for r in results
        if r.status == "found"
        and r.coords is not None
        and r.confidence > _MIN_HIT_CONFIDENCE
    ]

    if not hits:
        return FusedTarget(coords=None, confidence=0.0)

    # Step 3 — median of all hits
    all_xs = [r.coords[0] for r in hits]  # type: ignore[index]
    all_ys = [r.coords[1] for r in hits]  # type: ignore[index]
    med_x = statistics.median(all_xs)
    med_y = statistics.median(all_ys)

    # Step 4 — outlier rejection (Chebyshev distance)
    survivors = [
        r for r in hits
        if max(abs(r.coords[0] - med_x), abs(r.coords[1] - med_y))  # type: ignore[index]
        <= _OUTLIER_THRESHOLD_PX
    ]
    rejected_count = len(hits) - len(survivors)

    # Step 5 — all-outlier fallback
    all_outlier_penalty = False
    if not survivors:
        best = max(hits, key=lambda r: r.confidence)
        survivors = [best]
        all_outlier_penalty = True
        rejected_count = len(hits) - 1  # only the best survives

    frames_used = len(survivors)

    # Step 6 — fused coordinate (median of survivors, integer)
    sx = [r.coords[0] for r in survivors]  # type: ignore[index]
    sy = [r.coords[1] for r in survivors]  # type: ignore[index]
    fused_x = int(statistics.median(sx))
    fused_y = int(statistics.median(sy))

    # Step 7 — confidence-weighted mean: Σ(c²) / Σ(c)
    confs = [r.confidence for r in survivors]
    weighted_conf: float = sum(c * c for c in confs) / sum(confs)

    # Step 8 — jitter
    jitter = max(max(sx) - min(sx), max(sy) - min(sy)) if len(survivors) > 1 else 0.0

    # Step 9 — apply penalties
    if all_outlier_penalty:
        weighted_conf *= _ALL_OUTLIER_PENALTY

    if jitter > _JITTER_WARN_PX:
        weighted_conf *= _JITTER_WARN_FACTOR
    if jitter > _JITTER_SEVERE_PX:
        weighted_conf *= _JITTER_SEVERE_FACTOR

    weighted_conf = min(weighted_conf, _CONFIDENCE_CAP)

    return FusedTarget(
        coords=(fused_x, fused_y),
        confidence=weighted_conf,
        bbox_jitter=float(jitter),
        frames_used=frames_used,
        frames_rejected=rejected_count,
    )

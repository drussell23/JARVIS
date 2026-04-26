"""P0.5 Slice 2 — Arc-context input for ``DirectionInferrer``.

Captures the operator's recent long-arc direction (visible via the last 100
commits' momentum + the most recent session summary) as a structured
``ArcContextSignal`` for posture evaluation.

This module is the **producer side** of the arc-context branch — building
an ``ArcContextSignal`` from the available primitives:

  * ``backend.core.ouroboros.governance.git_momentum`` (P0.5 Slice 1)
  * ``backend.core.ouroboros.governance.last_session_summary`` v1.1a

The **consumer side** is ``DirectionInferrer`` (Slice 2 wiring), which
takes the signal as an optional kwarg, always logs it for observability,
and applies a small bounded score nudge **only when**
``JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED`` is on.

Design choices:

* **Pure data**, no side effects beyond the producer paths' own bounded
  subprocess calls. AST-pinned to NOT import any authority module.
* **Bounded nudges** — every per-posture nudge ≤ ``MAX_NUDGE_PER_POSTURE``
  (0.10) so existing weights still dominate. Catches a runaway-arc-signal
  failure mode by construction.
* **Optional everywhere** — every consumer that builds or accepts an
  ``ArcContextSignal`` treats absence as "no signal", matching the rest
  of DirectionInferrer's "deterministic, never None on well-formed input
  but graceful on missing input" contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.git_momentum import (
    MomentumSnapshot,
    compute_recent_momentum,
)
from backend.core.ouroboros.governance.posture import Posture

# Hard bound on per-posture nudge magnitude. With existing signal weights
# typically ±1.0 each across 12 signals, max raw posture score is ~12.0,
# so a 0.10 nudge is ~0.8% of that ceiling — enough to break a near-tie,
# nowhere near enough to override a clear winner. Pinned by tests.
MAX_NUDGE_PER_POSTURE: float = 0.10

# LSS verify-ratio thresholds (from the v1.1a `verify=P/T` token).
_LSS_VERIFY_LOW_THRESHOLD: float = 0.5   # below → recent failures → HARDEN nudge
_LSS_VERIFY_HIGH_THRESHOLD: float = 0.9  # above → clean → MAINTAIN credit

# Token parser for LSS one-liner. Matches `apply=mode/N`, `verify=P/T`,
# `commit=HASH[:10]`. Lenient — missing tokens just skip; the producer
# never raises.
_TOKEN_VERIFY_RE = re.compile(r"verify=(\d+)/(\d+)")
_TOKEN_APPLY_RE = re.compile(r"apply=([a-z]+)/(\d+)")


@dataclass(frozen=True)
class ArcContextSignal:
    """Structured arc-context input for posture evaluation.

    Attributes
    ----------
    momentum:
        Optional ``MomentumSnapshot`` from ``git_momentum.compute_recent_momentum``.
        ``None`` when git is unavailable or the repo has no parseable
        commits.
    lss_verify_ratio:
        Parsed ``verify=P/T`` from the most recent session summary. ``None``
        when no LSS available or token absent. ``0.0`` is a legitimate
        value (every test failed), distinct from ``None`` (no signal).
    lss_apply_count:
        Parsed ``apply=mode/N`` count from LSS. ``None`` when absent.
    lss_apply_mode:
        Parsed ``apply=mode/N`` mode string ("single" / "multi" / etc.).
        ``None`` when absent.
    lss_one_liner:
        The raw LSS one-liner string, kept for log-line transparency.
        Empty string when no LSS available.
    """

    momentum: Optional[MomentumSnapshot] = None
    lss_verify_ratio: Optional[float] = None
    lss_apply_count: Optional[int] = None
    lss_apply_mode: Optional[str] = None
    lss_one_liner: str = ""

    def is_empty(self) -> bool:
        """True when this signal carries no usable input."""
        return self.momentum is None and self.lss_verify_ratio is None

    def suggest_nudge(self) -> Dict[Posture, float]:
        """Compute per-posture score nudges from this arc-context.

        Each entry is bounded to ``[0.0, MAX_NUDGE_PER_POSTURE]``. The math
        is intentionally simple + interpretable + deterministic:

        * Momentum type histogram → posture nudges:
          - ``feat`` dominance      → EXPLORE
          - ``fix`` dominance       → HARDEN
          - ``refactor`` + ``docs`` → CONSOLIDATE
        * LSS verify ratio:
          - low  (< 0.5)  → HARDEN  (recent failures, harden the substrate)
          - high (> 0.9)  → MAINTAIN (clean, don't disrupt momentum)

        Returns a dict keyed by every ``Posture`` value (some may be 0.0).
        """
        nudges: Dict[Posture, float] = {p: 0.0 for p in Posture}

        if self.momentum is not None and not self.momentum.is_empty():
            type_counts = dict(self.momentum.top_types(8))
            total = sum(type_counts.values())
            if total > 0:
                feat_ratio = type_counts.get("feat", 0) / total
                fix_ratio = type_counts.get("fix", 0) / total
                cons_ratio = (
                    type_counts.get("refactor", 0) + type_counts.get("docs", 0)
                ) / total
                # Map ratio → nudge with conservative coefficients so that
                # even at 100% dominance the nudge stays at MAX cap.
                nudges[Posture.EXPLORE] = min(
                    MAX_NUDGE_PER_POSTURE, feat_ratio * MAX_NUDGE_PER_POSTURE
                )
                nudges[Posture.HARDEN] = min(
                    MAX_NUDGE_PER_POSTURE, fix_ratio * MAX_NUDGE_PER_POSTURE
                )
                nudges[Posture.CONSOLIDATE] = min(
                    MAX_NUDGE_PER_POSTURE, cons_ratio * MAX_NUDGE_PER_POSTURE
                )

        if self.lss_verify_ratio is not None:
            if self.lss_verify_ratio < _LSS_VERIFY_LOW_THRESHOLD:
                # Add to the HARDEN nudge from momentum, capped.
                add = (
                    (_LSS_VERIFY_LOW_THRESHOLD - self.lss_verify_ratio)
                    * MAX_NUDGE_PER_POSTURE * 2
                )
                nudges[Posture.HARDEN] = min(
                    MAX_NUDGE_PER_POSTURE, nudges[Posture.HARDEN] + add
                )
            elif self.lss_verify_ratio > _LSS_VERIFY_HIGH_THRESHOLD:
                # Small MAINTAIN credit on healthy verify ratio.
                nudges[Posture.MAINTAIN] = min(
                    MAX_NUDGE_PER_POSTURE,
                    nudges[Posture.MAINTAIN] + MAX_NUDGE_PER_POSTURE / 2,
                )

        return nudges

    def to_log_dict(self) -> Dict[str, object]:
        """Compact dict for posture observability log line.

        Always serializable to JSON. Used by ``PostureObserver`` to
        populate the ``arc_context=...`` field on every cycle log line."""
        return {
            "has_momentum": self.momentum is not None,
            "momentum_commits": self.momentum.commit_count if self.momentum else 0,
            "lss_verify_ratio": self.lss_verify_ratio,
            "lss_apply_mode": self.lss_apply_mode,
            "lss_apply_count": self.lss_apply_count,
        }


def _parse_lss_one_liner(line: str) -> Tuple[Optional[float], Optional[int], Optional[str]]:
    """Extract (verify_ratio, apply_count, apply_mode) from an LSS line.

    Returns (None, None, None) on empty / unparseable input. Never raises.
    """
    if not line:
        return (None, None, None)

    verify_ratio: Optional[float] = None
    apply_count: Optional[int] = None
    apply_mode: Optional[str] = None

    m = _TOKEN_VERIFY_RE.search(line)
    if m:
        try:
            passed = int(m.group(1))
            total = int(m.group(2))
            if total > 0:
                verify_ratio = passed / total
        except (ValueError, ZeroDivisionError):
            pass

    m = _TOKEN_APPLY_RE.search(line)
    if m:
        apply_mode = m.group(1)
        try:
            apply_count = int(m.group(2))
        except ValueError:
            pass

    return (verify_ratio, apply_count, apply_mode)


def build_arc_context(
    project_root: Path,
    lss_one_liner: str = "",
    max_commits: int = 100,
    timeout_s: float = 5.0,
) -> ArcContextSignal:
    """Construct an ``ArcContextSignal`` from the available primitives.

    Best-effort: any failure in the underlying producers (no git, missing
    LSS, malformed token) returns a partial signal — never raises. Callers
    can always pass the result to ``DirectionInferrer.infer(arc_context=...)``
    without further checks.
    """
    snapshot = compute_recent_momentum(
        project_root=project_root,
        max_commits=max_commits,
        timeout_s=timeout_s,
    )
    verify_ratio, apply_count, apply_mode = _parse_lss_one_liner(lss_one_liner)
    return ArcContextSignal(
        momentum=snapshot,
        lss_verify_ratio=verify_ratio,
        lss_apply_count=apply_count,
        lss_apply_mode=apply_mode,
        lss_one_liner=lss_one_liner,
    )


__all__ = [
    "ArcContextSignal",
    "MAX_NUDGE_PER_POSTURE",
    "build_arc_context",
]

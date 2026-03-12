"""backend/core/ouroboros/governance/autonomy/risk_classifier.py

Lightweight risk classification utility for L3 SafetyNet incident
escalation decisions.

NOT a replacement for the full :class:`RiskEngine` in
``governance/risk_engine.py``.  That engine classifies *proposed operations*
into risk tiers for the governance gate.  This module classifies
*operation outcomes* (rollback patterns, failure severity) so that
SafetyNet can decide how aggressively to escalate incidents.

All scoring is deterministic and configurable via constructor weights.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.RiskClassifier")


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------


class RiskLevel(Enum):
    """Risk levels for SafetyNet incident classification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RiskAssessment:
    """Result of a risk classification for an operation outcome."""

    level: RiskLevel
    score: float  # 0.0 to 1.0
    factors: Dict[str, float]  # individual factor scores
    reason: str  # human-readable explanation

    @property
    def is_actionable(self) -> bool:
        """True if risk level warrants SafetyNet intervention."""
        return self.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)


# ---------------------------------------------------------------------------
# Default scoring maps — extracted as module-level constants so they can be
# tested and overridden without subclassing.
# ---------------------------------------------------------------------------

#: Default factor weights (must sum to 1.0 for interpretable scores).
DEFAULT_WEIGHTS: Dict[str, float] = {
    "rollback_frequency": 0.30,
    "affected_file_count": 0.20,
    "pattern_repetition": 0.25,
    "root_cause_severity": 0.25,
}

#: Map of root-cause class names to severity scores.
ROOT_CAUSE_SEVERITY: Dict[str, float] = {
    "unknown": 0.3,
    "test_failure": 0.4,
    "validation_failure": 0.5,
    "timeout": 0.6,
    "syntax_error": 0.7,
    "permission_error": 0.9,
    "security_violation": 1.0,
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class OperationRiskClassifier:
    """Classifies operation risk for SafetyNet escalation decisions.

    NOT a replacement for the full RiskEngine in ``governance/risk_engine.py``.
    This is a lightweight utility that SafetyNet uses to determine how
    aggressively to escalate incidents.
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        self._weights: Dict[str, float] = weights or dict(DEFAULT_WEIGHTS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        rollback_count: int,
        window_s: float,  # noqa: ARG002 — reserved for rate-based scoring
        affected_files: List[str],
        root_cause_class: str,
        pattern_match: bool,
    ) -> RiskAssessment:
        """Classify risk based on rollback context.

        Parameters
        ----------
        rollback_count:
            Number of rollbacks recorded inside *window_s*.
        window_s:
            Duration of the observation window in seconds (reserved for rate-based scoring).
        affected_files:
            List of file paths touched by the rolled-back operation.
        root_cause_class:
            Classified root cause (e.g. ``"timeout"``, ``"permission_error"``).
        pattern_match:
            Whether a repeated-reason pattern was detected.

        Returns
        -------
        RiskAssessment
            Deterministic risk assessment with level, score, factors, and reason.
        """
        factors = {
            "rollback_frequency": self._score_rollback_frequency(rollback_count),
            "affected_file_count": self._score_file_count(len(affected_files)),
            "pattern_repetition": self._score_pattern(pattern_match),
            "root_cause_severity": self._score_root_cause(root_cause_class),
        }

        score = sum(
            factors[k] * self._weights.get(k, 0.0) for k in factors
        )
        # Clamp to [0.0, 1.0]
        score = max(0.0, min(1.0, score))

        level = self._score_to_level(score)
        reason = self._build_reason(
            level, score, factors, rollback_count, affected_files, root_cause_class, pattern_match,
        )

        return RiskAssessment(
            level=level,
            score=score,
            factors=factors,
            reason=reason,
        )

    def classify_from_rollback_history(
        self,
        history: List[Dict[str, Any]],
        window_s: float,
    ) -> RiskAssessment:
        """Convenience: classify from SafetyNet's ``_rollback_history`` format.

        Extracts rollback_count, affected_files, root_cause_class, and
        pattern_match from the history entries and delegates to :meth:`classify`.

        Parameters
        ----------
        history:
            List of dicts with keys ``op_id``, ``brain_id``, ``reason``, ``ts``,
            and optionally ``affected_files``.
        window_s:
            Duration of the observation window in seconds.
        """
        rollback_count = len(history)

        # Collect all affected files across history entries
        all_files: List[str] = []
        for entry in history:
            all_files.extend(entry.get("affected_files", []))
        # De-duplicate while preserving order
        seen: set[str] = set()
        unique_files: List[str] = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        # Determine dominant root cause class (most frequent reason)
        if history:
            from collections import Counter
            reason_counts = Counter(entry.get("reason", "unknown") for entry in history)
            dominant_reason = reason_counts.most_common(1)[0][0]
            # Check for pattern: same reason repeated 2+ times
            pattern_match = reason_counts.most_common(1)[0][1] >= 2
        else:
            dominant_reason = "unknown"
            pattern_match = False

        # Map the reason string to a root-cause class using the same
        # logic SafetyNet uses internally.  We import lazily to avoid
        # circular imports.
        root_cause_class = self._classify_reason_to_root_cause(dominant_reason)

        return self.classify(
            rollback_count=rollback_count,
            window_s=window_s,
            affected_files=unique_files,
            root_cause_class=root_cause_class,
            pattern_match=pattern_match,
        )

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_rollback_frequency(rollback_count: int) -> float:
        """Score rollback frequency: 0->0.0, 1->0.2, 2->0.4, 3-4->0.6, 5+->1.0."""
        if rollback_count <= 0:
            return 0.0
        if rollback_count == 1:
            return 0.2
        if rollback_count == 2:
            return 0.4
        if rollback_count <= 4:
            return 0.6
        return 1.0

    @staticmethod
    def _score_file_count(file_count: int) -> float:
        """Score affected file count: 0->0.0, 1-2->0.2, 3-5->0.5, 6-9->0.8, 10+->1.0."""
        if file_count <= 0:
            return 0.0
        if file_count <= 2:
            return 0.2
        if file_count <= 5:
            return 0.5
        if file_count <= 9:
            return 0.8
        return 1.0

    @staticmethod
    def _score_pattern(pattern_match: bool) -> float:
        """Score pattern repetition: False->0.0, True->0.7."""
        return 0.7 if pattern_match else 0.0

    @staticmethod
    def _score_root_cause(root_cause_class: str) -> float:
        """Score root cause severity using the module-level map."""
        return ROOT_CAUSE_SEVERITY.get(root_cause_class, 0.3)

    @staticmethod
    def _score_to_level(score: float) -> RiskLevel:
        """Map a [0.0, 1.0] score to a :class:`RiskLevel`.

        Thresholds: <0.3 LOW, <0.5 MEDIUM, <0.7 HIGH, >=0.7 CRITICAL.
        """
        if score < 0.3:
            return RiskLevel.LOW
        if score < 0.5:
            return RiskLevel.MEDIUM
        if score < 0.7:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL

    @staticmethod
    def _classify_reason_to_root_cause(reason: str) -> str:
        """Map a rollback reason string to a root-cause class.

        Mirrors :meth:`ProductionSafetyNet._classify_root_cause` logic
        without importing to avoid circular dependencies.
        """
        reason_lower = reason.lower()
        if "validation" in reason_lower or "validate" in reason_lower:
            return "validation_failure"
        if "timeout" in reason_lower:
            return "timeout"
        if "syntax" in reason_lower or "parse" in reason_lower:
            return "syntax_error"
        if "test" in reason_lower:
            return "test_failure"
        if "permission" in reason_lower or "access" in reason_lower:
            return "permission_error"
        if "security" in reason_lower:
            return "security_violation"
        return "unknown"

    @staticmethod
    def _build_reason(
        level: RiskLevel,
        score: float,
        factors: Dict[str, float],  # noqa: ARG004 — available for detailed reporting
        rollback_count: int,
        affected_files: List[str],
        root_cause_class: str,
        pattern_match: bool,
    ) -> str:
        """Build a human-readable reason string for the assessment."""
        parts: List[str] = [
            f"Risk {level.value} (score={score:.2f})",
        ]
        if rollback_count > 0:
            parts.append(f"{rollback_count} rollback(s)")
        if affected_files:
            parts.append(f"{len(affected_files)} file(s) affected")
        if pattern_match:
            parts.append("repeated pattern detected")
        parts.append(f"root cause: {root_cause_class}")
        return "; ".join(parts)

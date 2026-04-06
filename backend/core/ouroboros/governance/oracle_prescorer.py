# backend/core/ouroboros/governance/oracle_prescorer.py
"""
Oracle Pre-Scorer
=================

Fast approximate quality gate using TheOracle graph signals BEFORE full
validation.  Inspired by Wang's "oracle score function" suggestion.

The pre-score NEVER blocks candidates — it only prioritises and warns.
On any oracle failure the scorer always returns a neutral result (fail-open).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

logger = logging.getLogger("Ouroboros.OraclePreScorer")

# ---------------------------------------------------------------------------
# Risk mapping
# ---------------------------------------------------------------------------

_RISK_MAP: dict[str, float] = {
    "low": 0.0,
    "medium": 0.3,
    "high": 0.7,
    "critical": 1.0,
}

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

_W_BLAST: float = 0.30
_W_COUPLING: float = 0.25
_W_COMPLEXITY: float = 0.20
_W_TEST_COVERAGE: float = 0.15
_W_LOCALITY: float = 0.10

# ---------------------------------------------------------------------------
# Gate thresholds
# ---------------------------------------------------------------------------

_FAST_TRACK_THRESHOLD: float = 0.3
_WARN_THRESHOLD: float = 0.7


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreScoreResult:
    """Immutable result returned by :class:`OraclePreScorer`."""

    pre_score: float
    gate: str  # "FAST_TRACK" | "NORMAL" | "WARN"
    blast_radius_signal: float
    coupling_signal: float
    complexity_signal: float
    test_coverage_signal: float
    locality_signal: float


# ---------------------------------------------------------------------------
# Neutral / fallback singleton
# ---------------------------------------------------------------------------

_NEUTRAL = PreScoreResult(
    pre_score=0.5,
    gate="NORMAL",
    blast_radius_signal=0.5,
    coupling_signal=0.5,
    complexity_signal=0.5,
    test_coverage_signal=0.5,
    locality_signal=0.5,
)


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class OraclePreScorer:
    """Compute a pre-score for a change candidate using oracle graph signals.

    Parameters
    ----------
    oracle:
        Any object that exposes ``compute_blast_radius(file_path)``,
        ``get_dependencies(file_path)``, and ``get_dependents(file_path)``.
    """

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        target_files: List[str],
        max_complexity: int = 0,
        has_tests: bool = True,
    ) -> PreScoreResult:
        """Return a :class:`PreScoreResult` for the given candidate files.

        On any oracle failure returns the neutral result (0.5 / NORMAL).
        """
        try:
            return self._compute(target_files, max_complexity, has_tests)
        except Exception as exc:
            logger.warning("OraclePreScorer: oracle failure, returning neutral. %s", exc)
            return _NEUTRAL

    # ------------------------------------------------------------------
    # Internal computation
    # ------------------------------------------------------------------

    def _compute(
        self,
        target_files: List[str],
        max_complexity: int,
        has_tests: bool,
    ) -> PreScoreResult:
        # --- blast radius signal: worst across all target files -----------
        blast_radius_signal = 0.0
        for f in target_files:
            blast = self._oracle.compute_blast_radius(f)
            risk_score = _RISK_MAP.get(blast.risk_level, 0.5)
            linear_score = min(1.0, blast.total_affected / 50)
            file_signal = max(risk_score, linear_score)
            blast_radius_signal = max(blast_radius_signal, file_signal)

        # --- coupling signal: deps + dependents ---------------------------
        total_coupling = 0
        for f in target_files:
            total_coupling += len(self._oracle.get_dependencies(f))
            total_coupling += len(self._oracle.get_dependents(f))
        coupling_signal = min(1.0, total_coupling / 20)

        # --- complexity signal --------------------------------------------
        complexity_signal = min(1.0, max_complexity / 30)

        # --- test coverage signal ----------------------------------------
        test_coverage_signal = 0.0 if has_tests else 1.0

        # --- locality signal ---------------------------------------------
        if len(target_files) <= 1:
            locality_signal = 0.0
        else:
            unique_dirs = {str(Path(f).parent) for f in target_files}
            num_dirs = len(unique_dirs)
            locality_signal = 1.0 - (1.0 / num_dirs)

        # --- weighted pre-score ------------------------------------------
        pre_score = (
            _W_BLAST * blast_radius_signal
            + _W_COUPLING * coupling_signal
            + _W_COMPLEXITY * complexity_signal
            + _W_TEST_COVERAGE * test_coverage_signal
            + _W_LOCALITY * locality_signal
        )

        # --- gate assignment ---------------------------------------------
        if pre_score < _FAST_TRACK_THRESHOLD:
            gate = "FAST_TRACK"
        elif pre_score >= _WARN_THRESHOLD:
            gate = "WARN"
        else:
            gate = "NORMAL"

        return PreScoreResult(
            pre_score=pre_score,
            gate=gate,
            blast_radius_signal=blast_radius_signal,
            coupling_signal=coupling_signal,
            complexity_signal=complexity_signal,
            test_coverage_signal=test_coverage_signal,
            locality_signal=locality_signal,
        )

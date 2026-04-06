# backend/core/ouroboros/governance/convergence_tracker.py
"""
ConvergenceTracker — RSI Convergence Framework
===============================================

Monitors whether Ouroboros is converging healthily.  Based on Wang's
simulation results showing logarithmic convergence.  Uses composite scores
(lower = better) from ``composite_score.py``.

The tracker is **stateless**: it accepts a list of floats and returns a
:class:`ConvergenceReport`.  No persistence or LLM calls are needed.

Classification priority (highest to lowest):
  1. LOGARITHMIC   — r² of log-fit > 0.7 AND slope < -epsilon
  2. OSCILLATING   — oscillation ratio > 0.6
  3. PLATEAUED     — tail stddev < 0.02 AND |slope| < epsilon
  4. IMPROVING     — slope < -epsilon
  5. DEGRADING     — slope > epsilon
  6. PLATEAUED     — fallback
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import List

# ---------------------------------------------------------------------------
# ConvergenceState
# ---------------------------------------------------------------------------


class ConvergenceState(str, Enum):
    IMPROVING = "IMPROVING"
    LOGARITHMIC = "LOGARITHMIC"
    PLATEAUED = "PLATEAUED"
    OSCILLATING = "OSCILLATING"
    DEGRADING = "DEGRADING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# ConvergenceReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvergenceReport:
    """Frozen snapshot of a convergence analysis.

    Attributes
    ----------
    state:
        Classified convergence state.
    window_size:
        Maximum number of recent scores considered.
    slope:
        Slope from linear regression (negative = improving).
    r_squared_log:
        Coefficient of determination for the logarithmic fit.
        Returns 0.0 when the log-fit coefficient is non-negative (not decreasing).
    oscillation_ratio:
        Fraction of consecutive differences that alternate sign.
    plateau_stddev:
        Population standard deviation of the analysis window.
    scores_analyzed:
        Actual number of scores used (min of len(scores), window_size).
    recommendation:
        Human-readable guidance string.
    timestamp:
        Unix epoch timestamp of when the report was generated.
    """

    state: ConvergenceState
    window_size: int
    slope: float
    r_squared_log: float
    oscillation_ratio: float
    plateau_stddev: float
    scores_analyzed: int
    recommendation: str
    timestamp: float


# ---------------------------------------------------------------------------
# Recommendation strings
# ---------------------------------------------------------------------------

_RECOMMENDATIONS: dict[ConvergenceState, str] = {
    ConvergenceState.IMPROVING: (
        "Scores are trending downward — Ouroboros is making measurable progress. "
        "Maintain current strategy and monitor for plateau."
    ),
    ConvergenceState.LOGARITHMIC: (
        "Scores follow a logarithmic decay curve consistent with Wang's RSI model. "
        "Diminishing returns are expected; consider tightening constraints or "
        "introducing harder objectives to sustain progress."
    ),
    ConvergenceState.PLATEAUED: (
        "Scores have stabilised near a local minimum. "
        "Introduce diversity (mutation, new objectives) or adjust hyperparameters "
        "to escape the plateau."
    ),
    ConvergenceState.OSCILLATING: (
        "Scores are oscillating between high and low values. "
        "Reduce step size or learning rate and check for conflicting objectives."
    ),
    ConvergenceState.DEGRADING: (
        "Scores are trending upward — performance is regressing. "
        "Investigate recent changes, roll back if necessary, and inspect "
        "the blast-radius and lint metrics."
    ),
    ConvergenceState.INSUFFICIENT_DATA: (
        "Fewer than 5 data points are available; no reliable trend can be inferred. "
        "Collect more iterations before acting on convergence signals."
    ),
}

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_WINDOW = int(os.environ.get("OUROBOROS_CONVERGENCE_WINDOW", "20"))
_EPSILON = 0.01  # minimum |slope| to distinguish improving/degrading from plateau


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _linear_regression_slope(values: List[float]) -> float:
    """Return the slope *m* of the ordinary-least-squares line y = m*x + b.

    Indices are 0, 1, …, n-1.  Returns 0.0 for a single-element list.
    """
    n = len(values)
    if n <= 1:
        return 0.0

    # Precompute mean_x and mean_y
    mean_x = (n - 1) / 2.0  # sum(0..n-1)/n
    mean_y = sum(values) / n

    numerator = 0.0
    denominator = 0.0
    for i, y in enumerate(values):
        dx = i - mean_x
        numerator += dx * (y - mean_y)
        denominator += dx * dx

    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def _log_fit_r_squared(values: List[float]) -> float:
    """Return R² of the fit S = a*ln(t+1) + b, using t = 1..n.

    Returns 0.0 if the fitted coefficient *a* is >= 0 (i.e., the log curve
    is not decreasing — not the expected convergence pattern).

    The fit is computed via OLS on the transformed predictor x_i = ln(i+1),
    where i is the 0-based index (so t = i+1).
    """
    n = len(values)
    if n < 2:
        return 0.0

    # Build transformed x values: x_i = ln(t) where t = i+1 (1-indexed)
    xs = [math.log(i + 1) for i in range(n)]  # ln(1), ln(2), …, ln(n)

    mean_x = sum(xs) / n
    mean_y = sum(values) / n

    ss_xy = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)

    if ss_xx == 0.0:
        return 0.0

    a = ss_xy / ss_xx  # coefficient of ln(t+1)

    # Only meaningful for a decreasing log trend
    if a >= 0.0:
        return 0.0

    b = mean_y - a * mean_x
    y_pred = [a * xs[i] + b for i in range(n)]

    ss_res = sum((values[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((y - mean_y) ** 2 for y in values)

    if ss_tot == 0.0:
        return 0.0

    return max(0.0, 1.0 - ss_res / ss_tot)


def _oscillation_ratio(values: List[float]) -> float:
    """Return the fraction of consecutive differences whose signs alternate.

    A sign alternation at position i means sign(diff[i]) != sign(diff[i-1])
    and both differences are non-zero.

    Returns 0.0 for sequences with fewer than 2 elements or where no non-zero
    consecutive differences exist.
    """
    n = len(values)
    if n < 2:
        return 0.0

    diffs = [values[i + 1] - values[i] for i in range(n - 1)]

    # Filter out zero differences for sign comparison
    non_zero = [d for d in diffs if d != 0.0]
    if len(non_zero) < 2:
        return 0.0

    alternations = sum(
        1
        for i in range(1, len(non_zero))
        if (non_zero[i] > 0) != (non_zero[i - 1] > 0)
    )
    return alternations / (len(non_zero) - 1)


def _stddev(values: List[float]) -> float:
    """Return the population standard deviation of *values*.

    Returns 0.0 for a list with fewer than 2 elements.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# ConvergenceTracker
# ---------------------------------------------------------------------------


class ConvergenceTracker:
    """Stateless analyser that classifies a list of composite scores.

    Parameters
    ----------
    window_size:
        Maximum number of recent scores to consider.  Overrides the
        ``OUROBOROS_CONVERGENCE_WINDOW`` environment variable when supplied
        explicitly.
    epsilon:
        Minimum absolute slope to distinguish IMPROVING/DEGRADING from PLATEAUED.
    """

    def __init__(
        self,
        window_size: int = _DEFAULT_WINDOW,
        epsilon: float = _EPSILON,
    ) -> None:
        self._window_size = window_size
        self._epsilon = epsilon

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, scores: List[float]) -> ConvergenceReport:
        """Classify the convergence behaviour of *scores*.

        Parameters
        ----------
        scores:
            Ordered list of composite scores (oldest first, lower = better).

        Returns
        -------
        ConvergenceReport
            Frozen report with classification, statistics, and recommendation.
        """
        scores_analyzed = len(scores)
        window = scores[-self._window_size :]  # noqa: E203 — last N scores

        if scores_analyzed < 5:
            return ConvergenceReport(
                state=ConvergenceState.INSUFFICIENT_DATA,
                window_size=self._window_size,
                slope=0.0,
                r_squared_log=0.0,
                oscillation_ratio=0.0,
                plateau_stddev=0.0,
                scores_analyzed=scores_analyzed,
                recommendation=_RECOMMENDATIONS[ConvergenceState.INSUFFICIENT_DATA],
                timestamp=time.time(),
            )

        slope = _linear_regression_slope(window)
        r_sq_log = _log_fit_r_squared(window)
        osc_ratio = _oscillation_ratio(window)
        stddev = _stddev(window)

        state = self._classify(slope, r_sq_log, osc_ratio, stddev)

        return ConvergenceReport(
            state=state,
            window_size=self._window_size,
            slope=slope,
            r_squared_log=r_sq_log,
            oscillation_ratio=osc_ratio,
            plateau_stddev=stddev,
            scores_analyzed=len(window),
            recommendation=_RECOMMENDATIONS[state],
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Internal classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        slope: float,
        r_sq_log: float,
        osc_ratio: float,
        stddev: float,
    ) -> ConvergenceState:
        """Apply classification rules in priority order."""
        eps = self._epsilon

        # Priority 1: LOGARITHMIC
        if r_sq_log > 0.7 and slope < -eps:
            return ConvergenceState.LOGARITHMIC

        # Priority 2: OSCILLATING
        if osc_ratio > 0.6:
            return ConvergenceState.OSCILLATING

        # Priority 3: PLATEAUED (tight)
        if stddev < 0.02 and abs(slope) < eps:
            return ConvergenceState.PLATEAUED

        # Priority 4 / 5: IMPROVING / DEGRADING
        if slope < -eps:
            return ConvergenceState.IMPROVING
        if slope > eps:
            return ConvergenceState.DEGRADING

        # Fallback
        return ConvergenceState.PLATEAUED

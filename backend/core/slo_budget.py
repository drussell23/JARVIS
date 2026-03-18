"""backend/core/slo_budget.py — P3-1 SLO-backed health model.

Replaces binary alive/dead health checks with latency/error-rate/saturation
budget accounting.

Design:
* ``SLOTarget`` — immutable spec: metric, threshold, rolling window.
* ``SLOWindow`` — ring buffer of (timestamp, value) observations.
* ``SLOBudget`` — per-target budget tracker that maps observations → status.
* ``SLOHealthModel`` — holds multiple budgets; accepts ``record()`` calls and
  produces an aggregated ``HealthStatus``.
* ``SLORegistry`` + ``get_slo_registry()`` — process-wide registry.

All timing uses ``time.monotonic()``.  ``HealthStatus`` matches the values
in ``backend/core/health_contracts.py`` so callers can feed results there.
"""
from __future__ import annotations

import enum
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

__all__ = [
    "SLOMetric",
    "SLOStatus",
    "SLOTarget",
    "SLOBudget",
    "SLOHealthModel",
    "SLORegistry",
    "get_slo_registry",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SLOMetric(str, enum.Enum):
    """Standardised metric names accepted by SLOBudget."""

    LATENCY_P95_S = "latency_p95_s"
    ERROR_RATE = "error_rate"        # fraction 0‒1
    SATURATION = "saturation"        # fraction 0‒1 (e.g. queue/queue_cap)


class SLOStatus(str, enum.Enum):
    """Health status produced by SLO evaluation.

    Maps 1-to-1 with ``HealthStatus`` from ``health_contracts.py`` so callers
    can coerce without importing that module.
    """

    HEALTHY = "healthy"          # Within budget
    DEGRADED = "degraded"        # Burn rate elevated; budget shrinking fast
    UNHEALTHY = "unhealthy"      # Budget exhausted
    UNKNOWN = "unknown"          # Insufficient observations


# ---------------------------------------------------------------------------
# SLOTarget — immutable specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLOTarget:
    """Immutable SLO specification for one metric.

    Parameters
    ----------
    metric:
        Which metric this target governs.
    threshold:
        Acceptable upper bound (e.g. 0.05 for 5 % error rate, 2.0 for 2 s p95).
    window_s:
        Rolling window in seconds over which violations are counted.
    budget_fraction:
        Fraction of observations within *window_s* that may violate *threshold*
        before status degrades.  E.g. 0.01 = 1 % violation budget.
    degraded_burn_multiplier:
        If the current burn rate is this multiple of the target budget fraction,
        emit DEGRADED before the budget is actually exhausted.
    """

    metric: SLOMetric
    threshold: float
    window_s: float = 300.0          # 5-minute rolling window
    budget_fraction: float = 0.01    # 1 % error budget
    degraded_burn_multiplier: float = 2.0


# ---------------------------------------------------------------------------
# SLOWindow — bounded ring buffer of (mono_ts, value) pairs
# ---------------------------------------------------------------------------


class SLOWindow:
    """Rolling ring buffer of (monotonic_timestamp, value) observations.

    Only values within the target's ``window_s`` are retained.
    """

    def __init__(self, window_s: float) -> None:
        self._window_s = window_s
        self._buf: Deque[Tuple[float, float]] = deque()

    def record(self, value: float) -> None:
        now = time.monotonic()
        self._buf.append((now, value))
        self._evict(now)

    def violation_rate(self, threshold: float) -> float:
        """Fraction of observations in the window that exceed *threshold*."""
        now = time.monotonic()
        self._evict(now)
        total = len(self._buf)
        if total == 0:
            return 0.0
        violations = sum(1 for _, v in self._buf if v > threshold)
        return violations / total

    def count(self) -> int:
        now = time.monotonic()
        self._evict(now)
        return len(self._buf)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()


# ---------------------------------------------------------------------------
# SLOBudget — per-target budget tracker
# ---------------------------------------------------------------------------

# Minimum observations before we'll emit anything other than UNKNOWN.
_MIN_OBS_FOR_STATUS = 5


class SLOBudget:
    """Tracks error-budget consumption for one SLOTarget.

    Usage::

        budget = SLOBudget(SLOTarget(SLOMetric.ERROR_RATE, threshold=0.05))
        budget.record(0.02)  # 2 % error rate observation
        status = budget.status()
    """

    def __init__(self, target: SLOTarget) -> None:
        self._target = target
        self._window = SLOWindow(target.window_s)

    @property
    def target(self) -> SLOTarget:
        return self._target

    def record(self, value: float) -> None:
        """Record one observation of the metric (e.g. a latency sample)."""
        self._window.record(value)

    def status(self) -> SLOStatus:
        """Evaluate current budget consumption → SLOStatus."""
        if self._window.count() < _MIN_OBS_FOR_STATUS:
            return SLOStatus.UNKNOWN

        vr = self._window.violation_rate(self._target.threshold)
        budget = self._target.budget_fraction

        if vr > budget:
            logger.warning(
                "[SLO] %s violation_rate=%.3f > budget=%.3f → UNHEALTHY",
                self._target.metric.value, vr, budget,
            )
            return SLOStatus.UNHEALTHY

        burn_threshold = budget * self._target.degraded_burn_multiplier
        if vr > burn_threshold:
            logger.info(
                "[SLO] %s violation_rate=%.3f > burn_threshold=%.3f → DEGRADED",
                self._target.metric.value, vr, burn_threshold,
            )
            return SLOStatus.DEGRADED

        return SLOStatus.HEALTHY

    def remaining_budget(self) -> float:
        """Return fraction of error budget still available (negative = over)."""
        vr = self._window.violation_rate(self._target.threshold)
        return self._target.budget_fraction - vr


# ---------------------------------------------------------------------------
# SLOHealthModel — multi-metric aggregator
# ---------------------------------------------------------------------------


class SLOHealthModel:
    """Aggregates multiple SLOBudgets into a single component health status.

    The worst individual status wins (UNHEALTHY > DEGRADED > HEALTHY > UNKNOWN).

    Parameters
    ----------
    component:
        Name of the component being monitored (for logging).
    targets:
        List of SLOTargets to track.
    """

    _STATUS_RANK = {
        SLOStatus.UNKNOWN: 0,
        SLOStatus.HEALTHY: 1,
        SLOStatus.DEGRADED: 2,
        SLOStatus.UNHEALTHY: 3,
    }

    def __init__(self, component: str, targets: List[SLOTarget]) -> None:
        self._component = component
        self._budgets: Dict[SLOMetric, SLOBudget] = {
            t.metric: SLOBudget(t) for t in targets
        }

    def record(self, metric: SLOMetric, value: float) -> None:
        """Feed one observation.  Silently ignored for unregistered metrics."""
        budget = self._budgets.get(metric)
        if budget is not None:
            budget.record(value)

    def status(self) -> SLOStatus:
        """Return the worst status across all tracked metrics."""
        worst = SLOStatus.UNKNOWN
        for budget in self._budgets.values():
            s = budget.status()
            if self._STATUS_RANK[s] > self._STATUS_RANK[worst]:
                worst = s
        return worst

    def per_metric_status(self) -> Dict[str, SLOStatus]:
        """Return a {metric_name: status} dict for observability."""
        return {m.value: b.status() for m, b in self._budgets.items()}

    def remaining_budgets(self) -> Dict[str, float]:
        """Return remaining error budget per metric."""
        return {m.value: b.remaining_budget() for m, b in self._budgets.items()}


# ---------------------------------------------------------------------------
# SLORegistry — process-wide registry
# ---------------------------------------------------------------------------


class SLORegistry:
    """Process-wide registry of SLOHealthModel instances."""

    def __init__(self) -> None:
        self._models: Dict[str, SLOHealthModel] = {}

    def register(self, component: str, targets: List[SLOTarget]) -> SLOHealthModel:
        """Register (or replace) an SLOHealthModel for *component*."""
        model = SLOHealthModel(component, targets)
        self._models[component] = model
        logger.info("[SLORegistry] Registered SLOs for '%s': %d targets", component, len(targets))
        return model

    def get(self, component: str) -> Optional[SLOHealthModel]:
        """Return the model for *component*, or None if not registered."""
        return self._models.get(component)

    def all_components(self) -> Dict[str, SLOHealthModel]:
        """Return a snapshot of all registered models."""
        return dict(self._models)

    def aggregate_status(self) -> SLOStatus:
        """Return the worst status across all registered components."""
        rank = SLOHealthModel._STATUS_RANK
        worst = SLOStatus.UNKNOWN
        for model in self._models.values():
            s = model.status()
            if rank[s] > rank[worst]:
                worst = s
        return worst


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_registry: Optional[SLORegistry] = None


def get_slo_registry() -> SLORegistry:
    """Return (lazily creating) the process-wide SLORegistry."""
    global _g_registry
    if _g_registry is None:
        _g_registry = SLORegistry()
    return _g_registry

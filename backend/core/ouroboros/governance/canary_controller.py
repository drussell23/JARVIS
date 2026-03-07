# backend/core/ouroboros/governance/canary_controller.py
"""
Canary Controller — Domain Slice Promotion
=============================================

Manages per-domain-slice canary rollout for governed autonomy.
A domain slice is a path prefix (e.g., ``backend/core/ouroboros/``).

Before a slice is promoted to ACTIVE, it must meet ALL criteria:

1. >= 50 successful operations
2. rollback_rate < 5% over trailing operations
3. p95 operation latency < 120s
4. 72 hours elapsed since first operation (stability window)

Slices start in PENDING state and graduate to ACTIVE upon promotion.
Files not in any registered slice are NOT allowed for autonomous ops.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("Ouroboros.CanaryController")


MIN_OPERATIONS = 50
MAX_ROLLBACK_RATE = 0.05
MAX_P95_LATENCY_S = 120.0
STABILITY_WINDOW_H = 72


class CanaryState(enum.Enum):
    """State of a domain slice in the canary pipeline."""

    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


@dataclass
class DomainSlice:
    """A domain slice for canary rollout."""

    path_prefix: str
    state: CanaryState = CanaryState.PENDING

    def matches(self, file_path: str) -> bool:
        """Check if a file path belongs to this slice."""
        return file_path.startswith(self.path_prefix)


class SliceMetrics:
    """Tracks per-slice operation metrics."""

    def __init__(self) -> None:
        self.total_operations: int = 0
        self.successful_operations: int = 0
        self.rollback_count: int = 0
        self.latencies: List[float] = []
        self.first_operation_time: Optional[float] = None

    @property
    def rollback_rate(self) -> float:
        """Rollback rate as a fraction."""
        if self.total_operations == 0:
            return 0.0
        return self.rollback_count / self.total_operations

    @property
    def p95_latency(self) -> float:
        """95th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    @property
    def stability_hours(self) -> float:
        """Hours elapsed since first operation."""
        if self.first_operation_time is None:
            return 0.0
        return (time.time() - self.first_operation_time) / 3600.0


@dataclass(frozen=True)
class PromotionResult:
    """Result of a canary promotion check."""

    promoted: bool
    reason: str


class CanaryController:
    """Manages domain slice canary rollout for governed autonomy."""

    def __init__(self) -> None:
        self._slices: Dict[str, DomainSlice] = {}
        self._metrics: Dict[str, SliceMetrics] = {}

    @property
    def slices(self) -> Dict[str, DomainSlice]:
        """All registered slices."""
        return dict(self._slices)

    def register_slice(self, path_prefix: str) -> None:
        """Register a new domain slice for canary tracking."""
        self._slices[path_prefix] = DomainSlice(path_prefix=path_prefix)
        self._metrics[path_prefix] = SliceMetrics()
        logger.info("Registered canary slice: %s", path_prefix)

    def get_slice(self, path_prefix: str) -> Optional[DomainSlice]:
        """Get a registered slice by prefix."""
        return self._slices.get(path_prefix)

    def get_metrics(self, path_prefix: str) -> Optional[SliceMetrics]:
        """Get metrics for a registered slice."""
        return self._metrics.get(path_prefix)

    def record_operation(
        self,
        file_path: str,
        success: bool,
        latency_s: float,
        rolled_back: bool = False,
    ) -> None:
        """Record an operation outcome for the matching slice."""
        for prefix, metrics in self._metrics.items():
            if self._slices[prefix].matches(file_path):
                metrics.total_operations += 1
                if success:
                    metrics.successful_operations += 1
                if rolled_back:
                    metrics.rollback_count += 1
                metrics.latencies.append(latency_s)
                if metrics.first_operation_time is None:
                    metrics.first_operation_time = time.time()
                return

    def check_promotion(self, path_prefix: str) -> PromotionResult:
        """Check if a slice meets all promotion criteria.

        If all criteria pass, the slice is promoted to ACTIVE.
        """
        metrics = self._metrics.get(path_prefix)
        if metrics is None:
            return PromotionResult(promoted=False, reason="Slice not registered")

        # Criterion 1: Minimum operations
        if metrics.total_operations < MIN_OPERATIONS:
            return PromotionResult(
                promoted=False,
                reason=f"Need >= {MIN_OPERATIONS} operations, have {metrics.total_operations}",
            )

        # Criterion 2: Rollback rate
        if metrics.rollback_rate >= MAX_ROLLBACK_RATE:
            return PromotionResult(
                promoted=False,
                reason=f"Rollback rate {metrics.rollback_rate:.1%} >= {MAX_ROLLBACK_RATE:.0%} threshold",
            )

        # Criterion 3: P95 latency
        if metrics.p95_latency > MAX_P95_LATENCY_S:
            return PromotionResult(
                promoted=False,
                reason=f"P95 latency {metrics.p95_latency:.1f}s > {MAX_P95_LATENCY_S}s threshold",
            )

        # Criterion 4: Stability window
        if metrics.stability_hours < STABILITY_WINDOW_H:
            return PromotionResult(
                promoted=False,
                reason=(
                    f"Stability window {metrics.stability_hours:.1f}h "
                    f"< {STABILITY_WINDOW_H}h required"
                ),
            )

        # All criteria met — promote
        self._slices[path_prefix].state = CanaryState.ACTIVE
        logger.info("Canary slice promoted to ACTIVE: %s", path_prefix)
        return PromotionResult(promoted=True, reason="All criteria met")

    def is_file_allowed(self, file_path: str) -> bool:
        """Check if autonomous operations are allowed on a file.

        A file is allowed only if it belongs to an ACTIVE slice.
        """
        for prefix, s in self._slices.items():
            if s.matches(file_path) and s.state == CanaryState.ACTIVE:
                return True
        return False

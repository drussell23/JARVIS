"""backend/core/entropy_monitor.py — P3-4 long-uptime entropy management.

Monitors system entropy signals that accumulate over long uptimes:
* Queue depth growth
* File-descriptor creep
* Cache size bloat
* Stale entry accumulation
* Log file byte growth

Design:
* ``EntropyMetric`` — named metric categories.
* ``EntropyThreshold`` — (warn_at, critical_at) bounds per metric.
* ``EntropySnapshot`` — point-in-time immutable reading of all metrics.
* ``EntropyMonitor`` — records observations, evaluates thresholds, and
  invokes registered compaction handlers.
* ``CompactionAdvice`` — result of a threshold check.

All timing uses ``time.monotonic()``.
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

__all__ = [
    "EntropyMetric",
    "CompactionAdvice",
    "EntropyThreshold",
    "EntropySnapshot",
    "EntropyMonitor",
    "get_entropy_monitor",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EntropyMetric
# ---------------------------------------------------------------------------


class EntropyMetric(str, enum.Enum):
    """Named entropy signal categories."""

    QUEUE_DEPTH = "queue_depth"        # Number of items waiting in any queue
    FD_COUNT = "fd_count"              # Open file descriptors
    CACHE_SIZE = "cache_size"          # Entries in an in-process cache
    STALE_ENTRY_COUNT = "stale_entries"  # Entries past their TTL
    LOG_BYTES = "log_bytes"            # Total on-disk log size (bytes)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Built-in conservative defaults
_DEFAULT_THRESHOLDS: Dict[EntropyMetric, Tuple[float, float]] = {
    EntropyMetric.QUEUE_DEPTH:      (400,    800),     # warn @ 400, critical @ 800
    EntropyMetric.FD_COUNT:         (512,    1024),
    EntropyMetric.CACHE_SIZE:       (5_000,  20_000),
    EntropyMetric.STALE_ENTRY_COUNT:(100,    500),
    EntropyMetric.LOG_BYTES:        (50 * 1024 ** 2, 200 * 1024 ** 2),  # 50MB / 200MB
}


@dataclass(frozen=True)
class EntropyThreshold:
    """Warn/critical bounds for one EntropyMetric."""

    metric: EntropyMetric
    warn_at: float
    critical_at: float

    def evaluate(self, value: float) -> "CompactionAdvice":
        if value >= self.critical_at:
            return CompactionAdvice(
                required=True,
                reason=f"{self.metric.value}={value:.0f} >= critical={self.critical_at:.0f}",
            )
        if value >= self.warn_at:
            return CompactionAdvice(
                required=False,
                reason=f"{self.metric.value}={value:.0f} >= warn={self.warn_at:.0f}",
            )
        return CompactionAdvice(required=False, reason="")


# ---------------------------------------------------------------------------
# CompactionAdvice
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionAdvice:
    """Advice returned when checking a metric against its threshold.

    ``required=True`` means the metric is at or above the critical threshold
    and compaction should run immediately.

    ``required=False`` with a non-empty ``reason`` means the metric is in the
    warning zone; compaction is advisory.

    ``required=False`` with ``reason=""`` means the metric is within budget.
    """

    required: bool
    reason: str

    @property
    def in_budget(self) -> bool:
        return not self.required and not self.reason


# ---------------------------------------------------------------------------
# EntropySnapshot — immutable point-in-time capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntropySnapshot:
    """Point-in-time snapshot of all entropy metric readings."""

    timestamp_mono: float
    readings: Dict[str, float]   # metric.value → observed value

    @classmethod
    def capture(cls, readings: Dict[EntropyMetric, float]) -> "EntropySnapshot":
        return cls(
            timestamp_mono=time.monotonic(),
            readings={m.value: v for m, v in readings.items()},
        )

    def get(self, metric: EntropyMetric, default: float = 0.0) -> float:
        return self.readings.get(metric.value, default)


# ---------------------------------------------------------------------------
# EntropyMonitor
# ---------------------------------------------------------------------------

CompactionHandler = Callable[[EntropyMetric], None]


class EntropyMonitor:
    """Records entropy observations and drives compaction.

    Usage::

        monitor = EntropyMonitor()
        monitor.record(EntropyMetric.QUEUE_DEPTH, len(queue))
        advice = monitor.should_compact(EntropyMetric.QUEUE_DEPTH)
        if advice.required:
            monitor.compact(EntropyMetric.QUEUE_DEPTH)
    """

    def __init__(
        self,
        thresholds: Optional[Dict[EntropyMetric, Tuple[float, float]]] = None,
    ) -> None:
        raw = thresholds or _DEFAULT_THRESHOLDS
        self._thresholds: Dict[EntropyMetric, EntropyThreshold] = {
            m: EntropyThreshold(metric=m, warn_at=w, critical_at=c)
            for m, (w, c) in raw.items()
        }
        self._current: Dict[EntropyMetric, float] = {m: 0.0 for m in EntropyMetric}
        self._handlers: Dict[EntropyMetric, List[CompactionHandler]] = {
            m: [] for m in EntropyMetric
        }
        self._last_compact_mono: Dict[EntropyMetric, float] = {
            m: 0.0 for m in EntropyMetric
        }

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, metric: EntropyMetric, value: float) -> None:
        """Update the latest observed value for *metric*."""
        self._current[metric] = value

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def should_compact(self, metric: EntropyMetric) -> CompactionAdvice:
        """Check whether *metric* warrants compaction."""
        threshold = self._thresholds.get(metric)
        if threshold is None:
            return CompactionAdvice(required=False, reason="")
        return threshold.evaluate(self._current.get(metric, 0.0))

    def snapshot(self) -> EntropySnapshot:
        """Return an immutable snapshot of all current readings."""
        return EntropySnapshot.capture(dict(self._current))

    # ------------------------------------------------------------------
    # Compaction handler registry
    # ------------------------------------------------------------------

    def register_handler(
        self,
        metric: EntropyMetric,
        handler: CompactionHandler,
    ) -> None:
        """Register a compaction handler for *metric*.

        Handlers are called in registration order when ``compact()`` is
        invoked.  A handler should reduce the value of the metric it handles.
        """
        self._handlers[metric].append(handler)

    def compact(self, metric: EntropyMetric) -> None:
        """Invoke all registered compaction handlers for *metric*.

        Logs the before/after reading so the effectiveness is observable.
        """
        before = self._current.get(metric, 0.0)
        handlers = self._handlers.get(metric, [])
        if not handlers:
            logger.warning(
                "[EntropyMonitor] compact(%s) called but no handlers registered",
                metric.value,
            )
            return
        for handler in handlers:
            try:
                handler(metric)
            except Exception:
                logger.exception(
                    "[EntropyMonitor] compact handler failed for %s", metric.value
                )
        after = self._current.get(metric, 0.0)
        self._last_compact_mono[metric] = time.monotonic()
        logger.info(
            "[EntropyMonitor] compact(%s) complete: %.0f → %.0f",
            metric.value, before, after,
        )

    def compact_if_needed(self, metric: EntropyMetric) -> CompactionAdvice:
        """Evaluate threshold and auto-compact if critical.  Returns the advice."""
        advice = self.should_compact(metric)
        if advice.required:
            logger.warning(
                "[EntropyMonitor] auto-compact triggered: %s", advice.reason
            )
            self.compact(metric)
        elif advice.reason:
            logger.info(
                "[EntropyMonitor] advisory: %s (not compacting yet)", advice.reason
            )
        return advice

    def compact_all_needed(self) -> Dict[str, CompactionAdvice]:
        """Run ``compact_if_needed`` for every metric. Returns per-metric advice."""
        return {
            m.value: self.compact_if_needed(m)
            for m in EntropyMetric
        }

    def seconds_since_last_compact(self, metric: EntropyMetric) -> float:
        """Return seconds since last compaction for *metric* (inf if never)."""
        last = self._last_compact_mono.get(metric, 0.0)
        if last == 0.0:
            return float("inf")
        return time.monotonic() - last


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_g_monitor: Optional[EntropyMonitor] = None


def get_entropy_monitor() -> EntropyMonitor:
    """Return (lazily creating) the process-wide EntropyMonitor."""
    global _g_monitor
    if _g_monitor is None:
        _g_monitor = EntropyMonitor()
    return _g_monitor

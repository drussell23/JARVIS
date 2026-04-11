"""Rolling p95 DW RT latency tracker — dynamic Tier 0 budget (Manifesto §5).

The fixed 39s Tier 0 timeout starves cold DW endpoints and wastes budget on
hot endpoints. This tracker watches real RT latencies and recommends a
budget based on the rolling p95, clamped to a route-aware ceiling/floor.

Semantics
---------
- Cold-start safety: fewer than ``_COLD_THRESHOLD_SAMPLES`` samples → return
  the route ceiling (default 90s) so the first few calls get full runway.
- Failure backoff: ``_COLD_FAILURE_THRESHOLD`` consecutive failures → return
  ceiling (treat endpoint as cold again).
- Hot path: once we have enough samples, recommend ``p95 * _P95_MULT`` clamped
  to ``[floor, ceiling]``.  A p95 of 6s with mult 1.5 → 9s, clamped up to
  ``_FLOOR_S`` (default 15s).
- Thread-safe via a module-level lock.
- Env knobs:
    * ``JARVIS_DW_DYNAMIC_BUDGET_ENABLED`` — master switch (default on).
    * ``JARVIS_DW_LATENCY_WINDOW`` — rolling window size (default 20).
    * ``JARVIS_DW_LATENCY_CEILING_S`` — hard upper bound (default 90.0).
    * ``JARVIS_DW_LATENCY_FLOOR_S``   — hard lower bound (default 15.0).
    * ``JARVIS_DW_LATENCY_P95_MULT``  — safety multiplier (default 1.5).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() not in ("false", "0", "no", "off")


_ENABLED_DEFAULT = _env_bool("JARVIS_DW_DYNAMIC_BUDGET_ENABLED", True)
_WINDOW_DEFAULT = max(3, int(os.environ.get("JARVIS_DW_LATENCY_WINDOW", "20")))
_CEILING_DEFAULT = float(os.environ.get("JARVIS_DW_LATENCY_CEILING_S", "90.0"))
_FLOOR_DEFAULT = float(os.environ.get("JARVIS_DW_LATENCY_FLOOR_S", "15.0"))
_P95_MULT_DEFAULT = float(os.environ.get("JARVIS_DW_LATENCY_P95_MULT", "1.5"))

_COLD_THRESHOLD_SAMPLES = 3
_COLD_FAILURE_THRESHOLD = 3


class DwLatencyTracker:
    """Rolling-window p95 latency tracker for DW RT calls."""

    def __init__(
        self,
        *,
        window: int = _WINDOW_DEFAULT,
        ceiling_s: float = _CEILING_DEFAULT,
        floor_s: float = _FLOOR_DEFAULT,
        p95_mult: float = _P95_MULT_DEFAULT,
        enabled: Optional[bool] = None,
    ) -> None:
        self._window = max(3, int(window))
        self._ceiling_s = float(ceiling_s)
        self._floor_s = float(floor_s)
        self._p95_mult = float(p95_mult)
        self._enabled = _ENABLED_DEFAULT if enabled is None else bool(enabled)

        self._samples: Deque[float] = deque(maxlen=self._window)
        self._consecutive_failures = 0
        self._lock = threading.Lock()
        self._last_update_ns = 0
        self._total_samples = 0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_success(self, elapsed_s: float) -> None:
        """Record a successful RT generation latency in seconds."""
        if elapsed_s <= 0:
            return
        with self._lock:
            self._samples.append(float(elapsed_s))
            self._consecutive_failures = 0
            self._last_update_ns = time.monotonic_ns()
            self._total_samples += 1

    def record_failure(self) -> None:
        """Record a timeout or error (cold-backoff signal)."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_update_ns = time.monotonic_ns()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def p95(self) -> Optional[float]:
        """Return the current rolling p95 latency (s), or None if cold."""
        with self._lock:
            if len(self._samples) < _COLD_THRESHOLD_SAMPLES:
                return None
            return self._p95_locked()

    def _p95_locked(self) -> float:
        s = sorted(self._samples)
        idx = min(len(s) - 1, int(0.95 * len(s)))
        return s[idx]

    def recommended_budget(
        self,
        *,
        route_ceiling_s: Optional[float] = None,
        complexity_multiplier: float = 1.0,
    ) -> float:
        """Return the recommended Tier 0 budget for a call.

        Parameters
        ----------
        route_ceiling_s:
            Caller-provided hard ceiling (e.g. 120s for "complex" route,
            90s for "standard").  The tracker will never exceed this.
            Default: the tracker's own ``ceiling_s``.
        complexity_multiplier:
            Extra scaling for heavier tasks (1.0 = no change).
        """
        ceiling = route_ceiling_s if route_ceiling_s is not None else self._ceiling_s
        if not self._enabled:
            return ceiling

        with self._lock:
            # Cold-start: give the endpoint full runway.
            if self._consecutive_failures >= _COLD_FAILURE_THRESHOLD:
                return ceiling
            if len(self._samples) < _COLD_THRESHOLD_SAMPLES:
                return ceiling
            p95 = self._p95_locked()

        scaled = p95 * self._p95_mult * float(complexity_multiplier)
        return max(self._floor_s, min(ceiling, scaled))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        """Return a debug snapshot of tracker state."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "window": self._window,
                "samples": len(self._samples),
                "total_samples": self._total_samples,
                "consecutive_failures": self._consecutive_failures,
                "ceiling_s": self._ceiling_s,
                "floor_s": self._floor_s,
                "p95_mult": self._p95_mult,
                "p95_s": self._p95_locked() if len(self._samples) >= _COLD_THRESHOLD_SAMPLES else None,
            }

    def reset(self) -> None:
        """Clear all samples + failure state. For tests only."""
        with self._lock:
            self._samples.clear()
            self._consecutive_failures = 0
            self._total_samples = 0


# ---------------------------------------------------------------------------
# Module-level default tracker (shared across CandidateGenerator instances)
# ---------------------------------------------------------------------------


_default_tracker: Optional[DwLatencyTracker] = None
_default_lock = threading.Lock()


def get_default_tracker() -> DwLatencyTracker:
    """Return the process-wide default tracker, creating it on first call."""
    global _default_tracker
    with _default_lock:
        if _default_tracker is None:
            _default_tracker = DwLatencyTracker()
        return _default_tracker


def reset_default_tracker() -> None:
    """Reset the default tracker. For tests only."""
    global _default_tracker
    with _default_lock:
        _default_tracker = None

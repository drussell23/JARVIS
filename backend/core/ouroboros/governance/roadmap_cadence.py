"""Slice 211 — Adaptive stress-aware cadence for the roadmap orchestrator.

The roadmap orchestrator (``roadmap_orchestrator.execute_roadmap``) is a
complete, tested strategic driver that reads the operator-signed roadmap and
emits goal envelopes into intake — but it had ZERO callers in the live loop
(the disconnected wire found in the GOAL-001 autonomy test). This module is
the adaptive cadence that the GLS daemon uses to drive it in single-poll
bursts, so the strategic loop continuously feeds on operator goals while
backing off its token footprint when the vendor environment is hostile.

THE FORMULA CORRECTION (load-bearing). The proposed
``Interval = base * (1 + provider_exhaustions)`` is broken: provider_exhaustions
is a CUMULATIVE counter that only grows, so the interval would balloon to 51x,
101x, ... and NEVER recover even after the vendor stabilizes. The coherent
version backs off on the RECENT RATE — exhaustions since the last poll — so it
returns to baseline the moment stress subsides:

    Interval_next = min(base * (1 + recent_exhaustion_delta), max_interval)

recent_exhaustion_delta = provider_exhaustions(now) - provider_exhaustions(last
poll), floored at 0. delta == 0 (vendor stable) -> interval == base (recovered).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_BASE_S = "JARVIS_ROADMAP_CADENCE_BASE_S"
_ENV_MAX_S = "JARVIS_ROADMAP_CADENCE_MAX_S"
_DEFAULT_BASE_S = 120.0
_DEFAULT_MAX_S = 1800.0


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def base_interval_s() -> float:
    return _envf(_ENV_BASE_S, _DEFAULT_BASE_S)


def max_interval_s() -> float:
    return _envf(_ENV_MAX_S, _DEFAULT_MAX_S)


def compute_adaptive_interval(
    base_s: float,
    exhaustion_delta: float,
    max_s: float,
) -> float:
    """Recovering stress-aware backoff. delta is the RECENT exhaustion rate
    (count since last poll), NOT the cumulative total — so the interval
    returns to base when the vendor stabilizes. NEVER raises."""
    try:
        base = max(1.0, float(base_s))
        cap = max(base, float(max_s))
        delta = max(0.0, float(exhaustion_delta))
        return min(base * (1.0 + delta), cap)
    except Exception:  # noqa: BLE001
        return max(1.0, float(base_s) if base_s else _DEFAULT_BASE_S)


class AdaptiveRoadmapCadence:
    """Tracks the cumulative provider_exhaustions reading between polls and
    derives the recent-rate delta for the backoff. NEVER raises."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_exhaustions: Optional[int] = None

    def _current_exhaustions(self) -> int:
        try:
            from backend.core.ouroboros.governance.observability_registry import (
                PROVIDER_EXHAUSTIONS, get_observability_registry,
            )
            return int(get_observability_registry().get(PROVIDER_EXHAUSTIONS))
        except Exception:  # noqa: BLE001
            return 0

    def next_interval_s(self) -> float:
        """Compute the next poll interval from the recent exhaustion rate.
        First call anchors (delta 0 -> base). NEVER raises."""
        try:
            cur = self._current_exhaustions()
            with self._lock:
                if self._last_exhaustions is None:
                    self._last_exhaustions = cur
                    delta = 0.0
                else:
                    delta = max(0.0, float(cur - self._last_exhaustions))
                    self._last_exhaustions = cur
            interval = compute_adaptive_interval(
                base_interval_s(), delta, max_interval_s(),
            )
            if delta > 0:
                logger.info(
                    "[RoadmapCadence] vendor stress (exhaustion_delta=%.0f) -> "
                    "backoff poll interval %.0fs (base %.0fs)",
                    delta, interval, base_interval_s(),
                )
            return interval
        except Exception:  # noqa: BLE001
            return base_interval_s()

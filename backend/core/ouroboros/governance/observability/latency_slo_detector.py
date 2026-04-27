"""Phase 8.5 — Latency-SLO breach detector.

Per `OUROBOROS_VENOM_PRD.md` §3.6.4:

  > Bounded ledger of phase-level p95 + alert event when SLO violated.

This module ships a per-phase rolling-window p95 tracker with
operator-defined SLO thresholds. When a phase's recent p95
exceeds its SLO, emit one `LatencySLOBreachEvent`.

## Why per-phase rolling-window p95

The 11-phase governance pipeline (CLASSIFY → ROUTE → ... → COMPLETE)
has wildly different expected latencies. CLASSIFY is sub-second;
GENERATE can be 60s+. A naive global p95 would conflate these.

Per-phase tracking + per-phase SLOs means an operator can pin
"GENERATE p95 ≤ 90s, GATE p95 ≤ 5s" and get specific breach
signals.

## Default-off

`JARVIS_LATENCY_SLO_DETECTOR_ENABLED` (default false). When off,
``record()`` is a no-op + ``check_breaches()`` returns empty.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Rolling window size (per phase). 100 samples → robust p95 with
# bounded memory (~3 KiB per phase, ~33 KiB total at 11 phases).
DEFAULT_WINDOW_SIZE: int = 100

# Min samples required before a p95 calc is meaningful. Below this,
# the detector reports "insufficient_data" instead of breach.
MIN_SAMPLES_FOR_BREACH: int = 20

# Default SLO when caller doesn't supply one. Conservative — most
# phases should beat 60s.
DEFAULT_PHASE_SLO_S: float = 60.0


def is_detector_enabled() -> bool:
    """Master flag — ``JARVIS_LATENCY_SLO_DETECTOR_ENABLED``
    (default false)."""
    return os.environ.get(
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED", "",
    ).strip().lower() in _TRUTHY


def get_window_size() -> int:
    raw = os.environ.get("JARVIS_LATENCY_SLO_WINDOW_SIZE")
    if raw is None:
        return DEFAULT_WINDOW_SIZE
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_WINDOW_SIZE
    except ValueError:
        return DEFAULT_WINDOW_SIZE


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Compute pth percentile (linear interpolation). Sorted input
    expected. Returns 0.0 on empty."""
    if not sorted_values:
        return 0.0
    if pct <= 0:
        return sorted_values[0]
    if pct >= 100:
        return sorted_values[-1]
    n = len(sorted_values)
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


@dataclass(frozen=True)
class LatencySLOBreachEvent:
    """One breach observation. Frozen — emitted via SSE in
    production wiring."""

    phase: str
    p95_s: float
    slo_s: float
    sample_count: int
    ts_epoch: float

    @property
    def overshoot_s(self) -> float:
        return self.p95_s - self.slo_s

    @property
    def overshoot_pct(self) -> float:
        if self.slo_s <= 0:
            return 0.0
        return (self.p95_s - self.slo_s) / self.slo_s * 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "p95_s": self.p95_s,
            "slo_s": self.slo_s,
            "sample_count": self.sample_count,
            "overshoot_s": self.overshoot_s,
            "overshoot_pct": self.overshoot_pct,
            "ts_epoch": self.ts_epoch,
        }


class LatencySLODetector:
    """Per-phase rolling-window p95 tracker with operator-defined
    SLOs. Thread-safe."""

    def __init__(
        self,
        slos_s: Optional[Dict[str, float]] = None,
        window_size: Optional[int] = None,
    ) -> None:
        self._slos: Dict[str, float] = dict(slos_s or {})
        self._window_size = (
            window_size if window_size is not None else get_window_size()
        )
        self._samples: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window_size),
        )
        self._lock = threading.RLock()

    def set_slo(self, phase: str, slo_s: float) -> None:
        with self._lock:
            self._slos[phase] = float(slo_s)

    def get_slo(self, phase: str) -> float:
        with self._lock:
            return self._slos.get(phase, DEFAULT_PHASE_SLO_S)

    def record(
        self,
        phase: str,
        latency_s: float,
    ) -> Tuple[bool, str]:
        """Record one phase-latency observation. NEVER raises."""
        if not is_detector_enabled():
            return (False, "master_off")
        ph = (phase or "").strip()
        if not ph:
            return (False, "empty_phase")
        try:
            lat = float(latency_s)
        except (TypeError, ValueError):
            return (False, "non_numeric_latency")
        if lat < 0:
            return (False, "negative_latency")
        with self._lock:
            self._samples[ph].append(lat)
        return (True, "ok")

    def p95(self, phase: str) -> Optional[float]:
        """Return the p95 latency for one phase or None when there's
        insufficient data."""
        with self._lock:
            samples = self._samples.get(phase)
            if not samples or len(samples) < MIN_SAMPLES_FOR_BREACH:
                return None
            sorted_samples = sorted(samples)
        return _percentile(sorted_samples, 95.0)

    def check_breach(self, phase: str) -> Optional[LatencySLOBreachEvent]:
        """Return a breach event for one phase if its p95 exceeds the
        SLO. None when no breach (or insufficient data)."""
        if not is_detector_enabled():
            return None
        p95 = self.p95(phase)
        if p95 is None:
            return None
        slo = self.get_slo(phase)
        with self._lock:
            sample_count = len(self._samples.get(phase, []))
        if p95 <= slo:
            return None
        return LatencySLOBreachEvent(
            phase=phase,
            p95_s=p95,
            slo_s=slo,
            sample_count=sample_count,
            ts_epoch=time.time(),
        )

    def check_all_breaches(self) -> List[LatencySLOBreachEvent]:
        """Sweep every phase + return one event per phase in breach.
        Determinism: events sorted alpha by phase name."""
        if not is_detector_enabled():
            return []
        with self._lock:
            phases = sorted(self._samples.keys())
        out: List[LatencySLOBreachEvent] = []
        for ph in phases:
            ev = self.check_breach(ph)
            if ev is not None:
                out.append(ev)
        return out

    def stats(self) -> Dict[str, Dict[str, Any]]:
        """Return per-phase summary stats — used by /observability
        endpoints."""
        with self._lock:
            out: Dict[str, Dict[str, Any]] = {}
            for ph, samples in self._samples.items():
                if not samples:
                    continue
                sorted_samples = sorted(samples)
                out[ph] = {
                    "sample_count": len(samples),
                    "p50_s": _percentile(sorted_samples, 50.0),
                    "p95_s": _percentile(sorted_samples, 95.0),
                    "max_s": sorted_samples[-1],
                    "slo_s": self._slos.get(ph, DEFAULT_PHASE_SLO_S),
                }
        return out


_DEFAULT_DETECTOR: Optional[LatencySLODetector] = None


def get_default_detector() -> LatencySLODetector:
    global _DEFAULT_DETECTOR
    if _DEFAULT_DETECTOR is None:
        _DEFAULT_DETECTOR = LatencySLODetector()
    return _DEFAULT_DETECTOR


def reset_default_detector() -> None:
    global _DEFAULT_DETECTOR
    _DEFAULT_DETECTOR = None


__all__ = [
    "DEFAULT_PHASE_SLO_S",
    "DEFAULT_WINDOW_SIZE",
    "LatencySLOBreachEvent",
    "LatencySLODetector",
    "MIN_SAMPLES_FOR_BREACH",
    "get_default_detector",
    "get_window_size",
    "is_detector_enabled",
    "reset_default_detector",
]

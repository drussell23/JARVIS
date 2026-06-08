"""Slice 172 — DW failure-risk predictor (the predictive cortex).

Slice 170 is REACTIVE — it fails over to DW-batch once the streaming wire is CONFIRMED
degraded. This is PREDICTIVE: it forecasts P(rupture in the next N minutes) from the
clustering of recent rupture events, so the router can preemptively route standard/complex
ops to the stream-free batch lane BEFORE a rupture throws — keeping the operation inside
DW and never waking the expensive Claude fallback.

Model: a recency-weighted Poisson interval estimator. Pure-Python (only ``math`` /
``collections`` / ``threading`` — no torch/tf). Rupture events (the same stream the
surface-health ledger ingests, fed at ``_note_dw_live_transport_degraded``) land in a
bounded monotonic-timestamp ring. Each event within a lookback window contributes a weight
that decays with a half-life; the weighted rate λ = Σweights / lookback gives

    P(≥1 rupture in next horizon) = 1 - exp(-λ · horizon)   ∈ [0, 1].

New cognitive behaviour (acts on a forecast, not a confirmed failure) → master flag
default-**FALSE** per §33.1. Authority-free: forecasts + a routing hint, never gates.
"""
from __future__ import annotations

import collections
import math
import os
import threading
import time
from typing import Deque, Optional

_ENV_PREDICTIVE_ENABLED = "JARVIS_DW_PREDICTIVE_ROUTING_ENABLED"
_ENV_HORIZON_S = "JARVIS_DW_RUPTURE_HORIZON_S"
_ENV_LOOKBACK_S = "JARVIS_DW_RUPTURE_LOOKBACK_S"
_ENV_HALFLIFE_S = "JARVIS_DW_RUPTURE_HALFLIFE_S"
_ENV_RISK_THRESHOLD = "JARVIS_DW_RUPTURE_RISK_THRESHOLD"
_ENV_RING_SIZE = "JARVIS_DW_RUPTURE_RING_SIZE"

_DEFAULT_HORIZON_S = 300.0      # forecast window: next 5 minutes
_DEFAULT_LOOKBACK_S = 600.0     # consider ruptures within the last 10 minutes
_DEFAULT_HALFLIFE_S = 120.0     # a rupture's weight halves every 2 minutes
_DEFAULT_RISK_THRESHOLD = 0.7   # preempt above 70% forecast risk
_DEFAULT_RING_SIZE = 256


def _envf(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def predictive_routing_enabled() -> bool:
    """Master gate — default **FALSE** (§33.1: acts on a forecast). NEVER raises."""
    return os.environ.get(_ENV_PREDICTIVE_ENABLED, "").strip().lower() in ("1", "true", "yes", "on")


def rupture_horizon_s() -> float:
    return _envf(_ENV_HORIZON_S, _DEFAULT_HORIZON_S)


def rupture_lookback_s() -> float:
    return _envf(_ENV_LOOKBACK_S, _DEFAULT_LOOKBACK_S)


def rupture_halflife_s() -> float:
    return _envf(_ENV_HALFLIFE_S, _DEFAULT_HALFLIFE_S)


def rupture_risk_threshold() -> float:
    raw = _envf(_ENV_RISK_THRESHOLD, _DEFAULT_RISK_THRESHOLD)
    return max(0.0, min(1.0, raw))


def _ring_size() -> int:
    try:
        v = int(os.environ.get(_ENV_RING_SIZE, "").strip() or _DEFAULT_RING_SIZE)
        return v if v > 0 else _DEFAULT_RING_SIZE
    except Exception:  # noqa: BLE001
        return _DEFAULT_RING_SIZE


class DWFailurePredictor:
    """Recency-weighted Poisson estimator over a bounded ring of rupture timestamps.

    Thread-safe; record + probability are O(ring) with the ring bounded (default 256), so
    the hot-path cost is a lock-guarded append (no I/O). NEVER raises."""

    def __init__(self, *, max_ring: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._ring: Deque[float] = collections.deque(maxlen=max_ring or _ring_size())

    def record_rupture(self, now: Optional[float] = None) -> None:
        """Stamp a rupture event. Fed at the live-transport-degraded detection point.
        Lock-guarded append only. NEVER raises."""
        try:
            ts = time.monotonic() if now is None else float(now)
            with self._lock:
                self._ring.append(ts)
        except Exception:  # noqa: BLE001
            pass

    def rupture_probability(
        self,
        now: Optional[float] = None,
        *,
        horizon_s: Optional[float] = None,
        lookback_s: Optional[float] = None,
        halflife_s: Optional[float] = None,
    ) -> float:
        """P(≥1 rupture within ``horizon_s``) from the recency-weighted Poisson rate of
        recent ruptures. Returns 0.0 when no recent ruptures. NEVER raises; result ∈ [0,1]."""
        try:
            now = time.monotonic() if now is None else float(now)
            horizon = horizon_s if horizon_s is not None else rupture_horizon_s()
            lookback = lookback_s if lookback_s is not None else rupture_lookback_s()
            halflife = halflife_s if halflife_s is not None else rupture_halflife_s()
            with self._lock:
                recent = [ts for ts in self._ring if 0.0 <= (now - ts) <= lookback]
            if not recent:
                return 0.0
            weighted = 0.0
            for ts in recent:
                age = now - ts
                weighted += (0.5 ** (age / halflife)) if halflife > 0 else 1.0
            lam = weighted / lookback  # weighted episodes per second
            p = 1.0 - math.exp(-lam * horizon)
            return max(0.0, min(1.0, p))
        except Exception:  # noqa: BLE001
            return 0.0


_singleton: Optional[DWFailurePredictor] = None
_singleton_lock = threading.Lock()


def get_dw_failure_predictor() -> DWFailurePredictor:
    """Process-wide singleton (double-checked lock). NEVER raises."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = DWFailurePredictor()
    return _singleton


def render_rupture_risk(prob: float) -> str:
    """One-line render of the forecast for the Discord spine. NEVER raises."""
    try:
        pct = max(0.0, min(1.0, float(prob))) * 100.0
        bar = "🟢" if pct < 40 else ("🟡" if pct < rupture_risk_threshold() * 100 else "🔴")
        return f"{bar} DW rupture risk: {pct:.0f}% (next {int(rupture_horizon_s() // 60)}m)"
    except Exception:  # noqa: BLE001
        return "🟢 DW rupture risk: 0% (next 5m)"

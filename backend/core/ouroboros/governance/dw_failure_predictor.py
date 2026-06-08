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
import json
import math
import os
import threading
import time
from typing import Any, Deque, Optional

_ENV_PREDICTIVE_ENABLED = "JARVIS_DW_PREDICTIVE_ROUTING_ENABLED"
_ENV_HORIZON_S = "JARVIS_DW_RUPTURE_HORIZON_S"
_ENV_LOOKBACK_S = "JARVIS_DW_RUPTURE_LOOKBACK_S"
_ENV_HALFLIFE_S = "JARVIS_DW_RUPTURE_HALFLIFE_S"
_ENV_RISK_THRESHOLD = "JARVIS_DW_RUPTURE_RISK_THRESHOLD"
_ENV_RING_SIZE = "JARVIS_DW_RUPTURE_RING_SIZE"
# Slice 174 — self-calibration loop
_ENV_CALIBRATION_ENABLED = "JARVIS_DW_CALIBRATION_ENABLED"
_ENV_CALIBRATION_STEP = "JARVIS_DW_CALIBRATION_STEP"
_ENV_THRESHOLD_FLOOR = "JARVIS_DW_CALIBRATION_THRESHOLD_FLOOR"
_ENV_THRESHOLD_CEILING = "JARVIS_DW_CALIBRATION_THRESHOLD_CEILING"
_ENV_CALIBRATION_PERSIST_PATH = "JARVIS_DW_CALIBRATION_PERSIST_PATH"

_DEFAULT_HORIZON_S = 300.0      # forecast window: next 5 minutes
_DEFAULT_LOOKBACK_S = 600.0     # consider ruptures within the last 10 minutes
_DEFAULT_HALFLIFE_S = 120.0     # a rupture's weight halves every 2 minutes
_DEFAULT_RISK_THRESHOLD = 0.7   # INITIAL/baseline preempt threshold — env-seeded, self-tuned at runtime (Slice 174)
_DEFAULT_RING_SIZE = 256
_DEFAULT_CALIBRATION_STEP = 0.02   # per-FP/FN threshold nudge
_DEFAULT_THRESHOLD_FLOOR = 0.30    # never auto-tune below this
_DEFAULT_THRESHOLD_CEILING = 0.95  # never auto-tune above this
_DEFAULT_CALIBRATION_PENDING_MAX = 512


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


def _static_risk_threshold() -> float:
    """The env-seeded baseline threshold (Slice 172) — the calibrator's initial value and
    the value used when self-calibration is OFF. NEVER raises."""
    raw = _envf(_ENV_RISK_THRESHOLD, _DEFAULT_RISK_THRESHOLD)
    return max(0.0, min(1.0, raw))


def calibration_enabled() -> bool:
    """Slice 174 — master for the self-calibration loop. Default **FALSE** (§33.1 — new
    adaptive behavior; OFF is byte-identical to the Slice 172 static threshold). NEVER raises."""
    return os.environ.get(_ENV_CALIBRATION_ENABLED, "").strip().lower() in ("1", "true", "yes", "on")


def _calibration_step() -> float:
    return _envf(_ENV_CALIBRATION_STEP, _DEFAULT_CALIBRATION_STEP)


def _threshold_floor() -> float:
    return max(0.0, min(1.0, _envf(_ENV_THRESHOLD_FLOOR, _DEFAULT_THRESHOLD_FLOOR)))


def _threshold_ceiling() -> float:
    return max(0.0, min(1.0, _envf(_ENV_THRESHOLD_CEILING, _DEFAULT_THRESHOLD_CEILING)))


def _calibration_persist_path() -> str:
    explicit = os.environ.get(_ENV_CALIBRATION_PERSIST_PATH, "").strip()
    if explicit:
        return explicit
    base = os.environ.get("JARVIS_STATE_DIR", "").strip() or ".jarvis"
    return os.path.join(base, "dw_threshold_calibration.json")


def rupture_risk_threshold() -> float:
    """The LIVE preemptive threshold. Slice 174 — when self-calibration is enabled this is
    the calibrator's self-tuned, persisted value; otherwise the static env baseline (Slice
    172, byte-identical). NEVER raises."""
    if calibration_enabled():
        try:
            return get_threshold_calibrator().threshold()
        except Exception:  # noqa: BLE001 — fall back to the static baseline
            pass
    return _static_risk_threshold()


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
        self._last_pred_ts: Optional[float] = None  # Slice 174 — calibration debounce

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

    def risk_exceeds_threshold(self, now: Optional[float] = None) -> bool:
        """Slice 172/174 — is the live forecast at/above the (possibly self-calibrated)
        threshold? When calibration is ON this ALSO drives the feedback loop: evaluate due
        predictions against the rupture ring (FP/FN → tune), then record this prediction
        (debounced to ~one per horizon so correlated same-window decisions don't over-tune).
        When OFF it's the Slice 172 static comparison. NEVER raises."""
        try:
            now = time.monotonic() if now is None else float(now)
            prob = self.rupture_probability(now)
            if not calibration_enabled():
                return prob >= _static_risk_threshold()
            cal = get_threshold_calibrator()
            with self._lock:
                ring = list(self._ring)
                last = self._last_pred_ts
            cal.evaluate(now, ring, horizon=rupture_horizon_s())
            if last is None or (now - last) >= rupture_horizon_s():
                cal.record_prediction(now, prob)
                with self._lock:
                    self._last_pred_ts = now
            return prob >= cal.threshold()
        except Exception:  # noqa: BLE001
            return False


class ThresholdCalibrator:
    """Slice 174 — self-tuning rupture-risk threshold (closes Blindspot C). Evaluates the
    cortex's OWN past predictions against the actual rupture record and nudges the threshold:
    a False Positive (predicted high, stream stayed stable) RAISES it; a False Negative
    (predicted low, a rupture occurred) LOWERS it. Tracks a Brier score as the quality metric
    and persists the calibrated threshold so a restart doesn't cause amnesia. Thread-safe;
    every method NEVER raises."""

    def __init__(
        self, *, initial: Optional[float] = None, step: Optional[float] = None,
        lo: Optional[float] = None, hi: Optional[float] = None,
        persist_path: Any = "__default__", max_pending: Optional[int] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._step = step if step is not None else _calibration_step()
        self._lo = lo if lo is not None else _threshold_floor()
        self._hi = hi if hi is not None else _threshold_ceiling()
        self._persist_path = (
            _calibration_persist_path() if persist_path == "__default__" else persist_path
        )
        self._pending: Deque = collections.deque(
            maxlen=max_pending or _DEFAULT_CALIBRATION_PENDING_MAX
        )
        self._brier_sum = 0.0
        self._brier_n = 0
        self._fp = 0
        self._fn = 0
        base = initial if initial is not None else _static_risk_threshold()
        restored = self._restore()
        self._threshold = restored if restored is not None else base
        self._threshold = min(self._hi, max(self._lo, self._threshold))

    def threshold(self) -> float:
        with self._lock:
            return self._threshold

    def record_prediction(self, now: float, prob: float) -> None:
        try:
            with self._lock:
                was_high = float(prob) >= self._threshold
                self._pending.append((float(now), float(prob), was_high))
        except Exception:  # noqa: BLE001
            pass

    def evaluate(self, now: float, rupture_times, *, horizon: float) -> int:
        """Score every prediction whose window [ts, ts+horizon] has fully elapsed; outcome =
        any rupture in (ts, ts+horizon]. FP → raise, FN → lower (bounded). Returns the count
        evaluated. NEVER raises."""
        try:
            now = float(now)
            horizon = float(horizon)
            rts = [float(t) for t in rupture_times]
            evaluated = 0
            changed = False
            with self._lock:
                keep: Deque = collections.deque(maxlen=self._pending.maxlen)
                for (ts, prob, was_high) in self._pending:
                    if now - ts < horizon:
                        keep.append((ts, prob, was_high))  # window not elapsed yet
                        continue
                    outcome = any(ts < rt <= ts + horizon for rt in rts)
                    self._brier_sum += (prob - (1.0 if outcome else 0.0)) ** 2
                    self._brier_n += 1
                    evaluated += 1
                    if was_high and not outcome:          # False Positive → raise
                        self._fp += 1
                        nt = min(self._hi, self._threshold + self._step)
                        if nt != self._threshold:
                            self._threshold = nt
                            changed = True
                    elif (not was_high) and outcome:      # False Negative → lower
                        self._fn += 1
                        nt = max(self._lo, self._threshold - self._step)
                        if nt != self._threshold:
                            self._threshold = nt
                            changed = True
                self._pending = keep
            if changed:
                self._persist()
            return evaluated
        except Exception:  # noqa: BLE001
            return 0

    def snapshot(self) -> dict:
        with self._lock:
            brier = (self._brier_sum / self._brier_n) if self._brier_n else None
            return {
                "threshold": round(self._threshold, 4),
                "brier": round(brier, 4) if brier is not None else None,
                "false_positives": self._fp,
                "false_negatives": self._fn,
                "evaluated": self._brier_n,
            }

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            d = os.path.dirname(self._persist_path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            tmp = f"{self._persist_path}.tmp"
            with open(tmp, "w") as fh:
                json.dump({"threshold": self._threshold, "schema": 1}, fh)
            os.replace(tmp, self._persist_path)
        except Exception:  # noqa: BLE001
            pass

    def _restore(self) -> Optional[float]:
        if not self._persist_path:
            return None
        try:
            with open(self._persist_path) as fh:
                v = float(json.load(fh).get("threshold"))
            return v if 0.0 <= v <= 1.0 else None
        except Exception:  # noqa: BLE001
            return None


_calibrator_singleton: Optional["ThresholdCalibrator"] = None
_calibrator_lock = threading.Lock()


def get_threshold_calibrator() -> "ThresholdCalibrator":
    """Process-wide singleton (double-checked lock). NEVER raises."""
    global _calibrator_singleton
    if _calibrator_singleton is None:
        with _calibrator_lock:
            if _calibrator_singleton is None:
                _calibrator_singleton = ThresholdCalibrator()
    return _calibrator_singleton


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

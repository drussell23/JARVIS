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

# Slice 176 — multi-signal failure vectors + their predictive WEIGHTS. The risk score is a
# weighted Poisson rate Σ weight(kind)·decay(age): a quota lockdown is far more predictive of
# imminent total failure than a localized empty completion. Env-tunable per kind
# (JARVIS_DW_SIGNAL_WEIGHT_<KIND>); unknown kind → 1.0. transport=1.0 keeps the Slice-172
# transport-only ring byte-identical (unweighted == weight-1.0).
_DEFAULT_SIGNAL_WEIGHTS = {
    "transport": 1.0,   # SSE rupture / stream stall — baseline
    "economic": 2.0,    # 402/429 quota / balance — HIGH (imminent total vendor lockdown)
    "upstream": 0.4,    # empty/malformed completion, 5xx, parse — localized, low predictive
    "cancel": 0.6,      # batch param-rejection (Slice 168 class) — correctable, moderate
}


def _normalize_kind(kind: Any) -> str:
    """Canonical failure-vector key. NEVER raises; defaults to "transport"."""
    try:
        return str(kind or "transport").strip().lower() or "transport"
    except Exception:  # noqa: BLE001
        return "transport"


def _signal_weight(kind: Any) -> float:
    """Predictive weight for a failure vector (env-tunable; unknown → 1.0). NEVER raises."""
    k = _normalize_kind(kind)
    try:
        raw = os.environ.get(f"JARVIS_DW_SIGNAL_WEIGHT_{k.upper()}", "").strip()
        if raw:
            v = float(raw)
            if v >= 0:
                return v
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_SIGNAL_WEIGHTS.get(k, 1.0)


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


def _calibration_persist_path(model_id: Any = "") -> str:
    """Slice 175 — PER-MODEL calibration state file under .jarvis (or the env-overridden
    base dir). Each model learns + persists its own threshold independently. NEVER raises."""
    base = os.environ.get(_ENV_CALIBRATION_PERSIST_PATH, "").strip() \
        or os.environ.get("JARVIS_STATE_DIR", "").strip() or ".jarvis"
    key = _normalize_model(model_id) or "global"
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)
    return os.path.join(base, f"dw_threshold_calibration_{safe}.json")


def rupture_risk_threshold(model_id: Any = "") -> float:
    """The LIVE preemptive threshold for ``model_id``. Slice 174/175 — when self-calibration
    is enabled this is that model's self-tuned, persisted value; otherwise the static env
    baseline (Slice 172, byte-identical). NEVER raises."""
    if calibration_enabled():
        try:
            return get_threshold_calibrator(model_id).threshold()
        except Exception:  # noqa: BLE001 — fall back to the static baseline
            pass
    return _static_risk_threshold()


def _ring_size() -> int:
    try:
        v = int(os.environ.get(_ENV_RING_SIZE, "").strip() or _DEFAULT_RING_SIZE)
        return v if v > 0 else _DEFAULT_RING_SIZE
    except Exception:  # noqa: BLE001
        return _DEFAULT_RING_SIZE


def _normalize_model(model_id: Any) -> str:
    """Slice 175 — canonical per-model bucket key. Case-insensitive; "" is the valid
    "unknown / unattributed" bucket (kept ISOLATED from every named model). NEVER raises."""
    try:
        return str(model_id or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


class DWFailurePredictor:
    """Slice 175 — PER-MODEL recency-weighted Poisson estimator. Maintains an INDEPENDENT
    bounded rupture-timestamp ring per DW model, so a volatile model's ruptures never raise a
    stable model's forecast (Blindspot B). Thread-safe; record + probability are O(ring) with
    each ring bounded (default 256). NEVER raises."""

    def __init__(self, *, max_ring: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._max_ring = max_ring or _ring_size()
        self._rings: dict = {}            # model_key -> Deque[(ts, kind)]  (Slice 176)
        self._last_pred_ts: dict = {}     # model_key -> float (Slice 174 debounce, per model)

    def _ring_locked(self, key: str) -> Deque:
        r = self._rings.get(key)
        if r is None:
            r = collections.deque(maxlen=self._max_ring)
            self._rings[key] = r
        return r

    def record_failure(
        self, now: Optional[float] = None, model_id: Any = "", kind: Any = "transport",
    ) -> None:
        """Slice 176 — stamp a KIND-tagged failure event for ``model_id``'s ring (transport
        rupture / economic 402-429 / upstream empty-or-5xx / cancel batch-rejection). The kind
        determines its predictive weight in the fused risk score. Lock-guarded append only.
        NEVER raises."""
        try:
            ts = time.monotonic() if now is None else float(now)
            key = _normalize_model(model_id)
            k = _normalize_kind(kind)
            with self._lock:
                self._ring_locked(key).append((ts, k))
        except Exception:  # noqa: BLE001
            pass

    def record_rupture(self, now: Optional[float] = None, model_id: Any = "") -> None:
        """Slice 172 compat — a transport rupture is one failure vector. Delegates to
        record_failure(kind="transport"). NEVER raises."""
        self.record_failure(now, model_id, "transport")

    def rupture_probability(
        self,
        now: Optional[float] = None,
        *,
        model_id: Any = "",
        horizon_s: Optional[float] = None,
        lookback_s: Optional[float] = None,
        halflife_s: Optional[float] = None,
    ) -> float:
        """Slice 176 — fused failure risk for ``model_id``: P(≥1 failure within ``horizon_s``)
        from its OWN ring's recency- AND severity-weighted Poisson rate (Σ weight(kind)·decay).
        0.0 when that model has no recent failures. Transport-only rings (weight 1.0) are
        byte-identical to Slice 172. NEVER raises; result ∈ [0,1]."""
        try:
            now = time.monotonic() if now is None else float(now)
            horizon = horizon_s if horizon_s is not None else rupture_horizon_s()
            lookback = lookback_s if lookback_s is not None else rupture_lookback_s()
            halflife = halflife_s if halflife_s is not None else rupture_halflife_s()
            key = _normalize_model(model_id)
            with self._lock:
                ring = self._rings.get(key)
                recent = [(ts, kd) for (ts, kd) in ring if 0.0 <= (now - ts) <= lookback] if ring else []
            if not recent:
                return 0.0
            weighted = 0.0
            for ts, kd in recent:
                age = now - ts
                decay = (0.5 ** (age / halflife)) if halflife > 0 else 1.0
                weighted += _signal_weight(kd) * decay
            lam = weighted / lookback  # severity-weighted episodes per second
            p = 1.0 - math.exp(-lam * horizon)
            return max(0.0, min(1.0, p))
        except Exception:  # noqa: BLE001
            return 0.0

    def risk_exceeds_threshold(self, now: Optional[float] = None, model_id: Any = "") -> bool:
        """Slice 172/174/175 — is ``model_id``'s live forecast at/above ITS (possibly
        self-calibrated) threshold? When calibration is ON this drives that model's feedback
        loop (evaluate its due predictions against its ring, then record this prediction,
        debounced per model). When OFF it's the static comparison. NEVER raises."""
        try:
            now = time.monotonic() if now is None else float(now)
            key = _normalize_model(model_id)
            prob = self.rupture_probability(now, model_id=key)
            if not calibration_enabled():
                return prob >= _static_risk_threshold()
            cal = get_threshold_calibrator(key)
            with self._lock:
                # Slice 176 — the calibrator evaluates against failure OCCURRENCE times
                # (any vector counts as a positive outcome); extract timestamps from the
                # (ts, kind) ring.
                ring = [ts for (ts, _kd) in (self._rings.get(key) or ())]
                last = self._last_pred_ts.get(key)
            cal.evaluate(now, ring, horizon=rupture_horizon_s())
            if last is None or (now - last) >= rupture_horizon_s():
                cal.record_prediction(now, prob)
                with self._lock:
                    self._last_pred_ts[key] = now
            return prob >= cal.threshold()
        except Exception:  # noqa: BLE001
            return False

    def highest_risk_model(self, now: Optional[float] = None) -> tuple:
        """Slice 175 — the (model_key, probability) of the model with the highest current
        rupture forecast — the most likely to be batched. Used by the Discord spine so the
        operator sees the *riskiest model*, not a misleading global average. ("", 0.0) when
        no model has recent ruptures. NEVER raises."""
        try:
            now = time.monotonic() if now is None else float(now)
            with self._lock:
                keys = list(self._rings.keys())
            best_key, best = "", 0.0
            for k in keys:
                pr = self.rupture_probability(now, model_id=k)
                if pr > best:
                    best, best_key = pr, k
            return best_key, best
        except Exception:  # noqa: BLE001
            return "", 0.0

    def dominant_signal(
        self, now: Optional[float] = None, model_id: Any = "",
        *, lookback_s: Optional[float] = None, halflife_s: Optional[float] = None,
    ) -> str:
        """Slice 176 — the failure VECTOR contributing the most weighted mass to ``model_id``'s
        current risk (so the operator sees *what kind* of threat is driving the level, e.g.
        "economic"). "" when no recent failures. NEVER raises."""
        try:
            now = time.monotonic() if now is None else float(now)
            lookback = lookback_s if lookback_s is not None else rupture_lookback_s()
            halflife = halflife_s if halflife_s is not None else rupture_halflife_s()
            key = _normalize_model(model_id)
            with self._lock:
                recent = [(ts, kd) for (ts, kd) in (self._rings.get(key) or ())
                          if 0.0 <= (now - ts) <= lookback]
            if not recent:
                return ""
            mass: dict = {}
            for ts, kd in recent:
                decay = (0.5 ** ((now - ts) / halflife)) if halflife > 0 else 1.0
                mass[kd] = mass.get(kd, 0.0) + _signal_weight(kd) * decay
            return max(mass.items(), key=lambda kv: kv[1])[0]
        except Exception:  # noqa: BLE001
            return ""


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


_calibrators: dict = {}                  # Slice 175 — model_key -> ThresholdCalibrator
_calibrators_lock = threading.Lock()


def get_threshold_calibrator(model_id: Any = "") -> "ThresholdCalibrator":
    """Slice 175 — the per-model self-tuning calibrator (one independent learner + persist
    file per DW model; double-checked lock). NEVER raises."""
    key = _normalize_model(model_id)
    cal = _calibrators.get(key)
    if cal is None:
        with _calibrators_lock:
            cal = _calibrators.get(key)
            if cal is None:
                cal = ThresholdCalibrator(persist_path=_calibration_persist_path(key))
                _calibrators[key] = cal
    return cal


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


def render_rupture_risk(prob: float, threshold: Optional[float] = None, model_id: str = "") -> str:
    """One-line render of the forecast for the Discord spine. Slice 175 — shows the model's
    OWN threshold (and name when given), not a global average. NEVER raises."""
    try:
        pct = max(0.0, min(1.0, float(prob))) * 100.0
        thr = float(threshold) if threshold is not None else rupture_risk_threshold(model_id)
        bar = "🟢" if pct < 40 else ("🟡" if pct < thr * 100 else "🔴")
        tag = f" · {model_id}" if model_id else ""
        return (
            f"{bar} DW rupture risk: {pct:.0f}% (thr {thr * 100:.0f}%, "
            f"next {int(rupture_horizon_s() // 60)}m){tag}"
        )
    except Exception:  # noqa: BLE001
        return "🟢 DW rupture risk: 0% (next 5m)"

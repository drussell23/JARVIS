"""Sovereign Governor — thermal survival + biometric self-calibration.

24/7 residency hardening (operator authorization 2026-07-19).

**Native Thermal Governor (mandate 1 — zero psutil/SMC scraping):**
``NSProcessInfo.processInfo().thermalState()`` is the OS's OWN verdict
on Apple Silicon; the governor registers
``NSProcessInfoThermalStateDidChangeNotification`` (passive, zero-cost)
and on SERIOUS/CRITICAL executes graceful degradation:

  * Rolling Biometric Evolution disabled (no tensor arithmetic while
    the die is hot) — consumed via :func:`evolution_permitted`.
  * The PASSIVE_SENTRY evaluation stride widens
    (``sentry.chunk_stride``) — DSP load sheds by N× while the ear
    stays ajar.
  * The state broadcasts through the EXISTING attach pub/sub so the
    jarvis topology map renders ``[THERMAL DEGRADATION ACTIVE]``.

NOMINAL restores everything automatically.

**Ignition Calibration (threshold auto-tune):** during the first
24h of sovereign residency (state file timestamped at first boot) the
tuner watches cosine-score clustering: a persistent rejection cluster
just under the boundary EMA-lowers it; gate storms with zero
validations EMA-raise it. Clamped hard to [0.55, 0.90]; persisted to
``.jarvis/biometric_threshold.json`` (env override always wins).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger("Ouroboros.SovereignGovernor")

THERMAL_NOMINAL = "nominal"
THERMAL_FAIR = "fair"
THERMAL_SERIOUS = "serious"
THERMAL_CRITICAL = "critical"
_STATE_NAMES = {0: THERMAL_NOMINAL, 1: THERMAL_FAIR,
                2: THERMAL_SERIOUS, 3: THERMAL_CRITICAL}

_DEGRADED = {"active": False}


def evolution_permitted() -> bool:
    """Consumed by the Rolling-Evolution hook: tensor arithmetic is
    OFF while the die is hot. NEVER raises."""
    return not _DEGRADED["active"]


def _degraded_stride() -> int:
    try:
        return max(2, int(os.environ.get(
            "JARVIS_THERMAL_GATE_STRIDE", "4",
        )))
    except (TypeError, ValueError):
        return 4


class ThermalGovernor:
    """Injectable thermal FSM. ``thermal_source`` returns the current
    OS state int (production: NSProcessInfo); tests inject."""

    def __init__(
        self,
        *,
        sentry: Any = None,
        publish_thermal: Optional[Callable[[str], None]] = None,
        thermal_source: Optional[Callable[[], int]] = None,
    ) -> None:
        self._sentry = sentry
        self._publish = publish_thermal or (lambda _s: None)
        self._source = thermal_source or self._native_state
        self._token: Any = None
        self.state = THERMAL_NOMINAL
        self.stats: Dict[str, int] = {"degradations": 0, "restorations": 0}

    @staticmethod
    def _native_state() -> int:
        from Foundation import NSProcessInfo  # noqa: PLC0415
        return int(NSProcessInfo.processInfo().thermalState())

    def on_thermal_change(self) -> None:
        """The notification landing point (also directly callable by
        tests). Reads the OS verdict, applies/lifts degradation.
        NEVER raises."""
        try:
            raw = self._source()
            name = _STATE_NAMES.get(int(raw), THERMAL_NOMINAL)
            if name == self.state:
                return
            self.state = name
            hot = name in (THERMAL_SERIOUS, THERMAL_CRITICAL)
            if hot and not _DEGRADED["active"]:
                _DEGRADED["active"] = True
                self.stats["degradations"] += 1
                if self._sentry is not None:
                    try:
                        self._sentry.chunk_stride = _degraded_stride()
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning(
                    "[SovereignGovernor] THERMAL %s — evolution OFF, "
                    "sentry stride ×%d", name.upper(), _degraded_stride(),
                )
                self._safe_publish(name)
            elif not hot and _DEGRADED["active"]:
                _DEGRADED["active"] = False
                self.stats["restorations"] += 1
                if self._sentry is not None:
                    try:
                        self._sentry.chunk_stride = 1
                    except Exception:  # noqa: BLE001
                        pass
                logger.info(
                    "[SovereignGovernor] thermal %s — full capabilities "
                    "restored", name,
                )
                self._safe_publish(name)
            else:
                self._safe_publish(name)
        except Exception:  # noqa: BLE001
            logger.debug("[SovereignGovernor] change degraded", exc_info=True)

    def _safe_publish(self, name: str) -> None:
        try:
            self._publish(name)
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> bool:
        """Register the native notification (pyobjc). Dormant without
        it — never a poll loop. NEVER raises."""
        try:
            from Foundation import (  # noqa: PLC0415
                NSNotificationCenter,
                NSOperationQueue,
            )
            self._token = NSNotificationCenter.defaultCenter(
            ).addObserverForName_object_queue_usingBlock_(
                "NSProcessInfoThermalStateDidChangeNotification", None,
                NSOperationQueue.mainQueue(),
                lambda _n: self.on_thermal_change(),
            )
            self.on_thermal_change()          # seed from current state
            logger.info("[SovereignGovernor] thermal observer registered")
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        try:
            token = self._token
            self._token = None
            if token is not None:
                from Foundation import NSNotificationCenter  # noqa: PLC0415
                NSNotificationCenter.defaultCenter().removeObserver_(token)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Ignition Calibration — biometric threshold auto-tune
# ---------------------------------------------------------------------------


def threshold_state_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_BIOMETRIC_TUNE_FILE", ".jarvis/biometric_threshold.json",
    ))


def tuned_threshold(default: float = 0.70) -> float:
    """The effective threshold: env override wins; else the tuner's
    persisted value; else default. NEVER raises."""
    try:
        env = os.environ.get("JARVIS_TIER1_VBIA_THRESHOLD", "").strip()
        if env:
            return max(0.1, min(0.99, float(env)))
    except (TypeError, ValueError):
        pass
    try:
        p = threshold_state_path()
        if p.exists():
            return max(0.55, min(0.90, float(
                json.loads(p.read_text()).get("threshold", default),
            )))
    except Exception:  # noqa: BLE001
        pass
    return default


class ThresholdAutoTuner:
    """Score-clustering calibration, active for the first
    ``JARVIS_BIOMETRIC_CALIBRATION_H`` (24h) of sovereign residency.

      * ≥ N rejections clustered inside (thr − band, thr) →
        the boundary is cutting the OPERATOR: EMA-lower.
      * ≥ M consecutive gate windows with zero validations →
        the boundary admits noise pressure: EMA-raise.

    Hard clamp [0.55, 0.90]; slow α; persisted with the ignition
    timestamp so calibration self-expires."""

    def __init__(self, *, state_path: Optional[Path] = None) -> None:
        self._path = state_path or threshold_state_path()
        self._scores: Deque[tuple] = deque(maxlen=200)
        self._unvalidated_streak = 0
        state = self._load()
        self.threshold = state.get("threshold", tuned_threshold())
        self._ignited_at = state.get("ignited_at") or time.time()
        self.stats: Dict[str, int] = {"lowered": 0, "raised": 0}
        self._persist()

    def _load(self) -> dict:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text())
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "threshold": round(float(self.threshold), 4),
                "ignited_at": self._ignited_at,
                "schema_version": "biometric_tune.1",
            }))
        except Exception:  # noqa: BLE001
            pass

    def calibrating(self) -> bool:
        try:
            hours = max(1.0, float(os.environ.get(
                "JARVIS_BIOMETRIC_CALIBRATION_H", "24",
            )))
        except (TypeError, ValueError):
            hours = 24.0
        return (time.time() - self._ignited_at) < hours * 3600.0

    def record(self, score: float, verified: bool) -> float:
        """One verification outcome in; the (possibly tuned) threshold
        out. Outside the calibration window this is telemetry-only.
        NEVER raises."""
        try:
            self._scores.append((float(score), bool(verified)))
            if verified:
                self._unvalidated_streak = 0
            else:
                self._unvalidated_streak += 1
            if not self.calibrating():
                return self.threshold
            band = 0.08
            alpha = 0.10
            near_miss = [
                s for s, v in self._scores
                if not v and (self.threshold - band) <= s < self.threshold
            ]
            if len(near_miss) >= 5:
                target = min(near_miss)
                new = (1 - alpha) * self.threshold + alpha * target
                new = max(0.55, min(0.90, new))
                if new < self.threshold:
                    self.threshold = new
                    self.stats["lowered"] += 1
                    self._scores.clear()
                    self._persist()
                    logger.warning(
                        "[SovereignGovernor] threshold auto-tuned DOWN "
                        "→ %.3f (operator rejection cluster)", new,
                    )
            elif self._unvalidated_streak >= 25:
                new = max(0.55, min(0.90, self.threshold + 0.02))
                if new > self.threshold:
                    self.threshold = new
                    self.stats["raised"] += 1
                    self._persist()
                    logger.warning(
                        "[SovereignGovernor] threshold auto-tuned UP "
                        "→ %.3f (unvalidated gate pressure)", new,
                    )
                self._unvalidated_streak = 0
            return self.threshold
        except Exception:  # noqa: BLE001
            return self.threshold


__all__ = [
    "THERMAL_CRITICAL",
    "THERMAL_FAIR",
    "THERMAL_NOMINAL",
    "THERMAL_SERIOUS",
    "ThermalGovernor",
    "ThresholdAutoTuner",
    "evolution_permitted",
    "threshold_state_path",
    "tuned_threshold",
]

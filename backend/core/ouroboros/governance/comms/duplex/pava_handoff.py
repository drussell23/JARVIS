"""VBIA → PAVA handoff + Autonomous Ignition Reporter.

Operator authorization 2026-07-19 (reuse-not-duplicate directive).

**VBIA → PAVA (mandate 2):** the x-vector cosine (``biometric_scorer``)
is the ABSOLUTE gate. On a pass the payload hands off to the EXISTING
PAVA drift matrix — ``backend.voice.advanced_biometric_verification.
AdvancedBiometricVerifier`` (Bayesian + Mahalanobis + physics + anti-
spoof) — which probabilistically tracks background acoustic
degradation. Its confidence MODULATES the Rolling-Evolution EMA alpha:
a physics-plausible, spoof-clean sample teaches the profile faster;
a drifting/suspect one teaches slower or not at all. PAVA never
overrides VBIA's gate — it only shapes how much the profile learns.

**Autonomous Ignition Reporter (mandate 2):** the supervisor captures
its OWN boot telemetry (encoder load ms, first cosine scores,
threshold tunes, thermal state) and, on successful init, Daniel emits
a silent ``SYSTEM_BOOT_REPORT`` frame over the attach pub/sub — the
jarvis topology map renders it with no manual extraction.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger("Ouroboros.PavaHandoff")


class PavaDriftModulator:
    """Wraps the EXISTING AdvancedBiometricVerifier as the drift lane.

    ``pava_scorer(window) -> (plausibility: float, spoof_clean: bool)``
    is the injected seam; the production default composes the existing
    verifier. NEVER raises."""

    def __init__(
        self,
        *,
        pava_scorer: Optional[Callable[[Any], Awaitable[tuple]]] = None,
    ) -> None:
        self._pava = pava_scorer or self._default_pava
        self._history: List[float] = []
        self.stats: Dict[str, int] = {
            "handoffs": 0, "spoof_rejected": 0, "drift_slowed": 0,
        }

    @staticmethod
    async def _default_pava(window: Any) -> tuple:
        # EXISTING organ (operator directive): the physics + anti-spoof
        # verifier. Degrades to a neutral plausibility if unmountable
        # (VBIA already gated — PAVA only shapes learning rate).
        try:
            from backend.voice.advanced_biometric_verification import (  # noqa: E501,PLC0415
                AdvancedBiometricVerifier,
            )
            v = AdvancedBiometricVerifier(enable_adaptive_learning=False)
            res = await v.verify_speaker(
                np.asarray(window, dtype=np.float32),
            )
            plausibility = float(getattr(res, "physics_plausibility", 0.8))
            spoof_clean = bool(getattr(res, "anti_spoofing_passed", True))
            return plausibility, spoof_clean
        except Exception:  # noqa: BLE001
            return 0.8, True                   # neutral — VBIA already passed

    async def modulated_alpha(
        self, window: Any, base_alpha: float,
    ) -> float:
        """The handoff: PAVA drift → an evolution-alpha multiplier.
        Spoof-suspect → 0 (never teach); low physics plausibility →
        damped; clean + stable → full. NEVER raises."""
        try:
            self.stats["handoffs"] += 1
            plausibility, spoof_clean = await self._pava(window)
            if not spoof_clean:
                self.stats["spoof_rejected"] += 1
                return 0.0                     # anti-spoof veto on LEARNING
            self._history.append(float(plausibility))
            if len(self._history) > 100:
                self._history.pop(0)
            # Drift term: variance of recent plausibility widens →
            # acoustics unstable → slow the EMA.
            drift = float(np.std(self._history[-20:])) if len(
                self._history,
            ) >= 3 else 0.0
            factor = max(0.0, min(1.0, plausibility * (1.0 - min(1.0, drift * 3))))
            if factor < 0.9:
                self.stats["drift_slowed"] += 1
            return base_alpha * factor
        except Exception:  # noqa: BLE001
            return base_alpha


# ---------------------------------------------------------------------------
# Autonomous Ignition Reporter
# ---------------------------------------------------------------------------


class IgnitionReporter:
    """Accumulates boot telemetry; on ``finalize`` emits the silent
    SYSTEM_BOOT_REPORT over the injected publisher (the attach pub/sub
    ``publish_line`` / a thermal-style frame). NEVER raises."""

    def __init__(
        self,
        *,
        publish: Optional[Callable[[str], None]] = None,
        speak: Optional[Callable[..., Awaitable[bool]]] = None,
    ) -> None:
        self._publish = publish or (lambda _s: None)
        self._speak = speak
        self._t0 = time.monotonic()
        self.telemetry: Dict[str, Any] = {
            "encoder_load_ms": None, "first_cosines": [],
            "threshold": None, "thermal": "nominal", "sentry_capture": None,
        }

    def record_encoder_load(self, seconds: float) -> None:
        self.telemetry["encoder_load_ms"] = round(seconds * 1000.0, 1)

    def record_cosine(self, score: float) -> None:
        try:
            if len(self.telemetry["first_cosines"]) < 5:
                self.telemetry["first_cosines"].append(round(float(score), 3))
        except Exception:  # noqa: BLE001
            pass

    def record_threshold(self, thr: float) -> None:
        self.telemetry["threshold"] = round(float(thr), 3)

    def record_thermal(self, state: str) -> None:
        self.telemetry["thermal"] = str(state)

    def record_capture_state(self, state: str) -> None:
        self.telemetry["sentry_capture"] = str(state)

    def compose(self) -> str:
        t = self.telemetry
        boot_ms = round((time.monotonic() - self._t0) * 1000.0)
        enc = (
            f"{t['encoder_load_ms']}ms" if t["encoder_load_ms"] is not None
            else "deferred"
        )
        cos = (
            "/".join(str(c) for c in t["first_cosines"])
            if t["first_cosines"] else "none yet"
        )
        return (
            f"SYSTEM_BOOT_REPORT · boot {boot_ms}ms · encoder {enc} · "
            f"capture {t['sentry_capture'] or '?'} · thermal {t['thermal']} "
            f"· threshold {t['threshold'] if t['threshold'] is not None else '0.70'} "
            f"· first cosines {cos}"
        )

    async def finalize(self, *, spoken: bool = False) -> str:
        """Emit the report. ``spoken=True`` also delivers a short
        spoken digest through Daniel. NEVER raises."""
        report = self.compose()
        try:
            self._publish(report)
        except Exception:  # noqa: BLE001
            pass
        if spoken and self._speak is not None:
            try:
                digest = (
                    "Boot complete. "
                    + (
                        "Voice recognition is online."
                        if self.telemetry["encoder_load_ms"] is not None
                        else "Voice recognition is still warming up."
                    )
                )
                await self._speak(digest, "daniel")
            except Exception:  # noqa: BLE001
                pass
        return report


__all__ = ["IgnitionReporter", "PavaDriftModulator"]

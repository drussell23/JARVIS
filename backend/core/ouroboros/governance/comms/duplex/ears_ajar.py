"""Ears-Ajar Gate — the Passive Sentry's low-power acoustic pre-filter.

Operator authorization 2026-07-19, scout-validated (246µs/chunk DSP,
0.82% of one core). The gate solves the primary physical flaw of
energy-gated listeners at its ROOT — **Wake-Word Truncation**: by the
time RMS breaches the threshold, the initial plosive ("J-arvis",
"K-aren") is already in the past. No threshold-lowering, no wildcard
parsing: the gate keeps a strict rolling FIFO **pre-roll ring** of the
last ~500ms of RAW chunks and, on breach, STITCHES that pre-roll to
the front of the live stream — the recognizer and the VBIA/PAVA
biometric layer both receive the complete acoustic envelope.

Dynamic Noise-Floor Recalibration: the trigger threshold rides a slow
EMA of the room baseline (fast EMA for the signal), so an HVAC fan
engaging RAISES the floor instead of causing runaway triggers, and a
quieting room lowers it again. Both time constants env-tunable.

Pure DSP; ``collections.deque(maxlen=…)`` ring (memory-safe rotation,
zero explicit GC); no audio-capture authority — callers own the mic.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any, Deque, Dict, Optional

import numpy as np

_RATE_DEFAULT = 16000
_CHUNK_DEFAULT = 480          # 30ms @16k — the scout's proven geometry


def _env_f(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


class EarsAjarGate:
    """Feed 30ms chunks; receive ``None`` (stay cold) or a stitched
    payload (pre-roll + breach chunk) the moment speech-like energy
    crosses the adaptive gate.

    After a trigger the gate stays OPEN (``in_window``) and every
    subsequent chunk should be forwarded raw by the caller until it
    calls :meth:`close_window` — stitching is only for the FIRST
    chunk, where the truncation physics live.
    """

    def __init__(
        self,
        *,
        rate: int = _RATE_DEFAULT,
        chunk: int = _CHUNK_DEFAULT,
    ) -> None:
        self.rate = rate
        self.chunk = chunk
        preroll_ms = _env_f("JARVIS_SENTRY_PREROLL_MS", 500.0, 60.0, 2000.0)
        n_chunks = max(1, int(round((preroll_ms / 1000.0) * rate / chunk)))
        #: strict FIFO ring — deque(maxlen) rotates in O(1), no GC churn.
        self._preroll: Deque[np.ndarray] = deque(maxlen=n_chunks)
        self._noise_floor = _env_f(
            "JARVIS_SENTRY_FLOOR_SEED", 1e-4, 1e-6, 1.0,
        )
        #: slow baseline EMA — HVAC engagement takes ~seconds to absorb.
        self._floor_alpha = _env_f(
            "JARVIS_SENTRY_FLOOR_ALPHA", 0.005, 0.0001, 0.2,
        )
        self._ratio_min = _env_f("JARVIS_SENTRY_VOICE_RATIO", 0.55, 0.1, 0.95)
        self._floor_mult = _env_f("JARVIS_SENTRY_FLOOR_MULT", 3.0, 1.2, 10.0)
        self._abs_min = _env_f("JARVIS_SENTRY_ABS_MIN_RMS", 0.01, 0.0, 0.5)
        freqs = np.fft.rfftfreq(chunk, 1.0 / rate)
        self._band = (freqs >= 85.0) & (freqs <= 3000.0)
        self.in_window = False
        self.stats: Dict[str, Any] = {
            "chunks": 0, "triggers": 0, "floor": self._noise_floor,
            "floor_min": self._noise_floor, "floor_max": self._noise_floor,
        }

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    @property
    def preroll_chunks(self) -> int:
        return int(self._preroll.maxlen or 0)

    def feed(self, chunk: "np.ndarray") -> Optional["np.ndarray"]:
        """One 30ms chunk in. Returns the STITCHED payload
        (pre-roll ‖ breach chunk — the complete acoustic envelope,
        initial plosive preserved) on gate breach; ``None`` otherwise.
        While a window is open, returns ``None`` (caller forwards the
        live stream itself). NEVER raises; a malformed chunk is
        swallowed cold."""
        try:
            x = np.asarray(chunk, dtype=np.float32).reshape(-1)
            if x.size == 0:
                return None
            self.stats["chunks"] += 1
            rms = float(np.sqrt(np.mean(x * x)))
            # Dynamic floor: the SLOW room-baseline EMA. Deliberately
            # updated from every chunk INCLUDING speech (speech is
            # transient; alpha makes hours of fan dominate seconds of
            # words), preventing runaway triggers on ambience shifts.
            self._noise_floor = (
                (1.0 - self._floor_alpha) * self._noise_floor
                + self._floor_alpha * rms
            )
            self.stats["floor"] = self._noise_floor
            self.stats["floor_min"] = min(
                self.stats["floor_min"], self._noise_floor,
            )
            self.stats["floor_max"] = max(
                self.stats["floor_max"], self._noise_floor,
            )
            if self.in_window:
                # Caller streams live audio during an open window; the
                # ring keeps rolling so the NEXT trigger has pre-roll.
                self._preroll.append(x)
                return None
            threshold = max(
                self._floor_mult * self._noise_floor, self._abs_min,
            )
            breach = rms > threshold
            if breach:
                spec = np.abs(np.fft.rfft(x))
                voice_ratio = float(
                    spec[self._band].sum() / (spec.sum() + 1e-9),
                )
                breach = voice_ratio > self._ratio_min
            if not breach:
                self._preroll.append(x)
                return None
            # ---- Pre-Roll Stitching (the truncation root fix) ----
            payload = np.concatenate([*self._preroll, x]) if self._preroll \
                else x.copy()
            self._preroll.append(x)
            self.in_window = True
            self.stats["triggers"] += 1
            return payload
        except Exception:  # noqa: BLE001 — sentry DSP never crashes audio
            return None

    def close_window(self) -> None:
        """Recognition window ended (utterance finished / timeout) —
        the gate goes back to sleep with its ring warm. NEVER raises."""
        self.in_window = False


__all__ = ["EarsAjarGate"]

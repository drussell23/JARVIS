"""Adaptive Quantization Execution Matrix — hardware-aware ECAPA path router
(Slice 250.2c).

Picks the ECAPA execution PATH at call time from LIVE host signals:

    AC power  AND  NORMAL memory pressure   -> HIGH_FIDELITY (uncompressed)
    battery   OR   elevated memory pressure -> COMPRESSED (fp16/ONNX-quantized)

The decision is COMPUTED FROM THE PROBES ON EVERY CALL — there is NO cached or
hardcoded static preference. Unplug the charger mid-session and the very next
``select_path()`` flips to COMPRESSED (proven by the dynamic re-evaluation test).
This is the energy/thermal-aware analogue of the urgency router: a cheap,
deterministic, zero-LLM Tier-0 routing decision.

Structural injection (NO backend imports at module scope)
---------------------------------------------------------
Host signals are consumed by SHAPE, not by import:

  * ``PowerProbe   = Callable[[], PowerState]`` — production may pass a probe
    backed by ``psutil.sensors_battery()``; tests inject a fake.
  * ``PressureProbe = Callable[[], MemPressure]`` — structurally aligned to the
    supervisor's ``MemoryPressureGate`` (``backend/core/ouroboros/governance/
    memory_pressure_gate.py``), whose ``PressureLevel(str, Enum)`` has members
    ``OK / WARN / HIGH / CRITICAL`` and a ``.pressure() -> PressureLevel``
    method. We map: ``OK -> NORMAL``; anything-not-OK (WARN/HIGH/CRITICAL or any
    unknown level) -> ELEVATED. The default probe lazily imports the gate INSIDE
    the function and returns ``NORMAL`` on any failure (sandbox-safe).

The fp16 quantizer
------------------
``fp16_quantize_embedder`` wraps a base embedder: compute the base embedding,
round-trip it through ``np.float16`` (a faithful, dependency-free simulation of
fp16 / ONNX-quantized inference precision), then re-L2-normalize and return
float32. The fp16 round-trip is a tiny (~1e-3 relative, sub-1e-2 cosine)
perturbation, which is exactly what the parity proof bounds.

Pure numpy. No torch, no scipy.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Callable

import numpy as np


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class PowerState(str, Enum):
    AC = "ac"
    BATTERY = "battery"


class MemPressure(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"  # anything not NORMAL is treated as elevated


class ExecutionPath(str, Enum):
    HIGH_FIDELITY = "high_fidelity"
    COMPRESSED = "compressed"


# --------------------------------------------------------------------------- #
# Probe contracts (structural — never imported from the kernel)
# --------------------------------------------------------------------------- #
Embedder = Callable[[np.ndarray], np.ndarray]
PowerProbe = Callable[[], PowerState]
PressureProbe = Callable[[], MemPressure]


# --------------------------------------------------------------------------- #
# Lazy default probes — sandbox-safe, fail-soft, NO module-scope backend import.
# --------------------------------------------------------------------------- #
def default_power_probe() -> PowerState:
    """Lazily probe battery state via ``psutil.sensors_battery()``.

    AC when ``power_plugged`` is True (or when no battery exists — desktops are
    effectively always on AC). Returns ``AC`` on any failure so the router
    defaults to the high-fidelity path when host state is unknowable.
    """
    try:
        import psutil  # lazy + guarded

        batt = psutil.sensors_battery()
        if batt is None:
            return PowerState.AC  # no battery -> wall power
        return PowerState.AC if batt.power_plugged else PowerState.BATTERY
    except Exception:
        return PowerState.AC


def default_pressure_probe() -> MemPressure:
    """Lazily consult the supervisor ``MemoryPressureGate`` by SHAPE.

    Imported INSIDE the function (never at module scope) so this test module
    stays decoupled from the kernel. Maps ``PressureLevel.OK -> NORMAL`` and any
    other level -> ELEVATED. Returns ``NORMAL`` on any failure (sandbox / no
    gate / probe error) so the router never spuriously degrades to compressed.
    """
    try:
        from backend.core.ouroboros.governance.memory_pressure_gate import (  # type: ignore
            MemoryPressureGate,
        )

        gate = MemoryPressureGate()
        level = gate.pressure()
        # Structural map: OK -> NORMAL, everything else -> ELEVATED.
        name = getattr(level, "value", level)
        if str(name).lower() == "ok":
            return MemPressure.NORMAL
        return MemPressure.ELEVATED
    except Exception:
        return MemPressure.NORMAL


# --------------------------------------------------------------------------- #
# fp16 quantizer
# --------------------------------------------------------------------------- #
def fp16_quantize_embedder(base_embedder: Embedder) -> Embedder:
    """Wrap ``base_embedder`` to simulate fp16 / ONNX-quantized inference.

    Pipeline: base embedding -> round-trip through ``np.float16`` -> re-L2-
    normalize -> return ``float32``. Deterministic (no randomness); identical
    inputs yield byte-identical outputs. The fp16 round-trip introduces a small,
    bounded perturbation vs the base embedding — quantified and bounded by the
    parity proof (``test_quant_parity.py``).
    """

    def _quantized(x: np.ndarray) -> np.ndarray:
        base = np.asarray(base_embedder(x), dtype=np.float32).reshape(-1)
        # Simulate fp16 storage/inference precision via an explicit round-trip.
        fp16 = base.astype(np.float16).astype(np.float32)
        norm = float(np.linalg.norm(fp16))
        if norm > 0.0:
            fp16 = fp16 / np.float32(norm)
        return fp16.astype(np.float32, copy=False)

    return _quantized


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
class AdaptiveQuantizationRouter:
    """Hardware-aware router selecting the ECAPA execution path per call.

    The path is recomputed from the injected probes EVERY call — there is no
    cached/static preference. ``JARVIS_ECAPA_FORCE_PATH`` (values
    ``high_fidelity`` | ``compressed``) is an optional operator override read at
    call time; unset (default) means the probes decide.
    """

    _FORCE_ENV = "JARVIS_ECAPA_FORCE_PATH"

    def __init__(
        self,
        *,
        high_fidelity_embedder: Embedder,
        compressed_embedder: Embedder,
        power_probe: PowerProbe = default_power_probe,
        pressure_probe: PressureProbe = default_pressure_probe,
    ) -> None:
        self._hf = high_fidelity_embedder
        self._comp = compressed_embedder
        self._power_probe = power_probe
        self._pressure_probe = pressure_probe

    # ------------------------------------------------------------------ #
    # Decision (computed from probes EVERY call)
    # ------------------------------------------------------------------ #
    def _forced_path(self) -> ExecutionPath | None:
        raw = os.environ.get(self._FORCE_ENV)
        if not raw:
            return None
        val = raw.strip().lower()
        if val in ("high_fidelity", "hf", "uncompressed"):
            return ExecutionPath.HIGH_FIDELITY
        if val in ("compressed", "comp", "fp16"):
            return ExecutionPath.COMPRESSED
        return None  # unrecognized -> ignore, fall back to probes

    def select_path(self) -> ExecutionPath:
        forced = self._forced_path()
        if forced is not None:
            return forced
        power = self._power_probe()
        pressure = self._pressure_probe()
        if power is PowerState.AC and pressure is MemPressure.NORMAL:
            return ExecutionPath.HIGH_FIDELITY
        return ExecutionPath.COMPRESSED

    def select_embedder(self) -> Embedder:
        return self._hf if self.select_path() is ExecutionPath.HIGH_FIDELITY else self._comp

    def route_reason(self) -> str:
        """Human-readable rationale for observability (recomputed per call)."""
        forced = self._forced_path()
        if forced is not None:
            return f"forced->{forced.value}"
        power = self._power_probe()
        pressure = self._pressure_probe()
        if power is PowerState.AC and pressure is MemPressure.NORMAL:
            return "ac+normal->high_fidelity"
        if power is PowerState.BATTERY and pressure is MemPressure.NORMAL:
            return "battery->compressed"
        if power is PowerState.AC and pressure is MemPressure.ELEVATED:
            return "elevated->compressed"
        return "battery+elevated->compressed"

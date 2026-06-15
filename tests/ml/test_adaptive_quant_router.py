"""Tests for the Adaptive Quantization Execution Matrix router (Slice 250.2c).

A hardware-aware router that picks the ECAPA execution PATH (HIGH_FIDELITY vs
COMPRESSED) from live host signals (power + memory pressure) via STRUCTURAL
injection of duck-typed probes. The path is computed from the probes on EVERY
call — there is no cached/hardcoded static preference (proved by the dynamic
re-evaluation test below).

Pure numpy. No torch, no scipy. No module-scope backend import.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.ml.adaptive_quant_router import (
    AdaptiveQuantizationRouter,
    ExecutionPath,
    MemPressure,
    PowerState,
    fp16_quantize_embedder,
)


# --------------------------------------------------------------------------- #
# Fake probes (structural injection)
# --------------------------------------------------------------------------- #
def _const_power(state: PowerState):
    return lambda: state


def _const_pressure(level: MemPressure):
    return lambda: level


def _toy_embedder(x: np.ndarray) -> np.ndarray:
    """A deterministic non-trivial embedder (NOT yet L2-normalized) so the
    quantizer's re-normalization is observable. Shape derived from input."""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    # Cheap fixed-width "embedding": running stats over a few windows.
    n = max(x.size, 1)
    chunks = np.array_split(x, 8) if x.size >= 8 else [x]
    feats = [float(np.mean(c)) if c.size else 0.0 for c in chunks]
    feats += [float(np.std(c)) if c.size else 0.0 for c in chunks]
    v = np.asarray(feats, dtype=np.float32)
    # deliberately un-normalized magnitude so re-L2 in the quantizer matters
    return v * np.float32(7.0)


def _make_router(power: PowerState, pressure: MemPressure) -> AdaptiveQuantizationRouter:
    hi = _toy_embedder
    comp = fp16_quantize_embedder(_toy_embedder)
    return AdaptiveQuantizationRouter(
        high_fidelity_embedder=hi,
        compressed_embedder=comp,
        power_probe=_const_power(power),
        pressure_probe=_const_pressure(pressure),
    )


# --------------------------------------------------------------------------- #
# Routing matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "power,pressure,expected",
    [
        (PowerState.AC, MemPressure.NORMAL, ExecutionPath.HIGH_FIDELITY),
        (PowerState.BATTERY, MemPressure.NORMAL, ExecutionPath.COMPRESSED),
        (PowerState.AC, MemPressure.ELEVATED, ExecutionPath.COMPRESSED),
        (PowerState.BATTERY, MemPressure.ELEVATED, ExecutionPath.COMPRESSED),
    ],
)
def test_routing_matrix(power, pressure, expected):
    router = _make_router(power, pressure)
    assert router.select_path() is expected


@pytest.mark.parametrize(
    "power,pressure,expected_path",
    [
        (PowerState.AC, MemPressure.NORMAL, ExecutionPath.HIGH_FIDELITY),
        (PowerState.BATTERY, MemPressure.NORMAL, ExecutionPath.COMPRESSED),
        (PowerState.AC, MemPressure.ELEVATED, ExecutionPath.COMPRESSED),
        (PowerState.BATTERY, MemPressure.ELEVATED, ExecutionPath.COMPRESSED),
    ],
)
def test_select_embedder_matches_path(power, pressure, expected_path):
    hi = _toy_embedder
    comp = fp16_quantize_embedder(_toy_embedder)
    router = AdaptiveQuantizationRouter(
        high_fidelity_embedder=hi,
        compressed_embedder=comp,
        power_probe=_const_power(power),
        pressure_probe=_const_pressure(pressure),
    )
    selected = router.select_embedder()
    if expected_path is ExecutionPath.HIGH_FIDELITY:
        assert selected is hi
    else:
        assert selected is comp


def test_route_reason_strings():
    assert (
        _make_router(PowerState.AC, MemPressure.NORMAL).route_reason()
        == "ac+normal->high_fidelity"
    )
    assert "battery" in _make_router(PowerState.BATTERY, MemPressure.NORMAL).route_reason()
    assert "elevated" in _make_router(PowerState.AC, MemPressure.ELEVATED).route_reason()


# --------------------------------------------------------------------------- #
# Dynamic re-evaluation — proves not cached / not hardcoded
# --------------------------------------------------------------------------- #
def test_dynamic_re_evaluation_power_flip():
    state = {"power": PowerState.AC}
    router = AdaptiveQuantizationRouter(
        high_fidelity_embedder=_toy_embedder,
        compressed_embedder=fp16_quantize_embedder(_toy_embedder),
        power_probe=lambda: state["power"],
        pressure_probe=_const_pressure(MemPressure.NORMAL),
    )
    assert router.select_path() is ExecutionPath.HIGH_FIDELITY
    # Host unplugs the charger mid-session.
    state["power"] = PowerState.BATTERY
    assert router.select_path() is ExecutionPath.COMPRESSED
    # Back on AC.
    state["power"] = PowerState.AC
    assert router.select_path() is ExecutionPath.HIGH_FIDELITY


def test_dynamic_re_evaluation_pressure_flip():
    state = {"p": MemPressure.NORMAL}
    router = AdaptiveQuantizationRouter(
        high_fidelity_embedder=_toy_embedder,
        compressed_embedder=fp16_quantize_embedder(_toy_embedder),
        power_probe=_const_power(PowerState.AC),
        pressure_probe=lambda: state["p"],
    )
    assert router.select_path() is ExecutionPath.HIGH_FIDELITY
    state["p"] = MemPressure.ELEVATED
    assert router.select_path() is ExecutionPath.COMPRESSED


def test_determinism_stable_probes():
    router = _make_router(PowerState.AC, MemPressure.NORMAL)
    paths = {router.select_path() for _ in range(25)}
    assert paths == {ExecutionPath.HIGH_FIDELITY}
    router2 = _make_router(PowerState.BATTERY, MemPressure.NORMAL)
    paths2 = {router2.select_path() for _ in range(25)}
    assert paths2 == {ExecutionPath.COMPRESSED}


# --------------------------------------------------------------------------- #
# fp16 quantizer properties
# --------------------------------------------------------------------------- #
def test_fp16_quantize_output_dtype_float32():
    q = fp16_quantize_embedder(_toy_embedder)
    out = q(np.linspace(-1, 1, 4096).astype(np.float32))
    assert out.dtype == np.float32


def test_fp16_quantize_output_l2_normalized():
    q = fp16_quantize_embedder(_toy_embedder)
    out = q(np.linspace(-1, 1, 4096).astype(np.float32))
    assert abs(float(np.linalg.norm(out)) - 1.0) < 1e-5


def test_fp16_quantize_deterministic_byte_identical():
    q = fp16_quantize_embedder(_toy_embedder)
    x = np.sin(np.linspace(0, 50, 8000)).astype(np.float32)
    a = q(x)
    b = q(x)
    assert a.tobytes() == b.tobytes()


def test_fp16_quantize_bounded_drift_vs_base():
    base = _toy_embedder
    q = fp16_quantize_embedder(base)
    x = np.sin(np.linspace(0, 50, 8000)).astype(np.float32)
    base_emb = np.asarray(base(x), dtype=np.float64)
    base_emb = base_emb / np.linalg.norm(base_emb)
    q_emb = np.asarray(q(x), dtype=np.float64)
    cos = float(np.dot(base_emb, q_emb) / (np.linalg.norm(base_emb) * np.linalg.norm(q_emb)))
    # fp16 round-trip is a tiny perturbation: cosine very close to 1.
    assert cos >= 1.0 - 1e-2

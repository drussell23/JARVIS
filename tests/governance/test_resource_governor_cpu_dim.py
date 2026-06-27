from __future__ import annotations
import importlib, os
import backend.core.ouroboros.governance.memory_pressure_gate as mpg


def _gate_with_samples(samples):
    """Build a gate whose cpu/ctx sampler yields the given sequence."""
    it = iter(samples)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    return g


def test_ctx_switch_spike_declares_critical_at_60pct_ram(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT", "3.0")
    S = mpg.CpuCtxSample
    # seed(t=0) -> baseline(t=1, rate=1000/s) -> spike(t=2, rate=10000/s)
    g = _gate_with_samples([
        S(cpu_pct=0.0, ctx_switches=0,     ts=0.0),
        S(cpu_pct=0.0, ctx_switches=1000,  ts=1.0),
        S(cpu_pct=0.0, ctx_switches=11000, ts=2.0),
    ])
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.OK     # seed
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.OK     # baseline set
    assert g._cpu_ctx_dim()[0] == mpg.PressureLevel.CRITICAL  # spike at 60% RAM


def test_cpu_dim_off_is_inert(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    S = mpg.CpuCtxSample
    g = _gate_with_samples([S(cpu_pct=99.0, ctx_switches=10**9, ts=9.0)])
    assert g._cpu_ctx_dim() == (mpg.PressureLevel.OK, None, None)

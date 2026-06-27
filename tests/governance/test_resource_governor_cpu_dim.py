from __future__ import annotations
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


def test_pressure_off_is_byte_identical(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=55.0, total_bytes=1, available_bytes=1, source="test"))
    assert g.pressure() == mpg.PressureLevel.OK   # free% only, dims off


def test_pressure_escalates_on_ctx_spike(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    S = mpg.CpuCtxSample
    it = iter([S(0.0, 0, 0.0), S(0.0, 1000, 1.0), S(0.0, 11000, 2.0)])
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    g.pressure(); g.pressure()
    assert g.pressure() == mpg.PressureLevel.CRITICAL


def test_master_umbrella_enables_cpu_dim(monkeypatch):
    """FIX 1: JARVIS_RESOURCE_GOVERNOR_ENABLED=1 alone makes cpu_dim_enabled() true."""
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", "1")
    # _env_bool reads os.environ at call time — no module reload needed.
    assert mpg.cpu_dim_enabled() is True


def test_master_umbrella_drives_pressure_escalation(monkeypatch):
    """FIX 1: Master flag alone drives pressure() CRITICAL via ctx-spike."""
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT", "3.0")
    S = mpg.CpuCtxSample
    it = iter([S(0.0, 0, 0.0), S(0.0, 1000, 1.0), S(0.0, 11000, 2.0)])
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    g.pressure(); g.pressure()
    assert g.pressure() == mpg.PressureLevel.CRITICAL


def test_can_fanout_composes_cpu_dim(monkeypatch):
    """FIX 2: can_fanout with cpu_dim active + ctx-spike -> CRITICAL level, clamped."""
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_CTX_SPIKE_MULT", "3.0")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_CRITICAL_FANOUT_CAP", "1")
    S = mpg.CpuCtxSample
    samples = [S(0.0, 0, 0.0), S(0.0, 1000, 1.0), S(0.0, 11000, 2.0)]
    it = iter(samples)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    g._cpu_ctx_sampler = lambda: next(it)
    # Advance baseline: two calls set baseline
    g._cpu_ctx_dim()
    g._cpu_ctx_dim()
    # Now can_fanout calls _cpu_ctx_dim again -> spike -> CRITICAL
    decision = g.can_fanout(8)
    assert decision.level == mpg.PressureLevel.CRITICAL
    assert decision.n_allowed == 1   # clamped to critical cap


def test_can_fanout_off_parity(monkeypatch):
    """FIX 2 OFF: master+sub unset -> can_fanout uses free-% only (byte-identical)."""
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_CPU_DIM_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_MEMORY_PRESSURE_PROCESS_DIM_ENABLED", raising=False)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=60.0, total_bytes=1, available_bytes=1, source="test"))
    decision = g.can_fanout(8)
    assert decision.level == mpg.PressureLevel.OK
    assert decision.n_allowed == 8

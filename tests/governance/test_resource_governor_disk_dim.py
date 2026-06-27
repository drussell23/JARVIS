from __future__ import annotations
import backend.core.ouroboros.governance.memory_pressure_gate as mpg


def _gate(disk_samples, free_probe_pct=60.0):
    it = iter(disk_samples)
    g = mpg.MemoryPressureGate(probe_fn=lambda: mpg.MemoryProbe(
        free_pct=free_probe_pct, total_bytes=1, available_bytes=1, source="test"))
    g._disk_sampler = lambda: next(it)
    return g


def test_capacity_critical_below_free_pct(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=3.0, free_gb=40.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL   # 3% < 5% default


def test_capacity_gb_floor_overrides_pct(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_CRITICAL_FREE_GB", "10")
    D = mpg.DiskSample
    # 50% free but only 8GB left → CRITICAL via the absolute floor
    g = _gate([D(free_pct=50.0, free_gb=8.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL


def test_iops_spike_declares_critical(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_IO_SPIKE_MULT", "3.0")
    D = mpg.DiskSample
    # plenty of capacity (free 80%); IO rate seeds then spikes 10x
    g = _gate([
        D(free_pct=80.0, free_gb=400.0, io_bytes=0,         ts=0.0),
        D(free_pct=80.0, free_gb=400.0, io_bytes=1_000_000, ts=1.0),   # baseline 1MB/s
        D(free_pct=80.0, free_gb=400.0, io_bytes=21_000_000, ts=2.0),  # 20MB/s spike
    ])
    assert g._disk_dim()[0] == mpg.PressureLevel.OK    # seed
    assert g._disk_dim()[0] == mpg.PressureLevel.OK    # baseline
    assert g._disk_dim()[0] == mpg.PressureLevel.CRITICAL  # spike


def test_iops_none_safe_capacity_still_works(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=80.0, free_gb=400.0, io_bytes=None, ts=1.0)])
    assert g._disk_dim()[0] == mpg.PressureLevel.OK   # no IO counters → no crash


def test_disk_dim_off_is_inert(monkeypatch):
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_RESOURCE_GOVERNOR_ENABLED", raising=False)
    D = mpg.DiskSample
    g = _gate([D(free_pct=0.5, free_gb=1.0, io_bytes=10**12, ts=1.0)])
    assert g._disk_dim() == (mpg.PressureLevel.OK, None, None)


def test_pressure_and_canfanout_compose_disk(monkeypatch):
    monkeypatch.setenv("JARVIS_RESOURCE_GOVERNOR_DISK_DIM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    D = mpg.DiskSample
    g = _gate([D(free_pct=2.0, free_gb=20.0, io_bytes=None, ts=1.0)] * 4)
    assert g.pressure() == mpg.PressureLevel.CRITICAL
    dec = g.can_fanout(8)
    assert dec.level == mpg.PressureLevel.CRITICAL
    assert dec.n_allowed <= 1   # clamped at CRITICAL


def test_system_pressure_gate_alias():
    assert mpg.SystemPressureGate is mpg.MemoryPressureGate

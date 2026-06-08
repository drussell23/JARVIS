"""Slice 179 — warm-boot predictive matrix (eradicate the cold-start exhaustion).

The live soak exposed it: at container restart DW's RT stream was already rupturing on all
6 models, but the predictive cortex booted at 0% risk and Slice 170's check lapsed the
stale-but-degraded verdict — so the very first ops (incl. the sentinel model-selection
probe) exhausted on RT before any failover engaged. Warm-boot: read the PERSISTED
surface-health ledger raw at T=0; if DIRECT_STREAMING is degraded, force batch immediately
— the organism is armored from millisecond zero.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)
from backend.core.ouroboros.governance import doubleword_provider as DW


class _Ctx:
    def __init__(self, route="standard"):
        self.provider_route = route


def _seed_ledger(monkeypatch, tmp_path, *, stream, batch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_ENABLED", "true")
    p = tmp_path / "h.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(p))
    led = SurfaceHealthLedger(path=p)
    led.record(SurfaceKind.DIRECT_STREAMING, stream)
    led.record(SurfaceKind.BATCH_STORAGE, batch)


def _claude_available(monkeypatch):
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.setattr(DW, "_claude_breaker_open", lambda *a, **k: False)


def test_warm_degraded_reads_persisted_ledger(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.TRANSPORT_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert DW._dw_streaming_warm_degraded() is True


def test_warm_healthy_ledger_not_armed(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.HEALTHY)
    assert DW._dw_streaming_warm_degraded() is False


def test_cold_start_forces_batch_on_degraded_ledger(monkeypatch, tmp_path):
    # THE fix: predictor COLD (0 ruptures), Slice 170 lapsed, predictive routing OFF, Claude
    # available — but the persisted ledger shows the stream degraded → warm-boot forces batch.
    _claude_available(monkeypatch)
    monkeypatch.delenv("JARVIS_DW_PREDICTIVE_ROUTING_ENABLED", raising=False)  # 172 off
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)       # 170 lapsed
    monkeypatch.setattr(DW, "_dw_rupture_risk_high", lambda *a, **k: False)     # cold predictor
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="deepseek-v4-pro") is True


def test_warm_boot_respects_batch_health(monkeypatch, tmp_path):
    # if BOTH surfaces are degraded, warm-boot must NOT detour into a broken batch lane
    _claude_available(monkeypatch)
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)
    monkeypatch.setattr(DW, "_dw_rupture_risk_high", lambda *a, **k: False)
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.TRANSPORT_DEGRADED, batch=SurfaceVerdict.TRANSPORT_DEGRADED)
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is False  # stays RT → cascade


def test_warm_boot_kill_switch(monkeypatch, tmp_path):
    _claude_available(monkeypatch)
    monkeypatch.setenv("JARVIS_DW_WARM_BOOT_ENABLED", "0")
    monkeypatch.setattr(DW, "_slice41_ledger_force_batch", lambda: False)
    monkeypatch.setattr(DW, "_dw_rupture_risk_high", lambda *a, **k: False)
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.TRANSPORT_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert DW._slice36_should_force_batch(_Ctx("standard"), model_id="m") is False


def test_predictor_armed_by_warm_boot(monkeypatch, tmp_path):
    # _dw_rupture_risk_high returns True at T=0 (cold ring) when the ledger is degraded
    import backend.core.ouroboros.governance.dw_failure_predictor as P
    monkeypatch.setattr(P.DWFailurePredictor, "risk_exceeds_threshold", lambda *a, **k: False)
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.TRANSPORT_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert DW._dw_rupture_risk_high(model_id="m") is True


if __name__ == "__main__":
    import unittest
    unittest.main()

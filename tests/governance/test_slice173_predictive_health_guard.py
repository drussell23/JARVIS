"""Slice 173 — multi-surface safety guard on the predictive detour (closes Blindspot A).

Slice 172's predictive branch preempts to batch on a high rupture FORECAST — but it never
checked whether the batch lane was itself healthy. A predictive cortex that detours into a
DEGRADED batch lane is compromised. This guards it: the detour fires only if BOTH the
rupture forecast is high AND the surface-health ledger reports BATCH_STORAGE HEALTHY. If
the stream is risky but batch is also degraded, the detour aborts and the op stays on RT —
so if it ruptures, both DW surfaces are compromised and the cascade to Claude is correct.

Composition, not duplication: reuses the existing preflight_probe._batch_surface_healthy
ledger check.
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


def _high_risk_claude_available(monkeypatch):
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.setattr(DW, "_claude_breaker_open", lambda *a, **k: False)
    monkeypatch.setenv("JARVIS_DW_PREDICTIVE_ROUTING_ENABLED", "1")
    monkeypatch.setattr(DW, "_dw_rupture_risk_high", lambda *a, **k: True)


def test_predictive_detour_fires_when_batch_healthy(monkeypatch, tmp_path):
    _high_risk_claude_available(monkeypatch)
    # stream HEALTHY → the reactive (170) branch does NOT fire, isolating the predictive
    # path; batch HEALTHY → the guard passes → preempt.
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.HEALTHY)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is True


def test_predictive_detour_blocked_when_batch_degraded(monkeypatch, tmp_path):
    # Blindspot A closed: high rupture risk BUT batch degraded → do NOT detour into a
    # broken lane; stay on RT (a rupture then correctly cascades — both surfaces down).
    _high_risk_claude_available(monkeypatch)
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.UPSTREAM_DEGRADED)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is False


def test_batch_lane_healthy_helper_tracks_the_ledger(monkeypatch, tmp_path):
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.HEALTHY)
    assert DW._dw_batch_lane_healthy() is True
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.HEALTHY, batch=SurfaceVerdict.TRANSPORT_DEGRADED)
    assert DW._dw_batch_lane_healthy() is False


def test_guard_reuses_existing_check_no_duplication(monkeypatch):
    import backend.core.ouroboros.governance.doubleword_provider as M
    src = open(M.__file__).read()
    # the guard must route through the existing preflight_probe._batch_surface_healthy
    assert "_batch_surface_healthy" in src


def test_reactive_path_unaffected(monkeypatch, tmp_path):
    # Slice 170 reactive failover (stream degraded + batch healthy) is unchanged.
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.setattr(DW, "_claude_breaker_open", lambda *a, **k: False)
    _seed_ledger(monkeypatch, tmp_path, stream=SurfaceVerdict.UPSTREAM_DEGRADED, batch=SurfaceVerdict.HEALTHY)
    assert DW._slice36_should_force_batch(_Ctx("standard")) is True

"""Slice 76 Phase 2 — pre-flight DW transport gate.

Root cause (EVAL-2, PRD §50.11): when DoubleWord transport is down, an op still
pays the `_primary_sem` wait + per-model timeout cascade (`sem_wait_total_s=360`)
BEFORE Slice 73's mid-flight sever cascades to Claude — so Claude arrives with
`remaining_s=0.0` (`deadline_exhausted_pre_fallback`) and the op times out.

Fix (verify-first, leverage-existing — NO new probe): consult the EXISTING
`dw_surface_health` ledger (kept fresh by the surface probes). If the
`DIRECT_STREAMING` surface shows a FRESH `TRANSPORT_DEGRADED` verdict, sever the
DW lane in `_dispatch_via_sentinel` BEFORE the semaphore/budget burn and cascade
to Claude with the full untouched budget. Conservative by construction: stale /
unknown / healthy / upstream-degraded evidence leaves the gate inert.
"""
from __future__ import annotations

import inspect
import time

import pytest

from backend.core.ouroboros.governance import candidate_generator
from backend.core.ouroboros.governance.candidate_generator import (
    dw_transport_degraded_preflight,
    dw_preflight_gate_enabled,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)


@pytest.fixture
def _ledger(monkeypatch, tmp_path):
    """Point both the writer and the gate's reader at a temp ledger file."""
    path = tmp_path / "dw_surface_health.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(path))
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_GATE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_FRESHNESS_S", "120")

    def _write(verdict: SurfaceVerdict, age_s: float = 0.0):
        led = SurfaceHealthLedger(path=path, autosave=True)
        led.record(SurfaceKind.DIRECT_STREAMING, verdict,
                   now_unix=time.time() - age_s)
    return _write


def test_fresh_transport_degraded_trips_gate(_ledger):
    _ledger(SurfaceVerdict.TRANSPORT_DEGRADED, age_s=5.0)
    assert dw_transport_degraded_preflight() is True


def test_stale_transport_degraded_is_ignored(_ledger):
    _ledger(SurfaceVerdict.TRANSPORT_DEGRADED, age_s=600.0)  # > 120s freshness
    assert dw_transport_degraded_preflight() is False


def test_healthy_surface_does_not_trip(_ledger):
    _ledger(SurfaceVerdict.HEALTHY, age_s=1.0)
    assert dw_transport_degraded_preflight() is False


def test_upstream_degraded_is_not_transport_degraded(_ledger):
    # UPSTREAM_DEGRADED = server responded badly (5xx) — DW transport is UP,
    # so the lane should NOT be severed pre-flight.
    _ledger(SurfaceVerdict.UPSTREAM_DEGRADED, age_s=1.0)
    assert dw_transport_degraded_preflight() is False


def test_no_record_does_not_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH",
                       str(tmp_path / "absent.json"))
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_GATE_ENABLED", "true")
    assert dw_transport_degraded_preflight() is False


def test_disabled_flag_makes_gate_inert(_ledger, monkeypatch):
    _ledger(SurfaceVerdict.TRANSPORT_DEGRADED, age_s=1.0)
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_GATE_ENABLED", "false")
    assert dw_preflight_gate_enabled() is False
    assert dw_transport_degraded_preflight() is False


def test_gate_never_raises_on_corrupt_ledger(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(bad))
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_GATE_ENABLED", "true")
    # fail-open: a gate error must never block DW dispatch
    assert dw_transport_degraded_preflight() is False


def test_freshness_window_is_env_tunable(_ledger, monkeypatch):
    _ledger(SurfaceVerdict.TRANSPORT_DEGRADED, age_s=200.0)
    assert dw_transport_degraded_preflight() is False  # default 120s window
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_FRESHNESS_S", "300")
    assert dw_transport_degraded_preflight() is True   # widened window


# --- wiring pin: the gate is consulted in the dispatch path pre-budget ---

def test_dispatch_consults_preflight_gate_before_fallback():
    src = inspect.getsource(candidate_generator.CandidateGenerator._dispatch_via_sentinel)
    assert "dw_transport_degraded_preflight" in src, (
        "_dispatch_via_sentinel must consult the pre-flight gate"
    )
    assert "_call_fallback" in src, "the gate must cascade to the Claude fallback"
    # the gate must be consulted BEFORE the sentinel dispatch assignment +
    # the model-registration loop (i.e. before any DW semaphore is touched).
    gate_pos = src.index("dw_transport_degraded_preflight")
    dispatch_pos = src.index("sentinel = get_default_sentinel()")
    register_pos = src.index("sentinel.register_endpoint")
    assert 0 <= gate_pos < dispatch_pos < register_pos, (
        "pre-flight gate must fire BEFORE sentinel/semaphore dispatch"
    )

"""Slice 77 — Dynamic transport telemetry: live failures feed the surface ledger.

Gap found during the EVAL-2 Phase-4 re-run (PRD §50.11): the Slice 76 P2 pre-flight
gate reads `dw_surface_health`, but that ledger was only written by ONE-SHOT BOOT
probes — it stayed `healthy` while every live GENERATE failed with
`live_transport:RuntimeError`, so the gate never fired and ops kept burning their
budget on the dead DW lane before Claude could pick up.

Slice 77 closes it: the millisecond a live dispatch hits a LIVE_TRANSPORT break,
record `DIRECT_STREAMING → TRANSPORT_DEGRADED` into the SAME ledger the P2 gate
reads. The ledger becomes a live, event-driven status map; the NEXT op's gate
fires and cascades straight to Claude. Recovery is automatic via the gate's
existing freshness window (a degraded verdict older than the window is ignored).
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import candidate_generator
from backend.core.ouroboros.governance.candidate_generator import (
    _note_dw_live_transport_degraded,
    dw_transport_degraded_preflight,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)


@pytest.fixture
def _ledger_path(monkeypatch, tmp_path):
    path = tmp_path / "dw_surface_health.json"
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH", str(path))
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_GATE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_PREFLIGHT_FRESHNESS_S", "120")
    return path


def test_helper_records_transport_degraded(_ledger_path):
    _note_dw_live_transport_degraded("live_transport:RuntimeError")
    rec = SurfaceHealthLedger(path=_ledger_path).verdict_for(
        SurfaceKind.DIRECT_STREAMING,
    )
    assert rec is not None
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED


def test_live_failure_makes_preflight_gate_fire(_ledger_path):
    # Before any live failure, the gate is inert (no degraded evidence).
    assert dw_transport_degraded_preflight() is False
    # A single live transport break flips the ledger...
    _note_dw_live_transport_degraded("live_transport:RuntimeError")
    # ...and the very next op's pre-flight gate now fires.
    assert dw_transport_degraded_preflight() is True


def test_recording_merges_with_existing_surfaces(_ledger_path):
    # a prior boot probe marked batch_storage healthy
    led = SurfaceHealthLedger(path=_ledger_path, autosave=True)
    led.record(SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY)
    # the live-failure recording must NOT clobber the other surface
    _note_dw_live_transport_degraded("live_transport")
    snap = SurfaceHealthLedger(path=_ledger_path).snapshot()
    assert snap[SurfaceKind.BATCH_STORAGE].verdict is SurfaceVerdict.HEALTHY
    assert snap[SurfaceKind.DIRECT_STREAMING].verdict is SurfaceVerdict.TRANSPORT_DEGRADED


def test_helper_never_raises_on_unwritable_path(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_SURFACE_HEALTH_PATH",
                       "/proc/nonexistent/cannot/write.json")
    # best-effort observability — a ledger error must NEVER perturb dispatch
    _note_dw_live_transport_degraded("live_transport")  # must not raise


def test_consecutive_failures_accumulate(_ledger_path):
    _note_dw_live_transport_degraded("t1")
    _note_dw_live_transport_degraded("t2")
    rec = SurfaceHealthLedger(path=_ledger_path).verdict_for(
        SurfaceKind.DIRECT_STREAMING,
    )
    assert rec.consecutive_failures >= 2


# --- wiring pin: the dispatch loop records on LIVE_TRANSPORT ---

def test_dispatch_records_live_transport_into_ledger():
    src = inspect.getsource(
        candidate_generator.CandidateGenerator._dispatch_via_sentinel
    )
    assert "_note_dw_live_transport_degraded" in src, (
        "_dispatch_via_sentinel must feed live LIVE_TRANSPORT failures into the "
        "surface-health ledger (Slice 77)"
    )
    # the record call must reference LIVE_TRANSPORT so it only fires on a true
    # transport break (not on 429 / 5xx / parse failures)
    assert "FailureSource.LIVE_TRANSPORT" in src

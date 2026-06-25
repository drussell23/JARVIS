"""Tests for provider_heartbeat.py -- Gap 1 of the Sovereign Failover Mesh.

The preemptive DW heartbeat probes the DW endpoint proactively on an interval
and records the verdict into the EXISTING SurfaceHealthLedger (reuse). It must
detect DEGRADATION before total collapse: it surfaces ``is_degrading()`` once
the ``consecutive_failures`` streak crosses ``JARVIS_DW_DEGRADE_STREAK``
(default 2) -- strictly less than the quarantine outage window of 5, so the
lifecycle gets an EARLY warning.

TDD with injected fakes -- ZERO real network. The probe boundary is injectable;
the ledger is a real in-process SurfaceHealthLedger pointed at a tmp path.

Covered:
  * OFF (master gate false) -> heartbeat is inert: no probe, no record, no task,
    is_degrading() False (byte-identical legacy).
  * a HEALTHY probe records SurfaceVerdict.HEALTHY + resets the streak.
  * a failing probe records TRANSPORT_DEGRADED + bumps consecutive_failures.
  * is_degrading() fires at the degrade streak (default 2) BEFORE the outage
    window (5) -- the EARLY signal.
  * a probe exception is itself a degrade signal (fail-soft), never crashes.
  * latest_verdict() exposes the most recent record.
  * the async run loop is bounded + cancellable (no event-loop starvation).
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.governance.provider_heartbeat as ph
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """Heartbeat ON by default for the active-path tests; ledger -> tmp file."""
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_INTERVAL_S", "10")
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    monkeypatch.setenv(
        "JARVIS_DW_SURFACE_HEALTH_PATH", str(tmp_path / "surface_health.json")
    )
    ph._reset_singleton_for_tests()
    yield
    ph._reset_singleton_for_tests()


def _ledger(tmp_path=None) -> SurfaceHealthLedger:
    return SurfaceHealthLedger(autosave=False)


# ---------------------------------------------------------------------------
# OFF -> inert (byte-identical legacy)
# ---------------------------------------------------------------------------

async def test_off_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "false")
    probe_calls = []
    hb = ph.DWHeartbeat(
        probe_fn=lambda: probe_calls.append(1) or True,
        ledger=_ledger(),
    )
    # A single beat does nothing when OFF.
    await hb.beat()
    assert probe_calls == []
    assert hb.is_degrading() is False
    assert hb.latest_verdict() is None


async def test_off_run_returns_immediately(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "false")
    hb = ph.DWHeartbeat(probe_fn=lambda: True, ledger=_ledger())
    # run() exits at once -- no task left spinning.
    await asyncio.wait_for(hb.run(), timeout=1.0)


# ---------------------------------------------------------------------------
# HEALTHY probe -> records HEALTHY + resets streak
# ---------------------------------------------------------------------------

async def test_healthy_probe_records_healthy():
    led = _ledger()
    hb = ph.DWHeartbeat(probe_fn=lambda: True, ledger=led)
    await hb.beat()
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec is not None
    assert rec.verdict is SurfaceVerdict.HEALTHY
    assert rec.consecutive_failures == 0
    assert hb.is_degrading() is False


# ---------------------------------------------------------------------------
# Failing probe -> TRANSPORT_DEGRADED + streak bump
# ---------------------------------------------------------------------------

async def test_failing_probe_records_degraded():
    led = _ledger()
    hb = ph.DWHeartbeat(probe_fn=lambda: False, ledger=led)
    await hb.beat()
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec is not None
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED
    assert rec.consecutive_failures == 1


# ---------------------------------------------------------------------------
# is_degrading() fires at the degrade streak BEFORE the outage window.
# ---------------------------------------------------------------------------

async def test_is_degrading_fires_at_streak_before_outage(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    led = _ledger()
    hb = ph.DWHeartbeat(probe_fn=lambda: False, ledger=led)

    await hb.beat()  # streak 1 -> not yet degrading
    assert hb.is_degrading() is False

    await hb.beat()  # streak 2 -> DEGRADING (early signal, < outage window 5)
    assert hb.is_degrading() is True
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec.consecutive_failures == 2  # 2 < 5 (the outage window)


async def test_healthy_probe_clears_degrading():
    led = _ledger()
    flips = {"ok": False}
    hb = ph.DWHeartbeat(probe_fn=lambda: flips["ok"], ledger=led)
    await hb.beat()
    await hb.beat()
    assert hb.is_degrading() is True
    # DW recovers -> a healthy probe resets the streak -> not degrading.
    flips["ok"] = True
    await hb.beat()
    assert hb.is_degrading() is False


# ---------------------------------------------------------------------------
# A probe exception is itself a degrade signal (fail-soft).
# ---------------------------------------------------------------------------

async def test_probe_exception_is_degrade_signal():
    led = _ledger()

    def boom():
        raise RuntimeError("connection refused")

    hb = ph.DWHeartbeat(probe_fn=boom, ledger=led)
    # Must NOT raise -- fail-soft.
    await hb.beat()
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec is not None
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED
    assert rec.consecutive_failures == 1


async def test_async_probe_supported():
    led = _ledger()

    async def aprobe():
        return False

    hb = ph.DWHeartbeat(probe_fn=aprobe, ledger=led)
    await hb.beat()
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec.verdict is SurfaceVerdict.TRANSPORT_DEGRADED


# ---------------------------------------------------------------------------
# latest_verdict() exposes the most recent record.
# ---------------------------------------------------------------------------

async def test_latest_verdict_exposed():
    led = _ledger()
    hb = ph.DWHeartbeat(probe_fn=lambda: False, ledger=led)
    assert hb.latest_verdict() is None
    await hb.beat()
    v = hb.latest_verdict()
    assert v is SurfaceVerdict.TRANSPORT_DEGRADED


# ---------------------------------------------------------------------------
# run() loop is bounded + cancellable (no event-loop starvation).
# ---------------------------------------------------------------------------

async def test_run_loop_is_cancellable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_INTERVAL_S", "0.01")
    led = _ledger()
    beats = {"n": 0}

    def probe():
        beats["n"] += 1
        return True

    hb = ph.DWHeartbeat(probe_fn=probe, ledger=led)
    task = asyncio.create_task(hb.run())
    await asyncio.sleep(0.05)
    hb.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert beats["n"] >= 1  # the loop actually beat


# ---------------------------------------------------------------------------
# Singleton + default probe boundary import-clean.
# ---------------------------------------------------------------------------

def test_singleton_returns_same_instance():
    a = ph.get_dw_heartbeat()
    b = ph.get_dw_heartbeat()
    assert a is b


def test_default_probe_fn_importable_and_failsoft():
    # The default probe must never raise even with no DW reachable.
    v = ph._default_dw_probe()
    assert isinstance(v, bool)


def test_heartbeat_enabled_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "false")
    assert ph.heartbeat_enabled() is False
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    assert ph.heartbeat_enabled() is True
    # Default (unset) -> OFF (default-OFF byte-identical).
    monkeypatch.delenv("JARVIS_DW_HEARTBEAT_ENABLED", raising=False)
    assert ph.heartbeat_enabled() is False

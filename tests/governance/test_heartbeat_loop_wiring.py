"""Tests for wiring the DWHeartbeat probe loop into the live boot.

THE BUG (Hybrid soak bt-2026-06-29-053800): the Deep Inference Probe was built +
unit-tested + the heartbeat flag was ON, yet NOTHING in governed_loop_service.py
started ``get_dw_heartbeat().run()`` -- so the probe loop never beat,
``is_degrading()`` read a ledger nothing populated, and Layer 1 (the heartbeat
early-prewarm) was inert in production. Same "built but no caller" pattern the
failover tick loop itself once had (Omni-Soak #3).

These tests prove the WIRING: when failover + heartbeat are armed, GLS boot
launches the heartbeat probe loop as a peer task that ACTUALLY beats; OFF -> no
task; shutdown cancels it. ZERO real network (the probe is a fake).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
import backend.core.ouroboros.governance.provider_heartbeat as ph
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance import governed_loop_service as gls
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    ph._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_TICK_INTERVAL_S", "0.01")
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_INTERVAL_S", "0.01")
    monkeypatch.setenv("JARVIS_DW_DEGRADE_STREAK", "2")
    yield
    fl._reset_singleton_for_tests()
    ph._reset_singleton_for_tests()
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None


class _MinimalGLS:
    """Bare stand-in exposing only the failover/heartbeat wiring methods of GLS."""

    def __init__(self) -> None:
        self._failover_task: Optional[asyncio.Task] = None
        self._failover_controller = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._dw_heartbeat = None

    _start_failover_loop = gls.GovernedLoopService._start_failover_loop
    _failover_loop = gls.GovernedLoopService._failover_loop
    _stop_failover_loop = gls.GovernedLoopService._stop_failover_loop


def _inject_fake_heartbeat(monkeypatch, *, probe_ok: bool = False):
    """Point the singleton at a controllable heartbeat (fake probe + in-mem
    ledger) so the wired loop beats with ZERO real network."""
    led = SurfaceHealthLedger(autosave=False)
    hb = ph.DWHeartbeat(probe_fn=lambda: probe_ok, ledger=led)
    monkeypatch.setattr(ph, "get_dw_heartbeat", lambda: hb)
    return hb, led


@pytest.mark.asyncio
async def test_heartbeat_enabled_starts_probe_task(monkeypatch):
    _inject_fake_heartbeat(monkeypatch)
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        assert svc._heartbeat_task is not None
        assert isinstance(svc._heartbeat_task, asyncio.Task)
        assert not svc._heartbeat_task.done()
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_heartbeat_disabled_starts_no_probe_task(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_HEARTBEAT_ENABLED", "false")
    _inject_fake_heartbeat(monkeypatch)
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        assert svc._heartbeat_task is None
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_wired_heartbeat_actually_beats(monkeypatch):
    """The loop RUNS -- a failing probe records degrade verdicts into the ledger
    (proving the probe loop beats, not merely exists)."""
    hb, led = _inject_fake_heartbeat(monkeypatch, probe_ok=False)
    svc = _MinimalGLS()
    svc._start_failover_loop()
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
            if rec is not None and rec.consecutive_failures >= 2:
                break
        rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
        assert rec is not None and rec.consecutive_failures >= 2
        assert hb.is_degrading() is True  # Layer 1 signal is now LIVE
    finally:
        await svc._stop_failover_loop()


@pytest.mark.asyncio
async def test_shutdown_cancels_heartbeat(monkeypatch):
    _inject_fake_heartbeat(monkeypatch)
    svc = _MinimalGLS()
    svc._start_failover_loop()
    task = svc._heartbeat_task
    assert task is not None
    await svc._stop_failover_loop()
    assert task.done()
    assert svc._heartbeat_task is None

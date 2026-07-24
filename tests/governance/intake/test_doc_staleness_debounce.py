"""Regression spine for the doc_staleness FS-event debounce (2026-07-23).

Run #14 autopsy: this sensor produced 1049/1065 pool submissions by
reacting to the session's OWN .py writes — one full rglob+AST rescan
PER fs.changed event, upstream of every governor cap. Window-absorb
semantics (Slice 5 T2 pattern) + a completed-scan cooldown make a
thousand-event burst cost exactly ONE scan (or zero, if a fresh scan
already answers it).
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def sensor_module():
    import backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor as m
    return m


def _bare_sensor(sensor_module):
    s = sensor_module.DocStalenessSensor.__new__(sensor_module.DocStalenessSensor)
    s._fs_debounce_task = None
    s._fs_events_absorbed = 0
    s._last_scan_done_mono = 0.0
    return s


class _PyEvt:
    topic = "fs.changed.modified"
    payload = {"relative_path": "backend/x.py", "extension": ".py"}


@pytest.mark.asyncio
async def test_event_burst_costs_one_scan(sensor_module, monkeypatch):
    """1000 events inside the window → exactly ONE scan_once."""
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DOCSTALE_FS_DEBOUNCE_S", "0.1")
    monkeypatch.setenv("JARVIS_DOCSTALE_MIN_RESCAN_S", "0")
    s = _bare_sensor(sensor_module)
    calls = {"n": 0}

    async def _scan():
        calls["n"] += 1
        return []

    s.scan_once = _scan  # type: ignore[method-assign]
    for _ in range(1000):
        await s._on_fs_event(_PyEvt())
    await asyncio.sleep(0.3)                   # window closes
    assert calls["n"] == 1
    assert s._fs_events_absorbed == 999


@pytest.mark.asyncio
async def test_fresh_scan_cooldown_skips_the_burst(sensor_module, monkeypatch):
    """A scan completed moments ago answers the burst — zero new scans."""
    import time
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DOCSTALE_FS_DEBOUNCE_S", "0.05")
    monkeypatch.setenv("JARVIS_DOCSTALE_MIN_RESCAN_S", "300")
    s = _bare_sensor(sensor_module)
    s._last_scan_done_mono = time.monotonic()  # just scanned
    calls = {"n": 0}

    async def _scan():
        calls["n"] += 1
        return []

    s.scan_once = _scan  # type: ignore[method-assign]
    await s._on_fs_event(_PyEvt())
    await asyncio.sleep(0.2)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_window_zero_restores_legacy_per_event(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DOCSTALE_FS_DEBOUNCE_S", "0")
    s = _bare_sensor(sensor_module)
    calls = {"n": 0}

    async def _scan():
        calls["n"] += 1
        return []

    s.scan_once = _scan  # type: ignore[method-assign]
    for _ in range(3):
        await s._on_fs_event(_PyEvt())
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert calls["n"] == 3                     # byte-identical legacy


@pytest.mark.asyncio
async def test_second_burst_after_window_scans_again(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DOCSTALE_FS_DEBOUNCE_S", "0.05")
    monkeypatch.setenv("JARVIS_DOCSTALE_MIN_RESCAN_S", "0")
    s = _bare_sensor(sensor_module)
    calls = {"n": 0}

    async def _scan():
        calls["n"] += 1
        return []

    s.scan_once = _scan  # type: ignore[method-assign]
    await s._on_fs_event(_PyEvt())
    await asyncio.sleep(0.15)
    await s._on_fs_event(_PyEvt())
    await asyncio.sleep(0.15)
    assert calls["n"] == 2                     # fresh signal still flows

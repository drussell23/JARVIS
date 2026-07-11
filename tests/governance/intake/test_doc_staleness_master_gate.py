from __future__ import annotations

import pytest


@pytest.fixture()
def sensor_module(monkeypatch):
    import backend.core.ouroboros.governance.intake.sensors.doc_staleness_sensor as m
    return m


def test_sensor_enabled_default_true(sensor_module, monkeypatch):
    monkeypatch.delenv("JARVIS_DOC_STALENESS_ENABLED", raising=False)
    assert sensor_module.sensor_enabled() is True


def test_sensor_enabled_false_pins_off(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    assert sensor_module.sensor_enabled() is False


@pytest.mark.asyncio
async def test_fs_event_ignored_when_disabled(sensor_module, monkeypatch):
    """Master-off: the fs.changed handler returns without scanning or
    emitting — the Run #14 flood lane (1049/1065 submissions) is closed."""
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    sensor = sensor_module.DocStalenessSensor.__new__(sensor_module.DocStalenessSensor)
    called = {"scan": False}

    async def _boom(*a, **k):  # would only run if the gate leaked
        called["scan"] = True

    sensor.scan_once = _boom  # type: ignore[method-assign]

    class _Evt:
        topic = "fs.changed.modified"
        payload = {"path": "README.md"}

    await sensor._on_fs_event(_Evt())
    assert called["scan"] is False


@pytest.mark.asyncio
async def test_scan_once_short_circuits_when_disabled(sensor_module, monkeypatch):
    monkeypatch.setenv("JARVIS_DOC_STALENESS_ENABLED", "false")
    sensor = sensor_module.DocStalenessSensor.__new__(sensor_module.DocStalenessSensor)
    assert await sensor.scan_once() == []

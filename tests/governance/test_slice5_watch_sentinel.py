"""Slice 5 T4 — FSEventBridge sentinel self-verification (Run #15 L1, F1/F6)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake.fs_event_bridge import (
    FileSystemEventBridge,
)


class _Bus:
    def __init__(self):
        self.published = []

    async def publish_raw(self, topic, data, persist=False):
        self.published.append((topic, data))


class _Guard:
    """Stands in for FileWatchGuard: start() succeeds; test drives events."""
    is_healthy = True

    def __init__(self, **kw):
        self.on_event = kw.get("on_event")

    async def start(self):
        return True

    async def stop(self):
        return None

    def get_metrics(self):
        return {}


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "0.05")
    monkeypatch.setenv("JARVIS_FS_BRIDGE_READY_BUDGET_S", "1.0")
    from backend.core.ouroboros.governance.intake import fs_event_bridge as m
    monkeypatch.setattr(m, "FileWatchGuard", _Guard, raising=False)
    b = FileSystemEventBridge(project_root=tmp_path, event_bus=_Bus())
    return b


def _sentinel_event(b: FileSystemEventBridge):
    p = b._sentinel_path
    return SimpleNamespace(
        event_type=SimpleNamespace(value="modified"), path=p,
        checksum="s", timestamp=0.0, is_directory=False,
    )


class TestSentinel:
    @pytest.mark.asyncio
    async def test_sentinel_observed_publishes_ready_and_marker(self, bridge, caplog, monkeypatch):
        # Patch the guard import inside start()
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        with caplog.at_level("INFO"):
            await bridge.start()
            await bridge._on_file_event(_sentinel_event(bridge))
            await asyncio.wait_for(bridge._verify_task, timeout=2.0)
        topics = [t for t, _ in bridge._event_bus.published]
        assert "fs.watch.ready" in topics
        assert any("WATCH ACTIVE" in r.message for r in caplog.records)
        assert bridge.get_metrics()["watch_confirmed"] is True

    @pytest.mark.asyncio
    async def test_sentinel_events_not_published_downstream(self, bridge, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        await bridge.start()
        await bridge._on_file_event(_sentinel_event(bridge))
        fs_topics = [t for t, _ in bridge._event_bus.published if t.startswith("fs.changed")]
        assert fs_topics == [], "liveness probe leaked to sensors"
        await asyncio.wait_for(bridge._verify_task, timeout=2.0)

    @pytest.mark.asyncio
    async def test_budget_exhausted_warns_not_confirmed(self, bridge, caplog, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        with caplog.at_level("WARNING"):
            await bridge.start()          # never feed the sentinel event
            await asyncio.wait_for(bridge._verify_task, timeout=5.0)
        assert any("WATCH NOT CONFIRMED" in r.message for r in caplog.records)
        assert bridge.get_metrics()["watch_confirmed"] is False

    @pytest.mark.asyncio
    async def test_master_off_no_task(self, bridge, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_ENABLED", "false")
        await bridge.start()
        assert bridge._verify_task is None

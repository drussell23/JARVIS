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


class TestSentinelDirResolution:
    """The T4 review Critical: .jarvis is exclude_top_level_dirs'd (Slice
    12I) — the sentinel parent must resolve to a directory the guard
    actually schedules, regime-independently."""

    @pytest.mark.asyncio
    async def test_sentinel_avoids_excluded_and_hidden_dirs(self, tmp_path, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "0.05")
        monkeypatch.setenv("JARVIS_FS_BRIDGE_READY_BUDGET_S", "0.2")
        (tmp_path / ".jarvis").mkdir()
        (tmp_path / ".git").mkdir()
        (tmp_path / "venv").mkdir()       # exclude_top_level_dirs
        (tmp_path / "backend").mkdir()    # first sorted watchable candidate
        (tmp_path / "code").mkdir()
        b = FileSystemEventBridge(project_root=tmp_path, event_bus=_Bus())
        await b.start()
        assert b._sentinel_path.parent.name == "backend"
        assert b._sentinel_path.name == "fs_watch_sentinel.json"
        await asyncio.wait_for(b._verify_task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_sentinel_falls_back_to_root_when_no_subdir(self, tmp_path, monkeypatch):
        import backend.core.resilience.file_watch_guard as g
        monkeypatch.setattr(g, "FileWatchGuard", _Guard)
        monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "0.05")
        monkeypatch.setenv("JARVIS_FS_BRIDGE_READY_BUDGET_S", "0.2")
        b = FileSystemEventBridge(project_root=tmp_path, event_bus=_Bus())
        await b.start()
        assert b._sentinel_path.parent == b._project_root
        await asyncio.wait_for(b._verify_task, timeout=5.0)


class TestRealGuardIntegration:
    """Real FileWatchGuard end-to-end: the review Critical was invisible
    because every unit test above mocks the guard. PollingObserver is
    forced — the native FSEvents backend segfaults on macOS 26 ARM64 (see
    tests/test_ouroboros_governance/test_fs_event_bridge_integration.py)."""

    @pytest.mark.timeout(90)
    @pytest.mark.asyncio
    async def test_real_guard_confirms_watch_via_schedulable_dir(
        self, tmp_path, monkeypatch, caplog
    ):
        from watchdog.observers.polling import PollingObserver
        monkeypatch.setattr(
            "watchdog.observers.Observer", PollingObserver, raising=True
        )
        from backend.core.resilience.file_watch_guard import (
            get_global_watch_registry,
        )
        registry = get_global_watch_registry()
        registry.unregister(tmp_path)

        monkeypatch.setenv("JARVIS_FS_BRIDGE_SENTINEL_RETOUCH_S", "0.2")
        monkeypatch.setenv("JARVIS_FS_BRIDGE_READY_BUDGET_S", "20")

        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "real_module.py").write_text("x = 1\n")
        (tmp_path / ".jarvis").mkdir()

        bus = _Bus()
        b = FileSystemEventBridge(project_root=tmp_path, event_bus=bus)
        try:
            with caplog.at_level("INFO"):
                await b.start()
                # (i) chosen sentinel dir is code/, never .jarvis/
                assert b._sentinel_path.parent.name == "code"
                assert b._sentinel_path.parent.parent == b._project_root
                # (ii) WATCH ACTIVE within budget — real observer pipeline
                assert b._verify_task is not None
                await asyncio.wait_for(b._verify_task, timeout=60.0)
            assert b.get_metrics()["watch_confirmed"] is True
            assert any("WATCH ACTIVE" in r.message for r in caplog.records)
            # (iii) fs.watch.ready published on the bus
            topics = [t for t, _ in bus.published]
            assert "fs.watch.ready" in topics
            # Sentinel itself never leaked downstream
            assert not any(t.startswith("fs.changed") for t in topics)
        finally:
            try:
                await b.stop()
            except Exception:
                pass
            registry.unregister(tmp_path)

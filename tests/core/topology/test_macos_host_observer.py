"""Tests for MacOSHostObserver — passive host environment daemon."""
import asyncio
import os
import tempfile
import time

import pytest

from backend.core.topology.macos_host_observer import (
    EnvironmentChange,
    HostEvent,
    MacOSHostObserver,
    _DirectorySnapshot,
    _PollingDetector,
    _WatchTarget,
    _build_watch_targets,
)
from backend.core.topology.topology_map import CapabilityNode, TopologyMap


# ---------------------------------------------------------------------------
# _DirectorySnapshot tests
# ---------------------------------------------------------------------------


class TestDirectorySnapshot:
    def test_take_nonexistent_directory(self):
        snap = _DirectorySnapshot.take("/nonexistent/path/12345")
        assert snap.entries == {}

    def test_take_real_directory(self, tmp_path):
        (tmp_path / "file_a.txt").write_text("hello")
        (tmp_path / "file_b.txt").write_text("world")
        snap = _DirectorySnapshot.take(str(tmp_path))
        assert "file_a.txt" in snap.entries
        assert "file_b.txt" in snap.entries
        assert len(snap.entries) == 2

    def test_diff_added(self, tmp_path):
        snap1 = _DirectorySnapshot.take(str(tmp_path))
        (tmp_path / "new_file.txt").write_text("added")
        snap2 = _DirectorySnapshot.take(str(tmp_path))
        added, removed, modified = snap1.diff(snap2)
        assert "new_file.txt" in added
        assert len(removed) == 0
        assert len(modified) == 0

    def test_diff_removed(self, tmp_path):
        (tmp_path / "doomed.txt").write_text("gone")
        snap1 = _DirectorySnapshot.take(str(tmp_path))
        (tmp_path / "doomed.txt").unlink()
        snap2 = _DirectorySnapshot.take(str(tmp_path))
        added, removed, modified = snap1.diff(snap2)
        assert "doomed.txt" in removed
        assert len(added) == 0

    def test_diff_modified(self, tmp_path):
        f = tmp_path / "mutable.txt"
        f.write_text("v1")
        snap1 = _DirectorySnapshot.take(str(tmp_path))
        # Ensure different mtime
        time.sleep(0.05)
        f.write_text("v2")
        snap2 = _DirectorySnapshot.take(str(tmp_path))
        added, removed, modified = snap1.diff(snap2)
        assert "mutable.txt" in modified

    def test_diff_no_changes(self, tmp_path):
        (tmp_path / "stable.txt").write_text("ok")
        snap1 = _DirectorySnapshot.take(str(tmp_path))
        snap2 = _DirectorySnapshot.take(str(tmp_path))
        added, removed, modified = snap1.diff(snap2)
        assert len(added) == 0
        assert len(removed) == 0
        assert len(modified) == 0


# ---------------------------------------------------------------------------
# PollingDetector tests
# ---------------------------------------------------------------------------


class TestPollingDetector:
    def test_setup_filters_nonexistent(self, tmp_path):
        detector = _PollingDetector(interval=0.01)
        detector.setup([str(tmp_path), "/nonexistent/12345"])
        assert len(detector._dirs) == 1

    def test_detects_change(self, tmp_path):
        detector = _PollingDetector(interval=0.01)
        detector.setup([str(tmp_path)])
        # Create a file to change the directory mtime
        time.sleep(0.02)
        (tmp_path / "newfile.txt").write_text("detect me")
        changed = detector.wait_for_changes(timeout=0.05)
        assert str(tmp_path) in changed

    def test_no_false_positives(self, tmp_path):
        (tmp_path / "stable.txt").write_text("stable")
        detector = _PollingDetector(interval=0.01)
        detector.setup([str(tmp_path)])
        # No changes — should return empty
        changed = detector.wait_for_changes(timeout=0.05)
        assert changed == []

    def test_close(self):
        detector = _PollingDetector()
        detector.setup(["/tmp"])
        detector.close()
        assert detector._dirs == []


# ---------------------------------------------------------------------------
# WatchTarget tests
# ---------------------------------------------------------------------------


class TestBuildWatchTargets:
    def test_always_includes_applications(self):
        targets = _build_watch_targets()
        paths = [t.path for t in targets]
        assert "/Applications" in paths

    def test_always_includes_downloads(self):
        targets = _build_watch_targets()
        paths = [t.path for t in targets]
        downloads = str(os.path.expanduser("~/Downloads"))
        assert downloads in paths


# ---------------------------------------------------------------------------
# HostEvent tests
# ---------------------------------------------------------------------------


class TestHostEvent:
    def test_frozen(self):
        event = HostEvent(
            change_type=EnvironmentChange.APP_INSTALLED,
            path="/Applications/FinalCut.app",
            domain_hint="neural_mesh",
            timestamp=time.time(),
        )
        with pytest.raises(AttributeError):
            event.path = "/changed"

    def test_default_details(self):
        event = HostEvent(
            change_type=EnvironmentChange.FILE_DOWNLOADED,
            path="/tmp/file.zip",
            domain_hint="data_io",
            timestamp=0.0,
        )
        assert event.details == {}


# ---------------------------------------------------------------------------
# MacOSHostObserver tests
# ---------------------------------------------------------------------------


class TestMacOSHostObserver:
    def test_disabled_no_start(self):
        obs = MacOSHostObserver(enabled=False)
        assert obs.enabled is False
        assert obs.health()["enabled"] is False

    def test_health_snapshot(self):
        obs = MacOSHostObserver(enabled=True)
        h = obs.health()
        assert "enabled" in h
        assert "running" in h
        assert "events_emitted" in h
        assert h["events_emitted"] == 0

    def test_add_hook(self):
        obs = MacOSHostObserver(enabled=True)
        calls = []
        obs.add_hook(lambda e: calls.append(e))
        assert len(obs._on_change_hooks) == 1

    def test_topology_update_registers_node(self):
        topo = TopologyMap()
        obs = MacOSHostObserver(topology=topo, enabled=True)

        event = HostEvent(
            change_type=EnvironmentChange.APP_INSTALLED,
            path="/Applications/NewApp.app",
            domain_hint="neural_mesh",
            timestamp=time.time(),
            details={"name": "NewApp.app", "action": "added"},
        )
        obs._update_topology(event)

        # Should have registered a discovered capability
        assert any("newapp" in name for name in topo.nodes)

    def test_topology_update_increases_entropy(self):
        topo = TopologyMap()
        # Register one active and observe the domain entropy
        topo.register(CapabilityNode(
            name="existing_mesh_agent",
            domain="neural_mesh",
            repo_owner="jarvis",
            active=True,
        ))
        # One active node → coverage=100% → H=0
        assert topo.entropy_over_domain("neural_mesh") == 0.0

        obs = MacOSHostObserver(topology=topo, enabled=True)
        event = HostEvent(
            change_type=EnvironmentChange.APP_INSTALLED,
            path="/Applications/NewApp.app",
            domain_hint="neural_mesh",
            timestamp=time.time(),
            details={"name": "NewApp.app", "action": "added"},
        )
        obs._update_topology(event)

        # Now coverage < 100% because a new inactive node was added → H > 0
        entropy = topo.entropy_over_domain("neural_mesh")
        assert entropy > 0.0, f"Entropy should be >0 after adding inactive node, got {entropy}"

    def test_diff_directory_generates_events(self, tmp_path):
        target = _WatchTarget(
            path=str(tmp_path),
            add_type=EnvironmentChange.APP_INSTALLED,
            remove_type=EnvironmentChange.APP_REMOVED,
            domain_hint="neural_mesh",
        )
        obs = MacOSHostObserver(enabled=True)
        obs._watch_targets = [target]
        obs._snapshots[str(tmp_path)] = _DirectorySnapshot.take(str(tmp_path))

        # Add a file
        (tmp_path / "NewApp.app").mkdir()
        events = obs._diff_directory(str(tmp_path))
        assert len(events) == 1
        assert events[0].change_type == EnvironmentChange.APP_INSTALLED
        assert "NewApp.app" in events[0].path

    def test_emit_telemetry_without_bus(self):
        obs = MacOSHostObserver(enabled=True)
        event = HostEvent(
            change_type=EnvironmentChange.FILE_DOWNLOADED,
            path="/tmp/test.zip",
            domain_hint="data_io",
            timestamp=0.0,
        )
        # Should not raise
        obs._emit_telemetry(event)

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self, tmp_path):
        obs = MacOSHostObserver(
            enabled=True,
            detector=_PollingDetector(interval=0.01),
        )
        obs._watch_targets = [_WatchTarget(
            path=str(tmp_path),
            add_type=EnvironmentChange.FILE_DOWNLOADED,
            remove_type=EnvironmentChange.FILE_DOWNLOADED,
            domain_hint="data_io",
        )]
        await obs.start()
        assert obs.health()["running"] is True
        await obs.stop()
        assert obs._thread is None
        assert obs._task is None

    @pytest.mark.asyncio
    async def test_disabled_start_noop(self):
        obs = MacOSHostObserver(enabled=False)
        await obs.start()
        assert obs._task is None
        assert obs._thread is None

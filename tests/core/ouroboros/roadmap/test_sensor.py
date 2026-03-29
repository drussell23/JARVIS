"""
Tests for RoadmapSensor (Clock 1)
===================================

TDD suite verifying snapshot refresh, caching, change detection,
callback triggering, health reporting, and P1 opt-out behaviour.

All tests use ``tmp_path`` so they run in full isolation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from backend.core.ouroboros.roadmap.sensor import RoadmapSensor, RoadmapSensorConfig
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(tmp_path: Path, name: str = "alpha", content: str = "# Alpha\nsome text") -> Path:
    """Create a minimal spec file that crawl_specs() will pick up."""
    specs_dir = tmp_path / "docs" / "superpowers" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_file = specs_dir / f"{name}.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


# ---------------------------------------------------------------------------
# test_sensor_creates_snapshot
# ---------------------------------------------------------------------------

def test_sensor_creates_snapshot(tmp_path: Path) -> None:
    """First refresh returns a RoadmapSnapshot with at least one fragment."""
    _make_spec(tmp_path)
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))

    snapshot = sensor.refresh()

    assert isinstance(snapshot, RoadmapSnapshot)
    assert len(snapshot.fragments) >= 1
    assert snapshot.version >= 1
    assert snapshot.content_hash  # non-empty string


# ---------------------------------------------------------------------------
# test_sensor_returns_cached_when_unchanged
# ---------------------------------------------------------------------------

def test_sensor_returns_cached_when_unchanged(tmp_path: Path) -> None:
    """Second refresh with no file changes returns the exact same object (same hash + version)."""
    _make_spec(tmp_path)
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))

    first = sensor.refresh()
    second = sensor.refresh()

    assert first.content_hash == second.content_hash
    assert first.version == second.version


# ---------------------------------------------------------------------------
# test_sensor_detects_change
# ---------------------------------------------------------------------------

def test_sensor_detects_change(tmp_path: Path) -> None:
    """After a file is modified, the next refresh produces a different hash and bumped version."""
    spec_file = _make_spec(tmp_path, content="# Alpha\noriginal content")
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))

    first = sensor.refresh()

    # Modify the file — ensure mtime changes enough for any OS caching
    spec_file.write_text("# Alpha\nmodified content", encoding="utf-8")

    second = sensor.refresh()

    assert second.content_hash != first.content_hash
    assert second.version == first.version + 1


# ---------------------------------------------------------------------------
# test_sensor_calls_on_change_callback
# ---------------------------------------------------------------------------

def test_sensor_calls_on_change_callback(tmp_path: Path) -> None:
    """Callback fires on first refresh (content changed from nothing) but NOT on second (unchanged)."""
    _make_spec(tmp_path)
    calls: List[RoadmapSnapshot] = []

    def _cb(snap: RoadmapSnapshot) -> None:
        calls.append(snap)

    sensor = RoadmapSensor(
        repo_root=tmp_path,
        config=RoadmapSensorConfig(p1_enabled=False),
        on_snapshot_changed=_cb,
    )

    sensor.refresh()   # should trigger callback (hash changed from None baseline)
    sensor.refresh()   # same hash — callback must NOT fire again

    assert len(calls) == 1
    assert isinstance(calls[0], RoadmapSnapshot)


# ---------------------------------------------------------------------------
# test_sensor_health
# ---------------------------------------------------------------------------

def test_sensor_health(tmp_path: Path) -> None:
    """health() returns a dict with the required keys after at least one refresh."""
    _make_spec(tmp_path)
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))
    sensor.refresh()

    health = sensor.health()

    assert "snapshot_version" in health
    assert "fragment_count" in health
    assert "last_refresh_at" in health
    assert "content_hash" in health

    assert health["snapshot_version"] >= 1
    assert health["fragment_count"] >= 1
    assert health["last_refresh_at"] > 0
    assert health["content_hash"]


# ---------------------------------------------------------------------------
# test_p1_disabled_skips_git
# ---------------------------------------------------------------------------

def test_p1_disabled_skips_git(tmp_path: Path) -> None:
    """When p1_enabled=False no tier-1 (commit_log) fragments appear in the snapshot."""
    _make_spec(tmp_path)
    sensor = RoadmapSensor(
        repo_root=tmp_path,
        config=RoadmapSensorConfig(p1_enabled=False),
    )

    snapshot = sensor.refresh()

    tier1_fragments = [f for f in snapshot.fragments if f.tier == 1]
    assert len(tier1_fragments) == 0


# ---------------------------------------------------------------------------
# test_sensor_health_before_refresh
# ---------------------------------------------------------------------------

def test_sensor_health_before_refresh(tmp_path: Path) -> None:
    """health() works even before any refresh has been called."""
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))

    health = sensor.health()

    assert health["snapshot_version"] == 0
    assert health["fragment_count"] == 0
    assert health["last_refresh_at"] == 0.0
    assert health["content_hash"] == ""


# ---------------------------------------------------------------------------
# test_sensor_current_snapshot_property
# ---------------------------------------------------------------------------

def test_sensor_current_snapshot_property(tmp_path: Path) -> None:
    """current_snapshot property returns None before first refresh, snapshot after."""
    _make_spec(tmp_path)
    sensor = RoadmapSensor(repo_root=tmp_path, config=RoadmapSensorConfig(p1_enabled=False))

    assert sensor.current_snapshot is None

    snap = sensor.refresh()
    assert sensor.current_snapshot is snap


# ---------------------------------------------------------------------------
# test_sensor_callback_receives_correct_snapshot
# ---------------------------------------------------------------------------

def test_sensor_callback_receives_correct_snapshot(tmp_path: Path) -> None:
    """Callback receives the newly built snapshot, not the old one."""
    spec_file = _make_spec(tmp_path, content="# Alpha\nv1")
    received: List[RoadmapSnapshot] = []

    sensor = RoadmapSensor(
        repo_root=tmp_path,
        config=RoadmapSensorConfig(p1_enabled=False),
        on_snapshot_changed=lambda s: received.append(s),
    )

    sensor.refresh()
    spec_file.write_text("# Alpha\nv2", encoding="utf-8")
    sensor.refresh()

    assert len(received) == 2
    assert received[0].version != received[1].version

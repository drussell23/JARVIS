"""Tests for TestFailureSensor watcher wiring in IntakeLayerService."""
from pathlib import Path
from unittest.mock import MagicMock


def test_sensor_has_watcher_when_constructed_with_one():
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    watcher = MagicMock()
    watcher.poll_interval_s = 300
    sensor = TestFailureSensor(repo="jarvis", router=MagicMock(), test_watcher=watcher)
    assert sensor._watcher is not None
    assert sensor._watcher is watcher


def test_sensor_without_watcher_has_none():
    from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
        TestFailureSensor,
    )
    sensor = TestFailureSensor(repo="jarvis", router=MagicMock())
    assert sensor._watcher is None


def test_test_watcher_exists_and_accepts_repo_path():
    from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
    watcher = TestWatcher(
        repo="jarvis",
        repo_path="/tmp/fake-repo",
        poll_interval_s=300.0,
    )
    assert watcher.poll_interval_s == 300.0

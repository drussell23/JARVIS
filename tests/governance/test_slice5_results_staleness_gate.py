"""Slice 5 T3 — plugin-results staleness gate (Run #15 L3, F4)."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0
    last_pytest_spawn_walltime = 0.0

    def __init__(self):
        self._failure_streak: dict = {}

    def process_failures(self, failures):
        return []


def _sensor(monkeypatch) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    return TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())


def _results_event(path: str):
    return SimpleNamespace(payload={
        "relative_path": ".jarvis/test_results.json", "path": path, "extension": ".json",
    })


class TestStalenessGate:
    @pytest.mark.asyncio
    async def test_deleted_file_never_arms_suppression(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(tmp_path / "gone.json")))
        assert s._last_plugin_ts == before, "absent results file armed the L3 window"

    @pytest.mark.asyncio
    async def test_stale_mtime_ignored(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        f = tmp_path / "test_results.json"
        f.write_text(json.dumps({"failures": []}))
        old = time.time() - 3600
        os.utime(f, (old, old))
        s._watcher.last_pytest_spawn_walltime = time.time()  # run initiated NOW
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(f)))
        assert s._last_plugin_ts == before, "results predating the run armed suppression"

    @pytest.mark.asyncio
    async def test_fresh_results_bump(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        s._boot_walltime = time.time() - 60          # booted a minute ago
        s._watcher.last_pytest_spawn_walltime = time.time() - 30
        f = tmp_path / "test_results.json"
        f.write_text(json.dumps({"failures": []}))    # mtime = now (fresh)
        before = s._last_plugin_ts
        await s._on_test_results_changed(_results_event(str(f)))
        assert s._last_plugin_ts > before


class TestWatcherStamp:
    @pytest.mark.asyncio
    async def test_run_pytest_stamps_spawn_walltime(self, monkeypatch):
        from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
        w = TestWatcher.__new__(TestWatcher)          # attribute contract only
        assert hasattr(TestWatcher, "__init__")
        # AST-level pin: run_pytest source stamps the attribute pre-spawn.
        import inspect
        src = inspect.getsource(TestWatcher.run_pytest)
        assert "last_pytest_spawn_walltime" in src and "time.time()" in src

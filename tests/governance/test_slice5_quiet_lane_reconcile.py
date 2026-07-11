"""Slice 5 T5 — quiet-lane reconcile on the derate wake (Run #15 L4, F5/F6)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0
    last_pytest_spawn_walltime = 0.0

    def __init__(self):
        self.poll_calls: List = []
        self._failure_streak: dict = {}

    async def poll_once(self, target_paths=None):
        self.poll_calls.append(tuple(target_paths) if target_paths else None)
        return []


def _sensor(monkeypatch) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    s = TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())
    s._running = True
    return s


class TestQuietLaneReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_scopes_git_dirty_paths(self, monkeypatch):
        s = _sensor(monkeypatch)

        async def _dirty():
            return ["backend/leaf.py"]

        async def _resolve_union(paths):
            assert paths == ["backend/leaf.py"]
            return ["tests/test_leaf.py"]

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        monkeypatch.setattr(s, "_resolve_union", _resolve_union)
        ran: List = []

        async def _scoped(targets):
            ran.append(list(targets))

        monkeypatch.setattr(s, "_run_scoped_with_confirmation", _scoped)
        await s._reconcile_quiet_lane()
        assert ran == [["tests/test_leaf.py"]]

    @pytest.mark.asyncio
    async def test_clean_tree_runs_nothing(self, monkeypatch):
        s = _sensor(monkeypatch)

        async def _dirty():
            return []

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        await s._reconcile_quiet_lane()
        assert s._watcher.poll_calls == []      # NEVER whole-suite (T3 invariant)

    @pytest.mark.asyncio
    async def test_derate_wake_triggers_only_on_zero_events(self, monkeypatch):
        s = _sensor(monkeypatch)
        monkeypatch.setenv("JARVIS_TEST_FAILURE_FALLBACK_INTERVAL_S", "0.05")
        import backend.core.ouroboros.governance.intake.sensors.test_failure_sensor as m
        monkeypatch.setattr(m, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 0.05)
        monkeypatch.setattr(s, "_event_primary_derate", lambda: True)
        calls: List = []

        async def _rec():
            calls.append(1)
            s._running = False              # stop the loop after first fire

        monkeypatch.setattr(s, "_reconcile_quiet_lane", _rec)
        await asyncio.wait_for(s._poll_loop(), timeout=5.0)
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_derate_wake_skips_when_events_flowed(self, monkeypatch):
        s = _sensor(monkeypatch)
        import backend.core.ouroboros.governance.intake.sensors.test_failure_sensor as m
        monkeypatch.setattr(m, "_TEST_FAILURE_FALLBACK_INTERVAL_S", 0.05)
        monkeypatch.setattr(s, "_event_primary_derate", lambda: True)
        called: List = []

        async def _rec():
            called.append(1)

        monkeypatch.setattr(s, "_reconcile_quiet_lane", _rec)

        async def _stop_soon():
            await asyncio.sleep(0.02)
            s._fs_events_handled += 1        # lane is alive
            await asyncio.sleep(0.06)
            s._running = False

        await asyncio.wait_for(
            asyncio.gather(s._poll_loop(), _stop_soon()), timeout=5.0,
        )
        assert called == [], "reconcile fired despite live event lane"


class TestGitDirtyPyPaths:
    """Self-review edges: porcelain parsing + kill/reap on timeout."""

    @pytest.mark.asyncio
    async def test_parses_modified_and_rename_lines(self, monkeypatch, tmp_path):
        s = _sensor(monkeypatch)
        porcelain = (
            " M pkg/mod.py\n"
            "R  pkg/old_name.py -> pkg/new_name.py\n"
            " M pkg/notes.txt\n"
        )

        class _FakeProc:
            async def communicate(self):
                return porcelain.encode(), b""

            def kill(self):  # pragma: no cover — not exercised here
                raise AssertionError("kill() should not be called on success")

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        dirty = await s._git_dirty_py_paths()
        assert dirty == ["pkg/mod.py", "pkg/new_name.py"]

    @pytest.mark.asyncio
    async def test_timeout_kills_and_reaps_no_zombie(self, monkeypatch):
        s = _sensor(monkeypatch)
        events: List = []

        class _FakeProc:
            async def communicate(self):
                events.append("communicate")
                await asyncio.sleep(10)  # never resolves within the 10s bound
                return b"", b""

            def kill(self):
                events.append("kill")

            async def wait(self):
                events.append("wait")
                return 0

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
        dirty = await s._git_dirty_py_paths()
        assert dirty == []
        assert events == ["kill", "wait"]

    @pytest.mark.asyncio
    async def test_subprocess_exec_failure_never_raises(self, monkeypatch):
        s = _sensor(monkeypatch)

        async def _boom(*args, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
        dirty = await s._git_dirty_py_paths()
        assert dirty == []

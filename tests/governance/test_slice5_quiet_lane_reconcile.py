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
    async def test_reconcile_log_names_dirty_paths(self, monkeypatch, caplog):
        """Review fix: the quiet-lane reconcile INFO line must name the
        dirty paths (not just a count) so reconcile-lane detections are
        visible to the driver's evidence predicate."""
        s = _sensor(monkeypatch)

        async def _dirty():
            return ["backend/leaf.py"]

        async def _resolve_union(paths):
            return ["tests/test_leaf.py"]

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        monkeypatch.setattr(s, "_resolve_union", _resolve_union)

        async def _scoped(targets):
            return None

        monkeypatch.setattr(s, "_run_scoped_with_confirmation", _scoped)
        with caplog.at_level("INFO"):
            await s._reconcile_quiet_lane()
        matches = [
            r.message for r in caplog.records
            if "quiet-lane reconcile" in r.message
        ]
        assert matches, "no quiet-lane reconcile log line emitted"
        assert any("backend/leaf.py" in m for m in matches), (
            "reconcile log line does not name the dirty path(s)"
        )

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
    async def test_parses_quoted_paths_with_spaces(self, monkeypatch):
        """Review fix: git quotes porcelain paths containing spaces/special
        chars — the surrounding double-quotes must be stripped before the
        .endswith(".py") check or such files are silently skipped."""
        s = _sensor(monkeypatch)
        porcelain = ' M "pkg/a b.py"\n M pkg/plain.py\n'

        class _FakeProc:
            async def communicate(self):
                return porcelain.encode(), b""

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        dirty = await s._git_dirty_py_paths()
        assert dirty == ["pkg/a b.py", "pkg/plain.py"]

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

    @pytest.mark.asyncio
    async def test_git_spawn_anchors_at_repo_root_not_repo_label(
        self, monkeypatch
    ):
        """T5 review Critical: ``repo`` is a LABEL ("jarvis"/"prime") in
        production wiring (intake_layer_service), not a filesystem path —
        git must anchor at the authoritative ``_repo_root()``."""
        s = _sensor(monkeypatch)   # repo="." is the label here
        seen_cwd: List = []

        class _FakeProc:
            async def communicate(self):
                return b"", b""

        async def _fake_exec(*args, **kwargs):
            seen_cwd.append(kwargs.get("cwd"))
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await s._git_dirty_py_paths()
        assert seen_cwd == [str(s._repo_root())]
        assert seen_cwd != [s._repo], (
            "git anchored at the bare repo label — reconcile would "
            "silently no-op in production"
        )


class TestPollOnceSerialization:
    """T5 review Important: the reconcile (_poll_task) and the debounce
    run (_debounce_task) both reach watcher.poll_once, and
    _run_scoped_with_confirmation reads _failure_streak around a
    subprocess await — interleaving corrupts stability-gate accounting.
    Every sensor-side poll_once must serialize through _poll_once_lock."""

    @pytest.mark.asyncio
    async def test_concurrent_trigger_paths_serialize(self, monkeypatch):
        s = _sensor(monkeypatch)
        in_flight = {"now": 0, "max": 0}

        async def _slow_poll(target_paths=None):
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0.05)
            in_flight["now"] -= 1
            return []

        monkeypatch.setattr(s._watcher, "poll_once", _slow_poll)

        async def _dirty():
            return ["backend/leaf.py"]

        async def _resolve_union(paths):
            return ["tests/test_leaf.py"]

        monkeypatch.setattr(s, "_git_dirty_py_paths", _dirty)
        monkeypatch.setattr(s, "_resolve_union", _resolve_union)
        # Debounce lane exercises the legacy whole-suite poll_once branch
        # (scoping off); the reconcile lane goes scoped through
        # _run_scoped_with_confirmation. Fired concurrently.
        monkeypatch.setenv("JARVIS_TEST_DYNAMIC_SCOPING_ENABLED", "false")
        monkeypatch.setenv("JARVIS_TEST_FAILURE_DEBOUNCE_WINDOW_S", "0.0")
        s._pending_changed_paths.add("backend/other.py")

        await asyncio.wait_for(
            asyncio.gather(
                s._reconcile_quiet_lane(),
                s._debounced_pytest_run(),
            ),
            timeout=5.0,
        )
        assert in_flight["max"] == 1, (
            "poll_once interleaved across trigger paths — stability-gate "
            "accounting can corrupt"
        )
        assert in_flight["now"] == 0

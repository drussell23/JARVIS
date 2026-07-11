# tests/governance/test_slice5_coalescing_debounce.py
"""Slice 5 T2 — set-accumulator debounce, no eviction (Run #15 L2, F2)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)


class _StubWatcher:
    poll_interval_s = 30.0

    def __init__(self):
        self.poll_calls: List[Any] = []
        self._failure_streak: dict = {}
        self.last_pytest_spawn_walltime = 0.0

    async def poll_once(self, target_paths=None):
        self.poll_calls.append(tuple(target_paths) if target_paths else None)
        return []


def _sensor(monkeypatch, resolved_map) -> TestFailureSensor:
    monkeypatch.setenv("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TEST_FAILURE_DEBOUNCE_WINDOW_S", "0.05")
    s = TestFailureSensor(repo=".", router=SimpleNamespace(), test_watcher=_StubWatcher())
    async def _resolve(rel: str) -> Optional[list]:
        return resolved_map.get(rel)
    monkeypatch.setattr(s, "_resolve_scoped_targets", _resolve)
    s._last_plugin_ts = 0.0  # suppression window disarmed
    s._running = True
    return s


def _event(rel: str):
    return SimpleNamespace(payload={"relative_path": rel, "extension": ".py", "path": rel})


class TestNoEviction:
    @pytest.mark.asyncio
    async def test_burst_does_not_evict_first_path(self, monkeypatch):
        s = _sensor(monkeypatch, {
            "backend/leaf.py": ["tests/test_leaf.py"],
            "wt/a.py": None, "wt/b.py": None,
        })
        await s._on_fs_event(_event("backend/leaf.py"))
        await s._on_fs_event(_event("wt/a.py"))    # must NOT cancel/evict
        await s._on_fs_event(_event("wt/b.py"))
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert any(c and "tests/test_leaf.py" in c for c in s._watcher.poll_calls), (
            "leaf's scoped targets were evicted by the burst — the Run #15 L2 class"
        )

    @pytest.mark.asyncio
    async def test_union_deduped_single_run(self, monkeypatch):
        s = _sensor(monkeypatch, {
            "a.py": ["tests/t1.py", "tests/t2.py"],
            "b.py": ["tests/t2.py", "tests/t3.py"],
        })
        await s._on_fs_event(_event("a.py"))
        await s._on_fs_event(_event("b.py"))
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert len(s._watcher.poll_calls) == 1
        assert sorted(s._watcher.poll_calls[0]) == ["tests/t1.py", "tests/t2.py", "tests/t3.py"]

    @pytest.mark.asyncio
    async def test_mid_run_arrivals_rearm_followup(self, monkeypatch):
        s = _sensor(monkeypatch, {"a.py": ["tests/t1.py"], "late.py": ["tests/t9.py"]})
        await s._on_fs_event(_event("a.py"))
        await asyncio.sleep(0)          # let the window task start
        s._pending_changed_paths.add("late.py")   # simulates arrival mid-run
        await asyncio.wait_for(s._debounce_task, timeout=2.0)
        for _ in range(50):             # follow-up window drains the late path
            if any(c and "tests/t9.py" in c for c in s._watcher.poll_calls):
                break
            await asyncio.sleep(0.05)
        assert any(c and "tests/t9.py" in c for c in s._watcher.poll_calls)

    @pytest.mark.asyncio
    async def test_cap_logs_and_bounds(self, monkeypatch, caplog):
        monkeypatch.setenv("JARVIS_TEST_FAILURE_DEBOUNCE_MAX_PATHS", "2")
        s = _sensor(monkeypatch, {f"p{i}.py": [f"tests/t{i}.py"] for i in range(4)})
        for i in range(4):
            await s._on_fs_event(_event(f"p{i}.py"))
        with caplog.at_level("WARNING"):
            await asyncio.wait_for(s._debounce_task, timeout=2.0)
        assert any("dropped" in r.message for r in caplog.records), "cap must not be silent"

"""The repo-sync loop stops polling, and stops writing what did not change.

StallSampler caught this on the wedged main loop — at a ~500-byte JSON write
that ran every ten seconds whether or not anything had moved.

What the loop actually did per tick, measured rather than assumed: three
`Path.exists()` calls and three small `write_text` + `rename` pairs. No
directory walk, no delta comparison, no hashing. That measurement is why the
fix is event-driven + write-on-change + off-loop, and NOT a process pool: a
`ProcessPoolExecutor` dispatch costs more than the work it would isolate.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.coding_council.trinity import cross_repo_sync as crs


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_BACKSTOP_S", "0.2")
    m = crs.CrossRepoSync()
    m._sync_dir = tmp_path / "sync"
    m._sync_dir.mkdir(parents=True, exist_ok=True)
    return m


class TestWriteOnChange:
    async def test_an_unchanged_state_is_not_rewritten(self, mgr, tmp_path):
        """The single biggest win: three files rewritten every ten seconds,
        forever, for state that almost never changes."""
        rt = list(crs.RepoType)[0]
        repo = mgr._repos[rt]
        repo.online = True

        await mgr._write_repo_state(rt)
        state_file = next(iter(tmp_path.rglob(f"{rt.value}.json")), None)
        assert state_file is not None, "first write must happen"
        first_mtime = state_file.stat().st_mtime_ns

        await mgr._write_repo_state(rt)
        assert state_file.stat().st_mtime_ns == first_mtime, (
            "an unchanged state was rewritten")

    async def test_a_changed_state_IS_written(self, mgr, tmp_path):
        rt = list(crs.RepoType)[0]
        mgr._repos[rt].online = False
        await mgr._write_repo_state(rt)
        state_file = next(iter(tmp_path.rglob(f"{rt.value}.json")))
        assert json.loads(state_file.read_text())["online"] is False

        mgr._repos[rt].online = True
        await mgr._write_repo_state(rt)
        assert json.loads(state_file.read_text())["online"] is True

    async def test_the_timestamp_is_excluded_from_the_comparison(self, mgr):
        """Including it makes every payload unique and turns write-on-change
        back into write-always — the bug wearing the fix's clothes."""
        rt = list(crs.RepoType)[0]
        await mgr._write_repo_state(rt)
        stored = mgr._last_written[rt]
        assert "timestamp" not in stored
        assert "online" in stored and "sync_status" in stored


class TestEventDriven:
    async def test_an_fs_event_wakes_the_loop_instead_of_a_sleep(self, mgr):
        assert not mgr._wake.is_set()
        await mgr._on_fs_event(object())
        assert mgr._wake.is_set(), "a mutation must wake the evaluation"

    async def test_the_handler_only_flags_and_never_evaluates(self, mgr):
        """Doing the work in the handler would let a burst of file events run
        N overlapping evaluations — an event-driven rewrite that is worse
        than the poll it replaced."""
        called = {"discover": 0}

        async def _spy():
            called["discover"] += 1

        mgr._discover_repos = _spy
        for _ in range(50):
            await mgr._on_fs_event(object())
        assert called["discover"] == 0
        assert mgr._wake.is_set()

    async def test_a_missing_bus_degrades_to_the_backstop_not_to_blindness(
            self, mgr, monkeypatch):
        """Event-primary with a polling FLOOR — the repo's own Gap #4
        pattern. No bus must mean the old cadence, never no cadence."""
        monkeypatch.setattr(
            "backend.core.trinity_event_bus.get_event_bus_if_exists",
            lambda: None)
        assert await mgr._subscribe_to_fs_events() is False
        assert mgr._fs_subscribed is False
        assert mgr._backstop_s > 0

    async def test_a_hostile_bus_never_escapes_start(self, mgr, monkeypatch):
        class _Bus:
            async def subscribe(self, *_a, **_k):
                raise RuntimeError("bus exploded")

        monkeypatch.setattr(
            "backend.core.trinity_event_bus.get_event_bus_if_exists",
            lambda: _Bus())
        assert await mgr._subscribe_to_fs_events() is False

    async def test_it_subscribes_when_a_bus_exists(self, mgr, monkeypatch):
        seen = {}

        class _Bus:
            async def subscribe(self, pattern, handler):
                seen["pattern"] = pattern
                return "sub-1"

        monkeypatch.setattr(
            "backend.core.trinity_event_bus.get_event_bus_if_exists",
            lambda: _Bus())
        assert await mgr._subscribe_to_fs_events() is True
        assert seen["pattern"] == "fs.changed.*", (
            "must reuse the EXISTING topic four sensors already consume")


class TestTheLoopLeavesTheEventLoopAlone:
    async def test_a_tick_does_not_block_on_filesystem_stats(self, mgr):
        """The presence probe is three `exists()` calls — small, and still
        not the loop thread's job."""
        import time as _t

        slow = {"n": 0}
        real_exists = crs.Path.exists if hasattr(crs, "Path") else None

        worst = 0.0

        async def _ticker():
            nonlocal worst
            for _ in range(30):
                t0 = _t.monotonic()
                await asyncio.sleep(0.01)
                worst = max(worst, _t.monotonic() - t0)

        # Make the probe genuinely slow, the way a stalled disk would.
        for repo in mgr._repos.values():
            class _SlowPath(type(repo.repo_path)):
                def exists(self, *a, **k):      # noqa: D102
                    _t.sleep(0.05)
                    slow["n"] += 1
                    return True
            try:
                repo.repo_path = _SlowPath(str(repo.repo_path))
            except Exception:
                pytest.skip("path subclassing unavailable on this platform")

        ticker = asyncio.create_task(_ticker())
        mgr._running = True
        loop_task = asyncio.create_task(mgr._sync_loop())
        await asyncio.sleep(0.5)
        mgr._running = False
        loop_task.cancel()
        ticker.cancel()
        for t in (loop_task, ticker):
            try:
                await t
            except asyncio.CancelledError:
                pass
        assert slow["n"] > 0, "the probe never ran; the test proved nothing"
        assert worst < 0.15, f"the loop was blocked for {worst:.3f}s"

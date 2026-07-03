"""fs-hot-tier Phase 2 (audit row 6, 2026-07-02) — SessionManager
``list_active_cached`` regression spine.

``ConversationLedgerObserver.__call__`` (a SYNC bridge turn observer)
called ``SessionManager.list_active()`` -- a ``glob("*.json")`` + per-
file JSON parse of the session dir -- UNCONDITIONALLY on every
conversation turn, directly on the asyncio loop thread
(``session_manager.py:307``, audit row 6).

``list_active_cached()`` serves a TTL-cached snapshot and refreshes it
OFF the loop thread via the ``cooperative_fs_io.offload`` substrate
(fire-and-forget, ``loop.create_task``) whenever an event loop is
running in the calling thread; only when no loop is running at all
does it fall back to a direct synchronous scan (safe -- no loop to
stall).

No test file previously existed for ``session_manager.py`` --
this is a new regression spine.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io
from backend.core.ouroboros.governance.session_manager import (
    SessionManager,
    SessionState,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mgr(tmp_path) -> SessionManager:
    return SessionManager(storage_dir=tmp_path / "sessions")


def _seed_active_session(mgr: SessionManager, goal: str) -> str:
    session = mgr.create(goal)
    return session.session_id


@pytest.fixture(autouse=True)
def _clean_ttl_env(monkeypatch):
    monkeypatch.delenv("JARVIS_SESSION_ACTIVE_CACHE_TTL_S", raising=False)
    yield


# ---------------------------------------------------------------------------
# (a) Substrate routing -- the background refresh must go through
#     cooperative_fs_io.offload, not a bespoke executor call.
# ---------------------------------------------------------------------------


class TestSubstrateRouting:
    @pytest.mark.asyncio
    async def test_stale_cache_refresh_routes_through_offload(
        self, tmp_path, monkeypatch,
    ):
        mgr = _mgr(tmp_path)
        _seed_active_session(mgr, "goal-a")

        calls = {"count": 0}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, **kwargs):
            if fn == mgr.list_active:
                calls["count"] += 1
            return await real_offload(fn, *args, **kwargs)

        # SessionManager._refresh_active_cache_async does a
        # function-local ``from cooperative_fs_io import offload``,
        # so the spy must patch the SOURCE module attribute.
        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)

        # Cold cache -> schedules a background refresh (fire-and-
        # forget task) and returns immediately (degraded/empty).
        first = mgr.list_active_cached()
        assert first == []

        # Let the scheduled task run.
        await asyncio.sleep(0.05)

        assert calls["count"] == 1, (
            "background refresh must dispatch mgr.list_active via "
            "cooperative_fs_io.offload"
        )
        # After the refresh completed, the cache is warm.
        second = mgr.list_active_cached()
        assert len(second) == 1

    @pytest.mark.asyncio
    async def test_fresh_cache_does_not_reschedule_refresh(
        self, tmp_path, monkeypatch,
    ):
        mgr = _mgr(tmp_path)
        _seed_active_session(mgr, "goal-a")
        # Warm the cache once.
        mgr.list_active_cached()
        await asyncio.sleep(0.05)

        calls = {"count": 0}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, **kwargs):
            calls["count"] += 1
            return await real_offload(fn, *args, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)

        # Cache is fresh (default TTL 5s) -- no new refresh scheduled.
        mgr.list_active_cached()
        await asyncio.sleep(0.02)
        assert calls["count"] == 0


# ---------------------------------------------------------------------------
# (b) Correctness -- eventually consistent with the underlying
#     synchronous scan for a planted temp tree.
# ---------------------------------------------------------------------------


class TestCorrectness:
    @pytest.mark.asyncio
    async def test_cached_result_matches_sync_list_active(self, tmp_path):
        mgr = _mgr(tmp_path)
        sid_a = _seed_active_session(mgr, "goal-a")
        sid_b = _seed_active_session(mgr, "goal-b")
        # A paused session is still "active" per list_active's
        # contract; a completed one is not.
        paused = mgr.create("goal-paused")
        mgr.pause(paused.session_id)
        completed = mgr.create("goal-done")
        mgr.complete(completed.session_id)

        expected = {s.session_id for s in mgr.list_active()}
        assert expected == {sid_a, sid_b, paused.session_id}

        # Cold cache degrades to [] immediately, then the background
        # refresh converges to the same set as the sync scan.
        mgr.list_active_cached()
        await asyncio.sleep(0.05)
        cached_ids = {s.session_id for s in mgr.list_active_cached()}
        assert cached_ids == expected

    @pytest.mark.asyncio
    async def test_no_running_loop_falls_back_to_sync_scan(self, tmp_path):
        """When called with no event loop running in this thread,
        list_active_cached() must compute synchronously in-line
        (safe: there is no loop to stall) rather than degrade to
        empty."""
        mgr = _mgr(tmp_path)
        sid = _seed_active_session(mgr, "goal-sync")

        def _call_from_thread():
            return mgr.list_active_cached()

        result = await asyncio.to_thread(_call_from_thread)
        assert [s.session_id for s in result] == [sid]


# ---------------------------------------------------------------------------
# (c) Fail-soft -- an OffloadError from the substrate must degrade to
#     the cached/empty result, never raise, never crash the op.
# ---------------------------------------------------------------------------


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_offload_error_leaves_cache_stale_no_raise(
        self, tmp_path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )

        mgr = _mgr(tmp_path)
        _seed_active_session(mgr, "goal-a")

        async def _boom_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="list_active",
                exc_type="RuntimeError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)

        # Cold cache + failing refresh -> degrades to [] (documented
        # empty/degraded result), never raises.
        result = mgr.list_active_cached()
        await asyncio.sleep(0.02)
        assert result == []
        # A second call must not raise either, and the cache stays
        # empty (never crashed, never populated with garbage).
        assert mgr.list_active_cached() == []

    @pytest.mark.asyncio
    async def test_refresh_task_exception_never_propagates(
        self, tmp_path, monkeypatch,
    ):
        mgr = _mgr(tmp_path)
        _seed_active_session(mgr, "goal-a")

        async def _raising_offload(fn, *args, **kwargs):
            raise RuntimeError("synthetic catastrophic failure")

        monkeypatch.setattr(cooperative_fs_io, "offload", _raising_offload)

        # Must not raise out of list_active_cached() itself.
        result = mgr.list_active_cached()
        assert result == []
        await asyncio.sleep(0.02)
        # The observer-facing call site must still function.
        assert mgr.list_active_cached() == []


# ---------------------------------------------------------------------------
# TTL behavior
# ---------------------------------------------------------------------------


class TestTTL:
    @pytest.mark.asyncio
    async def test_ttl_env_knob_respected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_SESSION_ACTIVE_CACHE_TTL_S", "0.5")
        mgr = _mgr(tmp_path)
        _seed_active_session(mgr, "goal-a")
        mgr.list_active_cached()
        await asyncio.sleep(0.05)
        assert len(mgr.list_active_cached()) == 1

        # Manually age the cache past the TTL.
        with mgr._lock:
            mgr._active_cache_ts = time.time() - 1.0

        calls = {"count": 0}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, **kwargs):
            calls["count"] += 1
            return await real_offload(fn, *args, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
        mgr.list_active_cached()
        await asyncio.sleep(0.05)
        assert calls["count"] == 1, "stale cache must trigger a refresh"

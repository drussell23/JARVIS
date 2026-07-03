"""Loop Deadman fix -- stdlib_self_health_oracle off-loop + bounded scan.

Profiler capture (Loop Deadman faulthandler, run bt-iso-1783056401)
caught MainThread wedged 8-17s per periodic tick inside
``production_oracle_observer.py:325 run_periodic -> :252 tick_once ->
stdlib_self_health_oracle.py:352 query_signals -> :139
_load_recent_summaries -> :140 <genexpr>``.

Root cause, confirmed by reading the pre-fix source (both defects
present in the same commit):

  (a) ``_load_recent_summaries`` selected "recent" sessions via
      ``sorted(p.name for p in sessions_dir.iterdir() if
      p.is_dir())`` -- a full ``pathlib.Path.is_dir()`` (separate
      ``stat(2)`` syscall) across *every* directory under
      ``.ouroboros/sessions/`` before slicing to ``lookback``. With
      751+ session dirs and growing every run, this scan cost grows
      monotonically (1.5s -> 17.6s observed).

  (b) ``StdlibSelfHealthOracle.query_signals`` is ``async def`` but
      its body called ``_load_recent_summaries`` directly --
      synchronously, on the event loop thread, with no ``await``
      yielding control during the scan. An ``async def`` that never
      awaits is still a blocking callback.

This starves the loop for the scan's full duration, which cancels
in-flight aiohttp tool/provider round-trips elsewhere in the process
(observed: CancelledError -> DoublewordInfraError).

Fix (this file's RED->GREEN pins):
  (a) Bounded WORK, not just bounded result: ``os.scandir`` +
      ``heapq.nlargest`` newest-N-by-mtime selection, capped by env
      ``JARVIS_SELF_HEALTH_ORACLE_MAX_SESSIONS`` (default 25) --
      BEFORE any ``summary.json`` is opened.
  (b) The scan+parse now runs via
      ``loop.run_in_executor(None, _load_recent_summaries, ...)``
      inside ``query_signals`` -- off the event loop thread, same
      idiom as the sibling ``http_healthcheck_oracle.py`` adapter.
  (c) Fail-soft: a raising scan yields ``()`` / a DISABLED signal,
      never an exception into the caller.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import stdlib_self_health_oracle as mod
from backend.core.ouroboros.governance.stdlib_self_health_oracle import (
    StdlibSelfHealthOracle,
    _load_recent_summaries,
    _scan_recent_session_names,
    max_scan_sessions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_STDLIB_SELF_HEALTH_ORACLE_ENABLED",
        "JARVIS_STDLIB_SELF_HEALTH_LOOKBACK",
        "JARVIS_SELF_HEALTH_ORACLE_MAX_SESSIONS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_session(
    sessions_dir: Path, name: str, *, mtime_offset_s: float,
    session_outcome: str = "complete", cost_total: float = 0.1,
    stop_reason: str = "idle_timeout",
) -> Path:
    """Create a fake session dir with a parseable summary.json, and
    set its mtime explicitly (independent of lexicographic name
    order) so tests can prove mtime-based -- not name-based --
    selection."""
    d = sessions_dir / name
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({
        "session_outcome": session_outcome,
        "cost_total": cost_total,
        "stop_reason": stop_reason,
    }))
    now = time.time()
    target = now + mtime_offset_s
    os.utime(d, (target, target))
    return d


# ---------------------------------------------------------------------------
# (a) RED-first: bounded scan -- parses at most max_scan / lookback,
#     regardless of how many session dirs exist on disk, and selects
#     by mtime (newest first), not lexicographic name order.
# ---------------------------------------------------------------------------


class TestBoundedScan:
    def test_scan_caps_candidates_at_max_scan_sessions(
        self, tmp_path, monkeypatch,
    ):
        """35 session dirs on disk, max_scan=5 -> the scan phase
        returns at most 5 candidate names, never all 35."""
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        for i in range(35):
            _make_session(
                sessions_dir, f"sess-{i:03d}", mtime_offset_s=-i,
            )
        names = _scan_recent_session_names(sessions_dir, max_scan=5)
        assert len(names) == 5

    def test_scan_selects_by_mtime_not_lexicographic_name(
        self, tmp_path,
    ):
        """Names are chosen adversarially so that lexicographic
        order is the OPPOSITE of mtime order. If the scan still
        used name-based sort (the pre-fix behavior), this would
        select the wrong (oldest) sessions."""
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        # "aaa" is newest (mtime offset 0), "zzz" is oldest
        # (mtime offset -100) -- inverse of lexicographic order.
        _make_session(sessions_dir, "aaa-newest", mtime_offset_s=0)
        _make_session(sessions_dir, "mmm-middle", mtime_offset_s=-50)
        _make_session(sessions_dir, "zzz-oldest", mtime_offset_s=-100)
        names = _scan_recent_session_names(sessions_dir, max_scan=2)
        assert set(names) == {"aaa-newest", "mmm-middle"}
        assert "zzz-oldest" not in names

    def test_load_recent_summaries_parses_at_most_bounded_count(
        self, tmp_path, monkeypatch,
    ):
        """N+10 session dirs on disk (spy on json parse count via a
        wrapped Path.read_text): with max_scan=8 / lookback=3, at
        most 3 summary.json files are ever read, never all N+10."""
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        n = 8  # arbitrary bound picked below
        for i in range(n + 10):
            _make_session(
                sessions_dir, f"sess-{i:04d}", mtime_offset_s=-i,
            )

        read_calls = {"count": 0}
        orig_read_text = Path.read_text

        def _spy_read_text(self, *args, **kwargs):
            if self.name == "summary.json":
                read_calls["count"] += 1
            return orig_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _spy_read_text)

        result = _load_recent_summaries(
            tmp_path, lookback=3, max_scan=8,
        )
        assert len(result) == 3
        assert read_calls["count"] == 3, (
            f"expected exactly 3 summary.json reads (lookback), got "
            f"{read_calls['count']} -- scan is not bounding parse work"
        )

    def test_max_scan_sessions_env_knob(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SELF_HEALTH_ORACLE_MAX_SESSIONS", "7",
        )
        assert max_scan_sessions() == 7

    def test_max_scan_sessions_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SELF_HEALTH_ORACLE_MAX_SESSIONS", raising=False,
        )
        assert max_scan_sessions() == mod._DEFAULT_MAX_SCAN_SESSIONS


# ---------------------------------------------------------------------------
# (b) RED-first: the loop must stay responsive while the scan runs.
#     A concurrent 50ms-cadence heartbeat task must keep ticking
#     while query_signals() is in flight, even when the underlying
#     scan is artificially slow.
# ---------------------------------------------------------------------------


class TestOffLoopExecution:
    @pytest.mark.asyncio
    async def test_query_signals_is_async_and_does_not_block_loop(
        self, tmp_path, monkeypatch,
    ):
        """Monkeypatch the sync scan+parse function to sleep
        (simulating a slow scan) INSIDE the executor thread, then
        run it concurrently with a fast heartbeat coroutine on the
        same loop. If the scan still ran synchronously on the loop
        thread (pre-fix behavior), the heartbeat would starve for
        the full sleep duration."""
        oracle = StdlibSelfHealthOracle(project_root=tmp_path)

        def _slow_scan(project_root, lookback, max_scan):
            # Runs in the executor thread -- a real time.sleep here
            # is fine; it must NOT block the asyncio loop thread.
            time.sleep(0.3)
            return ()

        monkeypatch.setattr(mod, "_load_recent_summaries", _slow_scan)

        ticks = 0
        running = True

        async def heartbeat():
            nonlocal ticks
            while running:
                ticks += 1
                await asyncio.sleep(0.05)

        hb_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        before = ticks

        await oracle.query_signals()

        running = False
        await asyncio.sleep(0.01)
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        during = ticks - before
        assert during >= 2, (
            f"Heartbeat starved during query_signals: ticks={during} "
            "over a 0.3s slow scan -- the scan is still blocking the "
            "event loop thread (off-loop fix failed)"
        )

    def test_load_recent_summaries_is_plain_sync_function(self):
        """Contract pin: the executor payload must be a plain sync
        function (not a coroutine) -- run_in_executor requires this."""
        assert not asyncio.iscoroutinefunction(_load_recent_summaries)

    def test_query_signals_is_coroutine_function(self):
        assert asyncio.iscoroutinefunction(
            StdlibSelfHealthOracle.query_signals,
        )


# ---------------------------------------------------------------------------
# fs-hot-tier Phase 2 (2026-07-02) — convergence onto the shared
# cooperative_fs_io.offload substrate. Replaces the ad-hoc
# ``loop.run_in_executor(None, ...)`` (the DEFAULT/contested pool)
# with ``offload(..., cpu_bound=False)`` (the dedicated advisor-blast
# pool every other fs-hot-tier fix now routes through) — closing one
# of the three divergent off-loop mechanisms the audit found.
# ---------------------------------------------------------------------------


class TestSubstrateConvergence:
    @pytest.mark.asyncio
    async def test_query_signals_routes_through_offload_substrate(
        self, tmp_path, monkeypatch,
    ):
        """query_signals() must dispatch the scan via
        cooperative_fs_io.offload (spied), not a bespoke executor
        call — proves the convergence, not just the outcome."""
        from backend.core.ouroboros.governance import cooperative_fs_io

        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        _make_session(sessions_dir, "sess-000", mtime_offset_s=0)

        oracle = StdlibSelfHealthOracle(project_root=tmp_path)

        calls = {"count": 0}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, **kwargs):
            if fn is mod._load_recent_summaries:
                calls["count"] += 1
            return await real_offload(fn, *args, **kwargs)

        # ``query_signals`` does a function-local
        # ``from cooperative_fs_io import offload`` on every call, so
        # the spy must patch the SOURCE module attribute — patching
        # ``mod.offload`` would not be observed (the local import
        # always re-resolves against ``cooperative_fs_io`` directly).
        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)

        signals = await oracle.query_signals()
        assert calls["count"] == 1, (
            "query_signals must dispatch _load_recent_summaries via "
            "cooperative_fs_io.offload"
        )
        assert isinstance(signals, tuple) and len(signals) == 3

    @pytest.mark.asyncio
    async def test_offload_error_degrades_to_empty_summaries_no_raise(
        self, tmp_path, monkeypatch,
    ):
        """A pickling/offload-layer fault (OffloadError, not a raw
        exception from the scan fn) must ALSO degrade gracefully --
        never propagate into query_signals()'s caller."""
        from backend.core.ouroboros.governance import cooperative_fs_io
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )

        oracle = StdlibSelfHealthOracle(project_root=tmp_path)

        async def _fake_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="_load_recent_summaries",
                exc_type="RuntimeError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _fake_offload)

        signals = await oracle.query_signals()
        assert isinstance(signals, tuple)
        assert len(signals) == 3
        assert signals[0].verdict.value == "insufficient_data"


# ---------------------------------------------------------------------------
# (c) RED-first: fail-soft. A raising scan must never propagate an
#     exception -- neither at the pure sync-fn layer nor through the
#     async query_signals() boundary.
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_scan_raising_yields_empty_tuple(
        self, tmp_path, monkeypatch,
    ):
        """_scan_recent_session_names raising mid-scan must not
        propagate -- _load_recent_summaries wraps it and returns ()."""
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        _make_session(sessions_dir, "sess-000", mtime_offset_s=0)

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic scan failure")

        monkeypatch.setattr(mod, "_scan_recent_session_names", _boom)

        result = _load_recent_summaries(tmp_path, lookback=5, max_scan=5)
        assert result == ()

    def test_nonexistent_sessions_dir_yields_empty_tuple(
        self, tmp_path,
    ):
        missing_root = tmp_path / "does-not-exist"
        result = _load_recent_summaries(
            missing_root, lookback=5, max_scan=5,
        )
        assert result == ()

    @pytest.mark.asyncio
    async def test_query_signals_never_raises_when_scan_raises(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end: even if the executor-dispatched function
        raises, query_signals() must return a signal tuple, never
        propagate the exception to the caller (adapter contract).

        fs-hot-tier Phase 2 convergence (2026-07-02): the dispatch
        now routes through ``cooperative_fs_io.offload`` instead of
        an ad-hoc ``loop.run_in_executor(None, ...)``. The substrate
        catches the scan's exception INSIDE the executor and returns
        an ``OffloadError`` (never re-raised) — ``query_signals``
        degrades this to an empty ``summaries`` tuple BEFORE it ever
        reaches the outer ``except Exception`` handler. Empty
        summaries is the SAME "no data" shape
        ``_load_recent_summaries`` already produces on its own
        internal failures (see ``TestFailSoft`` above) and correctly
        renders as ``INSUFFICIENT_DATA`` (the documented empty-input
        verdict in ``_completion_signal``/``_cost_signal``/
        ``_stop_reason_signal``) rather than a coarse blanket
        DISABLED — strictly more informative, still fail-soft, still
        never raises.
        """
        oracle = StdlibSelfHealthOracle(project_root=tmp_path)

        def _boom(project_root, lookback, max_scan):
            raise RuntimeError("synthetic executor-side failure")

        monkeypatch.setattr(mod, "_load_recent_summaries", _boom)

        signals = await oracle.query_signals()
        assert isinstance(signals, tuple)
        assert len(signals) >= 1
        # Adapter contract: on internal scan failure it degrades to
        # the "no data" verdict, never raises.
        assert signals[0].verdict.value == "insufficient_data"

    def test_unparseable_summary_json_skipped_not_raised(
        self, tmp_path,
    ):
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        bad = sessions_dir / "sess-bad"
        bad.mkdir()
        (bad / "summary.json").write_text("{not valid json")
        good = _make_session(sessions_dir, "sess-good", mtime_offset_s=1)

        result = _load_recent_summaries(tmp_path, lookback=5, max_scan=5)
        assert len(result) == 1
        assert result[0]["session_outcome"] == "complete"


# ---------------------------------------------------------------------------
# GREEN sanity: end-to-end query_signals still produces the 3
# expected signal kinds against a small real-looking fixture.
# ---------------------------------------------------------------------------


class TestEndToEndParity:
    @pytest.mark.asyncio
    async def test_query_signals_returns_three_signals(self, tmp_path):
        sessions_dir = tmp_path / ".ouroboros" / "sessions"
        sessions_dir.mkdir(parents=True)
        for i in range(3):
            _make_session(
                sessions_dir, f"sess-{i:03d}", mtime_offset_s=-i,
                session_outcome="complete", cost_total=0.05,
                stop_reason="idle_timeout",
            )
        oracle = StdlibSelfHealthOracle(project_root=tmp_path)
        signals = await oracle.query_signals()
        assert len(signals) == 3
        kinds = {s.kind.value for s in signals}
        assert kinds == {"healthcheck", "performance", "metric"}

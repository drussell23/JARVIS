"""Q3 Slice 3 — SemanticIndex centroid amortization regression suite.

Closes the hot-path stall: every intake signal AND every CLASSIFY phase
called ``_si.build()`` synchronously. The interval gate kept steady-
state cheap, but the **first call after the refresh interval expired**
blocked the caller for multiple seconds (git-log subprocess + corpus
assembly + bulk-embedder inference).

Fix: ``build_async()`` — single-flight non-blocking trigger that returns
immediately. The expensive work runs in a daemon worker; ``score()`` /
``boost_for()`` continue against whichever centroid is currently loaded
(empty on cold start → returns 0, no harm done). Atomic centroid swap
inside ``build()`` (existing single-lock invariant) means readers never
observe a half-rebuilt index.

Covers:

  §1   build_async returns immediately on cold start (synchronous
       latency dominated by thread-spawn, not by build())
  §2   Single-flight: concurrent triggers return "skipped_running" until
       the worker completes
  §3   Interval gate is honored: a build_async after a recent build
       returns "skipped_fresh"
  §4   Master flag off → "skipped_disabled" without spawning a thread
  §5   Worker actually completes the build asynchronously and the
       counter advances
  §6   Worker exception cleanly clears the in-flight flag
  §7   Stats accessor returns the four counters in a snapshot dict
  §8   Cold-start score() returns 0 while a build is still in flight
       (no crash, no exception)
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List

from backend.core.ouroboros.governance.semantic_index import (
    SemanticIndex,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_master() -> None:
    os.environ["JARVIS_SEMANTIC_INFERENCE_ENABLED"] = "true"


def _disable_master() -> None:
    os.environ["JARVIS_SEMANTIC_INFERENCE_ENABLED"] = "false"


class _RecordingIndex(SemanticIndex):
    """SemanticIndex with build() instrumented so tests can:
       (a) measure how long build() takes (without flexing the real
           git-log + embedder),
       (b) assert that build_async actually invokes build(),
       (c) cause build() to fail / hang to exercise error paths."""

    def __init__(self, project_root: Path, *, build_delay_s: float = 0.0) -> None:
        super().__init__(project_root)
        self._build_calls: int = 0
        self._build_delay_s = build_delay_s
        self._build_should_raise = False

    def build(self, *, force: bool = False) -> bool:  # type: ignore[override]
        self._build_calls += 1
        if self._build_should_raise:
            raise RuntimeError("synthetic build failure")
        if self._build_delay_s > 0:
            time.sleep(self._build_delay_s)
        # Mark built_at so the interval-gate path engages on subsequent
        # calls (we don't actually populate centroid — the test surface
        # only cares about the gate semantics).
        with self._lock:
            self._built_at = time.time()
            self._stats.built_at = self._built_at
            self._stats.refreshes += 1
        return True


def _spin_until(predicate, timeout_s: float = 2.0, poll_s: float = 0.005) -> bool:
    """Block until ``predicate()`` returns truthy or timeout. Returns
    the final predicate value so callers can assert on it."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        v = predicate()
        if v:
            return True
        time.sleep(poll_s)
    return bool(predicate())


# ---------------------------------------------------------------------------
# §1 — build_async returns immediately
# ---------------------------------------------------------------------------


class TestNonBlocking(unittest.TestCase):
    def test_build_async_returns_immediately_on_cold_start(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td), build_delay_s=0.5)
            t0 = time.monotonic()
            result = idx.build_async()
            dt = time.monotonic() - t0
            # Caller returns in well under the 0.5s build delay.
            self.assertLess(dt, 0.1)
            self.assertEqual(result, "started")
            # Drain the worker so we don't leak a thread between tests.
            _spin_until(lambda: not idx.async_build_stats()["running"])


# ---------------------------------------------------------------------------
# §2 — Single-flight
# ---------------------------------------------------------------------------


class TestSingleFlight(unittest.TestCase):
    def test_concurrent_triggers_collapse_to_one_build(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td), build_delay_s=0.2)
            results: List[str] = []
            barrier = threading.Barrier(8)

            def _trigger():
                barrier.wait()
                results.append(idx.build_async())

            threads = [
                threading.Thread(target=_trigger) for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            # Exactly one "started" + the rest "skipped_running".
            started = sum(1 for r in results if r == "started")
            skipped = sum(1 for r in results if r == "skipped_running")
            self.assertEqual(started, 1)
            self.assertEqual(skipped, 7)
            # Worker eventually clears the flag.
            self.assertTrue(_spin_until(
                lambda: not idx.async_build_stats()["running"],
            ))
            # And only ONE build() invocation ran.
            self.assertEqual(idx._build_calls, 1)


# ---------------------------------------------------------------------------
# §3 — Interval gate
# ---------------------------------------------------------------------------


class TestIntervalGate(unittest.TestCase):
    def test_subsequent_call_after_recent_build_skipped_fresh(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td))
            # First call: starts a build.
            self.assertEqual(idx.build_async(), "started")
            self.assertTrue(_spin_until(
                lambda: not idx.async_build_stats()["running"],
            ))
            # Second call immediately after: interval gate fires.
            self.assertEqual(idx.build_async(), "skipped_fresh")
            stats = idx.async_build_stats()
            self.assertEqual(stats["skipped_fresh"], 1)


# ---------------------------------------------------------------------------
# §4 — Master flag off
# ---------------------------------------------------------------------------


class TestMasterDisabled(unittest.TestCase):
    def test_master_off_returns_skipped_disabled_no_thread(self):
        _disable_master()
        try:
            with TemporaryDirectory() as td:
                idx = _RecordingIndex(Path(td), build_delay_s=10.0)
                self.assertEqual(idx.build_async(), "skipped_disabled")
                # Confirm no thread was spawned: count is unchanged.
                # (Daemon threads may take a moment to settle; allow
                # for thread-pool noise by asserting build_calls=0.)
                self.assertEqual(idx._build_calls, 0)
                self.assertFalse(idx.async_build_stats()["running"])
                self.assertEqual(idx.async_build_stats()["started"], 0)
        finally:
            _enable_master()


# ---------------------------------------------------------------------------
# §5 — Worker completes asynchronously
# ---------------------------------------------------------------------------


class TestWorkerCompletes(unittest.TestCase):
    def test_completed_counter_advances_on_success(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td), build_delay_s=0.05)
            self.assertEqual(idx.build_async(), "started")
            self.assertTrue(_spin_until(
                lambda: idx.async_build_stats()["completed"] >= 1,
            ))
            stats = idx.async_build_stats()
            self.assertEqual(stats["completed"], 1)
            self.assertEqual(stats["failed"], 0)
            self.assertFalse(stats["running"])


# ---------------------------------------------------------------------------
# §6 — Worker exception is contained
# ---------------------------------------------------------------------------


class TestWorkerException(unittest.TestCase):
    def test_build_raise_marks_failed_and_clears_flag(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td))
            idx._build_should_raise = True
            self.assertEqual(idx.build_async(), "started")
            # Worker catches, marks failed, clears flag.
            self.assertTrue(_spin_until(
                lambda: idx.async_build_stats()["failed"] >= 1,
            ))
            stats = idx.async_build_stats()
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["completed"], 0)
            self.assertFalse(stats["running"])
            # Re-arming after a failure works (single-flight respected).
            idx._build_should_raise = False
            # Force-stale by zeroing built_at (worker exception path
            # didn't update it).
            with idx._lock:
                idx._built_at = 0.0
            self.assertEqual(idx.build_async(), "started")
            self.assertTrue(_spin_until(
                lambda: idx.async_build_stats()["completed"] >= 1,
            ))


# ---------------------------------------------------------------------------
# §7 — Stats accessor shape
# ---------------------------------------------------------------------------


class TestStatsShape(unittest.TestCase):
    def test_async_build_stats_has_six_keys(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td))
            stats = idx.async_build_stats()
            for key in (
                "running", "started", "completed", "failed",
                "skipped_running", "skipped_fresh",
            ):
                self.assertIn(key, stats)


# ---------------------------------------------------------------------------
# §8 — Cold-start score() returns 0 during in-flight build
# ---------------------------------------------------------------------------


class TestColdStartScoreSafety(unittest.TestCase):
    def test_score_during_in_flight_build_does_not_raise(self):
        _enable_master()
        with TemporaryDirectory() as td:
            idx = _RecordingIndex(Path(td), build_delay_s=0.2)
            self.assertEqual(idx.build_async(), "started")
            # Worker is in flight. Centroid is empty (cold start).
            # score() must not crash and must return 0.0.
            try:
                # SemanticIndex.score doesn't override our recording
                # index's build, so it'll fall through to the empty-
                # centroid branch.
                value = idx.score("anything")
                self.assertEqual(value, 0.0)
                boost = idx.boost_for("anything")
                self.assertEqual(boost, 0)
            finally:
                _spin_until(
                    lambda: not idx.async_build_stats()["running"],
                )


if __name__ == "__main__":
    unittest.main()

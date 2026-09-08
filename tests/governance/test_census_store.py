"""The census is read, never awaited — and a wedged suite cannot stall the loop.

The first live Sentinel run stalled because discovery awaited
``watcher.run_census()``, which drives pytest subprocesses. A timeout around it
did not help: ``asyncio.wait_for`` cancels only at an await boundary, and the
blocking work inside runs to completion regardless. So the census stopped being
something the critical path calls and became something it reads.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.autonomy.census_store import (
    CensusSnapshot,
    CensusStore,
    census_max_age_s,
    census_refresh_budget_s,
)


class _Watcher:
    def __init__(self, failures=(), delay=0.0, raises=False):
        self._failures = list(failures)
        self._delay = delay
        self._raises = raises
        self.calls = 0

    async def run_census(self):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("suite exploded")
        return self._failures, [], []


@pytest.fixture
def refresh_armed(monkeypatch):
    """Triggering a census is opt-in — these tests exercise the mechanism."""
    monkeypatch.setenv("JARVIS_CENSUS_REFRESH_ENABLED", "true")


class _BlockingWatcher:
    """Blocks the THREAD, the way real pytest subprocesses do — the case a
    timeout on a coroutine cannot rescue."""

    def __init__(self, seconds):
        self._seconds = seconds

    async def run_census(self):
        time.sleep(self._seconds)      # deliberately synchronous
        return [], [], []


# --------------------------------------------------------------------------
# Reads are instant and never start work
# --------------------------------------------------------------------------

def test_an_empty_store_reads_none_instantly():
    store = CensusStore()
    started = time.monotonic()
    assert store.snapshot() is None
    assert time.monotonic() - started < 0.1


def test_a_read_never_runs_the_census():
    store = CensusStore()
    watcher = _Watcher([1, 2])
    store.snapshot()
    assert watcher.calls == 0


def test_a_stale_snapshot_reads_as_absent_but_is_still_retrievable():
    store = CensusStore()
    store._snapshot = CensusSnapshot(failures=(1,), taken_at=time.time() - 10_000)
    assert store.snapshot() is None            # too old to decide on
    assert store.stale_snapshot() is not None  # ...but visible for telemetry


def test_freshness_is_configurable(monkeypatch):
    monkeypatch.setenv("JARVIS_CENSUS_MAX_AGE_S", "50")
    assert census_max_age_s() == 50.0
    snap = CensusSnapshot(failures=(), taken_at=time.time() - 10)
    assert snap.is_fresh() is True
    old = CensusSnapshot(failures=(), taken_at=time.time() - 100)
    assert old.is_fresh() is False


def test_the_budget_is_derived_from_the_pipeline(monkeypatch):
    monkeypatch.delenv("JARVIS_CENSUS_REFRESH_BUDGET_S", raising=False)
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "800")
    assert census_refresh_budget_s() == 400.0


# --------------------------------------------------------------------------
# Refreshes are background, single-flight, and bounded
# --------------------------------------------------------------------------

def test_a_refresh_populates_the_store(refresh_armed):
    async def _go():
        store = CensusStore()
        watcher = _Watcher(failures=["red_1"])
        assert store.ensure_refreshing(watcher) is True
        for _ in range(100):
            if store.snapshot() is not None:
                break
            await asyncio.sleep(0.05)
        return store

    store = asyncio.run(_go())
    snap = store.snapshot()
    assert snap is not None and len(snap.failures) == 1


def test_only_one_refresh_runs_at_a_time(refresh_armed):
    async def _go():
        store = CensusStore()
        watcher = _Watcher(delay=0.5)
        first = store.ensure_refreshing(watcher)
        second = store.ensure_refreshing(watcher)
        await asyncio.sleep(0.8)
        return first, second, watcher.calls

    first, second, calls = asyncio.run(_go())
    assert first is True and second is False
    assert calls == 1, "a second refresh doubled the subprocess load"


def test_a_fresh_store_does_not_refresh(refresh_armed):
    async def _go():
        store = CensusStore()
        store._snapshot = CensusSnapshot(failures=(), taken_at=time.time())
        return store.ensure_refreshing(_Watcher())

    assert asyncio.run(_go()) is False


def test_ensure_refreshing_returns_immediately_even_for_a_slow_census(refresh_armed):
    """The whole point: asking for a refresh must not cost the caller time."""
    async def _go():
        store = CensusStore()
        started = time.monotonic()
        store.ensure_refreshing(_Watcher(delay=5.0))
        elapsed = time.monotonic() - started
        await store.aclose()
        return elapsed

    assert asyncio.run(_go()) < 0.5


# --------------------------------------------------------------------------
# The wedge case — a suite that blocks its THREAD
# --------------------------------------------------------------------------

def test_a_blocking_census_is_abandoned_not_awaited(monkeypatch, refresh_armed):
    """`wait_for` cannot cancel synchronous work, so the deadline is enforced
    on the WAITER and the late result dropped. The store must come back
    usable, and fast."""
    monkeypatch.setenv("JARVIS_CENSUS_REFRESH_BUDGET_S", "1")

    async def _go():
        store = CensusStore()
        store.ensure_refreshing(_BlockingWatcher(20))
        started = time.monotonic()
        for _ in range(60):
            if not store.refreshing:
                break
            await asyncio.sleep(0.1)
        return time.monotonic() - started, store

    elapsed, store = asyncio.run(_go())
    assert elapsed < 8, "the store waited on a blocking census"
    assert store.snapshot() is None      # nothing poisoned the store


def test_a_failed_refresh_leaves_the_previous_snapshot_standing(refresh_armed):
    """Eventual consistency: slightly stale truth beats no truth. Clearing on
    failure would let one flaky run downgrade the organism to blindness."""
    async def _go():
        store = CensusStore()
        good = CensusSnapshot(failures=("keep_me",), taken_at=time.time())
        store._snapshot = good
        store._snapshot = CensusSnapshot(failures=("keep_me",),
                                         taken_at=time.time() - 10_000)
        store.ensure_refreshing(_Watcher(raises=True))
        for _ in range(60):
            if not store.refreshing:
                break
            await asyncio.sleep(0.05)
        return store

    store = asyncio.run(_go())
    assert store.stale_snapshot() is not None
    assert store.stale_snapshot().failures == ("keep_me",)


def test_repeated_failure_backs_off(refresh_armed):
    """A broken suite must not be re-run every single pass."""
    async def _go():
        store = CensusStore()
        store._failures = 3
        store._last_attempt_at = time.time()
        return store.ensure_refreshing(_Watcher())

    assert asyncio.run(_go()) is False


def test_no_watcher_is_a_no_op(refresh_armed):
    async def _go():
        return CensusStore().ensure_refreshing(None)

    assert asyncio.run(_go()) is False


def test_ensure_refreshing_outside_a_loop_does_not_raise(refresh_armed):
    assert CensusStore().ensure_refreshing(_Watcher()) is False


def test_aclose_is_safe_when_nothing_is_running():
    asyncio.run(CensusStore().aclose())


def test_the_snapshot_renders_for_telemetry():
    snap = CensusSnapshot(failures=(1, 2), taken_at=time.time(), duration_s=12.0)
    text = snap.render()
    assert "2 red" in text and "took 12s" in text


# --------------------------------------------------------------------------
# Triggering a census is OPT-IN
# --------------------------------------------------------------------------

def test_triggering_a_census_is_off_by_default(monkeypatch):
    """Moving the census off the critical path stopped it BLOCKING a pass; it
    did not make it cheap. A refresh shards the suite across many concurrent
    pytest subprocesses, and on this tree that destabilised the whole session
    — a 2400s run died at 79s with six ops in flight and no shutdown sequence
    while the census was spawning its swarm.

    Reading a census stays free and always on. What is gated is the organism
    deciding, unprompted, to run the entire suite while it is also working.
    """
    monkeypatch.delenv("JARVIS_CENSUS_REFRESH_ENABLED", raising=False)

    async def _go():
        return CensusStore().ensure_refreshing(_Watcher())

    assert asyncio.run(_go()) is False


def test_reads_are_never_gated(monkeypatch):
    """A gate on reads would make stale evidence unreachable for no benefit."""
    monkeypatch.delenv("JARVIS_CENSUS_REFRESH_ENABLED", raising=False)
    store = CensusStore()
    store._snapshot = CensusSnapshot(failures=("r",), taken_at=time.time())
    assert store.snapshot() is not None

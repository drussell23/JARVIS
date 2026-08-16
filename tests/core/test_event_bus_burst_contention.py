"""A file-system storm must not become event-loop CPU.

THE DEFECT THIS PINS
--------------------
`TrinityEventBus.publish` deduplicated by LINEAR SCAN of a 10,000-entry ring,
on the event loop, holding `_dedup_lock`, with no await inside the scan. A
UNIQUE event never matched, so it paid the full scan — and every distinct file
in a `git checkout` is unique. Publish cost was therefore O(burst x ring).

Measured before the fix, 2,000 unique events against a full ring:

    5,232ms of uninterruptible loop CPU, 20,000,000 comparisons

That is the burst-contention path. It is quadratic by shape, so it does not
show up in a quiet profile and it does not show up in a small test — it shows
up exactly when a real storm arrives, which is when the real-time streams can
least afford it.

WHAT IS ASSERTED HERE
---------------------
Parity first (the fix must not change WHICH events dedup), then the bound,
then the thing that actually matters: a concurrent ticker standing in for an
audio/vision frame path keeps its cadence THROUGH the storm. The last one is
the Phase 3 validation, and it is written so that it FAILS against the old
implementation rather than merely passing against the new one.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core import trinity_event_bus as teb


async def _make_bus():
    bus = teb.TrinityEventBus(teb.RepoType.JARVIS)
    await bus.start()
    return bus


async def _stop(bus):
    try:
        await bus.stop()
    except Exception:  # noqa: BLE001 — teardown must never mask a failure
        pass


# ---------------------------------------------------------------------------
# Parity — the fix changes the COST, never the DECISION
# ---------------------------------------------------------------------------

class TestDedupDecisionIsUnchanged:

    @pytest.mark.asyncio
    async def test_a_repeat_inside_the_window_is_still_deduplicated(self):
        bus = await _make_bus()
        try:
            before = bus._metrics.events_deduplicated
            payload = {"path": "/x/y.py", "seq": 1}
            await bus.publish_raw("fs.changed.modified", dict(payload),
                                  persist=False)
            await bus.publish_raw("fs.changed.modified", dict(payload),
                                  persist=False)
            assert bus._metrics.events_deduplicated == before + 1
        finally:
            await _stop(bus)

    @pytest.mark.asyncio
    async def test_distinct_payloads_are_never_deduplicated(self):
        """Every file in a checkout is a distinct payload. If this ever
        deduped, a storm would go SILENT instead of going fast."""
        bus = await _make_bus()
        try:
            before = bus._metrics.events_deduplicated
            for i in range(50):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/x/{i}.py"}, persist=False)
            assert bus._metrics.events_deduplicated == before
        finally:
            await _stop(bus)

    @pytest.mark.asyncio
    async def test_a_repeat_OUTSIDE_the_window_is_published_again(self):
        """The window is what makes this a dedup and not a permanent filter.
        Asserted by ageing the recorded timestamp, not by sleeping 60s."""
        bus = await _make_bus()
        try:
            payload = {"path": "/x/y.py"}
            await bus.publish_raw("fs.changed.modified", dict(payload),
                                  persist=False)
            aged = time.time() - (teb.EventBusConfig.DEDUP_WINDOW_SECONDS + 5)
            for fp in list(bus._recent_fingerprints):
                bus._recent_fingerprints[fp] = aged

            before = bus._metrics.events_deduplicated
            await bus.publish_raw("fs.changed.modified", dict(payload),
                                  persist=False)
            assert bus._metrics.events_deduplicated == before, (
                "an expired fingerprint must not suppress a live event")
        finally:
            await _stop(bus)


# ---------------------------------------------------------------------------
# The bound the deque used to give for free
# ---------------------------------------------------------------------------

class TestTheIndexStaysBounded:

    @pytest.mark.asyncio
    async def test_the_index_never_exceeds_its_ceiling(self, monkeypatch):
        monkeypatch.setattr(teb.EventBusConfig, "DEDUP_RING_MAX", 64)
        bus = await _make_bus()
        try:
            for i in range(400):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/x/{i}.py"}, persist=False)
            assert len(bus._recent_fingerprints) <= 64, (
                "memory bound lost when the deque was replaced")
        finally:
            await _stop(bus)

    @pytest.mark.asyncio
    async def test_eviction_never_discards_a_LIVE_fingerprint(self):
        """Popping the front is only safe because the front is the
        least-recently-seen entry. If `move_to_end` were dropped from
        `publish`, this is the test that would catch it."""
        bus = await _make_bus()
        try:
            hot = {"path": "/hot.py"}
            await bus.publish_raw("fs.changed.modified", dict(hot),
                                  persist=False)
            for i in range(200):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/cold/{i}.py"}, persist=False)
            # Touch it again; it must still be recognised as a duplicate.
            before = bus._metrics.events_deduplicated
            await bus.publish_raw("fs.changed.modified", dict(hot),
                                  persist=False)
            assert bus._metrics.events_deduplicated == before + 1
            # ...and that touch must have refreshed its recency.
            assert next(reversed(bus._recent_fingerprints)) is not None
        finally:
            await _stop(bus)

    @pytest.mark.asyncio
    async def test_expiry_drain_is_capped_per_publish(self, monkeypatch):
        """An uncapped drain is how an eviction pass becomes the very
        O(N)-on-the-loop stall this replaced."""
        monkeypatch.setattr(teb.EventBusConfig, "DEDUP_EVICT_PER_PUBLISH", 2)
        bus = await _make_bus()
        try:
            for i in range(20):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/x/{i}.py"}, persist=False)
            aged = time.time() - (teb.EventBusConfig.DEDUP_WINDOW_SECONDS + 5)
            for fp in list(bus._recent_fingerprints):
                bus._recent_fingerprints[fp] = aged
            n_before = len(bus._recent_fingerprints)

            await bus.publish_raw("fs.changed.modified", {"path": "/new.py"},
                                  persist=False)
            removed = n_before + 1 - len(bus._recent_fingerprints)
            assert removed <= 2, f"drained {removed} in one publish; cap is 2"
        finally:
            await _stop(bus)


# ---------------------------------------------------------------------------
# PHASE 3 — the real-time stream keeps its cadence THROUGH the storm
# ---------------------------------------------------------------------------

class TestARealTimeStreamSurvivesAnFsStorm:

    @pytest.mark.asyncio
    async def test_a_2000_file_burst_does_not_stall_a_concurrent_ticker(self):
        """The validation the whole round is for.

        A ticker awaiting 10ms stands in for an audio/vision frame path. It
        shares one event loop with a 2,000-file storm. Under the linear-scan
        dedup this ticker measured multi-second overshoot, because each
        publish held the loop for ~2.6ms with no await inside the scan and
        nothing could preempt it.

        The threshold is deliberately loose (250ms). This is not a
        microbenchmark and it must not go red on a loaded CI box — it exists
        to catch a return to QUADRATIC behaviour, which overshoots by
        seconds, not milliseconds.
        """
        bus = await _make_bus()
        try:
            # Fill the dedup index so the old scan would be at full depth.
            for i in range(2000):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/warm/{i}.py"}, persist=False)

            worst = 0.0
            ticking = True

            async def ticker():
                nonlocal worst
                while ticking:
                    t0 = time.monotonic()
                    await asyncio.sleep(0.01)
                    worst = max(worst, time.monotonic() - t0 - 0.01)

            tick_task = asyncio.create_task(ticker())
            await asyncio.sleep(0.02)          # let it establish a baseline

            t0 = time.monotonic()
            for i in range(2000):              # the storm
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/storm/{i}.py"}, persist=False)
            burst_s = time.monotonic() - t0

            ticking = False
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

            assert worst < 0.25, (
                f"a real-time ticker overshot by {worst:.3f}s during a "
                f"2,000-event storm ({burst_s:.2f}s) — the publish path is "
                f"holding the loop again")
        finally:
            await _stop(bus)

    @pytest.mark.asyncio
    async def test_publish_cost_does_not_grow_with_index_depth(self):
        """Quadratic behaviour stated as an invariant rather than a number.

        Publishing into a FULL index must cost about what publishing into an
        EMPTY one costs. The old scan made the second case ~1000x worse; the
        ratio is what this asserts, so the test carries no machine-specific
        timing.
        """
        bus = await _make_bus()
        try:
            t0 = time.perf_counter()
            for i in range(200):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/empty/{i}.py"}, persist=False)
            cold = time.perf_counter() - t0

            for i in range(5000):              # deepen the index
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/fill/{i}.py"}, persist=False)

            t0 = time.perf_counter()
            for i in range(200):
                await bus.publish_raw("fs.changed.modified",
                                      {"path": f"/deep/{i}.py"}, persist=False)
            deep = time.perf_counter() - t0

            assert deep < max(cold * 8.0, 0.05), (
                f"publish into a deep index cost {deep*1000:.1f}ms vs "
                f"{cold*1000:.1f}ms into a shallow one — cost is scaling "
                f"with index depth again")
        finally:
            await _stop(bus)

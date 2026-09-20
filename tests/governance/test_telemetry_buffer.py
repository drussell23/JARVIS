"""A telemetry producer must never be able to stall the FSM.

This control plane was just dug out of exactly that hole: measured
starvation of p50 2,050ms, p90 4,721ms, max 43.7s, whose top attributed
causes were a memory monitor and an embedder doing synchronous I/O on the
loop. REPAIR_TRAJECTORY_EMIT and SHADOW_HARNESS are both high-rate disk
producers, so the buffer had to exist before either switch flipped -- the
arming is the easy half.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.telemetry_backpressure import (
    BufferStats,
    TelemetryBuffer,
    all_stats,
    get_buffer,
    reset_buffers,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_buffers()
    yield
    reset_buffers()


# ---------------------------------------------------------------------------
# The producer never waits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offer_returns_immediately_even_with_a_slow_sink():
    """The load-bearing property: a sink that takes a second must not make
    the caller take a second."""
    async def _slow(_payload):
        await asyncio.sleep(1.0)

    buf = TelemetryBuffer("slow", _slow, maxsize=8)
    buf.start()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for i in range(8):
        buf.offer(i)
    assert loop.time() - t0 < 0.1, "offer awaited the sink"
    await buf.aclose(drain=False)


@pytest.mark.asyncio
async def test_offer_never_raises_on_a_broken_sink():
    def _boom(_payload):
        raise RuntimeError("disk gone")

    buf = TelemetryBuffer("broken", _boom, maxsize=4)
    buf.start()
    for i in range(10):
        assert buf.offer(i) is True
    await asyncio.sleep(0.1)
    assert buf.stats().sink_faults > 0
    await buf.aclose(drain=False)


@pytest.mark.asyncio
async def test_sync_sink_runs_without_blocking_the_loop():
    """A sync sink is assumed to touch a disk; a disk write on the event
    loop is the exact fault this module was built after."""
    ticks = []

    def _sink(_payload):
        pass

    buf = TelemetryBuffer("sync", _sink, maxsize=16)
    buf.start()

    async def _heartbeat():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.01)

    for i in range(16):
        buf.offer(i)
    await _heartbeat()
    assert len(ticks) == 5
    await buf.aclose()


# ---------------------------------------------------------------------------
# Bounded, and honest about what it threw away
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overflow_drops_the_oldest_and_keeps_the_newest():
    """A sampled record of a live process is most useful at its freshest.
    Refusing the new payload would freeze the record at the moment the
    system got interesting."""
    written = []
    buf = TelemetryBuffer("bound", written.append, maxsize=4)
    buf.start()
    for i in range(20):
        buf.offer(i)
    await asyncio.sleep(0.15)
    assert buf.stats().dropped_full > 0
    assert written, "nothing was written at all"
    assert written[-1] == 19, "the freshest payload was discarded"
    await buf.aclose()


@pytest.mark.asyncio
async def test_drops_are_counted_never_silent():
    """A gap in the record that is not itself in the record is
    indistinguishable from a period when nothing happened."""
    buf = TelemetryBuffer("counted", lambda _p: None, maxsize=2)
    buf.start()
    for i in range(30):
        buf.offer(i)
    stats = buf.stats()
    assert stats.offered == 30
    assert stats.dropped_full >= 20
    assert "dropped_full" in stats.render()
    await buf.aclose()


@pytest.mark.asyncio
async def test_queue_never_exceeds_its_bound():
    buf = TelemetryBuffer("cap", lambda _p: None, maxsize=3)
    for i in range(50):
        buf.offer(i)
    assert buf._queue.qsize() <= 3


# ---------------------------------------------------------------------------
# A broken sink is quarantined, not retried forever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_faults_quarantine_the_sink():
    """Retrying a broken endpoint at emission rate is a busy loop with
    extra steps."""
    def _boom(_payload):
        raise OSError("endpoint down")

    buf = TelemetryBuffer(
        "quarantine", _boom, maxsize=32, quarantine_after=3, quarantine_s=30.0,
    )
    buf.start()
    for i in range(10):
        buf.offer(i)
    await asyncio.sleep(0.15)
    assert buf.stats().quarantined is True
    await buf.aclose(drain=False)


@pytest.mark.asyncio
async def test_a_recovering_sink_is_not_quarantined():
    """One transient fault is not a broken endpoint."""
    calls = {"n": 0}

    def _flaky(_payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")

    buf = TelemetryBuffer("flaky", _flaky, maxsize=8, quarantine_after=3)
    buf.start()
    for i in range(5):
        buf.offer(i)
    await asyncio.sleep(0.15)
    assert buf.stats().quarantined is False
    assert buf.stats().written >= 3
    await buf.aclose()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_buffer_refuses_and_counts():
    buf = TelemetryBuffer("closed", lambda _p: None, maxsize=4)
    buf.start()
    await buf.aclose()
    assert buf.offer("x") is False
    assert buf.stats().dropped_closed == 1


@pytest.mark.asyncio
async def test_start_is_idempotent():
    buf = TelemetryBuffer("once", lambda _p: None, maxsize=4)
    assert buf.start() is True
    assert buf.start() is False
    await buf.aclose()


@pytest.mark.asyncio
async def test_aclose_is_safe_when_never_started():
    await TelemetryBuffer("never", lambda _p: None).aclose()


@pytest.mark.asyncio
async def test_get_buffer_is_one_per_name():
    a = get_buffer("stream", lambda _p: None)
    b = get_buffer("stream", lambda _p: None)
    assert a is b
    await a.aclose()


@pytest.mark.asyncio
async def test_all_stats_names_every_stream():
    get_buffer("one", lambda _p: None)
    get_buffer("two", lambda _p: None)
    snapshot = all_stats()
    assert set(snapshot) == {"one", "two"}
    assert "dropped_full" in snapshot["one"]


def test_offer_without_a_running_loop_does_not_raise():
    """Import-time or shutdown-path emission must not explode."""
    buf = TelemetryBuffer("noloop", lambda _p: None, maxsize=2)
    assert buf.start() is False
    assert isinstance(buf.stats(), BufferStats)

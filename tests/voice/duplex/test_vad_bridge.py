"""Tests for VADArbiterBridge — the VAD-speech-signal → arbiter barge-in bridge
(Sprint 3 Step 2).

Edge-coalescing (mandate #4) with NO timers (mandate #1): only genuine
False->True / True->False edges drive the arbiter; redundant same-state signals
are dropped. The sync `feed()` is audio-thread-safe and marshals onto the event
loop (mandate #2). Mock arbiter — no real audio.
"""
from __future__ import annotations

import asyncio

import pytest


class _FakeArbiter:
    def __init__(self, boom: bool = False) -> None:
        self.events = []
        self.boom = boom

    async def on_user_speech_start(self) -> None:
        if self.boom:
            raise RuntimeError("arbiter boom")
        self.events.append("start")

    async def on_user_speech_end(self) -> None:
        self.events.append("end")


from backend.core.ouroboros.governance.comms.duplex.vad_bridge import VADArbiterBridge


@pytest.mark.asyncio
async def test_only_genuine_edges_drive_the_arbiter():
    arb = _FakeArbiter()
    br = VADArbiterBridge(arb)
    # redundant same-state signals coalesce:
    await br.dispatch(True)    # False->True edge -> start
    await br.dispatch(True)    # redundant -> dropped
    await br.dispatch(True)    # redundant -> dropped
    await br.dispatch(False)   # True->False edge -> end
    await br.dispatch(False)   # redundant -> dropped
    assert arb.events == ["start", "end"]


@pytest.mark.asyncio
async def test_edge_returns_none_for_redundant():
    br = VADArbiterBridge(_FakeArbiter())
    assert br._edge(False) is None      # already not speaking
    assert br._edge(True) == "start"
    assert br._edge(True) is None       # redundant
    assert br._edge(False) == "end"
    assert br._edge(False) is None      # redundant


@pytest.mark.asyncio
async def test_genuine_toggle_fires_both_edges():
    arb = _FakeArbiter()
    br = VADArbiterBridge(arb)
    for s in (True, False, True):
        await br.dispatch(s)
    assert arb.events == ["start", "end", "start"]   # real edges pass through


@pytest.mark.asyncio
async def test_dispatch_fault_isolated():
    br = VADArbiterBridge(_FakeArbiter(boom=True))
    await br.dispatch(True)              # arbiter raises -> must NOT propagate
    # state still advanced so a later end is coherent (no deadlock)
    assert br._speaking is True


@pytest.mark.asyncio
async def test_feed_marshals_from_sync_caller_to_loop():
    """feed() is sync + non-blocking (audio-thread safe); the drain task applies
    it on the loop."""
    arb = _FakeArbiter()
    br = VADArbiterBridge(arb)
    br.start()                          # binds current loop + starts drain
    try:
        br.feed(True)                   # as if from the PortAudio thread
        br.feed(False)
        # let the drain task process the queued signals:
        for _ in range(50):
            await asyncio.sleep(0.005)
            if arb.events == ["start", "end"]:
                break
        assert arb.events == ["start", "end"]
    finally:
        await br.stop()


def test_feed_before_start_is_safe_noop():
    br = VADArbiterBridge(_FakeArbiter())
    br.feed(True)                        # no loop/queue yet -> no raise

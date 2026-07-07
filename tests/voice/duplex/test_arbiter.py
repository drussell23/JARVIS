"""Tests for the VoiceDuplexArbiter (Sprint 1, arbiter core).

Synchronization note: the arbiter is a cooperative multi-hop async state machine
(submit → run wakes → play task → resume → next). Tests therefore wait on
*conditions* (``_until``) with a timeout — never a fixed number of ``sleep(0)``
yields, which is non-deterministic across the resume chain and can leak a blocked
play task into teardown. ``_shutdown`` tears the arbiter down cleanly so no
blocked playback survives the test.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.comms.duplex.arbiter import VoiceDuplexArbiter
from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig, Priority, SpeechRequest, VoiceState,
)
from tests.voice.duplex.fakes import FakePlayback

_ON = ArbiterConfig(
    enabled=True, barge_in_enabled=True, proactive_enabled=True,
)


async def _until(predicate, timeout: float = 2.0) -> None:
    """Wait until ``predicate()`` is true. Fails fast (AssertionError) on
    timeout so a stuck arbiter never hangs the suite."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition not met within {timeout}s")


async def _shutdown(arb: VoiceDuplexArbiter, task: "asyncio.Task") -> None:
    """Stop the arbiter and its run task with no leaked blocked playback."""
    await arb.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_submit_then_play_highest_priority_first():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
        arb.submit(SpeechRequest("urgent", Priority.PROACTIVE_CRITICAL))

        # Higher priority (critical) plays first.
        await _until(lambda: fp.played[:1] == ["urgent"])
        assert arb.state == VoiceState.KAREN_SPEAKING

        fp.release()                       # finish "urgent"
        await _until(lambda: fp.played == ["urgent", "info"])

        fp.release()                       # finish "info"
        await _until(lambda: arb.state == VoiceState.LISTENING)
    finally:
        await _shutdown(arb, task)


@pytest.mark.asyncio
async def test_user_speech_start_barges_in_and_flushes():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.submit(SpeechRequest("a long narration", Priority.PROACTIVE_INFO))
        await _until(lambda: arb.state == VoiceState.KAREN_SPEAKING)

        await arb.on_user_speech_start()          # user interrupts
        assert fp.preempt_count == 1              # playback was killed
        assert arb.state == VoiceState.USER_SPEAKING

        await arb.on_user_speech_end()
        assert arb.state == VoiceState.LISTENING
    finally:
        await _shutdown(arb, task)


@pytest.mark.asyncio
async def test_no_play_while_user_speaking():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        await arb.on_user_speech_start()
        arb.submit(SpeechRequest("proactive", Priority.PROACTIVE_CRITICAL))
        await asyncio.sleep(0.05)                 # give the loop a chance to (wrongly) play
        assert fp.played == []                    # queued, not played
        assert arb.state == VoiceState.USER_SPEAKING

        await arb.on_user_speech_end()
        await _until(lambda: fp.played == ["proactive"])   # drains after user done
    finally:
        await _shutdown(arb, task)


@pytest.mark.asyncio
async def test_critical_preempts_active_info_playback():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
        await _until(lambda: fp.played == ["info"]
                     and arb.state == VoiceState.KAREN_SPEAKING)

        arb.submit(SpeechRequest("APPROVAL", Priority.PROACTIVE_CRITICAL))
        await _until(lambda: fp.played[-1:] == ["APPROVAL"])   # critical played
        assert fp.preempt_count == 1                            # info was cut
    finally:
        await _shutdown(arb, task)


@pytest.mark.asyncio
async def test_equal_or_lower_priority_does_not_preempt():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.submit(SpeechRequest("crit1", Priority.PROACTIVE_CRITICAL))
        await _until(lambda: fp.played == ["crit1"])

        arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
        await asyncio.sleep(0.05)                  # let the loop NOT preempt
        assert fp.preempt_count == 0               # info waits its turn
        assert fp.played == ["crit1"]
    finally:
        await _shutdown(arb, task)


@pytest.mark.asyncio
async def test_coalesce_keeps_latest_same_key():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    # Don't start run() — inspect the queue directly.
    arb.submit(SpeechRequest("hb v1", Priority.PROACTIVE_INFO, coalesce_key="hb"))
    arb.submit(SpeechRequest("hb v2", Priority.PROACTIVE_INFO, coalesce_key="hb"))
    q = arb._queues[Priority.PROACTIVE_INFO]
    assert [r.text for r in q] == ["hb v2"]        # only the latest survives
    assert arb.snapshot()["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_bounded_queue_drops_oldest():
    fp = FakePlayback()
    cfg = ArbiterConfig(enabled=True, barge_in_enabled=True,
                        proactive_enabled=True, queue_max_per_priority=2)
    arb = VoiceDuplexArbiter(fp, config=cfg)
    for i in range(4):
        arb.submit(SpeechRequest(f"m{i}", Priority.PROACTIVE_INFO))
    q = arb._queues[Priority.PROACTIVE_INFO]
    assert [r.text for r in q] == ["m2", "m3"]     # oldest two shed
    assert arb.snapshot()["shed_count"] == 2


class _BoomPlayback:
    """PlaybackHandle whose play() raises — must not crash the arbiter."""
    def __init__(self):
        self.preempt_count = 0
    @property
    def is_active(self):
        return False
    async def play(self, text):
        raise RuntimeError("audio device on fire")
    def preempt(self):
        self.preempt_count += 1


@pytest.mark.asyncio
async def test_disabled_arbiter_is_noop():
    fp = FakePlayback()
    off = ArbiterConfig(enabled=False, barge_in_enabled=False, proactive_enabled=False)
    arb = VoiceDuplexArbiter(fp, config=off)
    arb.submit(SpeechRequest("x", Priority.PROACTIVE_INFO))
    assert all(len(q) == 0 for q in arb._queues.values())  # nothing enqueued


@pytest.mark.asyncio
async def test_proactive_disabled_drops_proactive_requests():
    fp = FakePlayback()
    cfg = ArbiterConfig(enabled=True, barge_in_enabled=True, proactive_enabled=False)
    arb = VoiceDuplexArbiter(fp, config=cfg)
    arb.submit(SpeechRequest("fyi", Priority.PROACTIVE_INFO))
    arb.submit(SpeechRequest("crit", Priority.PROACTIVE_CRITICAL))
    assert all(len(q) == 0 for q in arb._queues.values())   # both dropped
    # user-tier requests still enqueue even when proactive is off
    arb.submit(SpeechRequest("answer", Priority.USER_RESPONSE))
    assert len(arb._queues[Priority.USER_RESPONSE]) == 1


@pytest.mark.asyncio
async def test_stop_before_run_does_not_resurrect_loop():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    await arb.stop()                       # stop before run() body executes
    # run() must return on its own (no task.cancel needed) within 1s
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_playback_exception_does_not_break_loop():
    arb = VoiceDuplexArbiter(_BoomPlayback(), config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.submit(SpeechRequest("boom", Priority.PROACTIVE_INFO))
        # play() raises immediately; the loop must survive and settle to LISTENING.
        await _until(lambda: arb.state == VoiceState.LISTENING)
    finally:
        await _shutdown(arb, task)


_FILLER_TEXTS = {"On it.", "Checking.", "Right.", "One sec.", "Hmm."}


@pytest.mark.asyncio
async def test_fire_filler_speaks_a_local_ack():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())
    try:
        arb.fire_filler()
        await _until(lambda: len(fp.played) == 1)
        assert fp.played[0] in _FILLER_TEXTS         # a local filler, no LLM
    finally:
        await _shutdown(arb, task)

@pytest.mark.asyncio
async def test_fillers_rotate_not_repeat_consecutively():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    seen = [arb._next_filler() for _ in range(3)]
    assert len(set(seen)) == 3                        # three distinct in a row

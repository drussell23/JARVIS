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

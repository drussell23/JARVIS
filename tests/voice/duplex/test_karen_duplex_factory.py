"""Tests for build_karen_duplex — the Sprint 3 wiring factory.

Assembles ArbiterTTSPlayback + VoiceDuplexArbiter + VADArbiterBridge into one
lifecycle-managed handle so the live audio bootstrap edit is a single call.
The end-to-end test drives the FULL barge-in chain (submit speech -> Karen
speaks -> VAD fires -> playback preempted -> USER_SPEAKING) with a mock TTS
engine — the only thing not exercised here is the physical mic/speaker.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.comms.duplex.karen_duplex_factory import (
    build_karen_duplex, KarenDuplexHandle,
)
from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig, Priority, VoiceState,
)


class _FakeEngine:
    def __init__(self):
        self.calls = []
        self._gate = None
    async def speak_stream(self, text, *, play_audio=True, cancel_event=None, source=None):
        self.calls.append({"text": text, "cancel_event": cancel_event})
        self._gate = cancel_event or asyncio.Event()
        await self._gate.wait()


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met")


_ON = ArbiterConfig(enabled=True, barge_in_enabled=True, proactive_enabled=True)


def test_build_wires_components_together():
    eng = _FakeEngine()
    h = build_karen_duplex(eng, config=_ON, source="karen")
    assert isinstance(h, KarenDuplexHandle)
    assert h.playback._engine is eng          # playback wraps the engine
    assert h.bridge._arbiter is h.arbiter      # bridge drives the arbiter
    assert h.arbiter._playback is h.playback   # arbiter speaks through the playback


@pytest.mark.asyncio
async def test_start_stop_lifecycle_clean():
    h = build_karen_duplex(_FakeEngine(), config=_ON)
    await h.start()
    await h.stop()                              # must not hang / leak the run task
    assert h._run_task is None or h._run_task.done()


@pytest.mark.asyncio
async def test_end_to_end_barge_in_via_vad():
    eng = _FakeEngine()
    h = build_karen_duplex(eng, config=_ON)
    await h.start()
    try:
        h.submit_speech("a long narration")
        await _until(lambda: h.playback.is_active is True)      # Karen speaking
        h.on_vad(True)                                          # user interrupts
        await _until(lambda: h.arbiter.state == VoiceState.USER_SPEAKING)
        assert eng.calls[0]["cancel_event"].is_set()           # playback cancelled
        h.on_vad(False)                                        # user stops
        await _until(lambda: h.arbiter.state == VoiceState.LISTENING)
    finally:
        await h.stop()


@pytest.mark.asyncio
async def test_disabled_config_is_inert():
    off = ArbiterConfig(enabled=False, barge_in_enabled=False, proactive_enabled=False)
    h = build_karen_duplex(_FakeEngine(), config=off)
    await h.start()
    try:
        h.submit_speech("x")                                   # gated off -> nothing queues
        await asyncio.sleep(0.05)
        assert all(len(q) == 0 for q in h.arbiter._queues.values())
    finally:
        await h.stop()

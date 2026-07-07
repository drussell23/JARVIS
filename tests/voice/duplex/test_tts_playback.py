"""Tests for ArbiterTTSPlayback — the production PlaybackHandle (Sprint 3).

Wraps UnifiedTTSEngine.speak_stream (streaming TTS + cancel_event barge-in) so
the Sprint-1 arbiter drives REAL audio. Unit-tested with a mock engine — the
live mic/speaker loop is a local interactive verification (no audio device in
CI). The adapter owns NO device state (mandate #3): the engine + AudioBus do.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.comms.duplex.protocols import PlaybackHandle
from backend.core.ouroboros.governance.comms.duplex.tts_playback import ArbiterTTSPlayback


class _FakeEngine:
    """Mock UnifiedTTSEngine.speak_stream: awaits the cancel_event so the test
    controls when playback 'finishes' (finish()) or is cut off (preempt)."""

    def __init__(self, boom: bool = False) -> None:
        self.calls = []
        self.boom = boom
        self._gate = None

    async def speak_stream(self, text, *, play_audio=True, cancel_event=None, source=None):
        self.calls.append({"text": text, "source": source, "cancel_event": cancel_event})
        if self.boom:
            raise RuntimeError("tts down")
        self._gate = cancel_event or asyncio.Event()
        await self._gate.wait()

    def finish(self) -> None:
        if self._gate is not None:
            self._gate.set()


def test_implements_playback_handle_protocol():
    assert isinstance(ArbiterTTSPlayback(_FakeEngine()), PlaybackHandle)


@pytest.mark.asyncio
async def test_play_calls_speak_stream_with_cancel_event_and_tracks_active():
    eng = _FakeEngine()
    pb = ArbiterTTSPlayback(eng, source="karen")
    task = asyncio.create_task(pb.play("Fix applied."))
    await asyncio.sleep(0)
    assert pb.is_active is True
    assert eng.calls[0]["text"] == "Fix applied."
    assert eng.calls[0]["source"] == "karen"
    assert eng.calls[0]["cancel_event"] is not None
    eng.finish()
    await task
    assert pb.is_active is False


@pytest.mark.asyncio
async def test_preempt_sets_cancel_event_and_stops_playback():
    eng = _FakeEngine()
    pb = ArbiterTTSPlayback(eng)
    task = asyncio.create_task(pb.play("a long narration"))
    await asyncio.sleep(0)
    assert pb.is_active is True

    pb.preempt()                                   # barge-in
    await task                                      # speak_stream returns (cancel set)
    assert pb.is_active is False
    assert eng.calls[0]["cancel_event"].is_set()


@pytest.mark.asyncio
async def test_preempt_before_play_is_noop():
    pb = ArbiterTTSPlayback(_FakeEngine())
    pb.preempt()                                    # nothing active → no-op, no raise
    assert pb.is_active is False


@pytest.mark.asyncio
async def test_engine_exception_does_not_raise_and_resets_active():
    pb = ArbiterTTSPlayback(_FakeEngine(boom=True))
    await pb.play("x")                              # must NOT raise
    assert pb.is_active is False


@pytest.mark.asyncio
async def test_none_engine_is_silent_noop():
    pb = ArbiterTTSPlayback(None)
    await pb.play("x")
    assert pb.is_active is False
    pb.preempt()                                    # no raise

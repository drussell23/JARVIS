from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge

class _FakeSensor:
    def __init__(self, result="enqueued"): self.result, self.calls = result, []
    async def handle_voice_command(self, payload):
        self.calls.append(payload); return self.result

@pytest.mark.asyncio
async def test_build_transcript_routed_to_sensor():
    s = _FakeSensor()
    br = VoiceBuildBridge(s)
    res = await br.on_final_transcript("add rate limiting to auth", confidence=0.9)
    assert res == "enqueued"
    assert len(s.calls) == 1
    assert s.calls[0].description == "add rate limiting to auth"
    assert s.calls[0].stt_confidence == 0.9

@pytest.mark.asyncio
async def test_chat_transcript_not_routed():
    s = _FakeSensor()
    br = VoiceBuildBridge(s)
    assert await br.on_final_transcript("what time is it") is None
    assert s.calls == []

@pytest.mark.asyncio
async def test_sensor_exception_is_isolated():
    class _Boom:
        async def handle_voice_command(self, p): raise RuntimeError("router down")
    br = VoiceBuildBridge(_Boom())
    assert await br.on_final_transcript("fix the test") is None   # no raise

from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    set_default_voice_sensor,
)

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


@pytest.mark.asyncio
async def test_lazy_sensor_resolution_binds_after_construction():
    """
    Regression: the bridge must NOT snapshot the sensor at __init__ time.
    If IntakeLayerService publishes the default sensor *after* the audio
    pipeline mounts the bridge, a constructor-bound bridge would be
    permanently wired to None (silent dead feature). The bridge must
    resolve the sensor lazily, per-call, via get_default_voice_sensor().
    """
    try:
        br = VoiceBuildBridge()  # no sensor passed — constructed before publish
        fake = _FakeSensor()
        set_default_voice_sensor(fake)  # published AFTER construction

        res = await br.on_final_transcript("add a feature to auth")

        assert res == "enqueued"
        assert len(fake.calls) == 1
        assert fake.calls[0].description == "add a feature to auth"
    finally:
        set_default_voice_sensor(None)

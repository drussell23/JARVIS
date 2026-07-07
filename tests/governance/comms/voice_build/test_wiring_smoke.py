"""Smoke test for Sprint 4 Task 4 wiring: VoiceBuildBridge over the default
voice sensor singleton (as used by the live audio_pipeline_bootstrap mount).

This does not exercise the audio pipeline itself (that's covered by AST
checks + a live manual test) — it proves the decision logic end-to-end: a
default sensor is installed via set_default_voice_sensor, a VoiceBuildBridge
is constructed against get_default_voice_sensor(), and a build utterance
routes through to the sensor while a chat utterance does not.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    get_default_voice_sensor,
    set_default_voice_sensor,
)


class _FakeSensor:
    def __init__(self, result="enqueued"):
        self.result = result
        self.calls = []

    async def handle_voice_command(self, payload):
        self.calls.append(payload)
        return self.result


@pytest.fixture(autouse=True)
def _reset_default_sensor():
    set_default_voice_sensor(None)
    yield
    set_default_voice_sensor(None)


@pytest.mark.asyncio
async def test_default_sensor_wiring_routes_build_utterance_end_to_end():
    fake = _FakeSensor()
    set_default_voice_sensor(fake)

    bridge = VoiceBuildBridge(get_default_voice_sensor())
    result = await bridge.on_final_transcript("add rate limiting to auth", confidence=0.95)

    assert result == "enqueued"
    assert len(fake.calls) == 1
    assert fake.calls[0].description == "add rate limiting to auth"


@pytest.mark.asyncio
async def test_default_sensor_wiring_ignores_chat_utterance():
    fake = _FakeSensor()
    set_default_voice_sensor(fake)

    bridge = VoiceBuildBridge(get_default_voice_sensor())
    result = await bridge.on_final_transcript("what time is it")

    assert result is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_no_default_sensor_installed_is_fault_isolated():
    # get_default_voice_sensor() returns None if nothing was ever installed —
    # the bridge must degrade to a no-op, not raise.
    bridge = VoiceBuildBridge(get_default_voice_sensor())
    result = await bridge.on_final_transcript("fix the failing test")

    assert result is None

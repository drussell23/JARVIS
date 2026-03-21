# tests/governance/test_gap6_voice_stop.py
"""VoiceCommandSensor: optional signal_bus fires on stop/cancel commands."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandSensor, VoiceCommandPayload,
)
from backend.core.ouroboros.governance.user_signal_bus import UserSignalBus


def make_sensor(bus=None):
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    return VoiceCommandSensor(router=router, repo="jarvis", signal_bus=bus)


@pytest.mark.asyncio
async def test_stop_command_fires_request_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="JARVIS stop",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "stopped"
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_cancel_command_fires_request_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="JARVIS cancel that",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.90,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "stopped"
    assert bus.is_stop_requested()


@pytest.mark.asyncio
async def test_normal_command_does_not_fire_stop():
    bus = UserSignalBus()
    sensor = make_sensor(bus=bus)
    payload = VoiceCommandPayload(
        description="fix the import in backend/foo.py",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    await sensor.handle_voice_command(payload)
    assert not bus.is_stop_requested()


@pytest.mark.asyncio
async def test_no_bus_stop_command_returns_error():
    """Without a bus, stop command should return 'error'."""
    sensor = make_sensor(bus=None)
    payload = VoiceCommandPayload(
        description="JARVIS stop",
        target_files=["backend/foo.py"],
        repo="jarvis",
        stt_confidence=0.95,
    )
    result = await sensor.handle_voice_command(payload)
    assert result == "error"

"""VoiceCommandSensor must route envelope.repo from payload.repo, not self._repo."""
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandPayload,
    VoiceCommandSensor,
)


async def test_voice_sensor_uses_payload_repo_not_self_repo():
    """Envelope repo comes from payload.repo, ignoring the sensor's default repo."""
    captured = []

    mock_router = MagicMock()
    async def fake_ingest(envelope):
        captured.append(envelope)
        return "enqueued"
    mock_router.ingest = fake_ingest

    # Sensor constructed with repo="jarvis"
    sensor = VoiceCommandSensor(
        router=mock_router,
        repo="jarvis",
        stt_confidence_threshold=0.5,
    )

    # But payload says repo="prime"
    payload = VoiceCommandPayload(
        description="fix prime test failures",
        target_files=["tests/test_prime.py"],
        repo="prime",
        stt_confidence=0.95,
    )

    await sensor.handle_voice_command(payload)

    assert len(captured) == 1
    assert captured[0].repo == "prime", (
        f"Expected envelope.repo='prime', got '{captured[0].repo}'"
    )


async def test_voice_sensor_self_repo_is_fallback_when_payload_repo_empty():
    """When payload.repo is empty string, fall back to self._repo."""
    captured = []

    mock_router = MagicMock()
    async def fake_ingest(envelope):
        captured.append(envelope)
        return "enqueued"
    mock_router.ingest = fake_ingest

    sensor = VoiceCommandSensor(
        router=mock_router,
        repo="jarvis",
        stt_confidence_threshold=0.5,
    )

    payload = VoiceCommandPayload(
        description="fix something",
        target_files=["tests/test_x.py"],
        repo="",   # empty — should fall back to self._repo
        stt_confidence=0.95,
    )

    await sensor.handle_voice_command(payload)

    assert len(captured) == 1
    assert captured[0].repo == "jarvis"

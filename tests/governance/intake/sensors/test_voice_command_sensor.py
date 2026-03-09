"""Tests for VoiceCommandSensor (Sensor C)."""
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandSensor,
    VoiceCommandPayload,
)


def _payload(
    description="fix the auth module",
    target_files=("backend/core/auth.py",),
    stt_confidence=0.95,
):
    return VoiceCommandPayload(
        description=description,
        target_files=list(target_files),
        repo="jarvis",
        stt_confidence=stt_confidence,
    )


async def test_high_confidence_command_enqueued():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    result = await sensor.handle_voice_command(_payload(stt_confidence=0.95))
    assert result == "enqueued"
    router.ingest.assert_called_once()
    env = router.ingest.call_args.args[0]
    assert env.source == "voice_human"
    assert env.urgency == "critical"
    assert env.requires_human_ack is False


async def test_low_confidence_requires_human_ack():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = VoiceCommandSensor(router=router, repo="jarvis", stt_confidence_threshold=0.82)
    result = await sensor.handle_voice_command(_payload(stt_confidence=0.75))
    assert result == "pending_ack"
    env = router.ingest.call_args.args[0]
    assert env.requires_human_ack is True


async def test_empty_target_files_returns_error():
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    result = await sensor.handle_voice_command(_payload(target_files=[]))
    assert result == "error"
    router.ingest.assert_not_called()


async def test_rate_limit_per_hour_enforced():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis", rate_limit_per_hour=2)
    # Fill up the rate limit
    for _ in range(2):
        await sensor.handle_voice_command(_payload(description=f"cmd {_}"))
    # Third should be rate-limited
    result = await sensor.handle_voice_command(_payload(description="cmd overflow"))
    assert result == "rate_limited"


async def test_causal_chain_source_preserved():
    router = MagicMock()
    router.ingest = AsyncMock(return_value="enqueued")
    sensor = VoiceCommandSensor(router=router, repo="jarvis")
    await sensor.handle_voice_command(_payload())
    env = router.ingest.call_args.args[0]
    # causal_id and signal_id should be set
    assert len(env.causal_id) > 0
    assert len(env.signal_id) > 0
    assert env.causal_id == env.signal_id  # voice: causal = signal (user is the origin)

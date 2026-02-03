# tests/unit/backend/core/test_voice_speaker.py
"""Tests for serialized voice speaker."""

import asyncio
import pytest

from backend.core.voice_orchestrator import SerializedSpeaker


@pytest.mark.asyncio
async def test_speaker_serializes_playback():
    """Test that speaker plays one at a time."""
    play_log = []

    async def mock_tts(text: str):
        play_log.append(f"start:{text}")
        await asyncio.sleep(0.1)  # Simulate playback
        play_log.append(f"end:{text}")

    speaker = SerializedSpeaker(tts_callback=mock_tts)

    # Start two speaks concurrently
    task1 = asyncio.create_task(speaker.speak("First"))
    task2 = asyncio.create_task(speaker.speak("Second"))

    await asyncio.gather(task1, task2)

    # First should complete before second starts
    assert play_log.index("end:First") < play_log.index("start:Second")


@pytest.mark.asyncio
async def test_speaker_stop_interrupts():
    """Test that stop_playback interrupts current playback."""
    started = asyncio.Event()

    async def slow_tts(text: str):
        started.set()
        await asyncio.sleep(10)  # Very long playback

    speaker = SerializedSpeaker(tts_callback=slow_tts)

    # Start playback
    speak_task = asyncio.create_task(speaker.speak("Long message"))

    # Wait for it to start
    await started.wait()

    # Stop it
    stopped = await speaker.stop_playback(timeout_s=0.5)

    # Should have stopped
    assert stopped or speak_task.done()


@pytest.mark.asyncio
async def test_speaker_metrics():
    """Test that speaker tracks metrics."""
    async def mock_tts(text: str):
        await asyncio.sleep(0.01)

    speaker = SerializedSpeaker(tts_callback=mock_tts)

    await speaker.speak("Test 1")
    await speaker.speak("Test 2")

    metrics = speaker.get_metrics()
    assert metrics["spoken_count"] == 2

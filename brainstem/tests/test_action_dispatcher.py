import pytest
from unittest.mock import AsyncMock
from brainstem.action_dispatcher import ActionDispatcher
from brainstem.hud import HUD

@pytest.fixture
def dispatcher():
    d = ActionDispatcher.__new__(ActionDispatcher)
    d.hud = HUD()
    d.ghost_hands = None
    d.tts_speak = AsyncMock(return_value=True)
    d._active_streams = {}
    d.jarvis_cu = None
    return d

@pytest.mark.asyncio
async def test_dispatch_token_starts_stream(dispatcher):
    await dispatcher.dispatch("token", {"command_id": "cmd-1", "token": "Hello", "source_brain": "claude", "sequence": 1})
    assert "cmd-1" in dispatcher._active_streams
    assert dispatcher._active_streams["cmd-1"] == ["Hello"]

@pytest.mark.asyncio
async def test_dispatch_token_appends(dispatcher):
    await dispatcher.dispatch("token", {"command_id": "cmd-1", "token": "Hello ", "source_brain": "claude", "sequence": 1})
    await dispatcher.dispatch("token", {"command_id": "cmd-1", "token": "world", "source_brain": "claude", "sequence": 2})
    assert dispatcher._active_streams["cmd-1"] == ["Hello ", "world"]

@pytest.mark.asyncio
async def test_dispatch_complete_clears_stream(dispatcher):
    dispatcher._active_streams["cmd-1"] = ["Hello"]
    await dispatcher.dispatch("complete", {"command_id": "cmd-1", "source_brain": "claude", "latency_ms": 500})
    assert "cmd-1" not in dispatcher._active_streams

@pytest.mark.asyncio
async def test_dispatch_daemon_informational_speaks(dispatcher):
    await dispatcher.dispatch("daemon", {"command_id": "cmd-1", "narration_text": "Scan complete", "narration_priority": "informational", "source_brain": "doubleword_397b"})
    dispatcher.tts_speak.assert_awaited_once_with("Scan complete")

@pytest.mark.asyncio
async def test_dispatch_daemon_ambient_does_not_speak(dispatcher):
    await dispatcher.dispatch("daemon", {"command_id": "cmd-1", "narration_text": "Queued", "narration_priority": "ambient", "source_brain": "claude"})
    dispatcher.tts_speak.assert_not_awaited()

@pytest.mark.asyncio
async def test_dispatch_unknown_event_ignored(dispatcher):
    await dispatcher.dispatch("unknown_event", {"data": "test"})

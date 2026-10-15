"""JARVIS's voice is heard where the user is: the client that asked.

2026-10-14: every line was spoken on the speakers of the machine JARVIS runs
on, by a player nothing could stop. A user on another device heard it late or
not at all; the browser could not interrupt it; and the browser's own speech
endpoint rendered through macOS ``say`` with a voice named in code, so on
Windows it fell silent and the browser spoke in a voice of its own choosing.
Now a client that plays JARVIS's voice declares it, every line said for its
commands is delivered to it, and the audio it plays comes from the same
engines, order and persona as the host's speech.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.core import command_identity as ci


# ------------------------------------------------------- where a line is heard
@pytest.fixture
def host(monkeypatch):
    """The host's speech path, recorded instead of played."""
    from backend.core import trinity_voice_coordinator as tvc
    from backend.core.supervisor import unified_voice_orchestrator as uvo

    said = []

    async def announce(text, **_kw):
        said.append(text)
        return True, "queued"

    monkeypatch.setattr(tvc, "announce", announce)
    monkeypatch.setattr(uvo.shutil, "which", lambda _name: None)       # the Windows path
    monkeypatch.setattr(uvo, "_voice_muted", lambda: False)
    from backend.core import operator_presence
    monkeypatch.setattr(operator_presence, "presence",
                        lambda: operator_presence.PresenceReading(True, "test", 0.0))
    uvo._global_speech_fingerprints.clear()
    return said


def test_a_line_for_a_clients_command_is_heard_by_that_client(host):
    from backend.core.supervisor.unified_voice_orchestrator import safe_say
    delivered = []

    async def client(text, source):
        delivered.append((text, source))
        return True

    async def run():
        with ci.voice_scope(client):
            return await safe_say("Your interview is at two.", source="command_reply")

    assert asyncio.run(run()) is True
    assert delivered == [("Your interview is at two.", "command_reply")]
    assert host == []                                            # not also on the host


def test_a_line_the_client_cannot_take_is_said_on_the_host(host):
    from backend.core.supervisor.unified_voice_orchestrator import safe_say

    async def gone(_text, _source):
        return False

    async def broken(_text, _source):
        raise ConnectionError("socket closed")

    async def run(sink, text):
        with ci.voice_scope(sink):
            return await safe_say(text, source="command_reply")

    assert asyncio.run(run(gone, "First line.")) is True
    assert asyncio.run(run(broken, "Second line.")) is True
    assert host == ["First line.", "Second line."]               # counted once, still said


def test_without_a_client_voice_the_host_speaks(host):
    from backend.core.supervisor.unified_voice_orchestrator import safe_say
    assert asyncio.run(safe_say("Boot complete.", source="boot")) is True
    assert host == ["Boot complete."]


def test_silence_outranks_the_client(host, monkeypatch):
    from backend.core.supervisor import unified_voice_orchestrator as uvo
    monkeypatch.setattr(uvo, "_voice_muted", lambda: True)
    delivered = []

    async def client(text, source):
        delivered.append(text)
        return True

    async def run():
        with ci.voice_scope(client):
            return await uvo.safe_say("Anything.", source="command_reply")

    assert asyncio.run(run()) is False and delivered == [] and host == []


def test_a_repeated_line_is_not_sent_twice(host):
    from backend.core.supervisor.unified_voice_orchestrator import safe_say
    delivered = []

    async def client(text, _source):
        delivered.append(text)
        return True

    async def run():
        with ci.voice_scope(client):
            await safe_say("On it.", source="command_acknowledgement")
            await safe_say("On it.", source="command_acknowledgement")

    asyncio.run(run())
    assert delivered == ["On it."]


# --------------------------------------------------------- who is the client
def test_only_a_client_that_declared_voice_playback_gets_its_lines(monkeypatch):
    from backend.api import unified_websocket as uw
    manager = uw.UnifiedWebSocketManager.__new__(uw.UnifiedWebSocketManager)
    sent = []

    async def send(client_id, message, mark_failed_on_error=True):
        sent.append((client_id, message))
        return True

    manager._safe_send_json = send
    monkeypatch.setitem(uw.connection_capabilities, "plain", set())
    monkeypatch.setitem(uw.connection_capabilities, "page", {ci.VOICE_PLAYBACK_CAPABILITY})
    assert manager.voice_sink("plain") is None
    assert manager.voice_sink("unknown") is None

    async def run():
        with ci.command_scope("cmd_1"):
            sink = manager.voice_sink("page")
        return await sink("Done.", "command_reply")

    assert asyncio.run(run()) is True
    assert sent == [("page", {"type": "voice_line", "text": "Done.", "source": "command_reply",
                              "requestId": "cmd_1"})]


# ------------------------------------------------------------- the rendering
class _Engine:
    def __init__(self, name, rendered=None, media_type="audio/wav"):
        self.name = name
        self.available = True
        self.last_error = None
        self._rendered = rendered
        self.render_media_type = media_type
        self.calls = 0

    async def render(self, message, personality, timeout=None):
        self.calls += 1
        return self._rendered

    def get_health_score(self):
        return 1.0


def _coordinator(engines, monkeypatch, order):
    from backend.core import trinity_voice_coordinator as tvc
    coordinator = tvc.TrinityVoiceCoordinator.__new__(tvc.TrinityVoiceCoordinator)
    coordinator._engines = engines
    coordinator._personality_profiles = {tvc.VoiceContext.RUNTIME: tvc.VoicePersonality(
        voice_name="", rate=175, pitch=50, volume=1.0, emotion="neutral")}
    monkeypatch.setenv("JARVIS_TTS_ENGINE_ORDER", order)
    return coordinator


def test_a_line_is_rendered_by_the_first_engine_that_can(monkeypatch):
    from backend.core.trinity_voice_coordinator import RenderedSpeech
    rendered = RenderedSpeech(audio=b"ID3...", media_type="audio/mpeg", engine="edge_tts")
    say, edge = _Engine("macos_say"), _Engine("edge_tts", rendered)
    coordinator = _coordinator([edge, say], monkeypatch, "macos_say,edge_tts")
    assert asyncio.run(coordinator.render("Good evening.")) == rendered
    assert say.calls == 1 and edge.calls == 1                    # speaking order, not list order


def test_nothing_renders_an_empty_line(monkeypatch):
    edge = _Engine("edge_tts")
    coordinator = _coordinator([edge], monkeypatch, "edge_tts")
    assert asyncio.run(coordinator.render("   ")) is None and edge.calls == 0


def test_an_engine_renders_through_the_same_synthesis_it_speaks_with(monkeypatch, tmp_path):
    from backend.core import trinity_voice_coordinator as tvc
    from backend.core import voice_mute
    monkeypatch.setattr(voice_mute, "voice_muted", lambda: False)
    engine = tvc.EdgeTTSEngine(tvc.VoiceConfig())
    written = []

    async def render_file(message, personality, timeout, path):
        written.append(message)
        with open(path, "wb") as handle:
            handle.write(b"mp3-bytes")
        return True

    monkeypatch.setattr(engine, "_render_file", render_file)
    personality = tvc.VoicePersonality(voice_name="", rate=175, pitch=50, volume=1.0, emotion="neutral")
    rendered = asyncio.run(engine.render("Hello.", personality, 5.0))
    assert rendered.audio == b"mp3-bytes" and rendered.media_type == "audio/mpeg"
    assert rendered.engine == "edge_tts" and written == ["Hello."]

    played = []

    async def play(path, **_kw):
        with open(path, "rb") as handle:
            played.append(handle.read())

    from backend.core import native_audio_player
    monkeypatch.setattr(native_audio_player, "play_audio_file", play)
    monkeypatch.setattr(tvc, "EDGE_TTS_AVAILABLE", True)
    assert asyncio.run(engine._speak_impl("Hello.", personality, 5.0)) is True
    assert played == [b"mp3-bytes"]


def test_a_failed_render_leaves_no_file_behind(monkeypatch, tmp_path):
    from backend.core import trinity_voice_coordinator as tvc
    from backend.core import voice_mute
    monkeypatch.setattr(voice_mute, "voice_muted", lambda: False)
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    engine = tvc.EdgeTTSEngine(tvc.VoiceConfig())

    async def fails(message, personality, timeout, path):
        raise RuntimeError("edge-tts unreachable")

    monkeypatch.setattr(engine, "_render_file", fails)
    personality = tvc.VoicePersonality(voice_name="", rate=175, pitch=50, volume=1.0, emotion="neutral")
    assert asyncio.run(engine.render("Hello.", personality, 5.0)) is None
    assert "unreachable" in engine.last_error
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------- the speech endpoint
def test_the_browser_gets_the_persona_audio_or_is_told_to_show_the_text(monkeypatch):
    from backend import main
    from backend.core import trinity_voice_coordinator as tvc
    from backend.core.trinity_voice_coordinator import RenderedSpeech
    asked = []

    async def render(text, **_kw):
        asked.append(text)
        return RenderedSpeech(audio=b"mp3", media_type="audio/mpeg", engine="edge_tts")

    monkeypatch.setattr(tvc, "render_speech", render)
    response = asyncio.run(main.audio_speak_post({"text": "It is 2 PM."}))
    assert response.body == b"mp3" and response.media_type == "audio/mpeg"
    assert response.headers["X-TTS-Status"] == "ok" and response.headers["X-TTS-Engine"] == "edge_tts"

    async def nothing(_text, **_kw):
        return None

    monkeypatch.setattr(tvc, "render_speech", nothing)
    silent = asyncio.run(main.audio_speak_post({"text": "It is 2 PM."}))
    assert silent.headers["X-TTS-Status"] == "silent"
    assert asyncio.run(main.audio_speak_post({"text": "  "})).headers["X-TTS-Status"] == "silent"


# ------------------------------------------------- end to end, through the socket
def test_a_page_that_plays_the_voice_receives_its_commands_lines(monkeypatch, host):
    """The real endpoint and manager: subscribe, send a command, hear it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api import unified_websocket as uw

    manager = uw.UnifiedWebSocketManager()

    async def command(client_id, message):
        await uw._say_reply("Your interview is at two.")
        return {"type": "command_response", "response": "Your interview is at two."}

    manager.handlers["probe_command"] = command
    monkeypatch.setattr(uw, "get_ws_manager", lambda: manager)
    app = FastAPI()
    app.add_api_websocket_route("/ws", uw.unified_websocket_endpoint)
    with TestClient(app).websocket_connect("/ws") as ws:
        frames = [ws.receive_json()]                          # connection_established
        ws.send_json({"type": "subscribe", "capabilities": [ci.VOICE_PLAYBACK_CAPABILITY]})
        while frames[-1].get("type") != "subscription_result":
            frames.append(ws.receive_json())
        ws.send_json({"type": "probe_command", "command_id": "cmd_9"})
        received = [ws.receive_json(), ws.receive_json()]
    line = next(f for f in received if f.get("type") == "voice_line")
    assert line == {"type": "voice_line", "text": "Your interview is at two.",
                    "source": "command_reply", "requestId": "cmd_9"}
    assert host == []                                          # not also on the host

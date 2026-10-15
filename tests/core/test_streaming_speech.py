"""JARVIS speaks a reply while it is still being written.

2026-10-15: "do I have any meetings tomorrow?" was silent for ~6 s -- the
reply was written whole, rendered whole, then played; the composer's legacy
client tried an absent local model and DoubleWord before J-Prime on every
call; and J-Prime streaming had never worked (wrong route, wrong field,
httpx branch for an aiohttp session).
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from backend.core import command_identity as ci
from backend.core.speech_segments import SentenceSplitter, sentences, voice_while_writing


async def _stream(*chunks):
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


async def _collect(agen):
    return [item async for item in agen]


# ------------------------------------------------------------ the splitter
def test_tokens_become_sentences_as_they_complete():
    got = asyncio.run(_collect(SentenceSplitter(10).split(_stream(
        "You have three ", "meetings tomorrow. First", " is at 8 AM on Teams. ", "Then Meet."))))
    assert got == ["You have three meetings tomorrow.", "First is at 8 AM on Teams.", "Then Meet."]


def test_finished_text_splits_by_the_same_rule():
    text = "You have three meetings tomorrow. First is at 8 AM. Then Meet."
    assert sentences(text, 10) == asyncio.run(_collect(SentenceSplitter(10).split(_stream(text))))
    assert sentences("Hi. Ok.", 10) == ["Hi. Ok."]          # too short to stand alone
    assert sentences("", 10) == []


def test_the_shortest_sentence_is_a_setting(monkeypatch):
    monkeypatch.setenv("JARVIS_SPEECH_MIN_SENTENCE_CHARS", "2")
    assert sentences("Hi. Ok.") == ["Hi.", "Ok."]
    monkeypatch.setenv("JARVIS_SPEECH_MIN_SENTENCE_CHARS", "lots")
    assert sentences("Hi. Ok.") == ["Hi. Ok."]                 # a typo is the default


def test_the_audio_pipeline_uses_the_same_splitter():
    from backend.audio import conversation_pipeline
    assert conversation_pipeline.SentenceSplitter is SentenceSplitter


# --------------------------------------------------- voiced while written
@pytest.fixture
def quiet(monkeypatch):
    from backend.core.supervisor import unified_voice_orchestrator as uvo
    monkeypatch.setattr(uvo, "_voice_muted", lambda: False)
    uvo._global_speech_fingerprints.clear()


def test_each_sentence_is_said_to_the_client_as_soon_as_it_is_written(quiet):
    said = []

    async def client(text, source):
        said.append((text, source))
        return True

    async def run():
        with ci.voice_scope(client), ci.voiced_scope():
            text = await voice_while_writing(
                _stream("Three meetings tomorrow. ", "The first is on Teams."), source="command_reply")
            return text, ci.voiced_covers(text)

    text, covered = asyncio.run(run())
    assert text == "Three meetings tomorrow. The first is on Teams."
    assert [s for s, _ in said] == ["Three meetings tomorrow.", "The first is on Teams."]
    assert covered                                   # the command's end will not say it again


def test_an_answer_asked_for_twice_is_said_twice(quiet):
    said = []

    async def client(text, source):
        said.append(text)
        return True

    async def run():
        with ci.voice_scope(client), ci.voiced_scope():
            for _ in range(2):
                await voice_while_writing(_stream("You have three meetings tomorrow."), source="command_reply")

    asyncio.run(run())
    assert said == ["You have three meetings tomorrow."] * 2


def test_without_a_client_voice_the_text_is_only_collected(quiet):
    async def run():
        with ci.voiced_scope():
            text = await voice_while_writing(_stream("One sentence. ", "Two sentences."), source="x")
            return text, ci.voiced_covers(text)

    assert asyncio.run(run()) == ("One sentence. Two sentences.", False)


def test_a_reply_not_voiced_whole_is_not_covered():
    async def run():
        with ci.voiced_scope():
            ci.note_voiced("Three meetings tomorrow.")
            return (ci.voiced_covers("Three meetings tomorrow.  "),
                    ci.voiced_covers("Three meetings tomorrow. And a fourth."))

    assert asyncio.run(run()) == (True, False)
    assert ci.voiced_covers("anything") is False      # outside a command


# ------------------------------------------------------ J-Prime streaming
def test_the_stream_is_read_as_openai_chunks_to_done():
    from backend.core.prime_client import PrimeClient

    async def lines():
        for event in ({"choices": [{"delta": {"role": "assistant"}}]},
                      {"choices": [{"delta": {"content": "Three "}}]},
                      {"choices": [{"delta": {"content": "meetings."}}]}):
            yield f"data: {json.dumps(event)}\n".encode()
        yield b": keep-alive\n"
        yield b"data: [DONE]\n"
        yield b'data: {"choices": [{"delta": {"content": "after done"}}]}\n'

    client = PrimeClient.__new__(PrimeClient)
    assert asyncio.run(_collect(client._stream_pieces(lines()))) == ["Three ", "meetings."]


def test_an_error_event_raises_instead_of_being_spoken():
    from backend.core.prime_client import PrimeClient

    async def lines():
        yield b'data: {"error": {"message": "model unloaded"}}\n'

    client = PrimeClient.__new__(PrimeClient)
    with pytest.raises(RuntimeError, match="model unloaded"):
        asyncio.run(_collect(client._stream_pieces(lines())))


def test_a_stream_and_a_whole_answer_use_one_endpoint():
    from backend.core.prime_client import PrimeClient
    client = PrimeClient.__new__(PrimeClient)
    client._config = types.SimpleNamespace(base_url="http://127.0.0.1:8000/v1")
    assert client._chat_completions_url() == "http://127.0.0.1:8000/v1/chat/completions"


def test_a_stream_resolves_an_unknown_model_status_first(monkeypatch):
    """A fresh router's stream fell straight to the cloud lane: "unknown"
    read as "down"."""
    from backend.core import prime_router as pr

    class _Status:
        name = "UNKNOWN"

    async def chunks(**_kw):
        yield "ready"

    client = types.SimpleNamespace(_status=_Status(), is_available=False, generate_stream=chunks)

    async def health():
        client._status = types.SimpleNamespace(name="AVAILABLE")
        client.is_available = True

    client._check_health = health
    router = pr.PrimeRouter.__new__(pr.PrimeRouter)
    router._initialized = True
    router._prime_client = client
    assert asyncio.run(_collect(router.generate_stream(prompt="hi", deadline=None))) == ["ready"]


# --------------------------------------------------- one sentence per frame
def test_a_whole_reply_reaches_the_client_a_sentence_per_frame(monkeypatch):
    from backend.api import unified_websocket as uw
    manager = uw.UnifiedWebSocketManager.__new__(uw.UnifiedWebSocketManager)
    sent = []

    async def send(client_id, message, mark_failed_on_error=True):
        sent.append(message["text"])
        return True

    manager._safe_send_json = send
    monkeypatch.setitem(uw.connection_capabilities, "page", {ci.VOICE_PLAYBACK_CAPABILITY})
    sink = manager.voice_sink("page")
    assert asyncio.run(sink("You have three meetings tomorrow. The first is on Teams.", "command_reply"))
    assert sent == ["You have three meetings tomorrow.", "The first is on Teams."]

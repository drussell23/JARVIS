"""One voice, said like a person, and quickly.

2026-10-14 10:29, "search for dogs": the reply was said twice at once (JARVIS's
voice and the browser, from the same response) over a fixed "On it, Sir.
Synthesizing a solution now."; it was the log line "Opened https://..."; it
took 20 s (J-Prime's model unloaded) and ran twice (the client re-sent it by
REST after 10 s of silence).
"""
from __future__ import annotations

import asyncio
import types

import pytest


# -- one voice --------------------------------------------------------------------
def test_a_task_jarvis_voices_is_marked_spoken_for_the_client():
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    done = types.SimpleNamespace(summary="I searched for dogs.", success=True, acted=True, spoken=True)
    reply = UnifiedCommandProcessor._runtime_task_response(done)
    assert reply["spoken"] is True and reply["response"] == "I searched for dogs."


def test_a_reply_not_yet_voiced_is_said_once_in_jarvis_voice(monkeypatch):
    from backend.api import unified_websocket as ws
    from backend.core.supervisor import unified_voice_orchestrator as uvo
    said = []

    async def fake_say(text, **kw):
        said.append((text, kw.get("source")))
        return True

    monkeypatch.setattr(uvo, "safe_say", fake_say)

    async def go():
        assert await ws._say_reply("The answer.\n\nSources: ...") is True
        await asyncio.sleep(0)
    asyncio.run(go())
    assert said == [("The answer.", "command_reply")]


def test_an_acknowledgement_is_said_only_when_the_command_is_slow(monkeypatch):
    from backend.api import unified_websocket as ws
    from backend.core.supervisor import unified_voice_orchestrator as uvo
    said = []

    async def fake_say(text, **kw):
        said.append(text)
        return True

    monkeypatch.setattr(uvo, "safe_say", fake_say)
    monkeypatch.setenv("JARVIS_ACK_AFTER_S", "0.2")

    async def answered_quickly():
        task = asyncio.create_task(ws._acknowledge_if_slow())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.3)

    async def slow():
        await ws._acknowledge_if_slow()

    asyncio.run(answered_quickly())
    assert said == []
    asyncio.run(slow())
    assert len(said) == 1 and said[0] in ws._ACKNOWLEDGEMENTS


def test_the_spoken_form_is_the_first_paragraph_within_the_limit(monkeypatch):
    from backend.core.supervisor.unified_voice_orchestrator import spoken_form
    monkeypatch.setenv("JARVIS_SPOKEN_SUMMARY_MAX_CHARS", "20")
    assert spoken_form("short one\n\nsources") == "short one"
    assert spoken_form("a much longer sentence that keeps going") == "a much longer…"


# -- said like a person -------------------------------------------------------------
def _orch(content=None, raises=None):
    from backend.core.runtime_task_orchestrator import RuntimeTaskOrchestrator

    class Prime:
        async def generate(self, prompt, **_):
            if raises:
                raise raises
            assert "still open" in prompt or "Opened" in prompt   # the facts reach the Mind
            return types.SimpleNamespace(content=content)
    return RuntimeTaskOrchestrator(prime_client=Prime())


def test_a_desktop_outcome_is_phrased_for_the_user():
    orch = _orch('"I opened the Google results for dogs."')
    said = asyncio.run(orch._phrase_for_user("search for dogs", "Opened https://www.google.com/search?q=dogs"))
    assert said == "I opened the Google results for dogs."


def test_without_the_mind_the_true_outcome_stands():
    orch = _orch(raises=RuntimeError("down"))
    outcome = "Notepad is still open -- it may be asking whether to save."
    assert asyncio.run(orch._phrase_for_user("close notepad", outcome)) == outcome


def test_only_desktop_work_is_rephrased_not_answers():
    from backend.core.runtime_task_orchestrator import RuntimeTaskOrchestrator, StepResolution, TaskResolution
    acted = [StepResolution("g", TaskResolution.DESKTOP_ACTION, "close", "close_for_user", False)]
    answered = [StepResolution("g", TaskResolution.EXISTING_AGENT, "web_search_agent", "web_research", False)]
    assert RuntimeTaskOrchestrator._worked_on_desktop(acted) is True
    assert RuntimeTaskOrchestrator._worked_on_desktop(answered) is False


# -- quickly: the Mind is resident before the user's first command ----------------
@pytest.mark.parametrize("loaded_before,load_status,expect_post,expect", [
    (True, 200, False, True),      # already resident: nothing to do
    (False, 200, True, True),      # loads it
    (False, 500, True, False),     # the engine refused: the first request will load it
])
def test_the_text_model_is_made_resident(monkeypatch, loaded_before, load_status, expect_post, expect):
    from backend.core import jarvis_prime_client as jpc

    client = jpc.JarvisPrimeClient.__new__(jpc.JarvisPrimeClient)
    client.config = types.SimpleNamespace(local_host="127.0.0.1", local_port=8000)
    state = {"loaded": loaded_before, "posted": False}

    async def health():
        return types.SimpleNamespace(available=True, model_loaded=state["loaded"])

    class Http:
        async def post(self, url, json=None, timeout=None):
            state["posted"] = url.endswith("/api/generate") and json == {}
            if load_status == 200:
                state["loaded"] = True
            return types.SimpleNamespace(status_code=load_status)

    async def http():
        return Http()

    client._check_local_health = health
    client._get_http_client = http
    assert asyncio.run(client.ensure_text_model_resident()) is expect
    assert state["posted"] is expect_post

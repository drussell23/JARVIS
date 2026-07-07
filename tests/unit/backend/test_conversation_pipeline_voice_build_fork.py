"""
Sprint 4 review fix — the voice->build fork must be genuinely
fire-and-forget: the spoken LLM reply must not block on the fork
coroutine's completion (e.g. a slow intake `ingest` doing WAL writes).

Regression for: conversation_pipeline.py previously did
`await self._on_turn_text(user_text)` inline, serializing the reply
behind the full voice->build route. The fix forks it via
asyncio.create_task, retaining a reference in self._turn_forks so the
task isn't GC'd mid-flight, with a done-callback that discards it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.audio.conversation_pipeline import ConversationPipeline


class _IntentClassifier:
    def predict_intent(self, _text: str):
        return {"intent": "conversation", "confidence": 0.95, "source": "test"}


@pytest.mark.asyncio
async def test_on_turn_text_fork_does_not_block_llm_reply():
    pipeline = ConversationPipeline(intent_classifier=_IntentClassifier())

    order: list = []
    fork_release = asyncio.Event()

    async def slow_fork(_text):
        order.append("fork_start")
        await fork_release.wait()  # would hang forever if awaited inline
        order.append("fork_end")

    async def fast_reply(**_kwargs):
        order.append("reply")

    pipeline._on_turn_text = slow_fork
    pipeline._generate_and_speak_response = fast_reply

    calls = {"n": 0}

    async def listen_once():
        calls["n"] += 1
        if calls["n"] == 1:
            return "hello there"
        pipeline._running = False
        return None

    pipeline._listen_for_turn = listen_once
    pipeline._is_self_voice_echo = lambda _text: _false()
    pipeline._classify_turn_intent = lambda _text: _intent_decision()

    await pipeline.start_session()
    pipeline._running = True

    # Bound the loop — it must complete without the fork ever finishing.
    await asyncio.wait_for(pipeline._conversation_loop(), timeout=5)

    # Core proof: the loop completed at all (bounded by wait_for(timeout=5))
    # without the fork ever finishing — "fork_end" is only appended after
    # fork_release.set(), which hasn't happened yet. A blocking-await
    # implementation would have hung here until the 5s timeout fired
    # (proven by the RED run against the pre-fix code).
    assert "reply" in order
    assert "fork_end" not in order, (
        "the loop must not have waited for the fork to finish"
    )
    assert len(pipeline._turn_forks) == 1, "fork task must be retained, not fire-and-lost"

    fork_release.set()
    # Let the retained task actually finish and drain via its done-callback.
    pending = next(iter(pipeline._turn_forks))
    await asyncio.wait_for(pending, timeout=5)
    await asyncio.sleep(0)  # allow done_callback to run
    assert order.count("reply") == 1
    assert order.count("fork_start") == 1
    assert order.count("fork_end") == 1
    assert len(pipeline._turn_forks) == 0, "done-callback must discard the finished task"


async def _false():
    return False


async def _intent_decision():
    return {"route": "discuss", "intent": "conversation", "confidence": 0.95}

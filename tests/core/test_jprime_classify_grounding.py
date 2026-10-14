"""Situational context reaches J-Prime as grounding, never as the user's words.

Merged into the user turn ("[Context: User focus: deep_work]\\n<query>"), the
model answered the label itself ("since you mentioned deep_work...").
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.core.jarvis_prime_client import JarvisPrimeClient


def _client_capturing(calls):
    client = JarvisPrimeClient.__new__(JarvisPrimeClient)

    async def fake_complete(**kw):
        calls.append(kw)
        return SimpleNamespace(success=True, content="Tokyo.", metadata={}, error=None,
                               backend="local", latency_ms=1.0, tokens_used=1)

    client.complete = fake_complete
    return client


def _classify(client, **kw):
    try:
        return asyncio.run(client.classify_and_complete(query="what is the capital of Japan", **kw))
    except Exception:
        return None  # response post-processing is not under test here


def test_context_travels_in_the_system_turn_and_the_user_turn_is_verbatim():
    calls = []
    _classify(_client_capturing(calls),
              context_metadata={"user_focus": "deep_work", "active_app": "Code"})
    msgs = calls[0]["messages"]
    assert [m.role for m in msgs] == ["system", "user"]
    assert msgs[-1].content == "what is the capital of Japan"
    assert "deep_work" in msgs[0].content and "never comment" in msgs[0].content
    assert calls[0]["system_prompt"] is None          # carried once, by the turn


def test_no_context_sends_only_the_user_turn():
    calls = []
    _classify(_client_capturing(calls))
    assert [m.role for m in calls[0]["messages"]] == ["user"]

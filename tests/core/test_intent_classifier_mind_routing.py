"""The Mind classifies from the live agent catalogue; keywords never act."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core.intent_classifier import (
    ClassificationResult, CommandIntent, IntentClassifier,
)


def _run(coro):
    return asyncio.run(coro)


def test_prompt_carries_the_live_catalogue_not_a_fixed_category_list():
    prompt = IntentClassifier._classify_system_prompt("- web_search_agent — research [web_research]")
    assert "web_search_agent" in prompt and '"capability"' in prompt
    assert "action_category" not in prompt


def test_budget_adds_the_reload_only_when_the_text_model_is_out(monkeypatch):
    monkeypatch.setenv("JARVIS_CLASSIFY_DEADLINE_S", "8")
    client = SimpleNamespace(cold_load_allowance_s=AsyncMock(return_value=90.0))
    with patch("backend.core.jarvis_prime_client.get_jarvis_prime_client", return_value=client):
        assert _run(IntentClassifier._classify_budget_s()) == 98.0
    client.cold_load_allowance_s.return_value = 0.0
    with patch("backend.core.jarvis_prime_client.get_jarvis_prime_client", return_value=client):
        assert _run(IntentClassifier._classify_budget_s()) == 8.0


def test_a_named_capability_makes_the_request_an_action():
    clf = IntentClassifier()
    router = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        content='{"intent": "query", "confidence": 0.9, "capability": "web_research", '
                '"search_query": "nvidia news"}',
        source="local_prime", latency_ms=5.0)))
    with patch("backend.core.prime_router.get_prime_router", AsyncMock(return_value=router)), \
         patch("backend.core.agent_bindings.capability_catalogue", return_value="- x"), \
         patch.object(IntentClassifier, "_classify_budget_s", AsyncMock(return_value=5.0)):
        r = _run(clf.classify_async("look up the latest news about NVIDIA"))
    assert r.intent == CommandIntent.ACTION and r.capability == "web_research"
    assert r.decided_by_mind
    assert r.to_task_context(source="t")["capability"] == "web_research"


def test_when_the_mind_does_not_answer_the_heuristic_may_not_act():
    clf = IntentClassifier()
    router = SimpleNamespace(generate=AsyncMock(side_effect=asyncio.TimeoutError()))
    with patch("backend.core.prime_router.get_prime_router", AsyncMock(return_value=router)), \
         patch("backend.core.agent_bindings.capability_catalogue", return_value="- x"), \
         patch.object(IntentClassifier, "_classify_budget_s", AsyncMock(return_value=5.0)):
        r = _run(clf.classify_async("search youtube for cats"))
    assert not r.decided_by_mind


def test_task_context_is_one_mapping():
    r = ClassificationResult(intent=CommandIntent.ACTION, confidence=0.9, matched_signal="ai",
                             action_category="", capability="", search_query="dogs",
                             classification_source="jprime")
    ctx = r.to_task_context(client_id="c1")
    assert ctx["intent"] == "action" and ctx["search_query"] == "dogs" and ctx["client_id"] == "c1"


def test_a_question_answered_by_looking_wins_over_a_capability_tag():
    def r(**kw):
        base = dict(intent=CommandIntent.ACTION, confidence=0.9, matched_signal="ai",
                    action_category="visual_monitor", capability="visual_monitor",
                    classification_source="jprime")
        base.update(kw)
        return ClassificationResult(**base)

    assert r(needs_screen=True).answers_by_looking                       # "can you see my screen?"
    assert not r(needs_screen=True, requires_vision=True).answers_by_looking  # operate the UI
    assert not r(needs_screen=False).answers_by_looking
    assert not r(needs_screen=True, classification_source="sync_heuristic").answers_by_looking

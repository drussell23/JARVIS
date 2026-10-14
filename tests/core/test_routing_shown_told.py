"""A request to be SHOWN something is carried out on screen, in the app named.

2026-10-14 09:46: "can you search for dogs on firefox?" -- the Mind reasoned
"User wants something SHOWN - search results for 'dogs' in Firefox" and
still chose web_search_agent (it shares the word "search"), which reported an
answer back. And had it routed to the screen, the URL would have opened in
the default browser: the app the user named was dropped.
"""
from __future__ import annotations

import asyncio
import json
import types

from backend.core import agent_bindings as ab


def _reload(monkeypatch, tmp_path, overrides=None):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(overrides or []), encoding="utf-8")
    monkeypatch.setenv("JARVIS_AGENT_BINDINGS_PATH", str(path))
    ab.reload_bindings()


def test_every_user_facing_entry_says_what_the_user_gets(monkeypatch, tmp_path):
    _reload(monkeypatch, tmp_path)
    try:
        lines = {line.split(" ")[1]: line for line in ab.capability_catalogue().splitlines()}
        assert "SHOWN" in lines["desktop_open"] and "TOLD" in lines["web_search_agent"]
        assert "TOLD" in lines["screen_question"]
    finally:
        ab.reload_bindings()


def test_a_user_binding_declares_its_own_delivery(monkeypatch, tmp_path):
    _reload(monkeypatch, tmp_path, [{"agent_id": "web_search_agent", "delivers": "shown"}])
    try:
        assert ab.get_agent_bindings()["web_search_agent"].delivers == "shown"
        line = next(l for l in ab.capability_catalogue().splitlines() if "web_search_agent" in l)
        assert "SHOWN" in line
    finally:
        monkeypatch.delenv("JARVIS_AGENT_BINDINGS_PATH")
        ab.reload_bindings()


def test_the_classifier_is_told_to_choose_by_delivery_not_shared_words():
    from backend.core.intent_classifier import IntentClassifier
    prompt = IntentClassifier._classify_system_prompt("- x")
    assert "an entry marked SHOWN for SHOWN" in prompt
    assert "A word the command shares with a capability name is not a reason" in prompt


def test_a_url_opens_in_the_browser_the_user_named(monkeypatch):
    from backend.core import desktop_open
    from backend.core import runtime_task_orchestrator as rto

    used = {}

    async def fake_open_url(url, *, browser=None):
        used["url"], used["browser"] = url, browser
        return types.SimpleNamespace(ok=True, in_front=True, resolved=url, method="test",
                                     describe=lambda: f"opened {url}")

    monkeypatch.setattr(desktop_open, "open_url", fake_open_url)
    orch = rto.RuntimeTaskOrchestrator()

    async def decide(goal, step):
        return {"open": "url", "url": "https://www.google.com/search?q=dogs", "completes_goal": True}
    orch._resolve_open_target = decide

    asyncio.run(orch._dispatch_to_vision("search for dogs on firefox", {"target_app": "firefox"}))
    assert used["browser"] == "firefox"

    monkeypatch.delenv("JARVIS_DEFAULT_BROWSER", raising=False)
    asyncio.run(orch._dispatch_to_vision("search for dogs", {}))
    assert used["browser"] == desktop_open.preferred_browser()

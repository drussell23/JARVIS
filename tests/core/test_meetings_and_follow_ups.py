"""JARVIS says what kind of meeting each event is, and follows a conversation.

2026-10-14: asked whether a meeting was on Zoom, Teams or Meet, JARVIS
guessed from the title -- only ``hangoutLink`` was kept, so a Teams event lost
its link and no event said where it was held. Asked a follow-up ("which one
is on teams?"), it answered by looking at the screen: the conversation history
had no writer, and every layer that decides something about a command saw
only its words. A restating pass in between was lossy (the local 30B dropped
"tomorrow", or wrote today's date into an unrelated email question); the
exchange itself now reaches the classifier, the workspace detector and the
composer through one context channel.
"""
from __future__ import annotations

import asyncio
import time
import types
from datetime import datetime

import pytest

from backend.core import command_identity as ci


# ---------------------------------------------------------- meeting platform
def test_the_calendar_names_the_conference_and_its_video_link():
    from backend.neural_mesh.agents.google_workspace_agent import event_meeting
    event = {"conferenceData": {
        "conferenceSolution": {"name": "Microsoft Teams Meeting"},
        "entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                        {"entryPointType": "video", "uri": "https://teams.microsoft.com/l/x"}]}}
    assert event_meeting(event) == {"meeting_platform": "Microsoft Teams Meeting",
                                    "meeting_link": "https://teams.microsoft.com/l/x"}


def test_a_pasted_invite_is_named_by_its_link_host():
    from backend.neural_mesh.agents.google_workspace_agent import event_meeting
    pasted = {"location": "Join: https://us02web.zoom.us/j/123?pwd=abc."}
    assert event_meeting(pasted) == {"meeting_platform": "us02web.zoom.us",
                                     "meeting_link": "https://us02web.zoom.us/j/123?pwd=abc"}
    meet = {"hangoutLink": "https://meet.google.com/abc-defg-hij"}
    assert event_meeting(meet)["meeting_platform"] == "meet.google.com"


def test_an_event_held_nowhere_online_says_nothing_about_where():
    from backend.neural_mesh.agents.google_workspace_agent import event_meeting
    assert event_meeting({"location": "Conference Room B"}) == {"meeting_platform": "",
                                                                "meeting_link": ""}


def test_an_event_time_is_one_phrase():
    """Two 12:30 events with separate start/end fields had their ends swapped."""
    from backend.api.unified_command_processor import _spoken_local_span
    start = datetime(2026, 10, 14, 12, 30).astimezone().isoformat()
    end = datetime(2026, 10, 14, 13, 30).astimezone().isoformat()
    assert _spoken_local_span(start, end) == "Wednesday 12:30 PM to 1:30 PM"
    overnight = datetime(2026, 10, 15, 1, 0).astimezone().isoformat()
    assert _spoken_local_span(start, overnight) == "Wednesday 12:30 PM to Thursday 1:00 AM"
    assert _spoken_local_span("2026-10-16", "2026-10-17") == "Friday (all day)"


def test_the_reflex_reads_how_many_days_a_request_covers():
    import re
    from backend.core.workspace_routing_intelligence import WorkspaceIntent, WorkspaceIntentDetector
    pattern = next(p for p in WorkspaceIntentDetector()._patterns
                   if p.intent is WorkspaceIntent.CHECK_CALENDAR)
    found = re.search(pattern.entity_patterns["number_of_days"], "meetings over the next 3 days")
    assert found and found.group(1) == "3"


def test_the_composer_is_told_where_each_meeting_is_held(monkeypatch):
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    proc = UnifiedCommandProcessor.__new__(UnifiedCommandProcessor)
    start = datetime(2026, 10, 15, 8, 0).astimezone().isoformat()
    end = datetime(2026, 10, 15, 8, 30).astimezone().isoformat()
    outcome = {"action": "check_calendar", "status": "success", "result": {"events": [
        {"summary": "Screening", "start": start, "end": end,
         "meeting_platform": "Microsoft Teams Meeting", "meeting_link": "https://teams.microsoft.com/l/x"},
        {"summary": "Lunch", "start": start, "end": end}]}}
    payload = proc._build_compose_payload(
        artifacts={}, ordered_outcomes=[outcome], command_id="c", workspace_action="check_calendar",
        confidence=0.9, deterministic_fallback="You have 2 events.", deadline_remaining=10.0)
    teams, lunch = payload.artifacts["events"]
    assert teams["meeting_platform"] == "Microsoft Teams Meeting" and teams["has_join_link"]
    assert teams["when"] == "Thursday 8:00 AM to 8:30 AM"
    assert lunch["meeting_platform"] == "" and not lunch["has_join_link"]
    assert payload.artifacts["event_count"] == 2


# --------------------------------------------------------------- conversation
def _context():
    from backend.api.unified_command_processor import UnifiedContext
    return UnifiedContext(conversation_history=[])


def test_each_exchange_is_kept_for_the_next_command():
    context = _context()
    context.record_turn("user", "do I have any meetings tomorrow?")
    context.record_turn("assistant", "Three: a screening at 8 on Teams, ...")
    context.record_turn("assistant", "   ")                              # nothing said
    assert context.recent_turns() == [
        {"role": "user", "content": "do I have any meetings tomorrow?"},
        {"role": "assistant", "content": "Three: a screening at 8 on Teams, ..."}]


def test_the_history_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATION_TURNS", "3")
    context = _context()
    for n in range(5):
        context.record_turn("user", f"q{n}")
    assert [t["content"] for t in context.recent_turns()] == ["q2", "q3", "q4"]


def test_a_question_after_a_pause_starts_a_new_conversation(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATION_IDLE_S", "600")
    monkeypatch.setenv("JARVIS_CONVERSATION_TURNS", "not a number")   # a typo is the default
    context = _context()
    context.record_turn("user", "an old topic")
    context.conversation_history[-1]["timestamp"] = time.time() - 3600
    context.record_turn("user", "the current one")
    assert [t["content"] for t in context.recent_turns()] == ["the current one"]


def test_the_conversation_reaches_every_layer_through_one_scope():
    assert ci.conversation_text() == "" and ci.conversation_preamble() == ""
    turns = [{"role": "user", "content": "meetings tomorrow?"},
             {"role": "assistant", "content": "Three."}]

    async def inside():
        await asyncio.sleep(0)
        return ci.conversation_text()

    with ci.conversation_scope(turns):
        assert asyncio.run(inside()) == "- User: meetings tomorrow?\n- JARVIS: Three."
        assert "meetings tomorrow?" in ci.conversation_preamble()
    assert ci.conversation_text() == ""


class _Recorder:
    def __init__(self, content):
        self.prompts = []
        self.content = content

    async def generate(self, **kw):
        self.prompts.append(kw["prompt"])
        return types.SimpleNamespace(content=self.content, source="local_prime", latency_ms=1.0)


def test_the_classifier_reads_a_follow_up_with_its_conversation(monkeypatch):
    from backend.core import intent_classifier as icm
    from backend.core import prime_router as pr
    recorder = _Recorder('{"answer_from_own_knowledge": false, "capability": "mail", '
                         '"confidence": 0.9}')

    async def router():
        return recorder
    monkeypatch.setattr(pr, "get_prime_router", router)
    monkeypatch.setattr(pr, "require_generation", lambda r: r.content)
    classifier = icm.IntentClassifier()

    async def budget():
        return 5.0
    monkeypatch.setattr(classifier, "_classify_budget_s", budget)
    with ci.conversation_scope([{"role": "user", "content": "do I have any meetings tomorrow?"}]):
        asyncio.run(classifier.classify_async("which one is on teams?"))
    assert "do I have any meetings tomorrow?" in recorder.prompts[0]
    assert recorder.prompts[0].rstrip().endswith('"which one is on teams?"')


def test_the_workspace_detector_reads_the_day_from_the_conversation(monkeypatch):
    from backend.core import prime_router as pr
    from backend.core.workspace_routing_intelligence import WorkspaceIntentDetector
    recorder = _Recorder('{"intent": "check_calendar", "entities": {"date": "2026-10-15"}, '
                         '"confidence": 0.9}')

    async def router():
        return recorder
    monkeypatch.setattr(pr, "get_prime_router", router)
    with ci.conversation_scope([{"role": "user", "content": "do I have any meetings tomorrow?"}]):
        result = asyncio.run(WorkspaceIntentDetector().detect("and is the vals one on google meet?"))
    assert "do I have any meetings tomorrow?" in recorder.prompts[0]
    assert result.entities == {"date": "2026-10-15"}
    # A command that starts a conversation is asked about on its own.
    asyncio.run(WorkspaceIntentDetector().detect("what's on my calendar today?"))
    assert "conversation" not in recorder.prompts[-1].lower()


def test_the_composer_resolves_references_but_answers_from_the_data():
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    prompt = UnifiedCommandProcessor._build_compose_system_prompt()
    assert "<earlier_conversation>" in prompt and "answer from <workspace_data>" in prompt


@pytest.mark.parametrize("said", ["", "   "])
def test_an_empty_turn_never_enters_a_prompt(said):
    assert ci.conversation_text([{"role": "user", "content": said}]) == ""

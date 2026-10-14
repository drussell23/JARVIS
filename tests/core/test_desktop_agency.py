"""JARVIS acts on the desktop by reasoning, not by remembered commands.

Close what is named, open real folders and files, run a chain of requests in
order -- each decided by the Mind against facts of this machine (open windows,
the user's folders, the browsers installed), with the Mind holding its own
choice of means to the goal it stated.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from backend.core import agent_bindings as ab
from backend.core import intent_classifier as ic


# -- no remembered commands --------------------------------------------------------
def test_the_classifier_prompt_holds_principles_not_test_commands():
    prompt = ic.IntentClassifier._classify_system_prompt("- x")
    for phrase in ("dogs", "cats", "firefox", "notepad", "downloads"):
        assert phrase not in prompt.lower(), phrase


# -- the Mind's own consistency ------------------------------------------------------
@pytest.mark.parametrize("result,contradiction", [
    ({"wants": "shown", "capability": "web_search_agent"}, True),     # reasons SHOWN, picks TOLD
    ({"form": "instruction", "wants": "told", "capability": "web_search_agent"}, True),
    ({"form": "question", "wants": "told", "capability": "web_search_agent"}, False),
    ({"wants": "shown", "capability": "desktop_open"}, False),
    ({"wants": "done", "capability": "desktop_close"}, False),
])
def test_a_contradiction_is_named_for_the_mind_to_reconsider(result, contradiction):
    assert bool(ic._delivery_mismatch(result)) is contradiction


def test_the_stated_goal_outlasts_a_means_that_cannot_deliver_it():
    open_route = ab.route_delivering("shown")
    assert ic._serve_stated_goal({"wants": "shown", "capability": "web_search_agent"})["capability"] == open_route
    # A goal of being told keeps the research agent; agreeing answers are untouched.
    for kept in ({"wants": "told", "capability": "web_search_agent"},
                 {"wants": "shown", "capability": "desktop_open"}):
        assert ic._serve_stated_goal(kept) == kept


def test_close_is_offered_as_something_done():
    line = next(l for l in ab.capability_catalogue().splitlines() if l.startswith("- desktop_close"))
    assert "DONE" in line and ab.route_for_capability("close_for_user") == "close"


# -- chains --------------------------------------------------------------------------
def test_chain_steps_are_plain_requests_two_or_more():
    assert ic._chain(["close it", "open that"]) == ("close it", "open that")
    assert ic._chain(["one"]) == ()
    assert ic._chain([{"capability": "x"}, {"capability": "y"}]) == ()   # objects are not requests
    assert ic._chain("not a list") == ()


def test_each_request_of_a_chain_is_classified_and_runs_after_the_last(monkeypatch):
    from backend.core import runtime_task_orchestrator as rto

    decided = {"first thing": "desktop_close", "second thing": "desktop_open"}

    class FakeClassifier:
        async def classify_async(self, request):
            return ic.ClassificationResult(
                intent=ic.CommandIntent.ACTION, confidence=0.9, matched_signal="t",
                action_category=decided[request], capability=decided[request],
                classification_source="jprime")

    monkeypatch.setattr(ic, "get_intent_classifier", lambda: FakeClassifier())
    orch = rto.RuntimeTaskOrchestrator()
    steps, how = asyncio.run(orch._decompose("do both", {"steps": ["first thing", "second thing"]}))
    assert how == "chain of 2 requests"
    assert [s["capability"] for s in steps] == ["desktop_close", "desktop_open"]
    assert steps[0]["dependencies"] == [] and steps[1]["dependencies"] == ["0"]


# -- close ---------------------------------------------------------------------------
def _windows():
    from backend.core.desktop_close import _Window
    return [
        _Window(1, "dogs - Google Search - Mozilla Firefox", "firefox.exe", 10, "Firefox", {"firefox", "Firefox"}),
        _Window(2, "notes.txt - Notepad", "notepad.exe", 11, "Notepad", {"notepad", "Notepad"}),
        _Window(3, "Mozilla Firefox Private Browsing", "firefox.exe", 10, "Firefox", {"firefox", "Firefox"}),
        _Window(4, "Downloads", "explorer.exe", 12, "Windows Explorer", {"explorer", "Windows Explorer"}),
    ]


def test_a_program_name_closes_all_its_windows_a_title_closes_one():
    from backend.core.desktop_close import _choose_windows
    chosen, how = _choose_windows("firefox", _windows())
    assert how == "program" and sorted(w.hwnd for w in chosen) == [1, 3]
    chosen, how = _choose_windows("Downloads", _windows())
    assert how == "window" and [w.hwnd for w in chosen] == [4]
    assert _choose_windows("spotify", _windows()) == ([], "")


def test_jarvis_never_closes_the_window_it_runs_in():
    from backend.core.desktop_close import _is_jarvis, _protected_pids
    window = _windows()[1]
    assert _is_jarvis(window, {window.pid}) is True


def test_close_is_decided_against_what_is_open(monkeypatch):
    from backend.core import desktop_close
    from backend.core import runtime_task_orchestrator as rto

    closed = {}

    async def fake_windows():
        return [("Firefox", "dogs - Google Search - Mozilla Firefox"), ("Notepad", "notes.txt - Notepad")]

    async def fake_close(target):
        closed["target"] = target
        return desktop_close.CloseOutcome(True, target, closed=(target,), method="test")

    monkeypatch.setattr(desktop_close, "open_windows", fake_windows)
    monkeypatch.setattr(desktop_close, "close_target", fake_close)

    class Prime:
        async def generate(self, prompt, **_):
            assert "dogs - Google Search - Mozilla Firefox" in prompt   # the facts reach the Mind
            return types.SimpleNamespace(content='{"close": "Firefox"}')

    orch = rto.RuntimeTaskOrchestrator(prime_client=Prime())
    result = asyncio.run(orch._dispatch_to_close("close the browser", {}))
    assert closed["target"] == "Firefox" and result["success"] is True


# -- folders and files ---------------------------------------------------------------
def test_a_slightly_wrong_file_name_resolves_to_what_the_folder_holds(tmp_path):
    from backend.core.desktop_open import resolve_path
    (tmp_path / "notes.txt").write_text("x")
    assert resolve_path(str(tmp_path / "notes")) == tmp_path / "notes.txt"
    assert resolve_path(str(tmp_path)) == tmp_path
    assert resolve_path(str(tmp_path / "nothing like it")) is None
    assert resolve_path(str(tmp_path / "no-such-dir" / "x")) is None


def test_the_users_folders_are_read_not_assumed(tmp_path, monkeypatch):
    from backend.core import host_platform
    (tmp_path / "Documents").mkdir()
    (tmp_path / ".cache").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    folders = {p.name for p in host_platform.user_folders()}
    assert "Documents" in folders and ".cache" not in folders

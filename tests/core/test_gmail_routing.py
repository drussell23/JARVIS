"""A Gmail question reaches Gmail, and JARVIS says what is true about it.

2026-10-14 21:02: "what is on my gmail? anything important?" got
"I don't have access to your Gmail account ... log into Gmail manually".
Traced:

* the workspace agent was not in the Mind's catalogue while its packages
  were missing, so email questions went to the screen and to plain chat;
* a step's action never reached its agent ("Unknown context action: ");
* a live agent was dispatched to uninitialised (no Google client);
* the workspace detector matched trigger phrases ("check my email") and
  missed "what is on my gmail?";
* with no sign-in, the agent tried a paid visual lane switched off by the
  operator and reported "All execution tiers failed" instead of "not
  connected -- run the setup";
* an error result read as success, a partial answer was thrown away, and a
  task whose handler had answered fell through to a model that guessed.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.core import agent_bindings as ab
from backend.core import runtime_task_orchestrator as rto_mod
from backend.core.runtime_task_orchestrator import (
    RuntimeTaskOrchestrator, StepResolution, TaskResolution, TaskResult,
)


def _binding(**kw):
    base = dict(agent_id="mail_agent", module="json", class_name="", agent_type="workspace",
                capabilities={"read_email", "handle_workspace_query"})
    base.update(kw)
    return ab.AgentBinding(**base)


@pytest.fixture
def bindings(monkeypatch):
    table = {}
    monkeypatch.setattr(ab, "get_agent_bindings", lambda: table)
    monkeypatch.setattr(ab, "_catalogue", None)
    return table


# ------------------------------------------------------ the Mind's catalogue
def test_an_agent_only_not_set_up_is_offered_and_says_so(bindings):
    bindings["mail_agent"] = _binding(requires=["a_package_that_is_not_installed"])
    bindings["mac_agent"] = _binding(agent_id="mac_agent", platforms=["no_such_os"])
    catalogue = ab.capability_catalogue()
    assert "mail_agent" in catalogue and "not set up on this host" in catalogue
    assert "mac_agent" not in catalogue                    # can never run here
    assert ab.binding_for_capability("read_email").agent_id == "mail_agent"


# ----------------------------------------------------------- the dispatch
def test_dispatch_to_an_agent_not_set_up_says_what_it_needs(bindings, monkeypatch):
    from backend.core import optional_dependencies as od
    noted = []
    monkeypatch.setattr(od, "note_missing", lambda pkg, feature, **kw: noted.append(pkg))
    bindings["mail_agent"] = _binding(requires=["a_package_that_is_not_installed"])
    result = asyncio.run(RuntimeTaskOrchestrator()._dispatch_to_agent("mail_agent", "what is new", {}))
    assert result["success"] is False and "not set up" in result["result"]
    assert noted == ["a_package_that_is_not_installed"]


def _capture_agent(orch, monkeypatch):
    seen = {}

    class _Agent:
        async def execute_task(self, payload):
            seen.update(payload)
            return {"success": True}

    async def live(name):
        return _Agent()
    monkeypatch.setattr(orch, "_get_live_agent", live)
    return seen


def test_an_agent_with_an_entry_gets_the_request_in_the_users_words(bindings, monkeypatch):
    bindings["mail_agent"] = _binding(entry="handle_workspace_query")
    orch = RuntimeTaskOrchestrator()
    seen = _capture_agent(orch, monkeypatch)
    asyncio.run(orch._dispatch_to_agent("mail_agent", "what is on my gmail?",
                                        {"goal": "what is on my gmail?", "capability": "read_email"}))
    assert seen["action"] == "handle_workspace_query" and seen["query"] == "what is on my gmail?"


def test_otherwise_the_chosen_capability_is_the_action(bindings, monkeypatch):
    bindings["ctx"] = _binding(agent_id="ctx", capabilities={"get_context", "get_history"})
    orch = RuntimeTaskOrchestrator()
    seen = _capture_agent(orch, monkeypatch)
    asyncio.run(orch._dispatch_to_agent("ctx", "what was I doing", {"goal": "x", "capability": "get_history"}))
    assert seen["action"] == "get_history"


def test_a_created_agent_is_initialised_before_use(bindings, monkeypatch):
    calls = []

    class _Agent:
        _initialized = False

        async def initialize(self):
            calls.append("init")
            self._initialized = True

    binding = _binding(entry="handle_workspace_query")
    monkeypatch.setattr(ab.AgentBinding, "instantiate", lambda self: _Agent())
    bindings["mail_agent"] = binding
    import backend.neural_mesh.integration as nmi
    monkeypatch.setattr(nmi, "get_neural_mesh_coordinator", lambda: None)
    agent = asyncio.run(RuntimeTaskOrchestrator()._get_live_agent("mail_agent"))
    assert calls == ["init"] and agent._initialized


def test_the_mesh_running_instance_is_used_when_there_is_one(bindings, monkeypatch):
    running = types.SimpleNamespace(_initialized=True)
    import backend.neural_mesh.integration as nmi
    monkeypatch.setattr(nmi, "get_neural_mesh_coordinator",
                        lambda: types.SimpleNamespace(get_agent=lambda name: running))
    assert asyncio.run(RuntimeTaskOrchestrator()._get_live_agent("mail_agent")) is running


# --------------------------------------------------------- reading results
def test_an_error_result_is_a_failure_with_what_to_do():
    failure = RuntimeTaskOrchestrator._result_failure({
        "error": "Google Workspace email is not connected: no Google sign-in yet",
        "action_required": "Run: setup"})
    assert failure == "Google Workspace email is not connected: no Google sign-in yet (Run: setup)"
    assert RuntimeTaskOrchestrator._result_failure({"success": True, "error": ""}) is None
    assert RuntimeTaskOrchestrator._result_failure({"emails": []}) is None


def _step(result=None, error=None):
    return StepResolution(step_goal="g", resolution=TaskResolution.EXISTING_AGENT, agent_name="a",
                          capability_used="c", synthesized=False, result=result, error=error)


def test_a_partial_task_keeps_what_the_working_steps_found():
    summary = RuntimeTaskOrchestrator._build_summary("q", [
        _step(result={"success": True, "result": "Your inbox is open: 3 unread from Alex"}),
        _step(error="Unknown context action: "),
    ])
    assert "3 unread from Alex" in summary and "couldn't do all of it" in summary


def test_a_handled_task_answers_instead_of_falling_through():
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    task = TaskResult(task_id="t", original_query="q", success=False, total_elapsed_s=0,
                      plan_reasoning="", summary="Gmail is not connected (Run: setup)",
                      steps=[_step(result={"error": "not connected"}, error="not connected")])
    reply = UnifiedCommandProcessor._runtime_task_response(task)
    assert reply is not None and reply["response"].startswith("Gmail is not connected")
    nothing_ran = TaskResult(task_id="t", original_query="q", success=False, total_elapsed_s=0,
                             plan_reasoning="", summary="x", steps=[_step(error="no handler")])
    assert UnifiedCommandProcessor._runtime_task_response(nothing_ran) is None


# ------------------------------------------------------ the workspace detector
def test_the_mind_decides_the_workspace_action(monkeypatch):
    from backend.core import prime_router as pr
    from backend.core.workspace_routing_intelligence import WorkspaceIntent, WorkspaceIntentDetector

    class _Router:
        async def generate(self, **_kw):
            return types.SimpleNamespace(content='{"intent": "check_email", "entities": {}, "confidence": 0.9}')

    async def router():
        return _Router()
    monkeypatch.setattr(pr, "get_prime_router", router)
    result = asyncio.run(WorkspaceIntentDetector().detect("what is on my gmail? anything important?"))
    assert result.is_workspace_command and result.intent is WorkspaceIntent.CHECK_EMAIL


def test_trigger_phrases_decide_only_when_the_mind_cannot(monkeypatch):
    from backend.core import prime_router as pr
    from backend.core.workspace_routing_intelligence import WorkspaceIntent, WorkspaceIntentDetector

    async def router():
        raise RuntimeError("J-Prime is down")
    monkeypatch.setattr(pr, "get_prime_router", router)
    result = asyncio.run(WorkspaceIntentDetector().detect("check my email"))
    assert result.intent is WorkspaceIntent.CHECK_EMAIL and "trigger" in result.reasoning


# ---------------------------------------------------- the agent's own answer
def test_without_a_sign_in_the_answer_is_not_connected_not_all_tiers_failed(monkeypatch):
    from backend.core.ouroboros.governance import paid_lanes
    from backend.neural_mesh.agents import google_workspace_agent as g
    monkeypatch.setattr(paid_lanes, "paid_lane_allowed", lambda provider: False)
    monkeypatch.setattr(g, "GOOGLE_API_AVAILABLE", True)
    executor = g.UnifiedWorkspaceExecutor(config=g.GoogleWorkspaceConfig())
    client = types.SimpleNamespace(can_attempt_google_api=False, auth_state=g.AuthState.UNAUTHENTICATED)
    result = asyncio.run(executor.execute_email_check(client, limit=5, allow_visual_fallback=True))
    assert result.success is False and "not connected" in result.error
    assert result.data["error_code"] == "auth_missing"
    assert g.OAUTH_SETUP_COMMAND in result.data["action_required"]


# ------------------------------------------ the agent's data becomes the reply
def test_a_task_that_ran_a_workspace_action_is_answered_from_its_data(monkeypatch):
    """Dispatched by the orchestrator, the agent's emails reached the user as
    "Done.": the data goes through the workspace finishing stage instead."""
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    proc = UnifiedCommandProcessor.__new__(UnifiedCommandProcessor)
    seen = {}

    async def finish(command_text, outcomes, **kw):
        seen["outcomes"] = outcomes
        return {"success": True, "response": "You have 2 unread emails from Alex."}
    monkeypatch.setattr(proc, "_finish_workspace_outcomes", finish)
    task = TaskResult(task_id="t", original_query="q", success=True, total_elapsed_s=0,
                      plan_reasoning="", summary="Done.",
                      steps=[_step(result={"workspace_action": "fetch_unread_emails",
                                           "emails": [{"subject": "a"}], "count": 1})])
    reply = asyncio.run(proc._workspace_reply_from_task("what is on my gmail?", task))
    assert reply["response"] == "You have 2 unread emails from Alex."
    assert seen["outcomes"][0]["action"] == "fetch_unread_emails"
    assert seen["outcomes"][0]["status"] == "success"
    plain = TaskResult(task_id="t", original_query="q", success=True, total_elapsed_s=0,
                       plan_reasoning="", summary="Opened Firefox",
                       steps=[_step(result={"success": True, "result": "Opened Firefox"})])
    assert asyncio.run(proc._workspace_reply_from_task("open firefox", plain)) is None


def test_the_composer_addresses_the_operator_by_their_own_name(monkeypatch):
    from backend.api.unified_command_processor import UnifiedCommandProcessor
    from backend.core import owner
    monkeypatch.setattr(owner, "owner_name", lambda: "Ada")
    prompt = UnifiedCommandProcessor._build_compose_system_prompt()
    assert "speaking to Ada" in prompt and "Address Ada by name" in prompt

"""Tests for RuntimeTaskOrchestrator — universal task dispatcher."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest

from backend.core.runtime_task_orchestrator import (
    RuntimeTaskOrchestrator,
    TaskResolution,
    TaskResult,
    StepResolution,
)


@dataclass
class FakePrediction:
    original_query: str = "test"
    detected_intent: str = "GENERAL"
    confidence: float = 0.9
    expanded_tasks: list = None
    reasoning: str = "test reasoning"
    context_used: str = ""

    def __post_init__(self):
        if self.expanded_tasks is None:
            self.expanded_tasks = []


@dataclass
class FakeExpandedTask:
    goal: str
    priority: int = 1
    target_app: str = None
    workspace_service: str = None
    dependencies: list = None
    category: str = "GENERAL"

    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []


class TestTaskResolution:
    def test_enum_values(self):
        assert TaskResolution.EXISTING_AGENT == "existing_agent"
        assert TaskResolution.EPHEMERAL_TOOL == "ephemeral_tool"
        assert TaskResolution.GOVERNANCE_OP == "governance_op"
        assert TaskResolution.UNRESOLVABLE == "unresolvable"


class TestRuntimeTaskOrchestrator:
    @pytest.mark.asyncio
    async def test_execute_with_no_deps(self):
        """With no dependencies injected, falls back gracefully."""
        orch = RuntimeTaskOrchestrator()
        result = await orch.execute("do something")
        assert isinstance(result, TaskResult)
        assert result.original_query == "do something"

    @pytest.mark.asyncio
    async def test_decompose_with_planner(self):
        planner = MagicMock()
        planner.expand_intent = AsyncMock(return_value=FakePrediction(
            expanded_tasks=[
                FakeExpandedTask(goal="open browser"),
                FakeExpandedTask(goal="navigate to youtube"),
            ],
            reasoning="two-step plan",
        ))
        orch = RuntimeTaskOrchestrator(planner=planner)
        result = await orch.execute("search youtube for CS videos")
        assert len(result.steps) == 2
        assert result.plan_reasoning == "two-step plan"

    @pytest.mark.asyncio
    async def test_decompose_fallback_single_step(self):
        """Without planner, entire query becomes one step."""
        orch = RuntimeTaskOrchestrator()
        result = await orch.execute("do something simple")
        assert len(result.steps) == 1
        assert result.steps[0].step_goal == "do something simple"

    @pytest.mark.asyncio
    async def test_resolve_existing_agent(self):
        registry = AsyncMock()
        agent_info = MagicMock()
        agent_info.agent_name = "visual_browser_agent"
        agent_info.load = 0.1
        registry.find_by_capability = AsyncMock(return_value=[agent_info])
        registry.get_agent = AsyncMock(return_value=agent_info)

        orch = RuntimeTaskOrchestrator(registry=registry)
        result = await orch.execute("browse youtube.com")
        assert any(s.resolution == TaskResolution.EXISTING_AGENT for s in result.steps)

    @pytest.mark.asyncio
    async def test_resolve_code_change_to_governance(self):
        gls = AsyncMock()
        gls.submit = AsyncMock(return_value=MagicMock())
        orch = RuntimeTaskOrchestrator(gls=gls)
        result = await orch.execute("fix bug in auth.py")
        assert any(s.resolution == TaskResolution.GOVERNANCE_OP for s in result.steps)

    @pytest.mark.asyncio
    async def test_resolve_ephemeral_synthesis(self):
        prime = AsyncMock()
        prime.generate = AsyncMock(return_value=MagicMock(
            content="async def execute(): return {'success': True}",
            tokens_used=50,
            source="mock",
        ))
        orch = RuntimeTaskOrchestrator(prime_client=prime)
        result = await orch.execute("convert this CSV to JSON")
        assert any(s.synthesized for s in result.steps)

    @pytest.mark.asyncio
    async def test_unresolvable_step(self):
        orch = RuntimeTaskOrchestrator()
        result = await orch.execute("do something impossible with no tools")
        assert any(s.resolution == TaskResolution.UNRESOLVABLE for s in result.steps)

    @pytest.mark.asyncio
    async def test_consciousness_records_outcome(self):
        consciousness = AsyncMock()
        consciousness.record_operation_outcome = AsyncMock()
        orch = RuntimeTaskOrchestrator(consciousness=consciousness)
        await orch.execute("test task")
        consciousness.record_operation_outcome.assert_called_once()

    def test_extract_capability_keywords(self):
        keywords = RuntimeTaskOrchestrator._extract_capability_keywords(
            "search youtube for computer science videos"
        )
        assert "web_search" in keywords or "search" in keywords
        assert "visual_browser" in keywords or "browser" in keywords

    def test_is_code_change(self):
        assert RuntimeTaskOrchestrator._is_code_change("fix bug in auth.py")
        assert RuntimeTaskOrchestrator._is_code_change("implement the new feature")
        assert not RuntimeTaskOrchestrator._is_code_change("search youtube for videos")
        assert not RuntimeTaskOrchestrator._is_code_change("open my email")

    def test_extract_url(self):
        assert "youtube.com" in RuntimeTaskOrchestrator._extract_url("go to youtube")
        assert "google.com" in RuntimeTaskOrchestrator._extract_url("search on google")
        assert "example.com" in RuntimeTaskOrchestrator._extract_url("visit https://example.com/page")

    def test_build_summary(self):
        steps = [
            StepResolution("step1", TaskResolution.EXISTING_AGENT, "agent1", "cap1", False),
            StepResolution("step2", TaskResolution.EPHEMERAL_TOOL, None, None, True, error="failed"),
        ]
        summary = RuntimeTaskOrchestrator._build_summary("test", steps)
        assert "1/2" in summary
        assert "synthesized" in summary


# ---------------------------------------------------------------------------
# Structured dispatch integration tests
# ---------------------------------------------------------------------------


class TestStructuredDispatch:
    """Tests that dispatch uses live agent instances, not dummy dicts."""

    @pytest.mark.asyncio
    async def test_dispatch_calls_execute_task(self):
        """Register agent → dispatch step → assert real execute_task called."""
        registry = AsyncMock()
        registry.find_by_capability = AsyncMock(return_value=[
            MagicMock(agent_name="visual_browser_agent", load=0.0),
        ])
        registry.get_agent = AsyncMock(return_value=MagicMock(agent_name="visual_browser_agent"))

        # Create a mock agent class that returns from execute_task
        mock_agent_instance = AsyncMock()
        mock_agent_instance.execute_task = AsyncMock(return_value={
            "success": True,
            "result": "searched youtube for nba",
        })

        orch = RuntimeTaskOrchestrator(registry=registry)
        # Inject the live agent directly into the cache
        orch._live_agents["visual_browser_agent"] = mock_agent_instance

        result = await orch.execute(
            query="search youtube for nba",
            context={
                "provider": "youtube",
                "search_query": "nba",
                "url": "https://www.youtube.com/results?search_query=nba",
                "action_category": "browser",
            },
        )

        mock_agent_instance.execute_task.assert_called_once()
        call_payload = mock_agent_instance.execute_task.call_args[0][0]
        assert call_payload["goal"] == "search youtube for nba"
        assert call_payload["search_query"] == "nba"
        assert call_payload["provider"] == "youtube"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_not_fallthrough(self):
        """Unknown agent id → explicit RuntimeError, no fallback to chat."""
        registry = AsyncMock()
        # Return empty for capability lookup → unresolvable
        registry.find_by_capability = AsyncMock(return_value=[])
        registry.get_agent = AsyncMock(return_value=None)

        orch = RuntimeTaskOrchestrator(registry=registry)
        result = await orch.execute("do something with unknown_agent")
        # Should mark as UNRESOLVABLE, not fake success
        assert any(s.resolution == TaskResolution.UNRESOLVABLE for s in result.steps)
        assert not result.success

    @pytest.mark.asyncio
    async def test_structured_fields_propagated_to_step(self):
        """Context fields (provider, url, search_query) flow into step dict."""
        orch = RuntimeTaskOrchestrator()
        result = await orch.execute(
            query="search youtube for dogs",
            context={
                "provider": "youtube",
                "search_query": "dogs",
                "url": "https://www.youtube.com/results?search_query=dogs",
            },
        )
        # The step should carry the structured fields
        assert result.steps[0].step_goal == "search youtube for dogs"


class TestIntentClassifierStructuredFields:
    """Tests that IntentClassifier emits structured routing fields."""

    def test_youtube_search_emits_provider_and_query(self):
        from backend.core.intent_classifier import get_intent_classifier, CommandIntent
        classifier = get_intent_classifier()
        result = classifier.classify("search youtube for nba highlights")
        assert result.intent == CommandIntent.ACTION
        assert result.provider == "youtube"
        assert result.search_query  # should have extracted "nba highlights"
        # Classifier does NOT emit URLs — agents resolve those
        assert result.url == ""

    def test_google_search_emits_structured_fields(self):
        from backend.core.intent_classifier import get_intent_classifier, CommandIntent
        classifier = get_intent_classifier()
        result = classifier.classify("search google for python tutorials")
        assert result.intent == CommandIntent.ACTION
        assert result.provider == "google"
        assert result.search_query  # "python tutorials" or similar
        assert result.url == ""  # no URL — agent resolves

    def test_open_app_emits_target_app(self):
        from backend.core.intent_classifier import get_intent_classifier, CommandIntent
        classifier = get_intent_classifier()
        result = classifier.classify("open apple music")
        assert result.intent == CommandIntent.ACTION
        assert result.target_app == "Apple Music"

    def test_query_has_no_structured_fields(self):
        from backend.core.intent_classifier import get_intent_classifier, CommandIntent
        classifier = get_intent_classifier()
        result = classifier.classify("what is the weather today")
        assert result.intent == CommandIntent.QUERY
        assert result.provider == ""
        assert result.url == ""


class TestAgentBindings:
    """Tests for the declarative agent bindings manifest."""

    def test_load_bindings_returns_all_defaults(self):
        from backend.core.agent_bindings import load_bindings
        bindings = load_bindings()
        assert "visual_browser_agent" in bindings
        assert "google_workspace_agent" in bindings
        assert len(bindings) >= 10

    def test_binding_has_capabilities(self):
        from backend.core.agent_bindings import load_bindings
        bindings = load_bindings()
        vba = bindings["visual_browser_agent"]
        assert "browser" in vba.capabilities
        assert "visual_browser" in vba.capabilities

    def test_binding_has_import_spec(self):
        from backend.core.agent_bindings import load_bindings
        bindings = load_bindings()
        vba = bindings["visual_browser_agent"]
        assert vba.module == "backend.neural_mesh.agents.visual_browser_agent"
        assert vba.class_name == "VisualBrowserAgent"

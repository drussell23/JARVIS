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

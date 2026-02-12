"""
Tests for v239-v241 autonomy features:
  - v239.0: NeuralMeshAgentTool, ToolRegistry, _execute_action registry dispatch
  - v240.0: _gather_heartbeat_context, _record_action_experience
  - v241.0: Proactive goal deduplication (_is_proactive_goal_cooled_down,
            _has_active_proactive_goal, _cleanup_proactive_cooldowns)
  - Goal submission (submit_goal, priority ordering, backpressure)

All tests use asyncio_mode=auto (from pytest.ini) so no @pytest.mark.asyncio
decorators are needed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — lazy imports to avoid import-time side effects from the codebase
# ---------------------------------------------------------------------------

def _import_tool_classes():
    from backend.autonomy.langchain_tools import (
        FunctionTool,
        JARVISTool,
        NeuralMeshAgentTool,
        ToolCategory,
        ToolMetadata,
        ToolRegistry,
        ToolRiskLevel,
    )
    return (
        FunctionTool,
        JARVISTool,
        NeuralMeshAgentTool,
        ToolCategory,
        ToolMetadata,
        ToolRegistry,
        ToolRiskLevel,
    )


def _import_runtime_classes():
    from backend.autonomy.agent_runtime import UnifiedAgentRuntime
    from backend.autonomy.agent_runtime_models import (
        Goal,
        GoalPriority,
        GoalStatus,
        TERMINAL_STATES,
    )
    return UnifiedAgentRuntime, Goal, GoalPriority, GoalStatus, TERMINAL_STATES


def _make_mock_agent(agent_name: str = "TestAgent", agent_type: str = "test"):
    """Build a lightweight mock that passes as a BaseNeuralMeshAgent."""
    agent = MagicMock()
    agent.agent_name = agent_name
    agent.agent_type = agent_type
    agent.execute_task = AsyncMock(return_value={"status": "ok"})
    return agent


def _make_tool(
    name: str = "test_tool",
    category=None,
    risk_level=None,
    capabilities=None,
    tags=None,
):
    """Produce a minimal concrete JARVISTool for registry tests."""
    (FunctionTool, JARVISTool_cls, _NMA, ToolCategory, ToolMetadata,
     _TR, ToolRiskLevel) = _import_tool_classes()
    cat = category or ToolCategory.UTILITY
    risk = risk_level or ToolRiskLevel.LOW

    metadata = ToolMetadata(
        name=name,
        description=f"Test tool {name}",
        category=cat,
        risk_level=risk,
        capabilities=capabilities or [],
        tags=tags or [],
    )

    class _ConcreteTool(JARVISTool_cls):
        async def _execute(self, **kwargs) -> Any:
            return {"echo": kwargs}

    return _ConcreteTool(metadata)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_tool_registry():
    """Ensure the ToolRegistry singleton is fresh for every test."""
    yield
    try:
        from backend.autonomy.langchain_tools import ToolRegistry
        ToolRegistry._instance = None
    except ImportError:
        pass


@pytest.fixture
def mock_autonomous_agent():
    """Minimal mock of the AutonomousAgent passed to UnifiedAgentRuntime."""
    agent = MagicMock()
    agent.reasoning_engine = MagicMock()
    agent.tool_orchestrator = MagicMock()
    agent.tool_orchestrator.execute = AsyncMock(
        return_value={"result": "orchestrated"}
    )
    agent.memory = MagicMock()
    return agent


@pytest.fixture
def runtime(mock_autonomous_agent):
    """UnifiedAgentRuntime with a mocked autonomous agent.

    The checkpoint store is stubbed so ``submit_goal`` does not touch SQLite.
    """
    UnifiedAgentRuntime, *_ = _import_runtime_classes()
    rt = UnifiedAgentRuntime(mock_autonomous_agent)
    # Stub checkpoint store so save() is a no-op
    rt._checkpoint_store = MagicMock()
    rt._checkpoint_store.save = AsyncMock()
    rt._checkpoint_store.initialize = AsyncMock()
    # Mark as running so _promote_pending_goals can be exercised
    rt._running = False
    return rt


# ===================================================================
# TestNeuralMeshAgentTool (5 tests)
# ===================================================================

class TestNeuralMeshAgentTool:
    """Tests for the NeuralMeshAgentTool wrapper (v239.0)."""

    async def test_execute_delegates_to_agent(self):
        """_execute calls agent.execute_task with {action: capability, **kwargs}."""
        *_, NeuralMeshAgentTool, ToolCategory, _, _, ToolRiskLevel = _import_tool_classes()
        agent = _make_mock_agent("EmailBot", "communication")
        tool = NeuralMeshAgentTool(
            agent=agent,
            capability="fetch_unread_emails",
            category=ToolCategory.COMMUNICATION,
        )
        await tool._execute(folder="inbox", limit=5)
        agent.execute_task.assert_awaited_once_with(
            {"action": "fetch_unread_emails", "folder": "inbox", "limit": 5}
        )

    def test_tool_name_format(self):
        """Name must follow mesh:{agent_name}:{capability}."""
        _, _, NeuralMeshAgentTool, *_ = _import_tool_classes()
        agent = _make_mock_agent("CalendarBot", "integration")
        tool = NeuralMeshAgentTool(agent=agent, capability="list_events")
        assert tool.name == "mesh:CalendarBot:list_events"

    def test_metadata_tags(self):
        """Tags include neural_mesh, agent_type, and agent_name."""
        _, _, NeuralMeshAgentTool, *_ = _import_tool_classes()
        agent = _make_mock_agent("SlackBot", "communication")
        tool = NeuralMeshAgentTool(agent=agent, capability="send_message")
        assert "neural_mesh" in tool.metadata.tags
        assert "communication" in tool.metadata.tags
        assert "SlackBot" in tool.metadata.tags

    async def test_execute_propagates_exception(self):
        """Exceptions from agent.execute_task must propagate, not be swallowed."""
        _, _, NeuralMeshAgentTool, *_ = _import_tool_classes()
        agent = _make_mock_agent()
        agent.execute_task = AsyncMock(side_effect=RuntimeError("boom"))
        tool = NeuralMeshAgentTool(agent=agent, capability="risky_op")
        with pytest.raises(RuntimeError, match="boom"):
            await tool._execute()

    def test_timeout_in_metadata(self):
        """timeout_seconds flows through to ToolMetadata."""
        _, _, NeuralMeshAgentTool, *_ = _import_tool_classes()
        agent = _make_mock_agent()
        tool = NeuralMeshAgentTool(
            agent=agent, capability="slow_task", timeout_seconds=120.0
        )
        assert tool.metadata.timeout_seconds == 120.0


# ===================================================================
# TestToolRegistry (8 tests)
# ===================================================================

class TestToolRegistry:
    """Tests for the ToolRegistry singleton (v239.0)."""

    def test_singleton_pattern(self):
        """get_instance() returns the same object on repeated calls."""
        _, _, _, _, _, ToolRegistry, _ = _import_tool_classes()
        a = ToolRegistry.get_instance()
        b = ToolRegistry.get_instance()
        assert a is b

    def test_register_indexes_correctly(self):
        """After register, the tool is findable by name, category, and capability."""
        (FunctionTool, _, _, ToolCategory, ToolMetadata,
         ToolRegistry, ToolRiskLevel) = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool = _make_tool(
            name="idx_test",
            category=ToolCategory.ANALYSIS,
            capabilities=["summarise"],
        )
        registry.register(tool)

        assert registry.get("idx_test") is tool
        assert tool in registry.get_by_category(ToolCategory.ANALYSIS)
        assert tool in registry.get_by_capability("summarise")

    def test_register_duplicate_raises(self):
        """Registering the same name without replace=True raises ValueError."""
        *_, ToolRegistry, _ = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool_a = _make_tool(name="dup")
        tool_b = _make_tool(name="dup")
        registry.register(tool_a)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(tool_b)

    def test_register_function_wraps(self):
        """register_function() creates a FunctionTool and registers it."""
        FunctionTool_cls, *_, ToolRegistry_cls, _ = _import_tool_classes()
        registry = ToolRegistry_cls.get_instance()

        def my_func(x: int) -> int:
            """Double x."""
            return x * 2

        tool = registry.register_function(my_func, name="doubler")
        assert isinstance(tool, FunctionTool_cls)
        assert registry.get("doubler") is tool

    def test_unregister_cleans_indices(self):
        """After unregister, the tool is not findable by any index."""
        _, _, _, ToolCategory, _, ToolRegistry, _ = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool = _make_tool(
            name="to_remove",
            category=ToolCategory.MONITORING,
            capabilities=["watch"],
        )
        registry.register(tool)
        assert registry.get("to_remove") is not None

        result = registry.unregister("to_remove")
        assert result is True
        assert registry.get("to_remove") is None
        assert tool not in registry.get_by_category(ToolCategory.MONITORING)
        assert tool not in registry.get_by_capability("watch")

    def test_get_by_category(self):
        """get_by_category returns only tools matching the requested category."""
        _, _, _, ToolCategory, _, ToolRegistry, _ = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool_a = _make_tool(name="cat_a", category=ToolCategory.NETWORK)
        tool_b = _make_tool(name="cat_b", category=ToolCategory.SECURITY)
        registry.register(tool_a)
        registry.register(tool_b)

        net_tools = registry.get_by_category(ToolCategory.NETWORK)
        assert tool_a in net_tools
        assert tool_b not in net_tools

    def test_get_by_capability(self):
        """get_by_capability returns only tools matching the capability."""
        *_, ToolRegistry, _ = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool_a = _make_tool(name="cap_a", capabilities=["email_read"])
        tool_b = _make_tool(name="cap_b", capabilities=["file_write"])
        registry.register(tool_a)
        registry.register(tool_b)

        email_tools = registry.get_by_capability("email_read")
        assert tool_a in email_tools
        assert tool_b not in email_tools

    def test_search_matches_name_and_tags(self):
        """search() finds tools by name substring or tag."""
        _, _, _, ToolCategory, _, ToolRegistry, ToolRiskLevel = _import_tool_classes()
        registry = ToolRegistry.get_instance()
        tool = _make_tool(name="alpha_search_beta", tags=["gamma_tag"])
        registry.register(tool)

        # Match by name substring
        results = registry.search("alpha_search")
        assert tool in results

        # Match by tag
        results = registry.search("gamma_tag")
        assert tool in results

        # No match
        results = registry.search("zzzzz_no_match")
        assert tool not in results


# ===================================================================
# TestExecuteAction (5 tests)
# ===================================================================

class TestExecuteAction:
    """Tests for UnifiedAgentRuntime._execute_action (v239.0 registry dispatch)."""

    async def test_tool_in_registry_dispatched(self, runtime):
        """When a tool_name is found in the registry, tool.run(**params) is called."""
        *_, ToolRegistry, _ = _import_tool_classes()
        registry = ToolRegistry.get_instance()

        mock_tool = MagicMock()
        mock_tool.name = "registered_tool"
        mock_tool.run = AsyncMock(return_value={"output": 42})
        registry._tools["registered_tool"] = mock_tool

        result = await runtime._execute_action({
            "tool": "registered_tool",
            "params": {"x": 1},
        })
        assert result["success"] is True
        assert result["tool"] == "registered_tool"
        mock_tool.run.assert_awaited_once_with(x=1)

    async def test_tool_not_in_registry_uses_orchestrator(self, runtime):
        """tool_name not in registry falls through to tool_orchestrator."""
        result = await runtime._execute_action({
            "tool": "unregistered_tool",
            "params": {"target": "file.txt"},
        })
        assert result["success"] is True
        runtime._agent.tool_orchestrator.execute.assert_awaited_once()

    async def test_reasoning_action_immediate_success(self, runtime):
        """action_type='reason' returns immediate success without tool call."""
        result = await runtime._execute_action({"type": "reason"})
        assert result["success"] is True
        assert "Reasoning complete" in result.get("message", "")

    async def test_shell_action_blocked(self, runtime):
        """action_type='shell' is blocked with an error message."""
        result = await runtime._execute_action({"type": "shell", "params": {"cmd": "rm -rf /"}})
        assert result["success"] is False
        assert "not allowed" in result.get("error", "").lower()

    async def test_unknown_action_passthrough(self, runtime):
        """Unknown type with no tool_name passes through as a dict."""
        result = await runtime._execute_action({"type": "custom_thing", "data": "hello"})
        assert result["success"] is True
        assert result.get("action_type") == "custom_thing"


# ===================================================================
# TestGatherHeartbeatContext (4 tests)
# ===================================================================

class TestGatherHeartbeatContext:
    """Tests for _gather_heartbeat_context (v240.0)."""

    async def test_disabled_returns_empty(self, runtime, monkeypatch):
        """AGENT_RUNTIME_HEARTBEAT_ENABLED=false returns {}."""
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_ENABLED", "false")
        # Re-read env since _env_bool is called at runtime
        result = await runtime._gather_heartbeat_context()
        assert result == {}

    async def test_no_coordinator_returns_system_only(self, runtime, monkeypatch):
        """_mesh_coordinator=None returns dict with system metrics only."""
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_ENABLED", "true")
        runtime._mesh_coordinator = None

        with patch("psutil.cpu_percent", return_value=42.0), \
             patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value = MagicMock(percent=67.5)
            result = await runtime._gather_heartbeat_context()

        assert "system_cpu_percent" in result
        assert result["system_cpu_percent"] == 42.0
        assert "system_memory_percent" in result
        assert result["system_memory_percent"] == 67.5
        assert "time_of_day" in result

    async def test_agent_timeout_partial_context(self, runtime, monkeypatch):
        """One agent times out; system metrics present, that agent's data absent."""
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_AGENT_TIMEOUT", "0.1")
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_TIMEOUT", "2.0")

        coordinator = MagicMock()

        # ContextTrackerAgent works; GoalInferenceAgent times out; others None
        ctx_agent = AsyncMock(return_value={"session_duration": 300})
        goal_agent = AsyncMock(side_effect=asyncio.TimeoutError)
        spatial_agent = None
        calendar_agent = None

        def _get_agent(name):
            mapping = {
                "ContextTrackerAgent": MagicMock(execute_task=ctx_agent),
                "GoalInferenceAgent": MagicMock(execute_task=goal_agent),
                "SpatialAwarenessAgent": spatial_agent,
                "GoogleWorkspaceAgent": calendar_agent,
            }
            return mapping.get(name)

        coordinator.get_agent = _get_agent
        runtime._mesh_coordinator = coordinator

        with patch("psutil.cpu_percent", return_value=10.0), \
             patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value = MagicMock(percent=50.0)
            result = await runtime._gather_heartbeat_context()

        # System metrics always present
        assert "system_cpu_percent" in result
        # ContextTracker data should be mapped
        assert result.get("time_without_break") == 300
        # GoalInference timed out → repetitive_actions absent
        assert "repetitive_actions" not in result

    async def test_full_context_with_all_agents(self, runtime, monkeypatch):
        """All four agent queries succeed; full context populated."""
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_ENABLED", "true")
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_AGENT_TIMEOUT", "5.0")
        monkeypatch.setenv("AGENT_RUNTIME_HEARTBEAT_TIMEOUT", "10.0")

        coordinator = MagicMock()

        ctx_data = {
            "session_duration": 1200,
            "current_task_duration": 600,
            "user_behavior": {"task_switches": 3, "time_on_task": 600, "recent_errors": 1},
            "system_interactions": {"help_searches": 2, "undo_redo_count": 5},
        }
        goal_data = {
            "history": [
                {"category": "coding"},
                {"category": "coding"},
                {"category": "email"},
            ]
        }
        spatial_data = {
            "screen_activity": {"idle_time": 120, "active_window": "Terminal"},
        }
        calendar_data = {"events": []}  # no imminent events

        def _get_agent(name):
            mapping = {
                "ContextTrackerAgent": AsyncMock(return_value=ctx_data),
                "GoalInferenceAgent": AsyncMock(return_value=goal_data),
                "SpatialAwarenessAgent": AsyncMock(return_value=spatial_data),
                "GoogleWorkspaceAgent": AsyncMock(return_value=calendar_data),
            }
            mock_agent = MagicMock()
            mock_agent.execute_task = mapping[name]
            return mock_agent

        coordinator.get_agent = _get_agent
        runtime._mesh_coordinator = coordinator

        with patch("psutil.cpu_percent", return_value=25.0), \
             patch("psutil.virtual_memory") as mock_vm:
            mock_vm.return_value = MagicMock(percent=55.0)
            result = await runtime._gather_heartbeat_context()

        assert result["system_cpu_percent"] == 25.0
        assert result["system_memory_percent"] == 55.0
        assert result["time_without_break"] == 1200
        assert result["time_in_current_task"] == 600
        assert result["user_behavior"]["task_switches"] == 3
        assert result["system_interactions"]["help_searches"] == 2
        # Goal inference → repetitive_actions = most common category count
        assert result["repetitive_actions"] == 2  # "coding" appears twice
        # Spatial → vision_data
        assert "vision_data" in result
        assert result["vision_data"]["screen_activity"]["active_window"] == "Terminal"


# ===================================================================
# TestRecordActionExperience (2 tests)
# ===================================================================

class TestRecordActionExperience:
    """Tests for _record_action_experience on the orchestrator (v240.0)."""

    async def test_disabled_returns_immediately(self, monkeypatch):
        """JARVIS_ORCH_RECORD_EXPERIENCES=0 returns without importing forwarder."""
        monkeypatch.setenv("JARVIS_ORCH_RECORD_EXPERIENCES", "0")

        from backend.agi_os.intelligent_action_orchestrator import (
            IntelligentActionOrchestrator,
        )

        orch = MagicMock(spec=IntelligentActionOrchestrator)
        # Bind the real method
        orch._record_action_experience = (
            IntelligentActionOrchestrator._record_action_experience.__get__(orch)
        )

        action = MagicMock()
        action.action_type = "test"

        # The env var check (line 820) returns before the forwarder import,
        # so we patch at the actual import location used inside the method.
        with patch(
            "backend.intelligence.cross_repo_experience_forwarder.get_experience_forwarder",
            new_callable=AsyncMock,
        ) as mock_fwd:
            await orch._record_action_experience(action, success=True)
            # With env var disabled, the forwarder import is never reached
            mock_fwd.assert_not_called()

    async def test_forwarder_none_returns_silently(self, monkeypatch):
        """When get_experience_forwarder() returns None, no crash."""
        monkeypatch.setenv("JARVIS_ORCH_RECORD_EXPERIENCES", "1")

        from backend.agi_os.intelligent_action_orchestrator import (
            IntelligentActionOrchestrator,
        )

        orch = MagicMock(spec=IntelligentActionOrchestrator)
        orch._record_action_experience = (
            IntelligentActionOrchestrator._record_action_experience.__get__(orch)
        )

        action = MagicMock()
        action.action_type = "test"
        action.confidence = 0.8
        action.target = ""
        action.description = ""
        action.reasoning = ""
        action.params = {}
        action.correlation_id = "abc"
        action.issue_type = None

        with patch(
            "backend.intelligence.cross_repo_experience_forwarder.get_experience_forwarder",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Must not raise
            await orch._record_action_experience(action, success=True, result=None)


# ===================================================================
# TestProactiveGoalDeduplication (5 tests)
# ===================================================================

class TestProactiveGoalDeduplication:
    """Tests for v241.0 proactive goal deduplication helpers."""

    def test_cooled_down_unknown_type(self, runtime):
        """Unknown situation_type is not in cooldowns, returns False."""
        assert runtime._is_proactive_goal_cooled_down("never_seen_before") is False

    def test_cooled_down_within_window(self, runtime):
        """Recently fired situation type returns True (still cooling)."""
        runtime._proactive_cooldowns["critical_error"] = time.time()
        assert runtime._is_proactive_goal_cooled_down("critical_error") is True

    def test_cooled_down_after_expiry(self, runtime):
        """Old enough entry returns False (cooldown expired)."""
        # Set cooldown far in the past (beyond any configured cooldown)
        runtime._proactive_cooldowns["health_reminder"] = time.time() - 999_999
        assert runtime._is_proactive_goal_cooled_down("health_reminder") is False

    def test_has_active_proactive_goal(self, runtime):
        """Active non-terminal proactive goal with matching situation_type returns True."""
        _, Goal, GoalPriority, GoalStatus, _ = _import_runtime_classes()
        goal = Goal(
            description="check health",
            source="proactive",
            status=GoalStatus.ACTIVE,
            metadata={"situation_type": "health_reminder"},
        )
        runtime._active_goals[goal.goal_id] = goal
        assert runtime._has_active_proactive_goal("health_reminder") is True
        # Different type should be False
        assert runtime._has_active_proactive_goal("critical_error") is False

    def test_cleanup_removes_expired(self, runtime):
        """_cleanup_proactive_cooldowns removes entries older than 2x max cooldown."""
        # Insert a very old entry
        runtime._proactive_cooldowns["ancient"] = time.time() - 999_999
        # Insert a fresh one
        runtime._proactive_cooldowns["fresh"] = time.time()

        runtime._cleanup_proactive_cooldowns()

        assert "ancient" not in runtime._proactive_cooldowns
        assert "fresh" in runtime._proactive_cooldowns


# ===================================================================
# TestSubmitGoal (3 tests)
# ===================================================================

class TestSubmitGoal:
    """Tests for UnifiedAgentRuntime.submit_goal."""

    async def test_submit_returns_goal_id(self, runtime):
        """submit_goal returns a string goal_id and the goal is queued."""
        goal_id = await runtime.submit_goal(
            description="Write unit tests",
            priority=_import_runtime_classes()[2].NORMAL,  # GoalPriority.NORMAL
            source="user",
        )
        assert isinstance(goal_id, str)
        assert len(goal_id) > 0
        # Goal should be in the queue
        assert runtime._goal_queue.qsize() == 1

    async def test_queue_full_raises(self, runtime):
        """When the goal queue is at max capacity, RuntimeError is raised."""
        _, _, GoalPriority, *_ = _import_runtime_classes()
        # Fill the queue to max
        runtime._max_queue_size = 2
        await runtime.submit_goal("goal 1", GoalPriority.NORMAL)
        await runtime.submit_goal("goal 2", GoalPriority.NORMAL)

        with pytest.raises(RuntimeError, match="Goal queue full"):
            await runtime.submit_goal("goal 3", GoalPriority.NORMAL)

    async def test_priority_ordering(self, runtime):
        """CRITICAL dequeues before NORMAL before BACKGROUND."""
        _, _, GoalPriority, *_ = _import_runtime_classes()

        await runtime.submit_goal("bg task", GoalPriority.BACKGROUND)
        await runtime.submit_goal("critical task", GoalPriority.CRITICAL)
        await runtime.submit_goal("normal task", GoalPriority.NORMAL)

        # PriorityQueue pops lowest value first; priorities are negated
        # so CRITICAL(-4) < NORMAL(-2) < BACKGROUND(-1)
        first = await runtime._goal_queue.get()
        second = await runtime._goal_queue.get()
        third = await runtime._goal_queue.get()

        # Each item is (neg_priority, goal_id, goal)
        assert first[2].description == "critical task"
        assert second[2].description == "normal task"
        assert third[2].description == "bg task"

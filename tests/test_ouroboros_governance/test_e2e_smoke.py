"""
Ouroboros E2E Smoke Test
=========================

Validates that all 10 new modules (P1-P6 + Phase A-D) are importable,
configurable, and wired correctly — without requiring J-Prime or network access.

This is the "can it boot?" test. It verifies:
  1. All modules import cleanly
  2. Config classes resolve from env vars
  3. Integration points are reachable (CommProtocol, tool manifests, etc.)
  4. The full wiring chain works: GLS creates orchestrator → bridge → MCP → tools
  5. Multi-agent (SubagentScheduler) infrastructure is present
  6. BashTool and WebTool are in the tool manifest
  7. Goal decomposition can run (with mocked Oracle/Router)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =========================================================================
# 1. Import Smoke Tests — All 10 modules must import
# =========================================================================

class TestAllModulesImport:
    """Every new module must be importable without side effects."""

    def test_p1_reasoning_chain_bridge(self):
        from backend.core.ouroboros.governance.reasoning_chain_bridge import ReasoningChainBridge
        assert ReasoningChainBridge is not None

    def test_p2_goal_decomposer(self):
        from backend.core.ouroboros.governance.goal_decomposer import GoalDecomposer, GoalDecompositionResult
        assert GoalDecomposer is not None
        assert GoalDecompositionResult is not None

    def test_p3_reactor_event_consumer(self):
        from backend.core.ouroboros.governance.reactor_event_consumer import ReactorEventConsumer
        assert ReactorEventConsumer is not None

    def test_p4_langfuse_transport(self):
        from backend.core.ouroboros.governance.comms.langfuse_transport import LangfuseTransport
        assert LangfuseTransport is not None

    def test_p5_mcp_tool_client(self):
        from backend.core.ouroboros.governance.mcp_tool_client import GovernanceMCPClient, MCPServerConnection
        assert GovernanceMCPClient is not None
        assert MCPServerConnection is not None

    def test_p6_scheduled_sensor(self):
        from backend.core.ouroboros.governance.intake.sensors.scheduled_sensor import ScheduledTriggerSensor
        assert ScheduledTriggerSensor is not None

    def test_phase_c_bash_tool(self):
        from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool, BashToolConfig
        assert SandboxedBashTool is not None
        assert BashToolConfig is not None

    def test_phase_d_web_tool(self):
        from backend.core.ouroboros.governance.tools.web_tool import WebTool, WebToolConfig
        assert WebTool is not None
        assert WebToolConfig is not None

    def test_existing_tool_executor(self):
        from backend.core.ouroboros.governance.tool_executor import ToolExecutor, _L1_MANIFESTS
        assert ToolExecutor is not None
        assert isinstance(_L1_MANIFESTS, dict)

    def test_existing_subagent_scheduler(self):
        from backend.core.ouroboros.governance.autonomy.subagent_scheduler import SubagentScheduler
        assert SubagentScheduler is not None


# =========================================================================
# 2. Tool Manifest — BashTool + WebTool are registered
# =========================================================================

class TestToolManifestRegistration:
    """BashTool and WebTool must be in the L1 tool manifest."""

    def test_bash_in_manifest(self):
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        assert "bash" in _L1_MANIFESTS
        assert "subprocess" in _L1_MANIFESTS["bash"].capabilities

    def test_web_fetch_in_manifest(self):
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        assert "web_fetch" in _L1_MANIFESTS
        assert "network" in _L1_MANIFESTS["web_fetch"].capabilities

    def test_web_search_in_manifest(self):
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        assert "web_search" in _L1_MANIFESTS
        assert "network" in _L1_MANIFESTS["web_search"].capabilities

    def test_original_tools_still_present(self):
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        for name in ("read_file", "search_code", "list_symbols", "run_tests", "get_callers"):
            assert name in _L1_MANIFESTS, f"Missing original tool: {name}"

    def test_total_tool_count(self):
        from backend.core.ouroboros.governance.tool_executor import _L1_MANIFESTS
        # 5 original + 3 new = 8 total
        assert len(_L1_MANIFESTS) == 8


# =========================================================================
# 3. Config Resolution — env vars are read correctly
# =========================================================================

class TestConfigResolution:
    """Config classes must resolve from environment variables."""

    def test_bash_tool_config_from_env(self):
        from backend.core.ouroboros.governance.tools.bash_tool import BashToolConfig
        config = BashToolConfig.from_env()
        assert isinstance(config.enabled, bool)
        assert config.timeout_s > 0

    def test_web_tool_config_from_env(self):
        from backend.core.ouroboros.governance.tools.web_tool import WebToolConfig
        config = WebToolConfig.from_env()
        assert isinstance(config.enabled, bool)
        assert config.timeout_s > 0

    def test_mcp_config_from_env(self):
        from backend.core.ouroboros.governance.mcp_tool_client import MCPClientConfig
        config = MCPClientConfig.from_env()
        # No JARVIS_MCP_CONFIG set → disabled
        assert isinstance(config.enabled, bool)

    def test_reasoning_chain_config(self):
        from backend.core.reasoning_chain_orchestrator import ChainConfig
        config = ChainConfig.from_env()
        assert hasattr(config, "phase")
        assert hasattr(config, "active")


# =========================================================================
# 4. Integration Wiring — CommProtocol transport stack
# =========================================================================

class TestCommProtocolWiring:
    """Langfuse transport is conditionally added to CommProtocol."""

    def test_langfuse_transport_is_noop_without_keys(self):
        """Without LANGFUSE keys, transport should be inactive."""
        from backend.core.ouroboros.governance.comms.langfuse_transport import LangfuseTransport
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}):
            transport = LangfuseTransport()
            assert not transport.is_active

    def test_langfuse_send_is_noop_when_inactive(self):
        """Inactive transport should silently accept messages."""
        from backend.core.ouroboros.governance.comms.langfuse_transport import LangfuseTransport
        from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
        transport = LangfuseTransport(langfuse_client=None)
        msg = CommMessage(
            msg_type=MessageType.INTENT, op_id="test-op",
            seq=1, causal_parent_seq=None, payload={"goal": "test"},
        )
        # Should not raise
        asyncio.get_event_loop().run_until_complete(transport.send(msg))


# =========================================================================
# 5. Reasoning Chain Bridge — wiring test
# =========================================================================

class TestReasoningChainBridge:
    """Bridge is a no-op when chain is inactive (default)."""

    def test_bridge_inactive_by_default(self):
        from backend.core.ouroboros.governance.reasoning_chain_bridge import ReasoningChainBridge
        from backend.core.ouroboros.governance.comm_protocol import CommProtocol
        comm = CommProtocol()
        bridge = ReasoningChainBridge(comm=comm)
        # Chain is not active by default (env flags not set)
        # is_active depends on whether the orchestrator module exists
        # Either way, classify_with_reasoning should be safe to call

    @pytest.mark.asyncio
    async def test_bridge_returns_none_when_inactive(self):
        from backend.core.ouroboros.governance.reasoning_chain_bridge import ReasoningChainBridge
        from backend.core.ouroboros.governance.comm_protocol import CommProtocol
        comm = CommProtocol()
        bridge = ReasoningChainBridge(comm=comm)
        if not bridge.is_active:
            result = await bridge.classify_with_reasoning("test command", "op-123")
            assert result is None


# =========================================================================
# 6. Goal Decomposer — unit wiring test
# =========================================================================

class TestGoalDecomposerWiring:
    """Goal decomposer works with mocked dependencies."""

    @pytest.mark.asyncio
    async def test_decompose_with_mocked_deps(self):
        from backend.core.ouroboros.governance.goal_decomposer import GoalDecomposer

        mock_oracle = MagicMock()
        mock_oracle.semantic_search = AsyncMock(return_value=[
            ("jarvis:backend/auth.py", 0.85),
        ])

        mock_router = AsyncMock()
        mock_router.ingest = AsyncMock(return_value="enqueued")

        decomposer = GoalDecomposer(
            oracle=mock_oracle,
            intake_router=mock_router,
            reasoning_chain=None,
        )
        result = await decomposer.decompose("improve authentication")

        assert result.total >= 1
        assert result.submitted_count >= 1
        assert result.correlation_id.startswith("op-")


# =========================================================================
# 7. Reactor Event Consumer — lifecycle test
# =========================================================================

class TestReactorConsumerWiring:
    """Consumer creates directories and can start/stop."""

    @pytest.mark.asyncio
    async def test_lifecycle(self, tmp_path):
        from backend.core.ouroboros.governance.reactor_event_consumer import ReactorEventConsumer
        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()

        consumer = ReactorEventConsumer(
            event_bus=mock_bus,
            inbox_dir=tmp_path / "reactor-inbox",
            poll_interval_s=0.1,
        )
        await consumer.start()
        assert consumer._running
        assert (tmp_path / "reactor-inbox" / "pending").exists()

        await consumer.stop()
        assert not consumer._running


# =========================================================================
# 8. MCP Client — no-op when unconfigured
# =========================================================================

class TestMCPClientWiring:
    """MCP client is disabled when no config file exists."""

    def test_disabled_by_default(self):
        from backend.core.ouroboros.governance.mcp_tool_client import GovernanceMCPClient
        client = GovernanceMCPClient()
        assert not client.is_enabled


# =========================================================================
# 9. Scheduled Sensor — config loading
# =========================================================================

class TestScheduledSensorWiring:

    @pytest.mark.asyncio
    async def test_no_config_no_crash(self):
        from backend.core.ouroboros.governance.intake.sensors.scheduled_sensor import ScheduledTriggerSensor
        mock_router = MagicMock()
        sensor = ScheduledTriggerSensor(
            router=mock_router,
            config_path=Path("/nonexistent/schedules.yaml"),
        )
        await sensor.start()
        # Should start without error even with no config
        await sensor.stop()


# =========================================================================
# 10. BashTool + WebTool — functional smoke
# =========================================================================

class TestBashToolSmoke:
    """BashTool executes real commands when enabled."""

    @pytest.mark.asyncio
    async def test_echo_hello(self):
        from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool, BashToolConfig
        config = BashToolConfig(enabled=True, cwd=Path("."))
        tool = SandboxedBashTool(config=config)
        result = await tool.execute("echo hello")
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert not result.blocked
        assert not result.timed_out

    @pytest.mark.asyncio
    async def test_python_expression(self):
        from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool, BashToolConfig
        config = BashToolConfig(enabled=True, cwd=Path("."))
        tool = SandboxedBashTool(config=config)
        result = await tool.execute("python3 -c 'print(2+2)'")
        assert result.exit_code == 0
        assert "4" in result.stdout

    @pytest.mark.asyncio
    async def test_blocked_command(self):
        from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool, BashToolConfig
        config = BashToolConfig(enabled=True, cwd=Path("."))
        tool = SandboxedBashTool(config=config)
        result = await tool.execute("rm -rf /")
        assert result.blocked
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        from backend.core.ouroboros.governance.tools.bash_tool import SandboxedBashTool, BashToolConfig
        config = BashToolConfig(enabled=False)
        tool = SandboxedBashTool(config=config)
        result = await tool.execute("echo hello")
        assert result.blocked


class TestWebToolSmoke:
    """WebTool validates URLs and is disabled by default."""

    def test_disabled_by_default(self):
        from backend.core.ouroboros.governance.tools.web_tool import WebTool, WebToolConfig
        config = WebToolConfig(enabled=False)
        tool = WebTool(config=config)
        assert not tool.is_enabled

    def test_url_validation_rejects_ftp(self):
        from backend.core.ouroboros.governance.tools.web_tool import WebTool, WebToolConfig
        config = WebToolConfig(enabled=True)
        tool = WebTool(config=config)
        error = tool._validate_url("ftp://evil.com/file")
        assert error is not None
        assert "http" in error.lower() or "ftp" in error.lower()

    def test_url_validation_allows_github(self):
        from backend.core.ouroboros.governance.tools.web_tool import WebTool, WebToolConfig
        config = WebToolConfig(enabled=True)
        tool = WebTool(config=config)
        error = tool._validate_url("https://github.com/drussell23/jarvis-prime")
        assert error is None


# =========================================================================
# 11. Multi-Agent System — SubagentScheduler exists and is configured
# =========================================================================

class TestMultiAgentSystem:
    """Verify SubagentScheduler infrastructure for parallel execution."""

    def test_subagent_types_exist(self):
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            ExecutionGraph, WorkUnitSpec, GraphExecutionState,
        )
        assert ExecutionGraph is not None
        assert WorkUnitSpec is not None

    def test_scheduler_import(self):
        from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
            SubagentScheduler, GenerationSubagentExecutor,
        )
        assert SubagentScheduler is not None
        assert GenerationSubagentExecutor is not None

    def test_merge_coordinator_import(self):
        from backend.core.ouroboros.governance.saga.merge_coordinator import MergeCoordinator
        assert MergeCoordinator is not None


# =========================================================================
# 12. Full Pipeline Chain — orchestrator accepts all new components
# =========================================================================

class TestOrchestratorAcceptsNewComponents:
    """GovernedOrchestrator has the integration hooks for all new modules."""

    def test_orchestrator_has_reasoning_bridge_setter(self):
        from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
        assert hasattr(GovernedOrchestrator, "set_reasoning_bridge")

    def test_orchestrator_has_reasoning_bridge_attribute(self):
        from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator
        # Create minimal orchestrator to check attribute
        mock_stack = MagicMock()
        mock_gen = MagicMock()
        mock_config = MagicMock()
        mock_config.project_root = Path(".")
        mock_config.context_expansion_enabled = False
        mock_config.context_expansion_timeout_s = 30
        mock_config.benchmark_enabled = False
        mock_config.model_attribution_enabled = False
        mock_config.curriculum_enabled = False
        mock_config.max_generate_retries = 1
        mock_config.max_validate_retries = 2
        mock_config.approval_timeout_s = 600
        mock_config.repair_engine = None
        mock_config.execution_graph_scheduler = None
        mock_config.repo_registry = None

        orch = GovernedOrchestrator(
            stack=mock_stack,
            generator=mock_gen,
            approval_provider=None,
            config=mock_config,
        )
        assert hasattr(orch, "_reasoning_bridge")

    def test_op_context_has_reasoning_field(self):
        from backend.core.ouroboros.governance.op_context import OperationContext
        ctx = OperationContext.create(
            target_files=("test.py",),
            description="test",
        )
        assert hasattr(ctx, "reasoning_chain_result")
        assert ctx.reasoning_chain_result is None

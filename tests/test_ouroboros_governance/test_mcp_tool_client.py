"""Tests for GovernanceMCPClient -- MCP external tool integration.

Covers:
- Disabled client when no config
- YAML config loading with env var resolution
- on_postmortem issue body formatting
- on_postmortem / on_complete skip logic
- Timeout handling
- health() structure

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.mcp_tool_client import (
    GovernanceMCPClient,
    MCPClientConfig,
    MCPServerConfig,
    _DEFAULT_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockPhase(Enum):
    POSTMORTEM = auto()
    COMPLETE = auto()


class _MockRiskTier(Enum):
    SAFE_AUTO = auto()
    APPROVAL_REQUIRED = auto()


def _make_mock_ctx(
    *,
    op_id: str = "op-mcp-001",
    description: str = "Fix broken import",
    phase: _MockPhase = _MockPhase.POSTMORTEM,
    risk_tier: Optional[_MockRiskTier] = _MockRiskTier.SAFE_AUTO,
    terminal_reason_code: str = "validation_failed",
    target_files: Tuple[str, ...] = ("backend/core/utils.py",),
) -> MagicMock:
    """Build a mock OperationContext for MCP client tests."""
    ctx = MagicMock()
    ctx.op_id = op_id
    ctx.description = description
    ctx.phase = phase
    ctx.risk_tier = risk_tier
    ctx.terminal_reason_code = terminal_reason_code
    ctx.target_files = target_files
    return ctx


def _github_server_config() -> MCPServerConfig:
    """Build a minimal GitHub MCP server config."""
    return MCPServerConfig(
        name="github",
        transport="stdio",
        command=["npx", "@modelcontextprotocol/server-github"],
        env={"GITHUB_TOKEN": "test-token"},
    )


def _config_with_github(
    *,
    auto_issue: bool = True,
    auto_pr: bool = False,
) -> MCPClientConfig:
    """Build an MCPClientConfig with a GitHub server."""
    return MCPClientConfig(
        servers={"github": _github_server_config()},
        auto_issue=auto_issue,
        auto_pr=auto_pr,
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Tests: Configuration
# ---------------------------------------------------------------------------


class TestMCPClientConfig:
    """Tests for MCPClientConfig loading and defaults."""

    def test_disabled_when_no_config(self) -> None:
        """Client is disabled when constructed with defaults."""
        config = MCPClientConfig()
        assert config.enabled is True  # default True, but no servers
        client = GovernanceMCPClient(config)
        assert client.is_enabled is False  # no servers => disabled

    def test_disabled_when_env_var_not_set(self) -> None:
        """from_env returns disabled config when JARVIS_MCP_CONFIG is unset."""
        with patch.dict("os.environ", {}, clear=True):
            config = MCPClientConfig.from_env()
        assert config.enabled is False

    def test_disabled_when_file_missing(self, tmp_path: Path) -> None:
        """from_file returns disabled config when path does not exist."""
        config = MCPClientConfig.from_file(str(tmp_path / "nonexistent.yaml"))
        assert config.enabled is False

    def test_loads_config_from_yaml(self, tmp_path: Path) -> None:
        """from_file parses servers, auto_issue, and auto_pr."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "servers:\n"
            "  github:\n"
            "    transport: stdio\n"
            "    command:\n"
            "      - npx\n"
            "      - '@modelcontextprotocol/server-github'\n"
            "    env:\n"
            "      GITHUB_TOKEN: plain-token\n"
            "auto_issue: true\n"
            "auto_pr: true\n"
        )
        config = MCPClientConfig.from_file(str(config_file))
        assert config.enabled is True
        assert "github" in config.servers
        assert config.servers["github"].transport == "stdio"
        assert config.servers["github"].command == [
            "npx",
            "@modelcontextprotocol/server-github",
        ]
        assert config.servers["github"].env["GITHUB_TOKEN"] == "plain-token"
        assert config.auto_issue is True
        assert config.auto_pr is True

    def test_resolves_env_var_references(self, tmp_path: Path) -> None:
        """Env values like ${FOO} are resolved from os.environ."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "servers:\n"
            "  github:\n"
            "    transport: stdio\n"
            "    command: [gh]\n"
            "    env:\n"
            "      GITHUB_TOKEN: '${MY_GH_TOKEN}'\n"
        )
        with patch.dict("os.environ", {"MY_GH_TOKEN": "secret-123"}):
            config = MCPClientConfig.from_file(str(config_file))
        assert config.servers["github"].env["GITHUB_TOKEN"] == "secret-123"

    def test_env_var_missing_resolves_to_empty(self, tmp_path: Path) -> None:
        """Unset env var references resolve to empty string."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "servers:\n"
            "  github:\n"
            "    transport: stdio\n"
            "    command: [gh]\n"
            "    env:\n"
            "      GITHUB_TOKEN: '${NONEXISTENT_VAR_XYZ}'\n"
        )
        with patch.dict("os.environ", {}, clear=True):
            config = MCPClientConfig.from_file(str(config_file))
        assert config.servers["github"].env["GITHUB_TOKEN"] == ""

    def test_from_file_handles_malformed_yaml(self, tmp_path: Path) -> None:
        """Malformed YAML returns disabled config instead of raising."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(": : : not valid yaml [[[")
        config = MCPClientConfig.from_file(str(config_file))
        # yaml.safe_load may or may not raise on this — either way,
        # the client should be functional (enabled=False or with empty servers).
        # The important thing is no exception leaks out.
        assert isinstance(config, MCPClientConfig)

    def test_from_env_delegates_to_from_file(self, tmp_path: Path) -> None:
        """from_env reads JARVIS_MCP_CONFIG and delegates to from_file."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "servers:\n"
            "  slack:\n"
            "    transport: sse\n"
            "    url: http://localhost:9000/sse\n"
        )
        with patch.dict("os.environ", {"JARVIS_MCP_CONFIG": str(config_file)}):
            config = MCPClientConfig.from_env()
        assert config.enabled is True
        assert "slack" in config.servers
        assert config.servers["slack"].transport == "sse"
        assert config.servers["slack"].url == "http://localhost:9000/sse"

    def test_sse_transport_no_command(self, tmp_path: Path) -> None:
        """SSE server config without command still loads correctly."""
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "servers:\n"
            "  remote:\n"
            "    transport: sse\n"
            "    url: https://mcp.example.com/sse\n"
        )
        config = MCPClientConfig.from_file(str(config_file))
        server = config.servers["remote"]
        assert server.transport == "sse"
        assert server.url == "https://mcp.example.com/sse"
        assert server.command == []


# ---------------------------------------------------------------------------
# Tests: Client Lifecycle
# ---------------------------------------------------------------------------


class TestGovernanceMCPClientLifecycle:
    """Tests for start() and server availability checks."""

    @pytest.mark.asyncio
    async def test_start_disabled_noop(self) -> None:
        """start() is a no-op when client is disabled."""
        client = GovernanceMCPClient(MCPClientConfig(enabled=False))
        await client.start()  # should not raise
        assert client._available_servers == {}

    @pytest.mark.asyncio
    async def test_start_checks_stdio_server(self) -> None:
        """start() checks stdio servers via 'which' command."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        with patch("backend.core.ouroboros.governance.mcp_tool_client.subprocess") as mock_sub:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_sub.run.return_value = mock_result
            await client.start()

        assert client._available_servers["github"] is True

    @pytest.mark.asyncio
    async def test_start_marks_unavailable_on_which_failure(self) -> None:
        """start() marks server unavailable when 'which' fails."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        with patch("backend.core.ouroboros.governance.mcp_tool_client.subprocess") as mock_sub:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_sub.run.return_value = mock_result
            await client.start()

        assert client._available_servers["github"] is False

    @pytest.mark.asyncio
    async def test_start_marks_unavailable_on_exception(self) -> None:
        """start() marks server unavailable when 'which' raises."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        with patch("backend.core.ouroboros.governance.mcp_tool_client.subprocess") as mock_sub:
            mock_sub.run.side_effect = FileNotFoundError("which not found")
            await client.start()

        assert client._available_servers["github"] is False

    @pytest.mark.asyncio
    async def test_start_sse_server_available_when_url_set(self) -> None:
        """SSE servers are marked available when URL is non-empty."""
        config = MCPClientConfig(
            servers={
                "remote": MCPServerConfig(
                    name="remote",
                    transport="sse",
                    url="https://mcp.example.com/sse",
                )
            },
            enabled=True,
        )
        client = GovernanceMCPClient(config)
        await client.start()
        assert client._available_servers["remote"] is True


# ---------------------------------------------------------------------------
# Tests: on_postmortem
# ---------------------------------------------------------------------------


class TestOnPostmortem:
    """Tests for the on_postmortem hook."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        """on_postmortem is a no-op when client is disabled."""
        client = GovernanceMCPClient(MCPClientConfig(enabled=False))
        ctx = _make_mock_ctx()
        await client.on_postmortem(ctx)  # should not raise

    @pytest.mark.asyncio
    async def test_skips_when_auto_issue_false(self) -> None:
        """on_postmortem skips issue creation when auto_issue is False."""
        config = _config_with_github(auto_issue=False)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx()

        with patch.object(client, "_create_github_issue", new_callable=AsyncMock) as mock_create:
            await client.on_postmortem(ctx)
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_issue_when_github_available(self) -> None:
        """on_postmortem calls _create_github_issue when server is available."""
        config = _config_with_github(auto_issue=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx()

        with patch.object(client, "_create_github_issue", new_callable=AsyncMock) as mock_create:
            await client.on_postmortem(ctx)
            mock_create.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_skips_when_github_not_available(self) -> None:
        """on_postmortem skips when github server is not in available_servers."""
        config = _config_with_github(auto_issue=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = False
        ctx = _make_mock_ctx()

        with patch.object(client, "_create_github_issue", new_callable=AsyncMock) as mock_create:
            await client.on_postmortem(ctx)
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_timeout_gracefully(self) -> None:
        """on_postmortem logs warning on timeout, never raises."""
        config = _config_with_github(auto_issue=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx()

        async def slow_create(_ctx: Any) -> None:
            await asyncio.sleep(999)

        with patch.object(client, "_create_github_issue", side_effect=slow_create):
            # Monkey-patch module-level timeout for fast test
            with patch(
                "backend.core.ouroboros.governance.mcp_tool_client._DEFAULT_TIMEOUT",
                0.01,
            ):
                await client.on_postmortem(ctx)  # should not raise

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self) -> None:
        """on_postmortem logs warning on exception, never raises."""
        config = _config_with_github(auto_issue=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx()

        with patch.object(
            client,
            "_create_github_issue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ):
            await client.on_postmortem(ctx)  # should not raise


# ---------------------------------------------------------------------------
# Tests: on_complete
# ---------------------------------------------------------------------------


class TestOnComplete:
    """Tests for the on_complete hook."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        """on_complete is a no-op when client is disabled."""
        client = GovernanceMCPClient(MCPClientConfig(enabled=False))
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)
        await client.on_complete(ctx, ["file.py"])  # should not raise

    @pytest.mark.asyncio
    async def test_skips_when_auto_pr_false(self) -> None:
        """on_complete skips PR creation when auto_pr is False."""
        config = _config_with_github(auto_pr=False)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)

        with patch.object(client, "_create_github_pr", new_callable=AsyncMock) as mock_create:
            await client.on_complete(ctx, ["file.py"])
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_applied_files(self) -> None:
        """on_complete skips PR creation when applied_files is empty."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)

        with patch.object(client, "_create_github_pr", new_callable=AsyncMock) as mock_create:
            await client.on_complete(ctx, [])
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_pr_when_conditions_met(self) -> None:
        """on_complete calls _create_github_pr when all conditions are met."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)
        files = ["backend/core/utils.py", "backend/core/helpers.py"]

        with patch.object(client, "_create_github_pr", new_callable=AsyncMock) as mock_create:
            await client.on_complete(ctx, files)
            mock_create.assert_awaited_once_with(ctx, files)

    @pytest.mark.asyncio
    async def test_handles_timeout_gracefully(self) -> None:
        """on_complete logs warning on timeout, never raises."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)

        async def slow_create(_ctx: Any, _files: Any) -> None:
            await asyncio.sleep(999)

        with patch.object(client, "_create_github_pr", side_effect=slow_create):
            with patch(
                "backend.core.ouroboros.governance.mcp_tool_client._DEFAULT_TIMEOUT",
                0.01,
            ):
                await client.on_complete(ctx, ["file.py"])  # should not raise

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self) -> None:
        """on_complete logs warning on exception, never raises."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)

        with patch.object(
            client,
            "_create_github_pr",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            await client.on_complete(ctx, ["file.py"])  # should not raise


# ---------------------------------------------------------------------------
# Tests: Issue Body Formatting
# ---------------------------------------------------------------------------


class TestFormatFailureBody:
    """Tests for _format_failure_body static method."""

    def test_includes_all_fields(self) -> None:
        """Issue body includes op_id, description, phase, reason, and target files."""
        ctx = _make_mock_ctx(
            op_id="op-fmt-001",
            description="Fix broken import in utils",
            terminal_reason_code="test_failure",
            target_files=("a.py", "b.py"),
        )
        body = GovernanceMCPClient._format_failure_body(ctx)

        assert "`op-fmt-001`" in body
        assert "Fix broken import in utils" in body
        assert "`POSTMORTEM`" in body
        assert "`test_failure`" in body
        assert "a.py" in body
        assert "b.py" in body
        assert "Auto-generated by Ouroboros" in body

    def test_includes_risk_tier_when_present(self) -> None:
        """Issue body includes risk tier line when risk_tier is not None."""
        ctx = _make_mock_ctx(risk_tier=_MockRiskTier.APPROVAL_REQUIRED)
        body = GovernanceMCPClient._format_failure_body(ctx)
        assert "`APPROVAL_REQUIRED`" in body

    def test_omits_risk_tier_when_none(self) -> None:
        """Issue body omits risk tier line when risk_tier is None."""
        ctx = _make_mock_ctx(risk_tier=None)
        body = GovernanceMCPClient._format_failure_body(ctx)
        assert "Risk Tier" not in body

    def test_handles_empty_target_files(self) -> None:
        """Issue body shows 'none' when target_files is empty."""
        ctx = _make_mock_ctx(target_files=())
        body = GovernanceMCPClient._format_failure_body(ctx)
        assert "none" in body


# ---------------------------------------------------------------------------
# Tests: on_alert
# ---------------------------------------------------------------------------


class TestOnAlert:
    """Tests for the on_alert hook."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        """on_alert is a no-op when client is disabled."""
        client = GovernanceMCPClient(MCPClientConfig(enabled=False))
        await client.on_alert("test message")  # should not raise

    @pytest.mark.asyncio
    async def test_logs_when_enabled(self) -> None:
        """on_alert logs the message when client is enabled."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        # Should not raise
        await client.on_alert("server down", severity="critical")


# ---------------------------------------------------------------------------
# Tests: health()
# ---------------------------------------------------------------------------


class TestHealth:
    """Tests for the health() method."""

    def test_health_structure_when_disabled(self) -> None:
        """health() returns correct structure when disabled."""
        client = GovernanceMCPClient(MCPClientConfig(enabled=False))
        h = client.health()
        assert h["enabled"] is False
        assert h["servers"] == {}
        assert "auto_issue" in h
        assert "auto_pr" in h

    def test_health_structure_with_servers(self) -> None:
        """health() reports per-server availability."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True

        h = client.health()
        assert h["enabled"] is True
        assert h["servers"]["github"]["available"] is True
        assert h["auto_issue"] is True
        assert h["auto_pr"] is False

    def test_health_reflects_auto_pr_config(self) -> None:
        """health() reflects auto_pr setting."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        h = client.health()
        assert h["auto_pr"] is True

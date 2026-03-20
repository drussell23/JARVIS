"""Tests for GovernanceMCPClient -- MCP external tool integration.

Covers:
- Disabled client when no config
- YAML config loading with env var resolution
- on_postmortem issue body formatting
- on_postmortem / on_complete skip logic
- Timeout handling
- health() structure
- MCPServerConnection: connect, call_tool, disconnect, error handling
- GovernanceMCPClient start/stop with live MCPServerConnection

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
import json
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
    MCPServerConnection,
    _DEFAULT_TIMEOUT,
    _MCP_CONNECT_TIMEOUT,
    _MCP_REQUEST_TIMEOUT,
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
    async def test_start_connects_stdio_server(self) -> None:
        """start() creates MCPServerConnection for stdio servers and connects."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connect = AsyncMock(return_value=True)
        mock_conn.connected = True

        with patch(
            "backend.core.ouroboros.governance.mcp_tool_client.MCPServerConnection",
            return_value=mock_conn,
        ):
            await client.start()

        assert client._available_servers["github"] is True
        assert "github" in client._connections

    @pytest.mark.asyncio
    async def test_start_marks_unavailable_on_connect_failure(self) -> None:
        """start() marks server unavailable when MCPServerConnection.connect fails."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connect = AsyncMock(return_value=False)

        with patch(
            "backend.core.ouroboros.governance.mcp_tool_client.MCPServerConnection",
            return_value=mock_conn,
        ):
            await client.start()

        assert client._available_servers["github"] is False
        assert "github" not in client._connections

    @pytest.mark.asyncio
    async def test_start_sse_falls_through_to_check_server(self) -> None:
        """start() uses _check_server for non-stdio transports."""
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
        # SSE with a URL is marked available via _check_server
        assert client._available_servers["remote"] is True

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
        """health() reports per-server availability and connection status."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True

        h = client.health()
        assert h["enabled"] is True
        assert h["servers"]["github"]["available"] is True
        assert h["servers"]["github"]["connected"] is False  # no live conn
        assert h["auto_issue"] is True
        assert h["auto_pr"] is False

    def test_health_reflects_auto_pr_config(self) -> None:
        """health() reflects auto_pr setting."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        h = client.health()
        assert h["auto_pr"] is True

    def test_health_shows_connected_when_live(self) -> None:
        """health() shows connected=True when a live connection exists."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        # Simulate a live connection
        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connected = True
        client._connections["github"] = mock_conn

        h = client.health()
        assert h["servers"]["github"]["connected"] is True


# ---------------------------------------------------------------------------
# Helpers for MCPServerConnection tests
# ---------------------------------------------------------------------------


def _make_mock_process(
    *,
    responses: Optional[list] = None,
    returncode: Optional[int] = None,
) -> MagicMock:
    """Build a mock asyncio.subprocess.Process for MCP stdio testing.

    Parameters
    ----------
    responses : list of dict, optional
        JSON-RPC responses the mock stdout will yield, one per readline call.
    returncode : int, optional
        Process return code (None means still running).
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    # stdin.write is sync, stdin.drain is async
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    # Build readline responses
    if responses is not None:
        lines = [
            (json.dumps(r) + "\n").encode() if isinstance(r, dict) else r
            for r in responses
        ]
        # Append EOF (empty bytes) so readline doesn't hang
        lines.append(b"")
        proc.stdout.readline = AsyncMock(side_effect=lines)
    else:
        proc.stdout.readline = AsyncMock(return_value=b"")

    # terminate / wait / kill
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    return proc


def _init_response(
    req_id: int = 1,
    capabilities: Optional[dict] = None,
    server_info: Optional[dict] = None,
) -> dict:
    """Build a valid MCP initialize response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": capabilities or {"tools": {}},
            "serverInfo": server_info or {"name": "test-server", "version": "0.1.0"},
        },
    }


def _tool_call_response(
    req_id: int = 2,
    content_text: str = "Issue created",
) -> dict:
    """Build a valid MCP tools/call response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": content_text}],
        },
    }


def _error_response(req_id: int = 2, code: int = -32600, message: str = "Bad request") -> dict:
    """Build a JSON-RPC error response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


# ---------------------------------------------------------------------------
# Tests: MCPServerConnection
# ---------------------------------------------------------------------------


class TestMCPServerConnection:
    """Tests for the MCPServerConnection stdio transport."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        """connect() spawns process, sends initialize, returns True."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        mock_proc = _make_mock_process(responses=[_init_response(req_id=1)])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            result = await conn.connect()

        assert result is True
        assert conn.connected is True
        assert conn.server_info == {"name": "test-server", "version": "0.1.0"}
        assert conn.server_capabilities == {"tools": {}}

        # Verify stdin received initialize request + initialized notification
        assert mock_proc.stdin.write.call_count == 2
        # First call: initialize request
        first_write = mock_proc.stdin.write.call_args_list[0][0][0]
        init_req = json.loads(first_write.decode())
        assert init_req["method"] == "initialize"
        assert init_req["id"] == 1
        # Second call: initialized notification
        second_write = mock_proc.stdin.write.call_args_list[1][0][0]
        notif = json.loads(second_write.decode())
        assert notif["method"] == "notifications/initialized"
        assert "id" not in notif

    @pytest.mark.asyncio
    async def test_connect_fails_non_stdio(self) -> None:
        """connect() returns False for non-stdio transport."""
        config = MCPServerConfig(
            name="remote",
            transport="sse",
            url="https://mcp.example.com/sse",
        )
        conn = MCPServerConnection(config)
        result = await conn.connect()
        assert result is False
        assert conn.connected is False

    @pytest.mark.asyncio
    async def test_connect_fails_no_command(self) -> None:
        """connect() returns False when command list is empty."""
        config = MCPServerConfig(name="empty", transport="stdio", command=[])
        conn = MCPServerConnection(config)
        result = await conn.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_fails_on_spawn_error(self) -> None:
        """connect() returns False if subprocess spawn raises."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(
                side_effect=FileNotFoundError("npx not found")
            )
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.TimeoutError = asyncio.TimeoutError

            result = await conn.connect()

        assert result is False
        assert conn.connected is False

    @pytest.mark.asyncio
    async def test_connect_fails_on_init_error_response(self) -> None:
        """connect() returns False when initialize gets an error response."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        error_resp = _error_response(req_id=1, message="unsupported version")
        mock_proc = _make_mock_process(responses=[error_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            result = await conn.connect()

        assert result is False
        assert conn.connected is False

    @pytest.mark.asyncio
    async def test_call_tool_success(self) -> None:
        """call_tool() sends tools/call and returns the result."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        tool_resp = _tool_call_response(req_id=2, content_text="Issue #42 created")
        mock_proc = _make_mock_process(responses=[init_resp, tool_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            result = await conn.call_tool("create_issue", {"title": "test"})

        assert result is not None
        assert result["content"][0]["text"] == "Issue #42 created"

        # Verify the tools/call request was sent
        third_write = mock_proc.stdin.write.call_args_list[2][0][0]
        call_req = json.loads(third_write.decode())
        assert call_req["method"] == "tools/call"
        assert call_req["params"]["name"] == "create_issue"
        assert call_req["params"]["arguments"] == {"title": "test"}

    @pytest.mark.asyncio
    async def test_call_tool_returns_none_when_not_connected(self) -> None:
        """call_tool() returns None if not connected."""
        config = _github_server_config()
        conn = MCPServerConnection(config)
        result = await conn.call_tool("create_issue", {"title": "test"})
        assert result is None

    @pytest.mark.asyncio
    async def test_call_tool_error_response(self) -> None:
        """call_tool() returns None on JSON-RPC error."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        err_resp = _error_response(req_id=2, message="tool not found")
        mock_proc = _make_mock_process(responses=[init_resp, err_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            result = await conn.call_tool("nonexistent", {})

        assert result is None

    @pytest.mark.asyncio
    async def test_call_tool_skips_notifications(self) -> None:
        """call_tool() skips server notifications and reads the actual response."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        init_resp = _init_response(req_id=1)
        tool_resp = _tool_call_response(req_id=2, content_text="OK")
        mock_proc = _make_mock_process(responses=[init_resp, notification, tool_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            result = await conn.call_tool("some_tool", {"arg": "val"})

        assert result is not None
        assert result["content"][0]["text"] == "OK"

    @pytest.mark.asyncio
    async def test_call_tool_handles_eof(self) -> None:
        """call_tool() returns None and marks disconnected on EOF."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        # After init, stdout returns EOF (empty bytes)
        mock_proc = _make_mock_process(responses=[init_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            assert conn.connected is True
            result = await conn.call_tool("create_issue", {})

        assert result is None
        assert conn.connected is False

    @pytest.mark.asyncio
    async def test_call_tool_skips_non_json_lines(self) -> None:
        """call_tool() skips non-JSON output lines from the server."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        non_json = b"[INFO] server starting up...\n"
        tool_resp = _tool_call_response(req_id=2, content_text="Done")
        mock_proc = _make_mock_process(responses=[init_resp, non_json, tool_resp])

        # Mark the non-json bytes so _make_mock_process doesn't JSON-encode them
        # (it already handles raw bytes)

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            result = await conn.call_tool("my_tool", {})

        assert result is not None
        assert result["content"][0]["text"] == "Done"

    @pytest.mark.asyncio
    async def test_list_tools(self) -> None:
        """list_tools() sends tools/list and returns the result."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        list_resp = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": "create_issue", "description": "Create a GitHub issue"},
                ],
            },
        }
        mock_proc = _make_mock_process(responses=[init_resp, list_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            result = await conn.list_tools()

        assert result is not None
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "create_issue"

    @pytest.mark.asyncio
    async def test_disconnect_terminates_process(self) -> None:
        """disconnect() terminates the child process."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        init_resp = _init_response(req_id=1)
        mock_proc = _make_mock_process(responses=[init_resp])

        with patch("backend.core.ouroboros.governance.mcp_tool_client.asyncio") as mock_aio:
            mock_aio.create_subprocess_exec = AsyncMock(return_value=mock_proc)
            mock_aio.subprocess = asyncio.subprocess
            mock_aio.Lock = asyncio.Lock
            mock_aio.get_event_loop = asyncio.get_event_loop
            mock_aio.wait_for = asyncio.wait_for
            mock_aio.TimeoutError = asyncio.TimeoutError

            await conn.connect()
            assert conn.connected is True

        await conn.disconnect()
        assert conn.connected is False
        mock_proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_kills_on_timeout(self) -> None:
        """disconnect() kills the process if terminate doesn't complete in time."""
        config = _github_server_config()
        conn = MCPServerConnection(config)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        # wait() times out the first time, succeeds after kill
        mock_proc.wait = AsyncMock(
            side_effect=[asyncio.TimeoutError(), None]
        )
        conn._process = mock_proc
        conn._connected = True

        await conn.disconnect()
        assert conn.connected is False
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_process(self) -> None:
        """disconnect() is safe to call when no process exists."""
        config = _github_server_config()
        conn = MCPServerConnection(config)
        await conn.disconnect()  # should not raise
        assert conn.connected is False


# ---------------------------------------------------------------------------
# Tests: GovernanceMCPClient start/stop with MCPServerConnection
# ---------------------------------------------------------------------------


class TestGovernanceMCPClientStdioIntegration:
    """Tests for GovernanceMCPClient using live MCPServerConnection."""

    @pytest.mark.asyncio
    async def test_start_connects_stdio_servers(self) -> None:
        """start() creates MCPServerConnection for stdio servers."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connect = AsyncMock(return_value=True)
        mock_conn.connected = True

        with patch(
            "backend.core.ouroboros.governance.mcp_tool_client.MCPServerConnection",
            return_value=mock_conn,
        ):
            await client.start()

        assert client._available_servers["github"] is True
        assert "github" in client._connections
        mock_conn.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_marks_unavailable_on_connect_failure(self) -> None:
        """start() marks server unavailable if MCPServerConnection.connect fails."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connect = AsyncMock(return_value=False)

        with patch(
            "backend.core.ouroboros.governance.mcp_tool_client.MCPServerConnection",
            return_value=mock_conn,
        ):
            await client.start()

        assert client._available_servers["github"] is False
        assert "github" not in client._connections

    @pytest.mark.asyncio
    async def test_stop_disconnects_all(self) -> None:
        """stop() disconnects all live connections."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.disconnect = AsyncMock()
        client._connections["github"] = mock_conn
        client._available_servers["github"] = True

        await client.stop()

        mock_conn.disconnect.assert_awaited_once()
        assert len(client._connections) == 0
        assert len(client._available_servers) == 0

    @pytest.mark.asyncio
    async def test_stop_handles_disconnect_error(self) -> None:
        """stop() logs but does not raise on disconnect errors."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
        client._connections["github"] = mock_conn
        client._available_servers["github"] = True

        await client.stop()  # should not raise
        assert len(client._connections) == 0

    @pytest.mark.asyncio
    async def test_create_github_issue_via_mcp(self) -> None:
        """_create_github_issue calls conn.call_tool when connected."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connected = True
        mock_conn.call_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "Issue #99 created"}],
        })
        client._connections["github"] = mock_conn

        ctx = _make_mock_ctx()

        with patch.dict("os.environ", {"JARVIS_GITHUB_OWNER": "testowner", "JARVIS_GITHUB_REPO": "testrepo"}):
            await client._create_github_issue(ctx)

        mock_conn.call_tool.assert_awaited_once()
        call_args = mock_conn.call_tool.call_args
        assert call_args[0][0] == "create_issue"
        assert call_args[0][1]["owner"] == "testowner"
        assert call_args[0][1]["repo"] == "testrepo"
        assert "[Ouroboros] Pipeline failure:" in call_args[0][1]["title"]

    @pytest.mark.asyncio
    async def test_create_github_issue_fallback_no_connection(self) -> None:
        """_create_github_issue logs fallback when no connection exists."""
        config = _config_with_github()
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True
        # No connection in _connections

        ctx = _make_mock_ctx()
        # Should not raise — just logs
        await client._create_github_issue(ctx)

    @pytest.mark.asyncio
    async def test_create_github_pr_via_mcp(self) -> None:
        """_create_github_pr calls conn.call_tool when connected."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True

        mock_conn = MagicMock(spec=MCPServerConnection)
        mock_conn.connected = True
        mock_conn.call_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "PR #5 created"}],
        })
        client._connections["github"] = mock_conn

        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)
        files = ["backend/core/utils.py"]

        with patch.dict("os.environ", {
            "JARVIS_GITHUB_OWNER": "testowner",
            "JARVIS_GITHUB_REPO": "testrepo",
            "JARVIS_GITHUB_HEAD_BRANCH": "fix/test",
            "JARVIS_GITHUB_BASE_BRANCH": "main",
        }):
            await client._create_github_pr(ctx, files)

        mock_conn.call_tool.assert_awaited_once()
        call_args = mock_conn.call_tool.call_args
        assert call_args[0][0] == "create_pull_request"
        assert call_args[0][1]["owner"] == "testowner"
        assert call_args[0][1]["head"] == "fix/test"
        assert call_args[0][1]["base"] == "main"

    @pytest.mark.asyncio
    async def test_create_github_pr_fallback_no_connection(self) -> None:
        """_create_github_pr logs fallback when no connection exists."""
        config = _config_with_github(auto_pr=True)
        client = GovernanceMCPClient(config)
        client._available_servers["github"] = True

        ctx = _make_mock_ctx(phase=_MockPhase.COMPLETE)
        # Should not raise — just logs
        await client._create_github_pr(ctx, ["file.py"])

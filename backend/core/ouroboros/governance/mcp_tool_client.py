"""
MCP External Tool Client
=========================

Connects the governance pipeline to external tool servers via MCP (Model Context Protocol).

Provides post-pipeline hooks:
  on_postmortem  -- create GitHub issues for pipeline failures
  on_complete    -- optionally create PRs for applied changes
  on_alert       -- send notifications to configured channels

Configuration via YAML file at JARVIS_MCP_CONFIG path, or disabled if not set.

All MCP calls are fire-and-forget with timeout. Failures are logged but never
block the governance pipeline.

MCP stdio protocol implementation
----------------------------------
MCPServerConnection manages a live subprocess running an MCP server (e.g.
``npx -y @modelcontextprotocol/server-github``).  Communication uses JSON-RPC
2.0 over newline-delimited stdin/stdout:

  1. ``connect()``  — spawn process, send ``initialize``, receive capabilities,
     send ``notifications/initialized``.
  2. ``call_tool()`` — send ``tools/call`` request, wait for JSON-RPC response.
  3. ``disconnect()`` — terminate the child process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.MCPToolClient")

_DEFAULT_TIMEOUT = 10.0  # seconds per MCP tool call
_MCP_REQUEST_TIMEOUT = 30.0  # seconds per individual MCP JSON-RPC request
_MCP_CONNECT_TIMEOUT = 15.0  # seconds for the initialize handshake


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    transport: str  # "stdio" or "sse"
    command: List[str] = field(default_factory=list)  # for stdio transport
    url: str = ""  # for sse transport
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class MCPClientConfig:
    """Configuration for all MCP server connections."""

    servers: Dict[str, MCPServerConfig] = field(default_factory=dict)
    auto_issue: bool = True       # Create GitHub issues on POSTMORTEM
    auto_pr: bool = False         # Create PRs on COMPLETE (opt-in)
    enabled: bool = True

    @classmethod
    def from_file(cls, config_path: str) -> MCPClientConfig:
        """Load config from YAML file."""
        path = Path(config_path)
        if not path.exists():
            return cls(enabled=False)
        try:
            import yaml  # type: ignore[import-untyped]

            with open(path) as f:
                data = yaml.safe_load(f) or {}
            servers: Dict[str, MCPServerConfig] = {}
            for name, server_data in data.get("servers", {}).items():
                # Resolve env var references in env dict
                env: Dict[str, str] = {}
                for k, v in (server_data.get("env") or {}).items():
                    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                        env[k] = os.getenv(v[2:-1], "")
                    else:
                        env[k] = str(v)
                servers[name] = MCPServerConfig(
                    name=name,
                    transport=server_data.get("transport", "stdio"),
                    command=server_data.get("command", []),
                    url=server_data.get("url", ""),
                    env=env,
                )
            return cls(
                servers=servers,
                auto_issue=data.get("auto_issue", True),
                auto_pr=data.get("auto_pr", False),
                enabled=bool(servers),
            )
        except Exception as exc:
            logger.warning("Failed to load MCP config from %s: %s", config_path, exc)
            return cls(enabled=False)

    @classmethod
    def from_env(cls) -> MCPClientConfig:
        """Load config from JARVIS_MCP_CONFIG env var."""
        config_path = os.getenv("JARVIS_MCP_CONFIG", "")
        if not config_path:
            return cls(enabled=False)
        return cls.from_file(config_path)


# ---------------------------------------------------------------------------
# MCP stdio transport — live connection to a single MCP server process
# ---------------------------------------------------------------------------


class MCPServerConnection:
    """A live connection to an MCP server via stdio transport.

    Manages the full lifecycle of a child MCP server process:
    * Spawn the process with the configured command and environment.
    * Perform the JSON-RPC ``initialize`` / ``notifications/initialized``
      handshake required by the MCP specification.
    * Expose ``call_tool`` for invoking tools on the server.
    * Gracefully terminate on ``disconnect``.

    All I/O is async and uses ``asyncio.subprocess``.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id: int = 0
        self._connected: bool = False
        self._server_capabilities: Optional[Dict[str, Any]] = None
        self._server_info: Optional[Dict[str, Any]] = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Whether the connection is live and initialized."""
        return self._connected

    @property
    def server_capabilities(self) -> Optional[Dict[str, Any]]:
        """Capabilities reported by the server during ``initialize``."""
        return self._server_capabilities

    @property
    def server_info(self) -> Optional[Dict[str, Any]]:
        """Server info reported during ``initialize``."""
        return self._server_info

    async def connect(self) -> bool:
        """Spawn the MCP server process and perform the initialize handshake.

        Returns ``True`` if the server started and completed initialization,
        ``False`` otherwise (logged as warning, never raises).
        """
        if self._config.transport != "stdio" or not self._config.command:
            logger.warning(
                "MCPServerConnection requires stdio transport with a command; "
                "server=%s transport=%s",
                self._config.name,
                self._config.transport,
            )
            return False

        try:
            env = dict(os.environ)
            env.update(self._config.env)

            self._process = await asyncio.create_subprocess_exec(
                *self._config.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            # --- MCP initialize handshake ---
            result = await self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "ouroboros-governance",
                        "version": "1.0.0",
                    },
                },
                timeout=_MCP_CONNECT_TIMEOUT,
            )

            if result is not None:
                self._server_capabilities = result.get("capabilities")
                self._server_info = result.get("serverInfo")

                # Acknowledge initialization
                await self._send_notification("notifications/initialized", {})
                self._connected = True
                logger.info(
                    "MCP server %s connected (serverInfo=%s)",
                    self._config.name,
                    self._server_info,
                )
                return True

            logger.warning(
                "MCP initialize returned None for server=%s", self._config.name
            )
            await self.disconnect()
            return False

        except Exception as exc:
            logger.warning(
                "MCP connect failed for %s: %s", self._config.name, exc
            )
            await self.disconnect()
            return False

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call a tool on the MCP server.

        Parameters
        ----------
        tool_name : str
            The MCP tool name (e.g. ``"create_issue"``).
        arguments : dict
            Tool-specific arguments.
        timeout : float, optional
            Per-request timeout; defaults to ``_MCP_REQUEST_TIMEOUT``.

        Returns
        -------
        dict or None
            The ``result`` field from the JSON-RPC response, or ``None``
            on any failure.
        """
        if not self._connected or self._process is None:
            return None
        return await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout or _MCP_REQUEST_TIMEOUT,
        )

    async def list_tools(
        self, *, timeout: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """List available tools on the MCP server.

        Returns the ``result`` from ``tools/list``, which typically contains
        a ``tools`` array.
        """
        if not self._connected or self._process is None:
            return None
        return await self._send_request(
            "tools/list",
            {},
            timeout=timeout or _MCP_REQUEST_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Internal JSON-RPC transport
    # ------------------------------------------------------------------

    async def _send_request(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        timeout: float = _MCP_REQUEST_TIMEOUT,
    ) -> Optional[Dict[str, Any]]:
        """Send a JSON-RPC 2.0 request and wait for the matching response.

        Uses a lock to serialize concurrent requests on the same stdio pipe.
        Notifications from the server (messages without ``id``) are silently
        skipped while waiting for the response.
        """
        if (
            self._process is None
            or self._process.stdin is None
            or self._process.stdout is None
        ):
            return None

        async with self._lock:
            self._request_id += 1
            req_id = self._request_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            line = json.dumps(request) + "\n"
            self._process.stdin.write(line.encode())
            await self._process.stdin.drain()

            # Read lines until we find the matching response
            try:
                deadline = asyncio.get_event_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        logger.warning(
                            "MCP request timed out: method=%s id=%d", method, req_id
                        )
                        return None

                    response_line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=remaining,
                    )

                    if not response_line:
                        # EOF — process exited
                        logger.warning(
                            "MCP server %s: EOF on stdout (process exited?)",
                            self._config.name,
                        )
                        self._connected = False
                        return None

                    try:
                        response = json.loads(response_line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError) as parse_err:
                        logger.debug(
                            "MCP: skipping non-JSON line from %s: %s",
                            self._config.name,
                            parse_err,
                        )
                        continue

                    # Skip server-initiated notifications (no "id" field)
                    if "id" not in response:
                        logger.debug(
                            "MCP notification from %s: method=%s",
                            self._config.name,
                            response.get("method", "?"),
                        )
                        continue

                    # Check if this is our response
                    if response.get("id") != req_id:
                        logger.debug(
                            "MCP: skipping response with mismatched id=%s (expected %d)",
                            response.get("id"),
                            req_id,
                        )
                        continue

                    if "result" in response:
                        return response["result"]
                    elif "error" in response:
                        logger.warning(
                            "MCP error from %s: %s",
                            self._config.name,
                            response["error"],
                        )
                        return None
                    else:
                        logger.warning(
                            "MCP malformed response from %s: %s",
                            self._config.name,
                            response,
                        )
                        return None

            except asyncio.TimeoutError:
                logger.warning(
                    "MCP request timed out: method=%s server=%s",
                    method,
                    self._config.name,
                )
            except Exception as exc:
                logger.warning(
                    "MCP response error from %s: %s", self._config.name, exc
                )
            return None

    async def _send_notification(
        self, method: str, params: Dict[str, Any]
    ) -> None:
        """Send a JSON-RPC 2.0 notification (no ``id``, no response expected)."""
        if self._process is None or self._process.stdin is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(notification) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Terminate the MCP server process and clean up."""
        self._connected = False
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                if self._process.returncode is None:
                    self._process.kill()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                if self._process.returncode is None:
                    self._process.kill()
            finally:
                self._process = None


class GovernanceMCPClient:
    """MCP client for governance pipeline external actions.

    Connects to configured MCP servers and provides hooks for:
    - Creating GitHub issues on pipeline failures
    - Creating PRs for applied changes
    - Sending alerts/notifications

    All operations are fire-and-forget with configurable timeout.

    In ``start()``, each configured stdio server is spawned as a child
    process and the MCP initialize handshake is performed.  If a server
    fails to connect, it is marked unavailable and its tools are skipped.
    """

    def __init__(self, config: Optional[MCPClientConfig] = None) -> None:
        self._config = config or MCPClientConfig.from_env()
        self._available_servers: Dict[str, bool] = {}
        self._connections: Dict[str, MCPServerConnection] = {}

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled and bool(self._config.servers)

    async def start(self) -> None:
        """Connect to all configured MCP servers.

        For stdio servers, spawns the child process and performs the MCP
        initialize handshake.  For SSE servers, marks them available if
        a URL is configured (actual SSE transport is not yet implemented).
        """
        if not self.is_enabled:
            logger.debug("MCP client disabled -- no servers configured")
            return
        for name, server in self._config.servers.items():
            if server.transport == "stdio" and server.command:
                conn = MCPServerConnection(server)
                connected = await conn.connect()
                self._available_servers[name] = connected
                if connected:
                    self._connections[name] = conn
                status = "connected" if connected else "unavailable"
                logger.info("MCP server %s: %s", name, status)
            else:
                # SSE or other transports — availability check only
                available = await self._check_server(server)
                self._available_servers[name] = available
                status = "available" if available else "unavailable"
                logger.info("MCP server %s: %s", name, status)

    async def stop(self) -> None:
        """Disconnect all live MCP server connections."""
        for name, conn in list(self._connections.items()):
            try:
                await conn.disconnect()
                logger.info("MCP server %s: disconnected", name)
            except Exception as exc:
                logger.warning("MCP server %s: disconnect error: %s", name, exc)
        self._connections.clear()
        self._available_servers.clear()

    async def _check_server(self, server: MCPServerConfig) -> bool:
        """Check if an MCP server is reachable (legacy path for non-stdio)."""
        if server.transport == "stdio" and server.command:
            try:
                cmd = server.command[0]
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["which", cmd],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return False
        return bool(server.url)

    async def discover_tools(self) -> List[Dict[str, Any]]:
        """Discover tools from all connected MCP servers.

        Returns a list of tool descriptors, each with:
          - ``name``: qualified tool name (``server_name:tool_name``)
          - ``server``: MCP server name
          - ``description``: tool description from the MCP server
          - ``input_schema``: JSON Schema for the tool's arguments

        Called during prompt construction so the model knows which
        external tools are available. Fire-and-forget on failure.
        """
        tools: List[Dict[str, Any]] = []
        if not self.is_enabled:
            return tools
        for name, conn in self._connections.items():
            if not conn.connected:
                continue
            try:
                result = await asyncio.wait_for(
                    conn.list_tools(timeout=_MCP_REQUEST_TIMEOUT),
                    timeout=_MCP_REQUEST_TIMEOUT + 2.0,
                )
                if result is None:
                    continue
                raw_tools = result.get("tools", [])
                for tool in raw_tools:
                    tool_name = tool.get("name", "")
                    if not tool_name:
                        continue
                    tools.append({
                        "name": f"mcp_{name}_{tool_name}",
                        "server": name,
                        "original_name": tool_name,
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema", {}),
                    })
                logger.debug(
                    "[MCPClient] Discovered %d tools from %s", len(raw_tools), name,
                )
            except Exception as exc:
                logger.debug("[MCPClient] Tool discovery failed for %s: %s", name, exc)
        return tools

    async def call_tool(
        self,
        qualified_name: str,
        arguments: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call an MCP tool by its qualified name (``server_name:tool_name``).

        The qualified name format is ``mcp_{server}_{tool}``. This method
        strips the prefix and routes to the correct server connection.
        """
        # Parse qualified name: mcp_{server}_{tool}
        prefix = "mcp_"
        if not qualified_name.startswith(prefix):
            return None
        remainder = qualified_name[len(prefix):]
        # Find the server name that matches
        for server_name, conn in self._connections.items():
            if remainder.startswith(server_name + "_"):
                tool_name = remainder[len(server_name) + 1:]
                if conn.connected:
                    return await conn.call_tool(
                        tool_name, arguments, timeout=timeout,
                    )
        return None

    async def on_postmortem(self, ctx: Any) -> None:
        """React to pipeline failures with external actions.

        Parameters
        ----------
        ctx : OperationContext
            Context with terminal phase POSTMORTEM.
        """
        if not self.is_enabled or not self._config.auto_issue:
            return

        if self._available_servers.get("github"):
            try:
                await asyncio.wait_for(
                    self._create_github_issue(ctx),
                    timeout=_DEFAULT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("GitHub issue creation timed out for op=%s", ctx.op_id)
            except Exception as exc:
                logger.warning("GitHub issue creation failed: %s", exc)

    async def on_complete(self, ctx: Any, applied_files: List[str]) -> None:
        """React to successful operations with external actions.

        Parameters
        ----------
        ctx : OperationContext
            Context with terminal phase COMPLETE.
        applied_files : list of str
            File paths that were modified.
        """
        if not self.is_enabled or not self._config.auto_pr or not applied_files:
            return

        if self._available_servers.get("github"):
            try:
                await asyncio.wait_for(
                    self._create_github_pr(ctx, applied_files),
                    timeout=_DEFAULT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("GitHub PR creation timed out for op=%s", ctx.op_id)
            except Exception as exc:
                logger.warning("GitHub PR creation failed: %s", exc)

    async def on_alert(self, message: str, *, severity: str = "info") -> None:
        """Send a notification to configured alert channels.

        Parameters
        ----------
        message : str
            Alert message body.
        severity : str
            One of ``"info"``, ``"warning"``, ``"critical"``.
        """
        if not self.is_enabled:
            return
        logger.info("MCP alert [%s]: %s", severity, message[:200])

    async def _create_github_issue(self, ctx: Any) -> None:
        """Create a GitHub issue for a pipeline failure via MCP tool call."""
        title = f"[Ouroboros] Pipeline failure: {ctx.description[:80]}"
        body = self._format_failure_body(ctx)

        conn = self._connections.get("github")
        if conn is not None and conn.connected:
            repo = os.getenv("JARVIS_GITHUB_REPO", "")
            owner = os.getenv("JARVIS_GITHUB_OWNER", "")
            result = await conn.call_tool(
                "create_issue",
                {
                    "owner": owner,
                    "repo": repo,
                    "title": title,
                    "body": body,
                },
            )
            if result is not None:
                logger.info(
                    "GitHub issue created via MCP: %s",
                    result.get("content", [{}])[0].get("text", "")
                    if isinstance(result.get("content"), list)
                    else result,
                )
            else:
                logger.warning(
                    "GitHub issue creation returned no result for op=%s",
                    ctx.op_id,
                )
        else:
            # Fallback: log only (no live connection)
            logger.info("Would create GitHub issue (no MCP connection): %s", title)
            logger.debug("GitHub issue body:\n%s", body[:500])

    async def _create_github_pr(self, ctx: Any, applied_files: List[str]) -> None:
        """Create a GitHub PR for applied changes via MCP tool call."""
        title = f"[Ouroboros] {ctx.description[:80]}"
        body_parts = [
            "## Ouroboros Auto-PR",
            "",
            f"**Operation ID**: `{ctx.op_id}`",
            f"**Description**: {ctx.description}",
            "",
            "### Modified files",
            "",
        ]
        body_parts.extend(f"- `{f}`" for f in applied_files)
        body_parts.extend([
            "",
            "---",
            "*Auto-generated by Ouroboros governance pipeline*",
        ])
        pr_body = "\n".join(body_parts)

        conn = self._connections.get("github")
        if conn is not None and conn.connected:
            repo = os.getenv("JARVIS_GITHUB_REPO", "")
            owner = os.getenv("JARVIS_GITHUB_OWNER", "")
            head_branch = os.getenv(
                "JARVIS_GITHUB_HEAD_BRANCH", f"ouroboros/{ctx.op_id}"
            )
            base_branch = os.getenv("JARVIS_GITHUB_BASE_BRANCH", "main")
            result = await conn.call_tool(
                "create_pull_request",
                {
                    "owner": owner,
                    "repo": repo,
                    "title": title,
                    "body": pr_body,
                    "head": head_branch,
                    "base": base_branch,
                },
            )
            if result is not None:
                logger.info(
                    "GitHub PR created via MCP: %s",
                    result.get("content", [{}])[0].get("text", "")
                    if isinstance(result.get("content"), list)
                    else result,
                )
            else:
                logger.warning(
                    "GitHub PR creation returned no result for op=%s",
                    ctx.op_id,
                )
        else:
            # Fallback: log only (no live connection)
            logger.info(
                "Would create GitHub PR (no MCP connection): %s (files: %s)",
                title,
                applied_files,
            )

    @staticmethod
    def _format_failure_body(ctx: Any) -> str:
        """Format a GitHub issue body from an OperationContext."""
        parts = [
            "## Pipeline Failure Report",
            "",
            f"**Operation ID**: `{ctx.op_id}`",
            f"**Description**: {ctx.description}",
            f"**Phase**: `{ctx.phase.name}`",
        ]
        if ctx.risk_tier is not None:
            parts.append(f"**Risk Tier**: `{ctx.risk_tier.name}`")
        parts.extend([
            f"**Reason**: `{ctx.terminal_reason_code}`",
            f"**Target Files**: {', '.join(ctx.target_files) if ctx.target_files else 'none'}",
            "",
            "---",
            "*Auto-generated by Ouroboros governance pipeline*",
        ])
        return "\n".join(parts)

    def health(self) -> Dict[str, Any]:
        """Return health status."""
        return {
            "enabled": self.is_enabled,
            "servers": {
                name: {
                    "available": avail,
                    "connected": name in self._connections
                    and self._connections[name].connected,
                }
                for name, avail in self._available_servers.items()
            },
            "auto_issue": self._config.auto_issue,
            "auto_pr": self._config.auto_pr,
        }

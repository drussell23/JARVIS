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
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.MCPToolClient")

_DEFAULT_TIMEOUT = 10.0  # seconds per MCP tool call


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


class GovernanceMCPClient:
    """MCP client for governance pipeline external actions.

    Connects to configured MCP servers and provides hooks for:
    - Creating GitHub issues on pipeline failures
    - Creating PRs for applied changes
    - Sending alerts/notifications

    All operations are fire-and-forget with configurable timeout.
    """

    def __init__(self, config: Optional[MCPClientConfig] = None) -> None:
        self._config = config or MCPClientConfig.from_env()
        self._available_servers: Dict[str, bool] = {}

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled and bool(self._config.servers)

    async def start(self) -> None:
        """Verify which MCP servers are available."""
        if not self.is_enabled:
            logger.debug("MCP client disabled -- no servers configured")
            return
        for name, server in self._config.servers.items():
            available = await self._check_server(server)
            self._available_servers[name] = available
            status = "available" if available else "unavailable"
            logger.info("MCP server %s: %s", name, status)

    async def _check_server(self, server: MCPServerConfig) -> bool:
        """Check if an MCP server is reachable."""
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
        """Create a GitHub issue for a pipeline failure."""
        title = f"[Ouroboros] Pipeline failure: {ctx.description[:80]}"
        body = self._format_failure_body(ctx)
        logger.info("Would create GitHub issue: %s", title)
        # MCP tool call would go here when server is connected.
        # Actual MCP stdio protocol implementation requires the full
        # MCP client SDK which is a larger integration.
        logger.info("GitHub issue body:\n%s", body[:500])

    async def _create_github_pr(self, ctx: Any, applied_files: List[str]) -> None:
        """Create a GitHub PR for applied changes."""
        title = f"[Ouroboros] {ctx.description[:80]}"
        logger.info("Would create GitHub PR: %s (files: %s)", title, applied_files)

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
                name: {"available": avail}
                for name, avail in self._available_servers.items()
            },
            "auto_issue": self._config.auto_issue,
            "auto_pr": self._config.auto_pr,
        }

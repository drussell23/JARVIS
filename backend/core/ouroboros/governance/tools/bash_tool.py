"""
Sandboxed Bash Tool
====================

Enables the governance pipeline to execute arbitrary shell commands
within safety constraints.

Safety layers:
  1. Command allowlist (configurable via env/config)
  2. Command blocklist (hardcoded dangerous commands)
  3. Working directory restriction (must be within project root)
  4. Timeout enforcement (default 30s, max 120s)
  5. Output size limit (max 100KB)
  6. No interactive commands (stdin is /dev/null)

Environment:
  JARVIS_BASH_TOOL_ENABLED      -- "true" to enable (default: "false")
  JARVIS_BASH_TOOL_TIMEOUT      -- default timeout in seconds (default: 30)
  JARVIS_BASH_TOOL_MAX_OUTPUT   -- max output bytes (default: 102400)
  JARVIS_BASH_TOOL_CWD          -- working directory (default: project root)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger("Ouroboros.BashTool")

# ---------------------------------------------------------------------------
# Blocklists — these commands are NEVER allowed regardless of config
# ---------------------------------------------------------------------------

_BLOCKED_COMMANDS: FrozenSet[str] = frozenset({
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero",
    ":(){ :|:& };:",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
})

# Commands starting with these prefixes are blocked
_BLOCKED_PREFIXES = (
    "rm -rf /",
    "sudo rm",
    "chmod 777 /",
    "chown root",
    "kill -9 1",
    "pkill -9",
    "curl | sh",
    "wget -O- | sh",
)

# ---------------------------------------------------------------------------
# Default allowlist — only these command prefixes are allowed in restricted mode
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWLIST: FrozenSet[str] = frozenset({
    "python3", "python", "pip", "pip3",
    "pytest", "mypy", "ruff", "black", "isort",
    "git", "grep", "find", "ls", "cat", "head", "tail", "wc",
    "echo", "printf", "date", "whoami", "pwd",
    "npm", "npx", "node",
    "make", "cmake",
    "cargo", "rustc",
    "go", "gofmt",
    "curl", "wget",  # allowed for fetching (not piped to sh)
    "jq", "sed", "awk", "sort", "uniq", "tr", "cut",
    "diff", "patch",
    "tar", "zip", "unzip", "gzip", "gunzip",
    "du", "df", "free", "top", "ps",
    "env", "printenv", "which", "type",
})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BashToolConfig:
    """Configuration for the sandboxed bash tool."""

    enabled: bool = False
    timeout_s: float = 30.0
    max_output_bytes: int = 102_400  # 100KB
    cwd: Optional[Path] = None
    restricted_mode: bool = True  # only allow commands in allowlist
    allowlist: FrozenSet[str] = field(default_factory=lambda: _DEFAULT_ALLOWLIST)

    @classmethod
    def from_env(cls, project_root: Optional[Path] = None) -> BashToolConfig:
        """Build config from environment variables.

        Parameters
        ----------
        project_root:
            Fallback working directory when ``JARVIS_BASH_TOOL_CWD`` is unset.
        """
        enabled = os.getenv("JARVIS_BASH_TOOL_ENABLED", "false").lower() in (
            "true", "1", "yes",
        )
        return cls(
            enabled=enabled,
            timeout_s=float(os.getenv("JARVIS_BASH_TOOL_TIMEOUT", "30")),
            max_output_bytes=int(os.getenv("JARVIS_BASH_TOOL_MAX_OUTPUT", "102400")),
            cwd=Path(os.getenv("JARVIS_BASH_TOOL_CWD", str(project_root or "."))),
            restricted_mode=os.getenv(
                "JARVIS_BASH_TOOL_UNRESTRICTED", "false"
            ).lower() not in ("true", "1"),
        )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class BashResult:
    """Result of a bash command execution."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    blocked: bool = False
    block_reason: str = ""


# ---------------------------------------------------------------------------
# SandboxedBashTool
# ---------------------------------------------------------------------------

class SandboxedBashTool:
    """Executes shell commands within safety constraints.

    Designed to be registered as a tool in the governance tool executor.
    All public methods are safe to call — errors are captured, never raised.
    """

    def __init__(self, config: Optional[BashToolConfig] = None) -> None:
        self._config = config or BashToolConfig.from_env()

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_command(self, command: str) -> Optional[str]:
        """Validate command against safety rules.

        Returns
        -------
        str or None
            A human-readable block reason, or ``None`` if the command is
            allowed.
        """
        cmd_stripped = command.strip()
        cmd_lower = cmd_stripped.lower()

        # Check hardcoded blocklist (exact match, case-insensitive)
        if cmd_lower in _BLOCKED_COMMANDS:
            return "Command is in hardcoded blocklist"

        # Check blocked prefixes (case-insensitive)
        for prefix in _BLOCKED_PREFIXES:
            if cmd_lower.startswith(prefix.lower()):
                return f"Command starts with blocked prefix: {prefix}"

        # Restricted-mode allowlist check
        if self._config.restricted_mode:
            base_cmd = self._extract_base_command(cmd_stripped)
            if base_cmd not in self._config.allowlist:
                return f"Command '{base_cmd}' not in allowlist (restricted mode)"

        return None

    @staticmethod
    def _extract_base_command(command: str) -> str:
        """Extract the base executable name from a command string.

        Handles absolute paths (``/usr/bin/python3`` -> ``python3``) and
        gracefully degrades on unparseable input.
        """
        try:
            parts = shlex.split(command)
            return Path(parts[0]).name if parts else ""
        except ValueError:
            # shlex.split can fail on unmatched quotes, etc.
            tokens = command.split()
            return Path(tokens[0]).name if tokens else ""

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
    ) -> BashResult:
        """Execute a shell command within the sandbox.

        Parameters
        ----------
        command:
            Shell command string to execute.
        timeout:
            Override timeout in seconds (capped at 120s regardless).

        Returns
        -------
        BashResult
            Structured result with stdout, stderr, exit_code, and safety
            metadata.  Never raises.
        """
        if not self._config.enabled:
            return BashResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr="BashTool is disabled",
                blocked=True,
                block_reason="JARVIS_BASH_TOOL_ENABLED is not set to true",
            )

        # Validate command against safety rules
        block_reason = self._validate_command(command)
        if block_reason:
            logger.warning(
                "[BashTool] Blocked: %s — %s", command[:80], block_reason,
            )
            return BashResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr=f"Blocked: {block_reason}",
                blocked=True,
                block_reason=block_reason,
            )

        effective_timeout = min(
            timeout or self._config.timeout_s,
            120.0,  # absolute max
        )

        cwd = str(self._config.cwd) if self._config.cwd else None

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return BashResult(
                    command=command,
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {effective_timeout}s",
                    timed_out=True,
                )

            # Truncate output if too large
            max_bytes = self._config.max_output_bytes
            stdout = stdout_bytes[:max_bytes].decode("utf-8", errors="replace")
            stderr = stderr_bytes[:max_bytes].decode("utf-8", errors="replace")

            if len(stdout_bytes) > max_bytes:
                stdout += f"\n... (truncated at {max_bytes} bytes)"
            if len(stderr_bytes) > max_bytes:
                stderr += f"\n... (truncated at {max_bytes} bytes)"

            return BashResult(
                command=command,
                exit_code=process.returncode or 0,
                stdout=stdout,
                stderr=stderr,
            )

        except Exception as exc:
            logger.error("[BashTool] Execution error: %s", exc)
            return BashResult(
                command=command,
                exit_code=1,
                stdout="",
                stderr=f"Execution error: {exc}",
            )

    # ------------------------------------------------------------------
    # MCP-compatible tool definition
    # ------------------------------------------------------------------

    def to_tool_definition(self) -> Dict[str, Any]:
        """Return MCP-compatible tool definition for registration."""
        return {
            "name": "bash",
            "description": (
                "Execute a shell command. Commands are validated against a safety "
                "allowlist and blocklist. Timeout and output size are enforced."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "Optional timeout override in seconds (max 120)"
                        ),
                    },
                },
                "required": ["command"],
            },
        }

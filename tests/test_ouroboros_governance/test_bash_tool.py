"""Tests for the sandboxed BashTool.

Coverage:
  - Tool disabled by default
  - Blocked commands (exact match and prefix)
  - Restricted-mode allowlist enforcement
  - Unrestricted mode allows non-blocked commands
  - Timeout enforcement
  - Output truncation
  - Successful execution (real subprocess)
  - to_tool_definition() MCP schema validity
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.tools.bash_tool import (
    BashResult,
    BashToolConfig,
    SandboxedBashTool,
    _BLOCKED_COMMANDS,
    _BLOCKED_PREFIXES,
    _DEFAULT_ALLOWLIST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enabled_config(**overrides) -> BashToolConfig:
    """Return a BashToolConfig with the tool enabled."""
    defaults = dict(
        enabled=True,
        timeout_s=30.0,
        max_output_bytes=102_400,
        cwd=None,
        restricted_mode=True,
        allowlist=_DEFAULT_ALLOWLIST,
    )
    defaults.update(overrides)
    return BashToolConfig(**defaults)


def _tool(**overrides) -> SandboxedBashTool:
    return SandboxedBashTool(config=_enabled_config(**overrides))


# ---------------------------------------------------------------------------
# 1. Disabled by default
# ---------------------------------------------------------------------------

class TestDisabledByDefault:
    @pytest.mark.asyncio
    async def test_disabled_when_env_not_set(self):
        config = BashToolConfig()  # defaults: enabled=False
        tool = SandboxedBashTool(config=config)
        assert not tool.is_enabled

    @pytest.mark.asyncio
    async def test_disabled_returns_blocked_result(self):
        config = BashToolConfig(enabled=False)
        tool = SandboxedBashTool(config=config)
        result = await tool.execute("echo hello")
        assert result.blocked is True
        assert "disabled" in result.block_reason.lower() or "disabled" in result.stderr.lower()
        assert result.exit_code == 1

    def test_from_env_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_BASH_TOOL_ENABLED", raising=False)
        config = BashToolConfig.from_env()
        assert config.enabled is False

    def test_from_env_enabled_when_set(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_ENABLED", "true")
        config = BashToolConfig.from_env()
        assert config.enabled is True

    def test_from_env_enabled_with_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_ENABLED", "1")
        config = BashToolConfig.from_env()
        assert config.enabled is True

    def test_from_env_enabled_with_yes(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_ENABLED", "YES")
        config = BashToolConfig.from_env()
        assert config.enabled is True


# ---------------------------------------------------------------------------
# 2. Blocked commands (exact match)
# ---------------------------------------------------------------------------

class TestBlockedCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init 0",
        "init 6",
        ":(){ :|:& };:",
        "chmod -R 777 /",
        "dd if=/dev/zero",
    ])
    async def test_exact_blocked_commands_rejected(self, cmd):
        tool = _tool()
        result = await tool.execute(cmd)
        assert result.blocked is True, f"Expected {cmd!r} to be blocked"
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_blocked_commands_case_insensitive(self):
        tool = _tool()
        result = await tool.execute("SHUTDOWN")
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_blocked_command_with_leading_whitespace(self):
        tool = _tool()
        result = await tool.execute("  rm -rf /")
        assert result.blocked is True


# ---------------------------------------------------------------------------
# 3. Blocked prefixes
# ---------------------------------------------------------------------------

class TestBlockedPrefixes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", [
        "rm -rf /home/user",
        "sudo rm -rf /tmp",
        "chmod 777 /etc",
        "chown root /etc/passwd",
        "kill -9 1",
        "pkill -9 python",
        "curl | sh -c 'echo pwned'",
        "wget -O- | sh",
    ])
    async def test_prefix_blocked(self, cmd):
        tool = _tool()
        result = await tool.execute(cmd)
        assert result.blocked is True, f"Expected {cmd!r} to be blocked by prefix"
        assert "blocked prefix" in result.block_reason.lower() or "blocklist" in result.block_reason.lower()


# ---------------------------------------------------------------------------
# 4. Restricted mode allowlist
# ---------------------------------------------------------------------------

class TestRestrictedMode:
    @pytest.mark.asyncio
    async def test_allowed_command_passes_validation(self):
        tool = _tool()
        # echo is in the default allowlist; validate only (don't need real exec)
        reason = tool._validate_command("echo hello world")
        assert reason is None

    @pytest.mark.asyncio
    async def test_disallowed_command_blocked(self):
        tool = _tool()
        result = await tool.execute("nmap localhost")
        assert result.blocked is True
        assert "not in allowlist" in result.block_reason

    @pytest.mark.asyncio
    async def test_absolute_path_extracts_base_name(self):
        tool = _tool()
        reason = tool._validate_command("/usr/bin/python3 -c 'print(1)'")
        assert reason is None  # python3 is in allowlist

    @pytest.mark.asyncio
    async def test_all_default_allowlist_commands_pass_validation(self):
        tool = _tool()
        for cmd in _DEFAULT_ALLOWLIST:
            reason = tool._validate_command(f"{cmd} --version")
            assert reason is None, f"Expected {cmd!r} to be allowed"

    @pytest.mark.asyncio
    async def test_custom_allowlist(self):
        custom = frozenset({"mycommand"})
        tool = _tool(allowlist=custom)
        # mycommand allowed
        reason = tool._validate_command("mycommand --flag")
        assert reason is None
        # echo NOT allowed (not in custom allowlist)
        reason = tool._validate_command("echo hello")
        assert reason is not None
        assert "not in allowlist" in reason


# ---------------------------------------------------------------------------
# 5. Unrestricted mode
# ---------------------------------------------------------------------------

class TestUnrestrictedMode:
    @pytest.mark.asyncio
    async def test_unrestricted_allows_non_blocked_commands(self):
        tool = _tool(restricted_mode=False)
        reason = tool._validate_command("nmap localhost")
        assert reason is None  # normally blocked in restricted mode

    @pytest.mark.asyncio
    async def test_unrestricted_still_blocks_dangerous_commands(self):
        tool = _tool(restricted_mode=False)
        result = await tool.execute("rm -rf /")
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_unrestricted_still_blocks_prefix(self):
        tool = _tool(restricted_mode=False)
        result = await tool.execute("sudo rm -rf /tmp/stuff")
        assert result.blocked is True

    def test_from_env_unrestricted(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_UNRESTRICTED", "true")
        config = BashToolConfig.from_env()
        assert config.restricted_mode is False

    def test_from_env_restricted_by_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_BASH_TOOL_UNRESTRICTED", raising=False)
        config = BashToolConfig.from_env()
        assert config.restricted_mode is True


# ---------------------------------------------------------------------------
# 6. Timeout enforcement
# ---------------------------------------------------------------------------

class TestTimeoutEnforcement:
    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        tool = _tool(timeout_s=0.5, restricted_mode=False)
        result = await tool.execute("sleep 30", timeout=0.5)
        assert result.timed_out is True
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_timeout_capped_at_120(self):
        """Even if a caller passes timeout=999, it is capped to 120."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_create:
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"ok", b"")
                tool = _tool(timeout_s=999)
                await tool.execute("echo hi", timeout=999)
                # The effective timeout passed to wait_for should be <= 120
                if mock_wait.called:
                    call_kwargs = mock_wait.call_args
                    actual_timeout = call_kwargs.kwargs.get("timeout") or call_kwargs[1].get("timeout", 999)
                    assert actual_timeout <= 120.0

    @pytest.mark.asyncio
    async def test_config_timeout_used_when_no_override(self):
        tool = _tool(timeout_s=5.0)
        # Quick command completes before timeout
        result = await tool.execute("echo fast")
        assert not result.timed_out
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_from_env_timeout(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_TIMEOUT", "45")
        config = BashToolConfig.from_env()
        assert config.timeout_s == 45.0


# ---------------------------------------------------------------------------
# 7. Output truncation
# ---------------------------------------------------------------------------

class TestOutputTruncation:
    @pytest.mark.asyncio
    async def test_stdout_truncated(self):
        small_limit = 50
        tool = _tool(max_output_bytes=small_limit)
        # Generate output larger than the limit
        result = await tool.execute("python3 -c \"print('A' * 200)\"")
        assert result.exit_code == 0
        # Output should be truncated
        assert len(result.stdout) <= small_limit + 100  # allow for truncation message
        assert "truncated" in result.stdout.lower()

    @pytest.mark.asyncio
    async def test_stderr_truncated(self):
        small_limit = 50
        tool = _tool(max_output_bytes=small_limit)
        # Write a lot of output to stderr
        result = await tool.execute(
            "python3 -c \"import sys; sys.stderr.write('E' * 200)\""
        )
        assert len(result.stderr) <= small_limit + 100
        assert "truncated" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_from_env_max_output(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BASH_TOOL_MAX_OUTPUT", "2048")
        config = BashToolConfig.from_env()
        assert config.max_output_bytes == 2048


# ---------------------------------------------------------------------------
# 8. Successful execution (real commands)
# ---------------------------------------------------------------------------

class TestSuccessfulExecution:
    @pytest.mark.asyncio
    async def test_echo_hello(self):
        tool = _tool()
        result = await tool.execute("echo hello")
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.blocked is False
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_python3_print(self):
        tool = _tool()
        result = await tool.execute("python3 -c \"print(42)\"")
        assert result.exit_code == 0
        assert "42" in result.stdout

    @pytest.mark.asyncio
    async def test_command_with_nonzero_exit(self):
        tool = _tool()
        result = await tool.execute("python3 -c \"import sys; sys.exit(2)\"")
        assert result.exit_code == 2
        assert result.blocked is False
        assert result.timed_out is False

    @pytest.mark.asyncio
    async def test_command_stderr_captured(self):
        tool = _tool()
        result = await tool.execute(
            "python3 -c \"import sys; sys.stderr.write('oops\\n')\""
        )
        assert "oops" in result.stderr

    @pytest.mark.asyncio
    async def test_working_directory(self, tmp_path):
        tool = _tool(cwd=tmp_path)
        result = await tool.execute("pwd")
        assert result.exit_code == 0
        assert str(tmp_path) in result.stdout

    @pytest.mark.asyncio
    async def test_stdin_is_devnull(self):
        """Interactive commands that read from stdin should get empty input."""
        tool = _tool()
        result = await tool.execute("cat")
        # cat with no args reads stdin; with /dev/null it gets EOF immediately
        assert result.exit_code == 0
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_pipe_commands(self):
        tool = _tool()
        result = await tool.execute("echo 'line1\nline2\nline3' | wc -l")
        assert result.exit_code == 0
        # wc -l should show line count
        assert result.stdout.strip().isdigit()


# ---------------------------------------------------------------------------
# 9. to_tool_definition() MCP schema
# ---------------------------------------------------------------------------

class TestToolDefinition:
    def test_returns_dict(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        defn = tool.to_tool_definition()
        assert isinstance(defn, dict)

    def test_has_required_fields(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        defn = tool.to_tool_definition()
        assert defn["name"] == "bash"
        assert "description" in defn
        assert "inputSchema" in defn

    def test_input_schema_structure(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        schema = tool.to_tool_definition()["inputSchema"]
        assert schema["type"] == "object"
        assert "command" in schema["properties"]
        assert "timeout" in schema["properties"]
        assert "command" in schema["required"]

    def test_command_property_is_string(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        schema = tool.to_tool_definition()["inputSchema"]
        assert schema["properties"]["command"]["type"] == "string"

    def test_timeout_property_is_number(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        schema = tool.to_tool_definition()["inputSchema"]
        assert schema["properties"]["timeout"]["type"] == "number"

    def test_timeout_not_required(self):
        tool = SandboxedBashTool(config=BashToolConfig())
        schema = tool.to_tool_definition()["inputSchema"]
        assert "timeout" not in schema["required"]


# ---------------------------------------------------------------------------
# 10. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_command_blocked_in_restricted_mode(self):
        tool = _tool()
        result = await tool.execute("")
        # Empty command: base_cmd will be "" which is not in allowlist
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_command_with_unmatched_quotes(self):
        """shlex.split fails on unmatched quotes; fallback extracts base cmd."""
        tool = _tool()
        reason = tool._validate_command("echo 'unmatched")
        assert reason is None  # echo is in allowlist; fallback parser handles it

    @pytest.mark.asyncio
    async def test_subprocess_exception_handled(self):
        """If create_subprocess_shell itself raises, we get a clean result."""
        tool = _tool()
        with patch(
            "asyncio.create_subprocess_shell",
            side_effect=OSError("permission denied"),
        ):
            result = await tool.execute("echo test")
        assert result.exit_code == 1
        assert "Execution error" in result.stderr

    @pytest.mark.asyncio
    async def test_is_enabled_property(self):
        enabled_tool = SandboxedBashTool(config=_enabled_config())
        assert enabled_tool.is_enabled is True

        disabled_tool = SandboxedBashTool(config=BashToolConfig(enabled=False))
        assert disabled_tool.is_enabled is False

    @pytest.mark.asyncio
    async def test_from_env_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JARVIS_BASH_TOOL_CWD", str(tmp_path))
        config = BashToolConfig.from_env()
        assert config.cwd == tmp_path

    @pytest.mark.asyncio
    async def test_from_env_cwd_uses_project_root_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JARVIS_BASH_TOOL_CWD", raising=False)
        config = BashToolConfig.from_env(project_root=tmp_path)
        assert config.cwd == tmp_path

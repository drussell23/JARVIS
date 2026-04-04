"""Tests for tool definitions and Iron Gate validation."""
import pytest
from backend.hud.tool_definitions import (
    TOOL_SCHEMAS,
    validate_tool_call,
    execute_tool,
    ToolCall,
    ToolResult,
)


class TestToolSchemas:
    def test_all_tools_have_schemas(self):
        expected = {"open_app", "open_url", "run_applescript", "vision_click",
                    "vision_type", "press_key", "take_screenshot", "wait", "bash"}
        assert set(TOOL_SCHEMAS.keys()) == expected

    def test_schema_has_required_fields(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema


class TestIronGateValidation:
    def test_safe_applescript_passes(self):
        call = ToolCall(name="run_applescript", args={"script": 'tell application "Finder" to activate'})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_dangerous_applescript_blocked(self):
        call = ToolCall(name="run_applescript", args={"script": 'do shell script "rm -rf /"'})
        ok, reason = validate_tool_call(call)
        assert ok is False
        assert "blocked" in reason.lower()

    def test_dangerous_bash_blocked(self):
        call = ToolCall(name="bash", args={"command": "sudo rm -rf /"})
        ok, reason = validate_tool_call(call)
        assert ok is False

    def test_safe_bash_passes(self):
        call = ToolCall(name="bash", args={"command": "ls -la"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_credential_applescript_blocked(self):
        call = ToolCall(name="run_applescript", args={"script": 'do shell script "cat ~/.ssh/id_rsa"'})
        ok, reason = validate_tool_call(call)
        assert ok is False

    def test_safe_url_passes(self):
        call = ToolCall(name="open_url", args={"url": "https://linkedin.com"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_open_app_always_safe(self):
        call = ToolCall(name="open_app", args={"app_name": "Google Chrome"})
        ok, reason = validate_tool_call(call)
        assert ok is True

    def test_unknown_tool_blocked(self):
        call = ToolCall(name="hack_system", args={})
        ok, reason = validate_tool_call(call)
        assert ok is False


class TestToolCallDataclass:
    def test_from_dict(self):
        d = {"name": "open_app", "args": {"app_name": "Safari"}}
        call = ToolCall.from_dict(d)
        assert call.name == "open_app"
        assert call.args["app_name"] == "Safari"

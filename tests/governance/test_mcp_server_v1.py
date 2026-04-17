"""Tests for the V1 MCP server (backend/core/ouroboros/mcp_server.py) —
the new stdio JSON-RPC protocol server, distinct from the earlier
``OuroborosMCPServer`` class-shaped implementation whose tests live in
``test_mcp_server.py``. V1 is the protocol-compliant server an MCP
client (Claude Code, etc.) can connect to over stdio.

Two scope axes:

  1. JSON-RPC 2.0 protocol framing: initialize, tools/list, tools/call
     shapes; error responses for unknown methods / unknown tools;
     notifications suppressed (no response written).

  2. Tool handlers: each V1 tool produces the expected output shape
     against real O+V internals (or a defensive degraded result when
     a subsystem is unavailable).

Protocol tests drive ``serve_stdio`` with an in-memory
``asyncio.StreamReader`` + captured-write shim so we never touch real
stdin/stdout.

Mutation tool gating: ``submit_intent`` must NOT appear in the
registry unless ``JARVIS_MCP_ALLOW_MUTATIONS=1`` is set at registry
build time (fail-closed default).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, List

import pytest

from backend.core.ouroboros.mcp_server import (
    Tool,
    ToolRegistry,
    build_default_registry,
    mutations_enabled,
    server_enabled,
    serve_stdio,
    tool_prefix,
    _handle_request,
    _tool_list_memories,
    _tool_list_orphaned_ops,
    _tool_list_sensors,
    _tool_preview_candidate,
    _tool_risk_classify,
    _tool_search_memories,
    _tool_session_status,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_MCP_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Env gates — fail-closed defaults
# ---------------------------------------------------------------------------


def test_server_disabled_by_default():
    assert server_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_server_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_MCP_SERVER_ENABLED", val)
    assert server_enabled() is True


def test_mutations_disabled_by_default():
    assert mutations_enabled() is False


def test_tool_prefix_default():
    assert tool_prefix() == "ov_"


def test_tool_prefix_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_TOOL_PREFIX", "custom_")
    assert tool_prefix() == "custom_"


# ---------------------------------------------------------------------------
# Registry — mutation gate + tool set
# ---------------------------------------------------------------------------


def test_default_registry_has_v1_readonly_tools():
    reg = build_default_registry()
    expected = {
        "list_orphaned_ops", "query_oracle", "risk_classify",
        "session_status", "list_sensors", "list_memories",
        "search_memories", "preview_candidate",
    }
    assert expected.issubset(set(reg.tools.keys()))


def test_default_registry_excludes_mutation_by_default():
    reg = build_default_registry()
    assert "submit_intent" not in reg.tools


def test_default_registry_includes_mutation_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_ALLOW_MUTATIONS", "1")
    reg = build_default_registry()
    assert "submit_intent" in reg.tools
    assert reg.tools["submit_intent"].mutating is True


def test_list_schemas_applies_prefix():
    reg = build_default_registry()
    schemas = reg.list_schemas(prefix="ov_")
    names = {s["name"] for s in schemas}
    assert "ov_list_orphaned_ops" in names
    assert all(n.startswith("ov_") for n in names)


def test_lookup_requires_prefixed_qualified_name():
    """lookup() expects the qualified (prefixed) name and strips the
    prefix before hitting the registry. Missing-prefix query → None.
    Real prefix enforcement happens via the configured
    ``tool_prefix()`` env — _handle_request always passes that same
    value, so cross-prefix lookups can't happen in production."""
    reg = build_default_registry()
    assert reg.lookup(
        prefix="ov_", qualified_name="ov_list_memories",
    ) is not None
    # Missing prefix → lookup fails (qualified name must carry prefix).
    assert reg.lookup(
        prefix="ov_", qualified_name="list_memories",
    ) is None


# ---------------------------------------------------------------------------
# Protocol — initialize / tools/list / tools/call framing
# ---------------------------------------------------------------------------


def test_initialize_returns_server_info():
    reg = ToolRegistry()

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "test", "version": "0.1"}},
        })
    resp = asyncio.run(_run())
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"]
    assert resp["result"]["serverInfo"]["name"] == "ouroboros"
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_returns_schemas(monkeypatch):
    reg = build_default_registry()

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
    resp = asyncio.run(_run())
    assert "result" in resp
    tools = resp["result"]["tools"]
    assert len(tools) >= 8
    for t in tools:
        assert t["name"].startswith("ov_")
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_notifications_suppressed():
    reg = ToolRegistry()

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
    assert asyncio.run(_run()) is None


def test_unknown_method_returns_error():
    reg = ToolRegistry()

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 99, "method": "totally/made/up",
        })
    resp = asyncio.run(_run())
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_unknown_tool_returns_error():
    reg = build_default_registry()

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ov_nonexistent", "arguments": {}},
        })
    resp = asyncio.run(_run())
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_tool_call_success_returns_text_content():
    reg = ToolRegistry()

    async def _echo(**kwargs):
        return {"got": kwargs}

    reg.register(Tool(
        name="echo", description="echo",
        input_schema={
            "type": "object", "properties": {},
            "additionalProperties": True,
        },
        handler=_echo,
    ))

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "ov_echo", "arguments": {"x": 1}},
        })
    resp = asyncio.run(_run())
    assert resp["result"]["isError"] is False
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["got"]["x"] == 1


def test_tool_call_handler_error_is_surfaced_as_non_protocol_error():
    """Handler exceptions produce isError:true but NOT a JSON-RPC
    transport error — per MCP spec. The response still carries a
    ``result`` key (not ``error``)."""
    reg = ToolRegistry()

    async def _broken(**kwargs):
        raise RuntimeError("handler exploded")

    reg.register(Tool(
        name="broken", description="boom",
        input_schema={
            "type": "object", "properties": {},
            "additionalProperties": True,
        },
        handler=_broken,
    ))

    async def _run():
        return await _handle_request(registry=reg, msg={
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "ov_broken", "arguments": {}},
        })
    resp = asyncio.run(_run())
    assert "result" in resp
    assert resp["result"]["isError"] is True
    assert "handler exploded" in resp["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# End-to-end stdio loop
# ---------------------------------------------------------------------------


def test_serve_stdio_handles_initialize_then_list(monkeypatch):
    """Full pipeline: initialize → notifications/initialized → tools/list.
    Notification is suppressed (no response write). Two real responses."""
    monkeypatch.setenv("JARVIS_MCP_SERVER_ENABLED", "1")

    async def _run():
        reg = build_default_registry()
        reader = asyncio.StreamReader()
        reader.feed_data(
            (json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "t", "version": "0"}},
            }) + "\n" + json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }) + "\n" + json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            }) + "\n").encode()
        )
        reader.feed_eof()

        writes: List[str] = []
        await serve_stdio(
            reg, stdin=reader, stdout_write=lambda s: writes.append(s),
        )
        return writes

    writes = asyncio.run(_run())
    assert len(writes) == 2
    init_resp = json.loads(writes[0])
    assert init_resp["id"] == 1
    assert init_resp["result"]["serverInfo"]["name"] == "ouroboros"
    list_resp = json.loads(writes[1])
    assert list_resp["id"] == 2
    assert any(
        t["name"] == "ov_list_orphaned_ops"
        for t in list_resp["result"]["tools"]
    )


def test_serve_stdio_exits_cleanly_when_disabled():
    async def _run():
        reg = build_default_registry()
        reader = asyncio.StreamReader()
        reader.feed_eof()
        writes: List[str] = []
        await serve_stdio(
            reg, stdin=reader, stdout_write=lambda s: writes.append(s),
        )
        return writes
    assert asyncio.run(_run()) == []


def test_serve_stdio_drops_malformed_lines(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_SERVER_ENABLED", "1")

    async def _run():
        reg = build_default_registry()
        reader = asyncio.StreamReader()
        reader.feed_data(
            ("{this is not json\n" + json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {},
            }) + "\n").encode()
        )
        reader.feed_eof()
        writes: List[str] = []
        await serve_stdio(
            reg, stdin=reader, stdout_write=lambda s: writes.append(s),
        )
        return writes
    writes = asyncio.run(_run())
    assert len(writes) == 1
    assert json.loads(writes[0])["id"] == 1


# ---------------------------------------------------------------------------
# Tool handlers — against real O+V internals
# ---------------------------------------------------------------------------


def test_tool_list_orphaned_ops_returns_shape():
    async def _run():
        return await _tool_list_orphaned_ops()
    result = asyncio.run(_run())
    assert "count" in result
    assert "orphans" in result
    assert isinstance(result["orphans"], list)


def test_tool_risk_classify_returns_tier():
    async def _run():
        return await _tool_risk_classify(
            description="Add type hints to utility functions",
            target_files=["backend/utils.py"],
        )
    result = asyncio.run(_run())
    # Either tier computed, or structured error surfaced.
    assert ("tier" in result) or ("error" in result)
    if "tier" in result:
        assert result["tier"] in (
            "SAFE_AUTO", "NOTIFY_APPLY", "APPROVAL_REQUIRED", "BLOCKED",
        )


def test_tool_preview_candidate_surfaces_guardian_findings():
    """Feed an adversarial candidate (removed import still referenced)
    and verify SemanticGuardian findings surface via the MCP tool."""
    async def _run():
        return await _tool_preview_candidate(
            file_path="foo.py",
            old_content=(
                "import hmac\n"
                "def verify(a, b):\n    return hmac.compare_digest(a, b)\n"
            ),
            new_content=(
                "def verify(a, b):\n    return hmac.compare_digest(a, b)\n"
            ),
        )
    result = asyncio.run(_run())
    assert result["findings_count"] >= 1
    patterns = [f["pattern"] for f in result["findings"]]
    assert "removed_import_still_referenced" in patterns
    assert result["recommended_tier_floor"] == "approval_required"


def test_tool_preview_candidate_silent_on_clean_change():
    async def _run():
        return await _tool_preview_candidate(
            file_path="foo.py",
            old_content="x = 1\n",
            new_content="x = 2\n",
        )
    result = asyncio.run(_run())
    assert result["findings_count"] == 0
    assert result["recommended_tier_floor"] == "none"


def test_tool_session_status_reports_unattached():
    async def _run():
        return await _tool_session_status()
    result = asyncio.run(_run())
    assert result["attached"] is False
    assert "reason" in result


def test_tool_list_sensors_returns_documented_set_when_unattached():
    async def _run():
        return await _tool_list_sensors()
    result = asyncio.run(_run())
    assert "sensors" in result
    if result.get("attached") is False:
        assert "TestFailure" in result["sensors"]
        assert "TodoScanner" in result["sensors"]


def test_tool_list_memories_returns_shape(tmp_path, monkeypatch):
    """Point the default store at a tmp dir via JARVIS_REPO_PATH so we
    don't read live operator data."""
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    async def _run():
        return await _tool_list_memories()
    result = asyncio.run(_run())
    assert "count" in result
    assert "memories" in result


def test_tool_search_memories_empty_query():
    async def _run():
        return await _tool_search_memories(query="")
    result = asyncio.run(_run())
    assert result["hits"] == []


# ---------------------------------------------------------------------------
# AST canary — public surface
# ---------------------------------------------------------------------------


def test_mcp_module_exports_public_surface():
    from backend.core.ouroboros import mcp_server as m
    for name in (
        "Tool", "ToolRegistry", "build_default_registry",
        "serve_stdio", "server_enabled", "mutations_enabled",
        "tool_prefix",
    ):
        assert hasattr(m, name), f"mcp_server.{name} missing"


def test_protocol_version_stamp_present():
    from backend.core.ouroboros import mcp_server as m
    src = Path(m.__file__).read_text(encoding="utf-8")
    assert "_PROTOCOL_VERSION" in src
    assert "protocolVersion" in src

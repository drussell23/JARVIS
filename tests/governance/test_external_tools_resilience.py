"""MCP + web tools reach every lane, and an external failure never stalls one.

2026-10-09 audit of a 4-hour soak: zero web or MCP calls. The MCP client was
constructed and injected but never started (discovery returned [], every call
None), ClaudeProvider never received it, the deferred tool registry pointed at
classes that did not exist, the web tools were bare signatures the model never
used, the local window fit could cut the tool list out, and a J-Prime prompt
logged itself as Claude.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import external_breaker as eb

_REPO = Path(__file__).resolve().parents[2]
_GOV = _REPO / "backend/core/ouroboros/governance"


@pytest.fixture(autouse=True)
def _fresh_breakers(monkeypatch):
    monkeypatch.setattr(eb, "_REGISTRY", {})
    monkeypatch.setenv("JARVIS_EXTERNAL_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("JARVIS_EXTERNAL_BREAKER_COOLDOWN_S", "30")


# ------------------------------------------------------------------ breaker


def test_breaker_opens_fails_fast_then_half_opens_once():
    now = [0.0]
    b = eb.ExternalBreaker("k", clock=lambda: now[0])
    b.record_failure("timeout")
    assert b.state == eb.CLOSED and b.admit()
    b.record_failure("timeout")
    assert b.state == eb.OPEN and not b.admit()
    now[0] = 31.0
    assert b.state == eb.HALF_OPEN
    assert b.admit() and not b.admit()          # exactly one trial
    b.record_failure("timeout")                  # trial failed -> longer cooldown
    now[0] = 31.0 + 59.0
    assert not b.admit()
    now[0] = 31.0 + 61.0
    assert b.admit()
    b.record_success()
    assert b.state == eb.CLOSED


def test_guard_classifies_timeout_error_and_malformed():
    async def hang():
        await asyncio.sleep(10)

    async def boom():
        raise RuntimeError("socket closed")

    async def junk():
        return "not a dict"

    async def run():
        t = await eb.guard("a", hang, timeout=0.05)
        e = await eb.guard("b", boom, timeout=1)
        m = await eb.guard("c", junk, timeout=1, valid=lambda r: isinstance(r, dict))
        return t, e, m

    t, e, m = asyncio.run(run())
    assert (t.ok, t.reason) == (False, "timeout")
    assert not e.ok and "socket closed" in e.reason
    assert (m.ok, m.reason) == (False, "malformed")


def test_a_cancelled_trial_releases_the_half_open_slot():
    now = [0.0]
    b = eb.ExternalBreaker("k", clock=lambda: now[0])
    b.record_failure("x")
    b.record_failure("x")
    now[0] = 100.0
    assert b.admit()
    b.release_trial()
    assert b.admit()


# ------------------------------------------------------------------ MCP


def test_mcp_config_is_resolved_adaptively(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import mcp_tool_client as mtc
    monkeypatch.delenv("JARVIS_MCP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert mtc.resolve_config_path() == ""
    (tmp_path / ".jarvis").mkdir()
    (tmp_path / ".jarvis" / "mcp.yaml").write_text("servers: {}\n")
    assert mtc.resolve_config_path() == str(tmp_path / ".jarvis" / "mcp.yaml")
    monkeypatch.setenv("JARVIS_MCP_CONFIG", str(tmp_path / "missing.yaml"))
    assert mtc.resolve_config_path() == ""          # explicit but missing: reported, not replaced


def test_outward_actions_are_opt_in():
    from backend.core.ouroboros.governance.mcp_tool_client import MCPClientConfig
    assert MCPClientConfig().auto_issue is False and MCPClientConfig().auto_pr is False


_FAKE_SERVER = textwrap.dedent('''
    import json, sys, time
    mode = sys.argv[1]
    for raw in sys.stdin:
        msg = json.loads(raw)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {}}
        elif mode == "hang":
            time.sleep(3600); continue
        elif method == "tools/list":
            res = {"tools": [{"name": "echo", "inputSchema": {}}]}
        else:
            res = {"content": [{"type": "text", "text": "pong"}]}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
        sys.stdout.flush()
''')


def test_a_hung_mcp_server_costs_one_timeout_and_never_its_neighbours(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import mcp_tool_client as mtc
    from backend.core.ouroboros.governance.tool_executor import AsyncProcessToolBackend, ToolCall
    server = tmp_path / "srv.py"
    server.write_text(_FAKE_SERVER)
    cfg = tmp_path / "mcp.yaml"
    cfg.write_text(f'servers:\n  good: {{transport: stdio, command: ["{sys.executable}", "{server}", "ok"]}}\n'
                   f'  slow: {{transport: stdio, command: ["{sys.executable}", "{server}", "hang"]}}\n')
    monkeypatch.setenv("JARVIS_MCP_CONFIG", str(cfg))
    monkeypatch.setattr(mtc, "_MCP_REQUEST_TIMEOUT", 0.5)

    async def run():
        client = mtc.GovernanceMCPClient()
        await client.start()
        tools = [t["name"] for t in await client.discover_tools()]
        be = AsyncProcessToolBackend.__new__(AsyncProcessToolBackend)
        be._mcp_client = client
        slow = [await be._run_mcp_tool(ToolCall(name="mcp_slow_echo", arguments={}), 0.5, 100)
                for _ in range(2)]
        good = await be._run_mcp_tool(ToolCall(name="mcp_good_echo", arguments={}), 2.0, 100)
        await client.stop()
        return tools, slow, good

    tools, slow, good = asyncio.run(run())
    assert tools == ["mcp_good_echo"]
    assert "open" in (slow[-1].error or "")          # fails fast once tripped
    assert good.output == "pong"


def test_gls_starts_the_mcp_client_and_wires_every_seat():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "asyncio.ensure_future(_mcp.start())" in src
    assert '("_primary", "_fallback", "_tier0")' in src
    assert "_mcp.stop()" in src


def test_the_dead_deferred_tool_registry_is_gone():
    assert not (_GOV / "deferred_tool_registry.py").exists()
    assert "deferred_tool_registry" not in (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ web


def test_the_web_block_is_derived_and_adapts_to_breaker_state():
    from backend.core.ouroboros.governance import providers
    from backend.core.ouroboros.governance.doc_fetcher import allowed_domains
    from backend.core.ouroboros.governance.web_search import max_results
    block = providers._web_tools_block()
    assert all(host in block for host in allowed_domains())
    assert f"top {max_results()} results" in block and "max_results=" not in block
    assert "THIS codebase" in block
    b = eb.breaker_for("web:search")
    b.record_failure("timeout")
    b.record_failure("timeout")
    assert "web_search is temporarily unavailable" in providers._web_tools_block()


def test_a_failed_fetch_says_why(monkeypatch):
    from backend.core.ouroboros.governance.tool_executor import AsyncProcessToolBackend, ToolCall
    be = AsyncProcessToolBackend.__new__(AsyncProcessToolBackend)
    out = asyncio.run(be._run_web_tool(
        ToolCall(name="web_fetch", arguments={"url": "https://example.invalid/x"}), 5.0, 1000))
    assert out.error and "allowlist" in out.error.lower()


# ------------------------------------------------------------------ J-Prime window + labels


def test_window_fit_never_cuts_the_tool_section():
    from backend.core.ouroboros.governance.local_inference_director import (
        estimate_tokens, fit_prompt_to_window,
    )
    tools = "## Available Tools\n" + "".join(f"- `tool_{i}(x)`\n" for i in range(40))
    user = ("## Task\nfix it\n\n## History\n" + "old\n" * 5000 + tools
            + "## Latest\n" + "newest\n" * 40)
    s, out, compressed = fit_prompt_to_window("SYS", user, max_tokens=2000)
    assert compressed and estimate_tokens(s) + estimate_tokens(out) <= 2000
    assert all(f"`tool_{i}(x)`" in out for i in range(40))
    assert out.index("## Task") < out.index("## Available Tools") < out.rindex("newest")
    s, tiny, _ = fit_prompt_to_window("S", user, max_tokens=50)
    assert estimate_tokens(s) + estimate_tokens(tiny) <= 50


def test_compaction_targets_the_tool_section_not_the_exploration_block():
    tree = ast.parse((_GOV / "providers.py").read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_build_lean_codegen_prompt":
            assert "compact_tool_section(_tool_section)" in ast.unparse(fn)
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and "exploration" in fn.name.lower():
            assert "compact_tool_section" not in ast.unparse(fn)


def test_prime_provider_logs_under_its_own_name():
    tree = ast.parse((_GOV / "providers.py").read_text(encoding="utf-8"))
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "PrimeProvider":
            assert "[ClaudeProvider]" not in ast.unparse(cls)

"""§41.5 WebBrowser → Venom tool-loop wiring regression spine.

Substrate (`web_browser.py`) was shipped 2026-05-11 in commit
`31933e5da9` but had zero consumers. This spine asserts the wiring
into `tool_executor.py`'s Venom tool loop: manifest entries are
registered, dispatch routing reaches the substrate, and the verdict
→ ToolResult mapping is correct."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


from backend.core.ouroboros.governance import tool_executor as te
from backend.core.ouroboros.governance.tool_executor import (
    _L1_MANIFESTS as BUILTIN_TOOL_MANIFESTS,
    ToolCall,
    ToolExecStatus,
)
from backend.core.ouroboros.governance.web_browser import (
    BrowsingAction,
    BrowsingResult,
    BrowsingVerdict,
)


# --- Manifest registration --------------------------------------------------


def test_web_browse_manifest_registered():
    assert "web_browse" in BUILTIN_TOOL_MANIFESTS
    m = BUILTIN_TOOL_MANIFESTS["web_browse"]
    assert m.name == "web_browse"
    assert "network" in m.capabilities


def test_web_follow_manifest_registered():
    assert "web_follow" in BUILTIN_TOOL_MANIFESTS
    m = BUILTIN_TOOL_MANIFESTS["web_follow"]
    assert m.name == "web_follow"
    assert "network" in m.capabilities


def test_web_cite_manifest_registered():
    assert "web_cite" in BUILTIN_TOOL_MANIFESTS
    m = BUILTIN_TOOL_MANIFESTS["web_cite"]
    assert m.name == "web_cite"
    # web_cite is a pure ledger write — write capability only
    assert "write" in m.capabilities
    assert "network" not in m.capabilities


def test_web_browse_schema_has_url_and_js_render():
    m = BUILTIN_TOOL_MANIFESTS["web_browse"]
    assert "url" in m.arg_schema
    assert "js_render" in m.arg_schema
    assert m.arg_schema["js_render"]["default"] is False


def test_web_follow_schema_has_url():
    m = BUILTIN_TOOL_MANIFESTS["web_follow"]
    assert "url" in m.arg_schema


def test_web_cite_schema_has_url_and_fragment():
    m = BUILTIN_TOOL_MANIFESTS["web_cite"]
    assert "url" in m.arg_schema
    assert "fragment" in m.arg_schema


def test_legacy_tools_still_registered():
    """Wiring must not delete or rename the legacy web_search /
    web_fetch tools — they remain byte-identical for backward
    compatibility per the augment-not-replace contract."""
    assert "web_search" in BUILTIN_TOOL_MANIFESTS
    assert "web_fetch" in BUILTIN_TOOL_MANIFESTS


# --- AST pin: dispatch routing ---------------------------------------------


def _load_tool_executor_source():
    return Path(
        "backend/core/ouroboros/governance/tool_executor.py"
    ).read_text()


def test_ast_pin_async_native_dispatch_includes_web_browser_tools():
    """The async-native dispatch tuple must include web_browse /
    web_follow / web_cite so the if-name-in-tuple gate routes
    them through _run_async_native_tool."""
    src = _load_tool_executor_source()
    # Locate the tuple literal that gates async-native dispatch
    assert '"web_browse"' in src
    assert '"web_follow"' in src
    assert '"web_cite"' in src


def test_ast_pin_dispatch_branch_imports_substrate():
    """The dispatch branch must lazy-import perform_browsing_action
    from web_browser — this is the substrate composition seam."""
    src = _load_tool_executor_source()
    assert "perform_browsing_action" in src
    assert "from backend.core.ouroboros.governance.web_browser import" in src


def test_ast_pin_dispatch_uses_closed_taxonomies():
    """The dispatch must use BrowsingAction + BrowsingVerdict
    enums (not raw strings) — proves the composition runs through
    the substrate's closed 4-value taxonomies."""
    src = _load_tool_executor_source()
    assert "BrowsingAction.NAVIGATE" in src
    assert "BrowsingAction.EXTRACT_TEXT" in src
    assert "BrowsingAction.FOLLOW_LINK" in src
    assert "BrowsingAction.CITE" in src
    assert "BrowsingVerdict.CLEAN" in src


def test_ast_pin_dispatch_threads_op_id():
    """Dispatch must thread policy_ctx.op_id into
    perform_browsing_action so ledger entries correlate with the
    Ouroboros op."""
    src = _load_tool_executor_source()
    assert "op_id=policy_ctx.op_id" in src


# --- Helper builders -------------------------------------------------------


def _clean_result(
    action: BrowsingAction,
    *,
    url: str = "https://docs.python.org/3/",
    body: str = "sample body",
    redacted: int = 0,
) -> BrowsingResult:
    return BrowsingResult(
        action=action,
        verdict=BrowsingVerdict.CLEAN,
        url=url,
        host="docs.python.org",
        content_bytes=len(body),
        sanitized_body=body,
        redacted_bytes=redacted,
        leaked_credential_kinds=(),
        backend_used="static",
        latency_ms=42.0,
        diagnostic="ok",
        op_id="op-test",
        evaluated_at_unix=1.0,
    )


def _failed_result(
    action: BrowsingAction, verdict: BrowsingVerdict,
    *, diagnostic: str = "test failure",
) -> BrowsingResult:
    return BrowsingResult(
        action=action,
        verdict=verdict,
        url="https://blocked.example",
        host="blocked.example",
        content_bytes=0,
        sanitized_body="",
        redacted_bytes=0,
        leaked_credential_kinds=(),
        backend_used="none",
        latency_ms=0.0,
        diagnostic=diagnostic,
        op_id="op-test",
        evaluated_at_unix=1.0,
    )


def _fake_policy_ctx():
    """Minimal stub matching the surface the dispatch uses
    (.op_id + .repo_root). The dispatch we test never reaches
    repo_root, but we set it to keep the type loose."""
    class _Ctx:
        op_id = "op-test"
        repo_root = Path(".")
    return _Ctx()


def _make_call(name: str, **args: Any) -> ToolCall:
    return ToolCall(name=name, arguments=args)


# --- Dispatch behavior tests -----------------------------------------------


def _build_executor():
    """Construct a minimal AsyncProcessToolBackend instance.

    `_run_async_native_tool` is hosted on AsyncProcessToolBackend
    (not ToolExecutor). The dispatch branch we exercise only
    touches `self._approval_provider` (None for web_browser path),
    so we sidestep the rest of the constructor via __new__."""
    inst = te.AsyncProcessToolBackend.__new__(te.AsyncProcessToolBackend)
    inst._approval_provider = None
    return inst


@pytest.mark.asyncio
async def test_web_browse_clean_returns_success(monkeypatch):
    captured = {}

    async def fake_perform(action, **kwargs):
        captured["action"] = action
        captured["url"] = kwargs.get("url")
        captured["js_render"] = kwargs.get("js_render")
        captured["op_id"] = kwargs.get("op_id")
        return _clean_result(action, body="hello world", redacted=3)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call(
        "web_browse",
        url="https://docs.python.org/3/", js_render=False,
    )
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.SUCCESS
    assert "hello world" in result.output
    assert "docs.python.org" in result.output
    assert "backend=static" in result.output
    assert "redacted_bytes=3" in result.output
    assert captured["action"] is BrowsingAction.NAVIGATE
    assert captured["url"] == "https://docs.python.org/3/"
    assert captured["js_render"] is False
    assert captured["op_id"] == "op-test"


@pytest.mark.asyncio
async def test_web_browse_js_render_routes_extract_text(monkeypatch):
    captured = {}

    async def fake_perform(action, **kwargs):
        captured["action"] = action
        captured["js_render"] = kwargs.get("js_render")
        return _clean_result(action)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call(
        "web_browse",
        url="https://example.org/", js_render=True,
    )
    await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert captured["action"] is BrowsingAction.EXTRACT_TEXT
    assert captured["js_render"] is True


@pytest.mark.asyncio
async def test_web_follow_routes_follow_link_action(monkeypatch):
    captured = {}

    async def fake_perform(action, **kwargs):
        captured["action"] = action
        captured["url"] = kwargs.get("url")
        return _clean_result(action)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_follow", url="https://docs.python.org/3/library/asyncio.html")
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.SUCCESS
    assert captured["action"] is BrowsingAction.FOLLOW_LINK
    assert "docs.python.org" in captured["url"]


@pytest.mark.asyncio
async def test_web_cite_routes_cite_action_no_body(monkeypatch):
    captured = {}

    async def fake_perform(action, **kwargs):
        captured["action"] = action
        captured["fragment"] = kwargs.get("fragment")
        return BrowsingResult(
            action=action, verdict=BrowsingVerdict.CLEAN,
            url="https://docs.python.org/3/",
            host="docs.python.org",
            content_bytes=42,
            sanitized_body="",
            redacted_bytes=0,
            leaked_credential_kinds=(),
            backend_used="ledger",
            latency_ms=0.5,
            diagnostic="cited",
            op_id="op-test",
            evaluated_at_unix=1.0,
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call(
        "web_cite",
        url="https://docs.python.org/3/",
        fragment="asyncio.wait_for cancels on timeout",
    )
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.SUCCESS
    assert "cited" in result.output
    assert "42 bytes recorded" in result.output
    assert captured["action"] is BrowsingAction.CITE
    assert "asyncio.wait_for" in captured["fragment"]


@pytest.mark.asyncio
async def test_master_off_returns_exec_error(monkeypatch):
    async def fake_perform(action, **kwargs):
        return _failed_result(
            BrowsingAction.NAVIGATE, BrowsingVerdict.FAILED,
            diagnostic="gate disabled via JARVIS_WEB_BROWSER_ENABLED=false",
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_browse", url="https://example.com/")
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "gate disabled" in (result.error or "")
    assert result.output == ""


@pytest.mark.asyncio
async def test_out_of_allowlist_returns_exec_error(monkeypatch):
    async def fake_perform(action, **kwargs):
        return _failed_result(
            BrowsingAction.NAVIGATE,
            BrowsingVerdict.OUT_OF_ALLOWLIST,
            diagnostic="host not in operator allowlist",
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_browse", url="https://evil.example/")
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "out_of_allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_credential_leaked_returns_exec_error(monkeypatch):
    """Even though the substrate replaces the body with a
    placeholder upstream, the wiring still surfaces this as
    EXEC_ERROR so the model treats it as a hard stop rather than
    a partial result."""
    async def fake_perform(action, **kwargs):
        return BrowsingResult(
            action=BrowsingAction.NAVIGATE,
            verdict=BrowsingVerdict.CREDENTIAL_LEAKED,
            url="https://oops.example/",
            host="oops.example",
            content_bytes=0,
            sanitized_body="[CREDENTIAL_LEAKED — body suppressed]",
            redacted_bytes=0,
            leaked_credential_kinds=("aws_secret_key",),
            backend_used="static",
            latency_ms=10.0,
            diagnostic="aws_secret_key shape detected",
            op_id="op-test",
            evaluated_at_unix=1.0,
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_browse", url="https://oops.example/")
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert result.status == ToolExecStatus.EXEC_ERROR
    assert "credential_leaked" in (result.error or "")


@pytest.mark.asyncio
async def test_body_respects_cap(monkeypatch):
    big_body = "x" * 5000

    async def fake_perform(action, **kwargs):
        return _clean_result(action, body=big_body)

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_browse", url="https://example.com/")
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=100,
    )
    assert result.status == ToolExecStatus.SUCCESS
    # Body capped to 100; surrounding headers + trailing line not
    # counted in the cap (they're frame metadata, not body)
    assert result.output.count("x") <= 100


@pytest.mark.asyncio
async def test_missing_args_pass_empty_strings(monkeypatch):
    """Missing url should pass empty string, not raise. Substrate
    will return FAILED with a diagnostic."""
    captured = {}

    async def fake_perform(action, **kwargs):
        captured["url"] = kwargs.get("url")
        return _failed_result(
            action, BrowsingVerdict.FAILED, diagnostic="empty url",
        )

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.web_browser."
        "perform_browsing_action",
        fake_perform,
    )
    inst = _build_executor()
    call = _make_call("web_browse")  # no args
    result = await inst._run_async_native_tool(
        call, _fake_policy_ctx(), timeout=5.0, cap=1024,
    )
    assert captured["url"] == ""
    assert result.status == ToolExecStatus.EXEC_ERROR

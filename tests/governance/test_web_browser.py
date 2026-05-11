"""Regression spine for §41.5 Phase 0 — WebBrowser composer."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import web_browser as wb
from backend.core.ouroboros.governance.web_browser import (
    WEB_BROWSER_SCHEMA_VERSION,
    BrowsingAction,
    BrowsingResult,
    BrowsingVerdict,
    CitationRecord,
    _ENV_ALLOW_JS_RENDER,
    _ENV_CITATION_BOUND,
    _ENV_DOMAIN_ALLOWLIST,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_FETCH_BYTES,
    _ENV_PERSIST,
    _ENV_REQUEST_TIMEOUT_S,
    _coerce_action,
    _matches_allowlist,
    _normalize_url,
    citation_bound,
    format_browsing_panel,
    js_render_enabled,
    ledger_path,
    master_enabled,
    max_fetch_bytes,
    operator_domain_allowlist,
    perform_browsing_action,
    perform_browsing_action_sync,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    request_timeout_s,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_ALLOW_JS_RENDER,
        _ENV_DOMAIN_ALLOWLIST, _ENV_MAX_FETCH_BYTES,
        _ENV_REQUEST_TIMEOUT_S, _ENV_CITATION_BOUND,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "wb.jsonl"),
    )
    yield


def _run(coro):
    return asyncio.run(coro)


# Defaults / taxonomies


def test_schema():
    assert WEB_BROWSER_SCHEMA_VERSION == "web_browser.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_js_render_default_false():
    assert js_render_enabled() is False


def test_max_fetch_bytes_default():
    assert max_fetch_bytes() == 200_000


def test_request_timeout_default():
    assert request_timeout_s() == 20


def test_citation_bound_default():
    assert citation_bound() == 256


def test_operator_allowlist_empty_by_default():
    assert operator_domain_allowlist() == ()


def test_operator_allowlist_parses_comma(monkeypatch):
    monkeypatch.setenv(
        _ENV_DOMAIN_ALLOWLIST,
        "github.com, docs.python.org , stackoverflow.com ",
    )
    out = operator_domain_allowlist()
    assert out == (
        "github.com", "docs.python.org", "stackoverflow.com",
    )


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert str(p) == ".jarvis/web_browsing_ledger.jsonl"


def test_action_taxonomy_closed():
    assert {a.value for a in BrowsingAction} == {
        "search", "navigate", "follow_link",
        "extract_text", "extract_image", "cite",
    }


def test_verdict_taxonomy_closed():
    assert {v.value for v in BrowsingVerdict} == {
        "clean", "credential_leaked", "out_of_allowlist",
        "rate_limited", "failed",
    }


@pytest.mark.parametrize("a", list(BrowsingAction))
def test_action_glyph_known(a):
    assert wb.action_glyph(a) != "?"


@pytest.mark.parametrize("v", list(BrowsingVerdict))
def test_verdict_glyph_known(v):
    assert wb.verdict_glyph(v) != "?"


# Coercion


def test_coerce_action_enum_passthrough():
    assert _coerce_action(BrowsingAction.SEARCH) is BrowsingAction.SEARCH


def test_coerce_action_string():
    assert _coerce_action("search") is BrowsingAction.SEARCH


def test_coerce_action_unknown():
    assert _coerce_action("not-a-real-action") is None


# URL normalization


def test_normalize_url_https():
    u, h = _normalize_url("https://github.com/foo")
    assert u == "https://github.com/foo"
    assert h == "github.com"


def test_normalize_url_http():
    u, h = _normalize_url("http://example.com:8080")
    assert u == "http://example.com:8080"
    assert h == "example.com"


def test_normalize_url_rejects_bare_hostname():
    u, h = _normalize_url("github.com")
    assert (u, h) == ("", "")


def test_normalize_url_rejects_file_scheme():
    u, h = _normalize_url("file:///etc/passwd")
    assert (u, h) == ("", "")


def test_normalize_url_empty():
    assert _normalize_url("") == ("", "")


def test_normalize_url_none():
    assert _normalize_url(None) == ("", "")


# Allowlist


def test_allowlist_empty_allows_all():
    assert _matches_allowlist("anything.com", ()) is True


def test_allowlist_exact_match():
    assert _matches_allowlist(
        "github.com", ("github.com",),
    ) is True


def test_allowlist_suffix_match():
    assert _matches_allowlist(
        "api.github.com", ("github.com",),
    ) is True


def test_allowlist_no_match():
    assert _matches_allowlist(
        "evil.com", ("github.com",),
    ) is False


def test_allowlist_case_insensitive():
    assert _matches_allowlist(
        "API.GitHub.com", ("github.com",),
    ) is True


# Master-off behavior


def test_master_off_returns_failed():
    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.com",
    ))
    assert result.verdict is BrowsingVerdict.FAILED
    assert _ENV_MASTER in result.diagnostic


def test_unknown_action_returns_failed():
    result = _run(perform_browsing_action(
        "garbage-action",
        url="https://example.com",
    ))
    assert result.verdict is BrowsingVerdict.FAILED
    assert "unknown action" in result.diagnostic


# CITE — pure ledger write


def test_cite_master_on_no_url(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.CITE,
        fragment="key insight from research",
        op_id="op-1",
    ))
    assert result.action is BrowsingAction.CITE
    assert result.verdict is BrowsingVerdict.CLEAN
    p = ledger_path()
    assert p.exists()
    rows = [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("kind") == "citation" for r in rows)


def test_cite_with_url(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.CITE,
        url="https://docs.python.org/3/library/ast.html",
        fragment="ast.parse returns Module node",
        op_id="op-2",
    ))
    assert result.verdict is BrowsingVerdict.CLEAN
    assert result.host == "docs.python.org"


def test_cite_malformed_url_returns_failed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.CITE,
        url="not-a-url",
        fragment="x",
    ))
    assert result.verdict is BrowsingVerdict.FAILED
    assert "malformed" in result.diagnostic.lower()


def test_cite_truncates_fragment(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_CITATION_BOUND, "20")
    result = _run(perform_browsing_action(
        BrowsingAction.CITE,
        fragment="x" * 1000,
    ))
    assert result.verdict is BrowsingVerdict.CLEAN
    assert len(result.sanitized_body) <= 20


def test_cite_persist_disabled_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    _run(perform_browsing_action(
        BrowsingAction.CITE, fragment="x",
    ))
    assert not ledger_path().exists()


# Allowlist gating


def test_navigate_outside_allowlist(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(
        _ENV_DOMAIN_ALLOWLIST, "github.com,docs.python.org",
    )
    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://evil.example.com/data",
    ))
    assert result.verdict is BrowsingVerdict.OUT_OF_ALLOWLIST
    assert "evil.example.com" in result.diagnostic


def test_navigate_in_allowlist_attempts_fetch(monkeypatch):
    """Allowlist passes; fetch attempts. Backend may fail
    (network) but verdict should NOT be OUT_OF_ALLOWLIST."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")
    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.test/page",
    ))
    # Allowlist passes — verdict is FAILED/RATE_LIMITED from
    # backend, not OUT_OF_ALLOWLIST.
    assert result.verdict is not BrowsingVerdict.OUT_OF_ALLOWLIST


# SEARCH — composes search backends


def test_search_missing_query(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.SEARCH, query="",
    ))
    assert result.verdict is BrowsingVerdict.FAILED
    assert "missing query" in result.diagnostic.lower()


def test_search_with_query_uses_backend(monkeypatch):
    """Query is non-empty — composer routes to web_research_service
    first, falls back to web_search. Either way result is built
    (success or backend failure)."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.SEARCH, query="python asyncio docs",
    ))
    assert result.action is BrowsingAction.SEARCH
    # Verdict could be CLEAN or FAILED depending on backend
    # availability; what we assert is the verdict is one of the
    # closed taxonomy values + the backend_used is recorded.
    assert result.verdict in BrowsingVerdict
    assert result.backend_used  # non-empty


# Renderer


def test_format_panel_master_off():
    out = format_browsing_panel()
    assert "disabled" in out
    assert _ENV_MASTER in out


def test_format_panel_with_result(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    result = _run(perform_browsing_action(
        BrowsingAction.CITE,
        url="https://github.com/a/b",
        fragment="ok",
    ))
    out = format_browsing_panel(result)
    assert "Web Browser" in out
    assert "cite" in out


# Sync wrapper


def test_sync_wrapper_outside_loop():
    result = perform_browsing_action_sync(
        BrowsingAction.NAVIGATE,
        url="https://example.com",
    )
    # Master off → FAILED
    assert isinstance(result, BrowsingResult)
    assert result.verdict is BrowsingVerdict.FAILED


def test_sync_wrapper_inside_loop_returns_failed():
    """Sync wrapper called inside a running loop must return
    FAILED with diagnostic (not deadlock or raise)."""
    async def inner():
        return perform_browsing_action_sync(
            BrowsingAction.NAVIGATE, url="https://example.com",
        )
    result = asyncio.run(inner())
    assert result.verdict is BrowsingVerdict.FAILED
    assert "event loop" in result.diagnostic.lower()


# Credential leak path (synthetic — inject test-only mcp finding)


def test_credential_leak_replaces_body(monkeypatch):
    """Simulate sanitized body containing a fake credential
    by monkeypatching _scan_credentials to return findings."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")

    # Force the static fetch backend to return a body
    async def _fake_static(url):
        return (
            "API_KEY=AKIAFAKE1234567890",
            "test_backend",
            "synthetic fetch",
        )
    monkeypatch.setattr(wb, "_backend_fetch_static", _fake_static)

    # Force scanner to detect credentials
    def _fake_scan(text, source_label):
        if "API_KEY" in text:
            return 1, ("aws_key",)
        return 0, ()
    monkeypatch.setattr(wb, "_scan_credentials", _fake_scan)

    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.test/leak",
    ))
    assert result.verdict is BrowsingVerdict.CREDENTIAL_LEAKED
    assert "REDACTED" in result.sanitized_body
    assert "API_KEY" not in result.sanitized_body
    assert "aws_key" in result.leaked_credential_kinds


def test_clean_body_passes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")

    async def _fake_static(url):
        return "Hello world", "test_backend", "synthetic fetch"
    monkeypatch.setattr(wb, "_backend_fetch_static", _fake_static)

    def _fake_scan(text, source_label):
        return 0, ()
    monkeypatch.setattr(wb, "_scan_credentials", _fake_scan)

    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.test/page",
    ))
    assert result.verdict is BrowsingVerdict.CLEAN
    assert result.sanitized_body == "Hello world"


def test_empty_body_returns_failed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")

    async def _fake_static(url):
        return "", "test_backend", "no body"
    monkeypatch.setattr(wb, "_backend_fetch_static", _fake_static)

    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.test/empty",
    ))
    assert result.verdict is BrowsingVerdict.FAILED


def test_timeout_body_returns_rate_limited(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")

    async def _fake_static(url):
        return "", "test_backend", "timeout"
    monkeypatch.setattr(wb, "_backend_fetch_static", _fake_static)

    result = _run(perform_browsing_action(
        BrowsingAction.NAVIGATE,
        url="https://example.test/slow",
    ))
    assert result.verdict is BrowsingVerdict.RATE_LIMITED


# JS render gating


def test_js_render_disabled_falls_back_static(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")
    monkeypatch.setenv(_ENV_ALLOW_JS_RENDER, "false")

    async def _fake_static(url):
        return "Static body", "static", "ok"
    monkeypatch.setattr(wb, "_backend_fetch_static", _fake_static)

    def _fake_scan(text, source_label):
        return 0, ()
    monkeypatch.setattr(wb, "_scan_credentials", _fake_scan)

    result = _run(perform_browsing_action(
        BrowsingAction.EXTRACT_TEXT,
        url="https://example.test/page",
        js_render=True,
    ))
    assert result.verdict is BrowsingVerdict.CLEAN
    assert result.backend_used == "static"


def test_extract_image_disabled_when_no_js(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_DOMAIN_ALLOWLIST, "example.test")
    monkeypatch.setenv(_ENV_ALLOW_JS_RENDER, "false")
    result = _run(perform_browsing_action(
        BrowsingAction.EXTRACT_IMAGE,
        url="https://example.test/page",
    ))
    assert result.verdict is BrowsingVerdict.FAILED
    assert "js_render" in result.diagnostic.lower()


# to_dict


def test_result_to_dict():
    r = BrowsingResult(
        action=BrowsingAction.NAVIGATE,
        verdict=BrowsingVerdict.CLEAN,
        url="https://example.com",
        host="example.com",
        content_bytes=100,
        sanitized_body="hello",
        redacted_bytes=0,
        leaked_credential_kinds=(),
        backend_used="static",
        latency_ms=12.5,
        diagnostic="ok",
    )
    d = r.to_dict()
    assert d["action"] == "navigate"
    assert d["verdict"] == "clean"
    assert d["schema_version"] == WEB_BROWSER_SCHEMA_VERSION


def test_citation_to_dict():
    c = CitationRecord(
        url="https://x.com",
        host="x.com",
        fragment="key fact",
        cited_at_unix=1.0,
        op_id="op",
    )
    d = c.to_dict()
    assert d["kind"] == "citation"
    assert d["schema_version"] == WEB_BROWSER_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/web_browser.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "action_taxonomy_closed",
        "verdict_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_action_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "action_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class BrowsingAction(str, enum.Enum):\n"
        "    SEARCH = 'search'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class BrowsingVerdict(str, enum.Enum):\n"
        "    CLEAN = 'clean'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_forbids_tool_executor():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.tool_executor "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    out = pin.validate(ast.parse(bad), bad)
    assert out  # multiple violations


# Flag registry


class _FakeRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _FakeRegistry()
    count = register_flags(reg)
    assert count == 7


def test_flag_master_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_flag_js_render_default_false():
    reg = _FakeRegistry()
    register_flags(reg)
    js = next(
        s for s in reg.registered if s.name == _ENV_ALLOW_JS_RENDER
    )
    assert js.default is False


# SSE event


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_WEB_BROWSING_ACTION
        == "web_browsing_action"
    )
    assert "web_browsing_action" in ios._VALID_EVENT_TYPES

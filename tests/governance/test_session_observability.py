"""Tests for GET /observability/sessions* (extension Slice 4).

Uses :func:`aiohttp.test_utils.make_mocked_request` to exercise the
handlers directly — matches the pattern established in
``test_ide_observability.py`` (sandbox-safe, no socket bind).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
    IDE_OBSERVABILITY_SCHEMA_VERSION,
)
from backend.core.ouroboros.governance.session_browser import (
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
    reset_default_session_singletons,
    set_default_session_browser,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_session(
    root: Path, session_id: str,
    *,
    ops_total: int = 3, ops_applied: int = 2,
    stop_reason: str = "complete", cost: float = 0.10,
    replay: bool = False, corrupt: bool = False,
) -> Path:
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    if corrupt:
        (d / "summary.json").write_text("{ not json")
    else:
        (d / "summary.json").write_text(json.dumps({
            "stop_reason": stop_reason,
            "stats": {
                "ops_total": ops_total, "ops_applied": ops_applied,
                "cost": {"spent_usd": cost},
            },
        }))
    if replay:
        (d / "replay.html").write_text("<html>ok</html>")
    return d


def _mock_request(
    path: str,
    *,
    query: Optional[Dict[str, str]] = None,
    match_info: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    full = path
    if query:
        full = f"{path}?{urlencode(query)}"
    req = make_mocked_request("GET", full, headers=headers or {})
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


async def _body(resp: web.Response) -> Dict[str, Any]:
    return json.loads(resp.text or "{}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_IDE_OBSERVABILITY_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_session_singletons()
    yield
    reset_default_session_singletons()


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    p = tmp_path / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def browser(tmp_path: Path, sessions_root: Path) -> SessionBrowser:
    bm_root = tmp_path / "bmroot"
    bm_root.mkdir(parents=True, exist_ok=True)
    b = SessionBrowser(
        index=SessionIndex(root=sessions_root),
        bookmarks=BookmarkStore(bookmark_root=bm_root),
    )
    set_default_session_browser(b)
    return b


@pytest.fixture
def router() -> IDEObservabilityRouter:
    return IDEObservabilityRouter()


# ===========================================================================
# Surface availability
# ===========================================================================


async def test_health_announces_sessions_surface(router):
    resp = await router._handle_health(_mock_request("/observability/health"))
    body = await _body(resp)
    assert resp.status == 200
    assert "sessions" in body["surface"]


async def test_sessions_list_empty(router, browser):
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    body = await _body(resp)
    assert resp.status == 200
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
    assert body["count"] == 0
    assert body["sessions"] == []


async def test_sessions_list_happy_path(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-a", ops_total=3, cost=0.10)
    _mk_session(sessions_root, "bt-b", ops_total=5, cost=0.20)
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    body = await _body(resp)
    assert resp.status == 200
    assert body["count"] == 2
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-a", "bt-b"}
    for s in body["sessions"]:
        assert "bookmarked" in s
        assert "pinned" in s
        assert s["bookmarked"] is False
        assert s["pinned"] is False


async def test_sessions_detail_happy_path(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-x", ops_total=7)
    browser.index.scan()
    resp = await router._handle_session_detail(
        _mock_request(
            "/observability/sessions/bt-x",
            match_info={"session_id": "bt-x"},
        ),
    )
    body = await _body(resp)
    assert resp.status == 200
    assert body["session_id"] == "bt-x"
    assert body["ops_total"] == 7
    assert body["bookmarked"] is False
    assert body["pinned"] is False


async def test_sessions_detail_reflects_bookmark_and_pin(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-pinned")
    browser.index.scan()
    browser.pin("bt-pinned", note="keeper")
    resp = await router._handle_session_detail(
        _mock_request(
            "/observability/sessions/bt-pinned",
            match_info={"session_id": "bt-pinned"},
        ),
    )
    body = await _body(resp)
    assert body["bookmarked"] is True
    assert body["pinned"] is True
    assert body["bookmark_note"] == "keeper"
    assert body["bookmark_ts"]


# ===========================================================================
# Filters
# ===========================================================================


async def test_filter_ok_true(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-ok", stop_reason="complete")
    _mk_session(sessions_root, "bt-bad", stop_reason="error", ops_total=0)
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions", query={"ok": "true"}),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert "bt-ok" in ids
    assert "bt-bad" not in ids


async def test_filter_has_replay(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-with", replay=True)
    _mk_session(sessions_root, "bt-without")
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"has_replay": "true"},
        ),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-with"}


async def test_filter_parse_error(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-ok")
    _mk_session(sessions_root, "bt-corrupt", corrupt=True)
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"parse_error": "true"},
        ),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-corrupt"}


async def test_filter_bookmarked_only(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-bm")
    _mk_session(sessions_root, "bt-plain")
    browser.index.scan()
    browser.bookmark("bt-bm")
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"bookmarked": "true"},
        ),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-bm"}


async def test_filter_pinned_only(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-pin")
    _mk_session(sessions_root, "bt-plain")
    _mk_session(sessions_root, "bt-bm")
    browser.index.scan()
    browser.pin("bt-pin")
    browser.bookmark("bt-bm")
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"pinned": "true"},
        ),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-pin"}


async def test_filter_prefix(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-match-1")
    _mk_session(sessions_root, "bt-match-2")
    _mk_session(sessions_root, "xy-other")
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"prefix": "bt-match"},
        ),
    )
    body = await _body(resp)
    ids = {s["session_id"] for s in body["sessions"]}
    assert ids == {"bt-match-1", "bt-match-2"}


async def test_limit_respected(router, browser, sessions_root):
    for i in range(12):
        _mk_session(sessions_root, f"bt-{i:03d}")
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"limit": "5"},
        ),
    )
    body = await _body(resp)
    assert body["count"] == 5


# ===========================================================================
# Error paths
# ===========================================================================


async def test_sessions_detail_unknown_is_404(router, browser):
    resp = await router._handle_session_detail(
        _mock_request(
            "/observability/sessions/bt-ghost",
            match_info={"session_id": "bt-ghost"},
        ),
    )
    body = await _body(resp)
    assert resp.status == 404
    assert body["reason_code"] == "ide_observability.unknown_session_id"


async def test_sessions_detail_malformed_is_400(router, browser):
    resp = await router._handle_session_detail(
        _mock_request(
            "/observability/sessions/bad%20id",
            match_info={"session_id": "bad id"},
        ),
    )
    assert resp.status == 400


async def test_filter_prefix_malformed_is_400(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"prefix": "bad prefix"},
        ),
    )
    body = await _body(resp)
    assert resp.status == 400
    assert body["reason_code"] == "ide_observability.malformed_prefix"


async def test_limit_malformed_is_400(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"limit": "-5"},
        ),
    )
    assert resp.status == 400


async def test_limit_non_integer_is_400(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"limit": "abc"},
        ),
    )
    assert resp.status == 400


async def test_limit_too_large_is_400(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions", query={"limit": "999999"},
        ),
    )
    assert resp.status == 400


# ===========================================================================
# Kill switch — disabled returns 403
# ===========================================================================


async def test_list_disabled_returns_403(router, browser, monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    body = await _body(resp)
    assert resp.status == 403
    assert body["reason_code"] == "ide_observability.disabled"


async def test_detail_disabled_returns_403(router, browser, monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = await router._handle_session_detail(
        _mock_request(
            "/observability/sessions/bt-anything",
            match_info={"session_id": "bt-anything"},
        ),
    )
    assert resp.status == 403


# ===========================================================================
# Headers
# ===========================================================================


async def test_response_carries_no_store_cache(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-a")
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    assert resp.headers.get("Cache-Control") == "no-store"


async def test_response_carries_schema_version(router, browser, sessions_root):
    _mk_session(sessions_root, "bt-a")
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    body = await _body(resp)
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION


# ===========================================================================
# CORS — allowlist
# ===========================================================================


async def test_cors_echoes_localhost_origin(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions",
            headers={"Origin": "http://localhost:3000"},
        ),
    )
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


async def test_cors_rejects_unrecognized_origin(router, browser):
    resp = await router._handle_session_list(
        _mock_request(
            "/observability/sessions",
            headers={"Origin": "https://evil.example.com"},
        ),
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


# ===========================================================================
# Rate limit — storm yields 429
# ===========================================================================


async def test_rate_limit_fires(router, browser, monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "3")
    # Three ok, fourth 429
    for _ in range(3):
        resp = await router._handle_session_list(
            _mock_request("/observability/sessions"),
        )
        assert resp.status == 200
    resp = await router._handle_session_list(
        _mock_request("/observability/sessions"),
    )
    assert resp.status == 429


# ===========================================================================
# Route registration — list + detail paths wired
# ===========================================================================


def test_route_registration_adds_session_paths():
    app = web.Application()
    IDEObservabilityRouter().register_routes(app)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/observability/sessions" in paths
    assert "/observability/sessions/{session_id}" in paths

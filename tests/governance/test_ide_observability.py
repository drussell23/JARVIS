"""Regression spine — Gap #6 Slice 1 IDE observability surface.

Pins the authority + security + shape contracts locked by
authorization:

  1. Deny-by-default (env flag + string-"false" edge).
  2. Authority invariant: no imports from gate/execution modules.
  3. Loopback-only binding: assert_loopback_only() rejects 0.0.0.0
     and its friends.
  4. Rate limiting: sliding-window cap; storm behavior; 429 on cap.
  5. CORS: narrow allowlist; never "*" with credentials; unmatched
     origins get no ACAO header (silent drop, not wildcard).
  6. JSON shape: schema_version stamped on every payload; response
     layout for /health, /tasks, /tasks/{op_id}.
  7. Security: malformed op_id → 400 with stable reason_code;
     unknown op_id → 404 with stable reason_code; no stack traces
     or internal paths leaked.
  8. No-secret-leakage: responses echo only the public Task fields
     (already-sanitized audit surface).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDE_OBSERVABILITY_SCHEMA_VERSION,
    IDEObservabilityRouter,
    _cors_origin_patterns,
    _rate_limit_per_min,
    assert_loopback_only,
    ide_observability_enabled,
)
from backend.core.ouroboros.governance.task_board import (
    TaskBoard,
)
from backend.core.ouroboros.governance.task_tool import (
    _BOARDS,
    get_or_create_task_board,
    reset_task_board_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ide_env(monkeypatch):
    """Clean env + registry per test."""
    for key in list(os.environ.keys()):
        if (
            key.startswith("JARVIS_IDE_OBSERVABILITY_")
            or key.startswith("JARVIS_TOOL_TASK_BOARD_")
        ):
            monkeypatch.delenv(key, raising=False)
    reset_task_board_registry()
    yield
    reset_task_board_registry()


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")


def _make_request(
    path: str,
    *,
    method: str = "GET",
    headers: Dict[str, str] = None,
    match_info: Dict[str, str] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    """Build a minimal aiohttp Request for handler testing without
    spinning a real HTTP server. Uses aiohttp.test_utils helper."""
    headers = headers or {}
    req = make_mocked_request(method, path, headers=headers)
    # Inject match_info (URL template params).
    if match_info:
        req.match_info.update(match_info)
    # Override remote — make_mocked_request doesn't set it.
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


# ---------------------------------------------------------------------------
# 1. Env gate — deny-by-default
# ---------------------------------------------------------------------------


def test_ide_observability_default_post_graduation_is_true(monkeypatch):
    """Gap #6 Slice 4 graduation (2026-04-20): the master switch
    defaults ``true`` now that Slices 1-3 have shipped the full
    read-only GET + SSE + VS Code consumer stack. Explicit
    ``=false`` remains the kill switch."""
    monkeypatch.delenv("JARVIS_IDE_OBSERVABILITY_ENABLED", raising=False)
    assert ide_observability_enabled() is True


def test_env_false_string_opts_out(monkeypatch):
    """Explicit ``=false`` reverts to the pre-graduation deny-by-
    default posture. This is the runtime kill switch."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    assert ide_observability_enabled() is False


def test_env_explicit_true_still_enables(monkeypatch):
    """Explicit ``=true`` matches the graduated default — idempotent."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    assert ide_observability_enabled() is True


def test_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "TRUE")
    assert ide_observability_enabled() is True
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "FALSE")
    assert ide_observability_enabled() is False


# ---------------------------------------------------------------------------
# 2. Authority invariant — no imports from gate/execution modules
# ---------------------------------------------------------------------------


def test_ide_observability_does_not_import_gate_modules():
    """Slice 1 test 5 (CRITICAL, authorization-locked): the
    ide_observability module MUST NOT import any authority-carrying
    module. Read-only observability stays read-only — no Iron Gate,
    no policy engine, no orchestrator, no tool_executor, no risk
    tier floor, no semantic guardian. Grep-enforced so future
    refactors can't smuggle authority in."""
    src = Path(
        "backend/core/ouroboros/governance/ide_observability.py"
    ).read_text()
    forbidden = [
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.policy_engine",
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
    ]
    for f in forbidden:
        assert f not in src, (
            "Slice 1 authority violation: ide_observability.py now "
            "imports " + repr(f) + ". GET surface MUST stay "
            "observability-only."
        )


# ---------------------------------------------------------------------------
# 3. Loopback-binding validator
# ---------------------------------------------------------------------------


def test_loopback_accepts_127_0_0_1():
    assert_loopback_only("127.0.0.1")


def test_loopback_accepts_ipv6():
    assert_loopback_only("::1")


def test_loopback_accepts_localhost_alias():
    assert_loopback_only("localhost")


def test_loopback_rejects_0_0_0_0():
    """Slice 1 test 9 (CRITICAL): the GET surface MUST NOT bind
    to 0.0.0.0. Validator raises at boot so misconfiguration fails
    loudly, never silently exposing the surface to LAN."""
    with pytest.raises(ValueError, match="non-loopback"):
        assert_loopback_only("0.0.0.0")


def test_loopback_rejects_wildcard_ipv6():
    with pytest.raises(ValueError):
        assert_loopback_only("::")


def test_loopback_rejects_empty_string():
    with pytest.raises(ValueError):
        assert_loopback_only("")


def test_loopback_rejects_external_looking_address():
    """Slice 1 test 12: even a routable-looking address is rejected
    — the allowlist is explicit (127.0.0.1 / ::1 / localhost) rather
    than a denylist."""
    with pytest.raises(ValueError, match="must be one of"):
        assert_loopback_only("192.168.1.1")


# ---------------------------------------------------------------------------
# 4. Handlers — disabled path (403 for every route)
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Helper: run an async handler synchronously inside a test."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_health_returns_403_when_explicitly_disabled(monkeypatch):
    """Post-Slice-4 graduation: the disabled path is exercised by
    setting ``=false`` explicitly (the runtime kill switch). When
    off, health returns 403 — NOT 200 with ``{enabled: false}``.
    Port scanners see no signal about what's behind the listener."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.disabled"
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION


def test_tasks_list_returns_403_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tasks")
    resp = _run_async(router._handle_task_list(req))
    assert resp.status == 403


def test_task_detail_returns_403_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/op-x",
        match_info={"op_id": "op-x"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert resp.status == 403


# ---------------------------------------------------------------------------
# 5. Handlers — happy path
# ---------------------------------------------------------------------------


def test_health_returns_200_when_enabled(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
    assert body["enabled"] is True
    assert body["surface"] == "tasks"
    assert "api_version" in body


def test_tasks_list_returns_empty_when_no_boards(monkeypatch):
    _enable(monkeypatch)
    reset_task_board_registry()
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tasks")
    resp = _run_async(router._handle_task_list(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["op_ids"] == []
    assert body["count"] == 0


def test_tasks_list_returns_registered_op_ids(monkeypatch):
    _enable(monkeypatch)
    get_or_create_task_board("op-alpha")
    get_or_create_task_board("op-beta")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tasks")
    resp = _run_async(router._handle_task_list(req))
    body = json.loads(resp.body.decode("utf-8"))
    assert sorted(body["op_ids"]) == ["op-alpha", "op-beta"]
    assert body["count"] == 2


def test_task_detail_returns_projection(monkeypatch):
    """Slice 1 test 18: task detail returns the documented JSON
    projection (tasks array, active_task_id, closed flag,
    board_size, schema_version)."""
    _enable(monkeypatch)
    board = get_or_create_task_board("op-detail")
    t1 = board.create(title="first task")
    t2 = board.create(title="second task", body="longer body content")
    board.start(t1.task_id)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/op-detail",
        match_info={"op_id": "op-detail"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
    assert body["op_id"] == "op-detail"
    assert body["closed"] is False
    assert body["active_task_id"] == t1.task_id
    assert body["board_size"] == 2
    assert len(body["tasks"]) == 2
    ids = [t["task_id"] for t in body["tasks"]]
    assert t1.task_id in ids and t2.task_id in ids
    # Task fields projected, nothing extra.
    for t in body["tasks"]:
        assert set(t.keys()) == {
            "task_id", "state", "title", "body", "sequence", "cancel_reason",
        }


def test_task_detail_reflects_closed_board(monkeypatch):
    """Slice 1 test 19: closed board's detail is still readable;
    ``closed: true`` in the projection. Matches TaskBoard's
    documented semantic (reads work post-close)."""
    _enable(monkeypatch)
    board = get_or_create_task_board("op-closed")
    board.create(title="t")
    board.close(reason="test")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/op-closed",
        match_info={"op_id": "op-closed"},
    )
    resp = _run_async(router._handle_task_detail(req))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["closed"] is True


# ---------------------------------------------------------------------------
# 6. Handlers — error paths
# ---------------------------------------------------------------------------


def test_task_detail_unknown_op_id_returns_404(monkeypatch):
    """Slice 1 test 20: unknown op_id returns 404 with stable
    reason_code. No stack trace, no internal path."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/op-ghost",
        match_info={"op_id": "op-ghost"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert resp.status == 404
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.unknown_op_id"


def test_task_detail_malformed_op_id_returns_400(monkeypatch):
    """Slice 1 test 21: op_id with URL-invalid characters returns
    400. The regex allows [A-Za-z0-9_-] only."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    for bad in ("../etc/passwd", "op-with spaces", "op;drop;table",
                "op<script>", ""):
        req = _make_request(
            "/observability/tasks/" + bad,
            match_info={"op_id": bad},
        )
        resp = _run_async(router._handle_task_detail(req))
        assert resp.status == 400, "expected 400 for " + repr(bad)
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.malformed_op_id"


# ---------------------------------------------------------------------------
# 7. Rate limiting — storm behavior
# ---------------------------------------------------------------------------


def test_rate_limit_blocks_after_cap(monkeypatch):
    """Slice 1 test 22 (CRITICAL): an IDE client hammering the
    endpoint past the per-minute cap is 429'd. Protects agent
    logging + OS from polling storms."""
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "5",
    )
    router = IDEObservabilityRouter()
    # 5 OK calls...
    for i in range(5):
        req = _make_request("/observability/health")
        resp = _run_async(router._handle_health(req))
        assert resp.status == 200, "call " + str(i) + " should pass"
    # 6th call — 429.
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 429
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.rate_limited"


def test_rate_limit_is_per_client(monkeypatch):
    """Slice 1 test 23: rate limiter keys by remote address. Two
    clients hammering simultaneously don't starve each other."""
    _enable(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "3",
    )
    router = IDEObservabilityRouter()
    # Client A: 3 calls.
    for _ in range(3):
        req = _make_request("/observability/health", remote="127.0.0.1")
        resp = _run_async(router._handle_health(req))
        assert resp.status == 200
    # Client B: still has budget.
    req = _make_request("/observability/health", remote="::1")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200


# ---------------------------------------------------------------------------
# 8. CORS — narrow allowlist; never wildcard with credentials
# ---------------------------------------------------------------------------


def test_cors_allows_localhost_origin(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/health",
        headers={"Origin": "http://localhost:3000"},
    )
    resp = _run_async(router._handle_health(req))
    assert resp.headers.get("Access-Control-Allow-Origin") == (
        "http://localhost:3000"
    )
    # No ACAO wildcard; Vary header present.
    assert resp.headers["Access-Control-Allow-Origin"] != "*"
    assert "Origin" in resp.headers.get("Vary", "")


def test_cors_no_header_for_unmatched_origin(monkeypatch):
    """Slice 1 test 25 (CRITICAL): unmatched Origin → NO ACAO
    header (silent drop). Never echo arbitrary origins; never
    wildcard for this surface."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/health",
        headers={"Origin": "http://evil.example.com"},
    )
    resp = _run_async(router._handle_health(req))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_no_credentials_wildcard(monkeypatch):
    """Slice 1 test 26: even on a matched origin, NO
    ``Access-Control-Allow-Credentials`` header. Prevents
    cookie-carrying fetch() from the browser side; IDE extensions
    don't need credentials on this local read surface."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/health",
        headers={"Origin": "http://localhost:3000"},
    )
    resp = _run_async(router._handle_health(req))
    assert "Access-Control-Allow-Credentials" not in resp.headers


# ---------------------------------------------------------------------------
# 9. JSON shape — schema_version + cache headers
# ---------------------------------------------------------------------------


def test_every_response_carries_schema_version(monkeypatch):
    """Slice 1 test 27 (§8 contract): every JSON response carries
    ``schema_version``. Future consumers can feature-detect."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()

    # /health
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert json.loads(resp.body.decode("utf-8"))["schema_version"] == "1.0"

    # /tasks
    req = _make_request("/observability/tasks")
    resp = _run_async(router._handle_task_list(req))
    assert json.loads(resp.body.decode("utf-8"))["schema_version"] == "1.0"

    # Error payload
    req = _make_request(
        "/observability/tasks/bad..path",
        match_info={"op_id": "bad..path"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert json.loads(resp.body.decode("utf-8"))["schema_version"] == "1.0"


def test_cache_control_no_store(monkeypatch):
    """Slice 1 test 28: observability is live state, not cacheable.
    Every response carries ``Cache-Control: no-store`` so IDE
    clients / proxies don't serve stale snapshots."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert resp.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# 10. No-secret-leakage contract
# ---------------------------------------------------------------------------


def test_task_detail_projection_is_bounded_set(monkeypatch):
    """Slice 1 test 29 (CRITICAL): task detail response's
    per-task dict keys are exactly the documented bounded set.
    New fields on Task don't auto-leak — they require an
    explicit handler update (and ideally a schema_version bump)."""
    _enable(monkeypatch)
    board = get_or_create_task_board("op-projection")
    board.create(title="t")
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/op-projection",
        match_info={"op_id": "op-projection"},
    )
    resp = _run_async(router._handle_task_detail(req))
    body = json.loads(resp.body.decode("utf-8"))
    expected_keys = {
        "task_id", "state", "title", "body", "sequence", "cancel_reason",
    }
    for t in body["tasks"]:
        assert set(t.keys()) == expected_keys, (
            "projection drift: " + repr(set(t.keys()) - expected_keys)
        )


def test_cors_patterns_helper_default(monkeypatch):
    """Slice 1 test 30: default CORS allowlist matches localhost +
    127.0.0.1 + vscode-webview only. No public-internet default."""
    patterns = _cors_origin_patterns()
    assert patterns  # non-empty
    # Must include localhost; must NOT include a wildcard.
    assert any("localhost" in p for p in patterns)
    assert not any(p == "*" or p == ".*" for p in patterns)


# ---------------------------------------------------------------------------
# Gap #6 Slice 4 graduation pins (2026-04-20)
# ---------------------------------------------------------------------------
#
# These 10 tests pin the properties that MUST NOT drift after
# flipping ``JARVIS_IDE_OBSERVABILITY_ENABLED`` default false→true.
# They mirror the discipline from Gap #5 Slice 4 + Gap #4 Slice 4 +
# Phase B subagent graduations: graduation flips opt-in friction,
# NEVER authority surface.


def test_slice4_graduation_default_is_true(monkeypatch):
    """Anchor: the source-of-truth flag check reads ``true`` when
    nothing is set. This is the single bit that distinguishes
    "graduated" from "deny-by-default"."""
    monkeypatch.delenv("JARVIS_IDE_OBSERVABILITY_ENABLED", raising=False)
    assert ide_observability_enabled() is True


def test_slice4_graduation_explicit_false_is_full_revert(monkeypatch):
    """The ``=false`` opt-out is the operator's runtime kill switch.
    Every handler returns 403 with the canonical reason_code, and
    the schema_version stamp is still present. Matches the pre-
    graduation behavior bit-for-bit."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    router = IDEObservabilityRouter()
    for path, handler in [
        ("/observability/health", router._handle_health),
        ("/observability/tasks", router._handle_task_list),
    ]:
        req = _make_request(path)
        resp = _run_async(handler(req))
        assert resp.status == 403, path
        body = json.loads(resp.body.decode("utf-8"))
        assert body["reason_code"] == "ide_observability.disabled"
        assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION
    # Plus task detail with a valid op_id.
    req = _make_request(
        "/observability/tasks/op-x", match_info={"op_id": "op-x"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert resp.status == 403


def test_slice4_graduation_authority_invariant_preserved():
    """Graduation must NOT have added imports of gate / policy /
    orchestrator modules. Re-run the Slice 1 grep pin as a smoke
    check that the module still respects the boundary principle."""
    path = Path(__file__).resolve().parent.parent.parent / (
        "backend/core/ouroboros/governance/ide_observability.py"
    )
    src = path.read_text(encoding="utf-8")
    forbidden = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier_floor",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.semantic_firewall",
        "from backend.core.ouroboros.governance.policy_engine",
    ]
    for f in forbidden:
        assert f not in src, (
            "ide_observability.py still must not import "
            + f + " post-graduation"
        )


def test_slice4_graduation_loopback_assert_still_strict():
    """Graduation must NOT relax the loopback-only pin. Same
    accept/reject matrix as Slice 1."""
    assert_loopback_only("127.0.0.1")
    assert_loopback_only("::1")
    assert_loopback_only("localhost")
    for host in ["0.0.0.0", "::", "*", "", "203.0.113.7", "2001:db8::1"]:
        try:
            assert_loopback_only(host)
        except ValueError:
            continue
        raise AssertionError(
            "expected ValueError for non-loopback host " + repr(host)
        )


def test_slice4_graduation_rate_limit_still_enforced(monkeypatch):
    """Graduation must NOT lift the per-client rate limit. Setting
    the limit to 2/min and burning through it still trips the 429
    path, as in Slice 1."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "2")
    router = IDEObservabilityRouter()
    ok = 0
    limited = 0
    for _ in range(4):
        # Manual rate-limit check (public surface has this via handlers,
        # but we test the helper directly for a single-signal pin).
        if router._check_rate_limit("client-a"):
            ok += 1
        else:
            limited += 1
    assert ok == 2
    assert limited == 2


def test_slice4_graduation_cors_still_no_wildcard(monkeypatch):
    """Graduation must NOT widen CORS to ``*`` or add a
    credentials wildcard. Defaults stay narrow (localhost +
    127.0.0.1 + vscode-webview)."""
    patterns = _cors_origin_patterns()
    assert not any(p == "*" for p in patterns), patterns
    assert not any(p == ".*" for p in patterns), patterns


def test_slice4_graduation_malformed_op_id_still_400(monkeypatch):
    """Graduation must NOT relax op_id validation at the URL
    boundary. A space in the op_id still yields 400 with the
    stable reason_code."""
    monkeypatch.delenv("JARVIS_IDE_OBSERVABILITY_ENABLED", raising=False)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tasks/bad id",
        match_info={"op_id": "bad id"},
    )
    resp = _run_async(router._handle_task_detail(req))
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.malformed_op_id"


def test_slice4_graduation_docstring_references_graduation():
    """Docstring bit-rot guard: the master-switch docstring must
    name the Slice 4 graduation so future readers know the current
    default is graduated, not drifted."""
    doc = ide_observability_enabled.__doc__ or ""
    assert "graduated" in doc.lower(), doc
    assert "2026-04-20" in doc, doc


def test_slice4_graduation_full_revert_matrix(monkeypatch):
    """Explicit off → disabled. Unset → enabled. Explicit on →
    enabled. One test, three conditions — the full revert matrix
    in a single assertion block."""
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    assert ide_observability_enabled() is False
    monkeypatch.delenv("JARVIS_IDE_OBSERVABILITY_ENABLED", raising=False)
    assert ide_observability_enabled() is True
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    assert ide_observability_enabled() is True


def test_slice4_graduation_cursor_compat_shape():
    """Gap #6 Slice 4 Cursor compat: the CORS default allowlist
    accepts Cursor's webview origin scheme. Cursor is a VS Code
    fork and uses the same ``vscode-webview://`` scheme, so the
    existing regex matches without any server-side change.
    Pinning this invariant prevents a future CORS tightening
    from accidentally breaking Cursor consumers."""
    patterns = _cors_origin_patterns()
    joined = " | ".join(patterns)
    assert "vscode-webview" in joined, joined

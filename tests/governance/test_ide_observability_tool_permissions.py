"""Regression spine for Venom V2 Slice 4 — IDE GET endpoints
exposing the ``permission_decision_archive`` ring.

Pins the load-bearing invariants for the three new routes:

* ``GET /observability/tool-permissions[?limit=N]`` — recent ring
* ``GET /observability/tool-permissions/by-tool/{tool_name}`` —
  exact-match filter on tool_name
* ``GET /observability/tool-permissions/{op_id}`` — exact-match
  filter on op_id

Plus the route-order invariant (specific path before
parameterized path), dual master-flag gating, payload schema, and
authority asymmetry.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, Iterator, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDE_OBSERVABILITY_SCHEMA_VERSION,
    IDEObservabilityRouter,
)
from backend.core.ouroboros.governance.permission_decision_archive import (
    ARCHIVE_SIZE_ENV_VAR,
    MASTER_FLAG_ENV_VAR as ARCHIVE_FLAG,
    maybe_record_decision,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.tool_permission import (
    AggregatePermissionDecision,
    TOOL_PERMISSION_SCHEMA_VERSION,
    ToolPermissionDecision,
)


_IDE_FLAG = "JARVIS_IDE_OBSERVABILITY_ENABLED"


def _make_request(
    path: str,
    *,
    method: str = "GET",
    match_info: Optional[Dict[str, str]] = None,
    query: Optional[Dict[str, str]] = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    """Build a minimal aiohttp Request for handler testing without
    spinning a real HTTP server."""
    if query:
        path = path + "?" + "&".join(
            f"{k}={v}" for k, v in query.items()
        )
    req = make_mocked_request(method, path)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(_IDE_FLAG, "true")
    monkeypatch.setenv(ARCHIVE_FLAG, "true")


def _make_decision(
    *,
    tool_name: str = "read_file",
    op_id: str = "op-A",
    value: ToolPermissionDecision = ToolPermissionDecision.ALLOW,
    detail: str = "test",
) -> AggregatePermissionDecision:
    return AggregatePermissionDecision(
        schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
        tool_name=tool_name,
        op_id=op_id,
        decision=value,
        total_callbacks=1,
        detail=detail,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with both master flags cleared + fresh
    archive singleton. Per-test enable via ``_enable``."""
    monkeypatch.delenv(_IDE_FLAG, raising=False)
    monkeypatch.delenv(ARCHIVE_FLAG, raising=False)
    monkeypatch.delenv(ARCHIVE_SIZE_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ---------------------------------------------------------------------------
# Route registration — invariants
# ---------------------------------------------------------------------------


def test_routes_registered_on_aiohttp_app():
    """All 3 tool-permissions paths MUST register on the router.
    Drift here is the surface-disappearance failure mode."""
    app = web.Application()
    IDEObservabilityRouter().register_routes(app)
    paths = set()
    for r in app.router.routes():
        paths.add(str(r.resource))
    # GET + HEAD share a resource; we count unique paths.
    assert any(
        "tool-permissions>" in p
        and "by-tool" not in p
        and "{" not in p
        for p in paths
    ), "recent route missing"
    assert any(
        "by-tool" in p for p in paths
    ), "by-tool route missing"
    assert any(
        "{op_id}" in p for p in paths
    ), "by-op route missing"


def test_route_order_specific_before_parameterized():
    """The ``/by-tool/{tool_name}`` route MUST register BEFORE
    the generic ``/{op_id}`` route — aiohttp matches in order
    and the generic op_id pattern would otherwise capture
    ``by-tool`` as an op_id. Load-bearing positional invariant."""
    src = Path(
        "backend/core/ouroboros/governance/ide_observability.py",
    ).read_text(encoding="utf-8")
    by_tool_idx = src.index(
        '"/observability/tool-permissions/by-tool/{tool_name}"',
    )
    by_op_idx = src.index(
        '"/observability/tool-permissions/{op_id}"',
    )
    assert by_tool_idx < by_op_idx, (
        "Route-order regression: /by-tool/{tool_name} MUST appear "
        "BEFORE /{op_id} in register_routes — aiohttp matches in "
        "registration order; the generic op_id pattern would "
        "otherwise capture 'by-tool' as an op_id"
    )


def test_surface_field_advertises_tool_permissions(monkeypatch):
    """The ``/observability/health`` surface field MUST advertise
    the tool-permissions domain — operator/IDE feature detection
    relies on this."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/health")
    resp = _run_async(router._handle_health(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert "tool-permissions" in body["surface"]


# ---------------------------------------------------------------------------
# Gate enforcement — 403/429/200
# ---------------------------------------------------------------------------


def test_recent_returns_403_when_ide_disabled(monkeypatch):
    """IDE observability master off → 403 (port-scanner discipline)."""
    monkeypatch.setenv(_IDE_FLAG, "false")
    monkeypatch.setenv(ARCHIVE_FLAG, "true")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tool-permissions")
    resp = _run_async(router._handle_tool_permissions_recent(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.disabled"


def test_recent_returns_403_when_archive_disabled(monkeypatch):
    """IDE on but archive off → 403 (archive owns its surface)."""
    monkeypatch.setenv(_IDE_FLAG, "true")
    monkeypatch.delenv(ARCHIVE_FLAG, raising=False)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tool-permissions")
    resp = _run_async(router._handle_tool_permissions_recent(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.tool_permissions_disabled"
    )


def test_by_tool_returns_400_on_missing_tool_name(monkeypatch):
    """Empty tool_name → 400 with operator-readable reason_code."""
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions/by-tool/",
        match_info={"tool_name": ""},
    )
    resp = _run_async(
        router._handle_tool_permissions_by_tool(req),
    )
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.tool_permissions_missing_tool_name"
    )


def test_by_op_returns_400_on_missing_op_id(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions/",
        match_info={"op_id": ""},
    )
    resp = _run_async(
        router._handle_tool_permissions_by_op(req),
    )
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.tool_permissions_missing_op_id"
    )


# ---------------------------------------------------------------------------
# Happy path — payload schema + projections
# ---------------------------------------------------------------------------


def test_recent_empty_archive_returns_200_with_empty_records(
    monkeypatch,
):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tool-permissions")
    resp = _run_async(router._handle_tool_permissions_recent(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 0
    assert body["records"] == []
    assert body["snapshot"]["size"] == 0
    assert body["schema_version"] == IDE_OBSERVABILITY_SCHEMA_VERSION


def test_recent_returns_records_newest_first(monkeypatch):
    _enable(monkeypatch)
    maybe_record_decision(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(),
    )
    maybe_record_decision(
        op_id="op-A", tool_name="write_file",
        decision=_make_decision(tool_name="write_file"),
    )
    router = IDEObservabilityRouter()
    req = _make_request("/observability/tool-permissions")
    resp = _run_async(router._handle_tool_permissions_recent(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 2
    # Newest first: write_file (p-2) before read_file (p-1)
    refs = [r["ref"] for r in body["records"]]
    assert refs == ["p-2", "p-1"]
    # Canonical projection nested correctly
    assert (
        body["records"][0]["schema_version"]
        == "permission_decision_archive.v1"
    )
    assert (
        body["records"][0]["decision"]["schema_version"]
        == TOOL_PERMISSION_SCHEMA_VERSION
    )


def test_recent_limit_query_param_respected(monkeypatch):
    _enable(monkeypatch)
    for i in range(5):
        maybe_record_decision(
            op_id=f"op-X{i}", tool_name="read_file",
            decision=_make_decision(op_id=f"op-X{i}"),
        )
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions",
        query={"limit": "2"},
    )
    resp = _run_async(router._handle_tool_permissions_recent(req))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 2


def test_recent_limit_clamped_to_max(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions",
        query={"limit": "999"},
    )
    resp = _run_async(router._handle_tool_permissions_recent(req))
    # Should not 5xx — clamped to ceiling=200, just returns empty.
    assert resp.status == 200


def test_by_tool_filters_exact_match(monkeypatch):
    _enable(monkeypatch)
    maybe_record_decision(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(tool_name="read_file"),
    )
    maybe_record_decision(
        op_id="op-2", tool_name="bash",
        decision=_make_decision(tool_name="bash", op_id="op-2"),
    )
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions/by-tool/read_file",
        match_info={"tool_name": "read_file"},
    )
    resp = _run_async(
        router._handle_tool_permissions_by_tool(req),
    )
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["tool_name"] == "read_file"
    assert body["count"] == 1
    assert body["records"][0]["tool_name"] == "read_file"


def test_by_op_filters_exact_match(monkeypatch):
    _enable(monkeypatch)
    maybe_record_decision(
        op_id="op-Alpha", tool_name="read_file",
        decision=_make_decision(op_id="op-Alpha"),
    )
    maybe_record_decision(
        op_id="op-Beta", tool_name="bash",
        decision=_make_decision(
            op_id="op-Beta", tool_name="bash",
        ),
    )
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/tool-permissions/op-Alpha",
        match_info={"op_id": "op-Alpha"},
    )
    resp = _run_async(
        router._handle_tool_permissions_by_op(req),
    )
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["op_id"] == "op-Alpha"
    assert body["count"] == 1
    assert body["records"][0]["op_id"] == "op-Alpha"


# ---------------------------------------------------------------------------
# Authority asymmetry — read-only invariant
# ---------------------------------------------------------------------------


def test_handlers_do_not_mutate_archive(monkeypatch):
    """Read-only contract: a GET MUST NOT mutate the archive's
    snapshot. We record a baseline, hit each route, then verify
    the archive shape is unchanged."""
    _enable(monkeypatch)
    maybe_record_decision(
        op_id="op-X", tool_name="read_file",
        decision=_make_decision(),
    )
    from backend.core.ouroboros.governance.permission_decision_archive import (  # noqa: E501
        get_default_archive,
    )
    pre = get_default_archive().snapshot().to_dict()
    router = IDEObservabilityRouter()
    for path, mi, handler in (
        (
            "/observability/tool-permissions",
            None,
            router._handle_tool_permissions_recent,
        ),
        (
            "/observability/tool-permissions/by-tool/read_file",
            {"tool_name": "read_file"},
            router._handle_tool_permissions_by_tool,
        ),
        (
            "/observability/tool-permissions/op-X",
            {"op_id": "op-X"},
            router._handle_tool_permissions_by_op,
        ),
    ):
        req = _make_request(path, match_info=mi)
        _run_async(handler(req))
    post = get_default_archive().snapshot().to_dict()
    assert pre == post, (
        "GET handlers MUST NOT mutate the archive — "
        f"pre={pre} post={post}"
    )


def test_ide_observability_does_not_import_archive_policy():
    """The IDE GET surface MUST NOT IMPORT the archive's policy
    symbols (BoundedDecisionArchive constructor, DecisionRecord
    dataclass) — it composes only the canonical projection via
    get_default_archive() + to_dict(). Note: docstring mentions
    of the type names are fine; only ``import`` statements are
    forbidden."""
    import ast
    src = Path(
        "backend/core/ouroboros/governance/ide_observability.py",
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden_archive_symbols = {
        "BoundedDecisionArchive",
        "DecisionRecord",
        "ArchiveSnapshot",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (
                node.module
                and "permission_decision_archive" in node.module
            ):
                for alias in node.names:
                    assert (
                        alias.name not in forbidden_archive_symbols
                    ), (
                        f"ide_observability MUST NOT import "
                        f"{alias.name!r} directly from "
                        f"permission_decision_archive — compose "
                        f"via get_default_archive() + .to_dict() "
                        f"projection only"
                    )

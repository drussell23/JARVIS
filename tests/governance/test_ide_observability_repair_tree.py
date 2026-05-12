"""Regression spine for Treefinement Phase 4 Slice 4f — IDE GET
endpoints exposing the ``repair_tree_archive`` ring.

Pins the load-bearing invariants for the three new routes:

* ``GET /observability/repair-tree[?limit=N]`` — recent ring
* ``GET /observability/repair-tree/op/{op_id}`` — op_id filter
* ``GET /observability/repair-tree/branch/{ref}`` — single-branch
  lookup by b-N ref

Plus the route-order invariant (specific before parameterized),
dual master-flag gating, payload schema, and authority asymmetry.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    LayerVerdict,
    RepairBranch,
    RepairTreeLayer,
    RepairTreeResult,
)
from backend.core.ouroboros.governance.repair_tree_archive import (
    ARCHIVE_MASTER_FLAG_ENV_VAR as ARCHIVE_FLAG,
    ARCHIVE_SIZE_ENV_VAR,
    get_default_archive,
    reset_default_archive_for_tests,
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
    if query:
        path = path + "?" + "&".join(f"{k}={v}" for k, v in query.items())
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


def _make_branch(*, bid: str, score: float = 0.5,
                 outcome: BranchOutcome = BranchOutcome.PROMOTED,
                 layer_index: int = 0) -> RepairBranch:
    return RepairBranch(
        branch_id=bid, parent_branch_id=None,
        layer_index=layer_index, failure_class="test",
        fix_hypothesis="strategy", diff="--- a\n+++ b\n",
        validator_score=score, outcome=outcome,
        prune_reason=None, worktree_id="unit-x",
        cost_usd=0.001, validation_runs_consumed=1,
    )


def _make_result(*, op_id: str = "op-A",
                 branches_per_layer: int = 2) -> RepairTreeResult:
    branches = tuple(
        _make_branch(bid=f"{op_id}-{i}")
        for i in range(branches_per_layer)
    )
    layer = RepairTreeLayer(
        layer_index=0, branches=branches,
        verdict=LayerVerdict.EXPANDED, wall_ms=10.0,
        parallel_units_actual=branches_per_layer,
    )
    return RepairTreeResult(
        root_op_id=op_id, layers=(layer,),
        winning_branch_path=(), final_status=None,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(_IDE_FLAG, raising=False)
    monkeypatch.delenv(ARCHIVE_FLAG, raising=False)
    monkeypatch.delenv(ARCHIVE_SIZE_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ===========================================================================
# Route registration + ordering
# ===========================================================================


def test_routes_registered_on_aiohttp_app():
    app = web.Application()
    IDEObservabilityRouter().register_routes(app)
    paths = {str(r.resource) for r in app.router.routes()}
    assert any(
        "repair-tree>" in p
        and "{" not in p
        for p in paths
    ), "recent route missing"
    assert any(
        "repair-tree/op/{op_id}" in p for p in paths
    ), "by-op route missing"
    assert any(
        "repair-tree/branch/{ref}" in p for p in paths
    ), "by-ref route missing"


def test_route_order_specific_before_parameterized():
    """The literal-prefix routes (/op/{op_id}, /branch/{ref}) MUST
    register together — there's no shared parameterized catch-all
    that could shadow them, but the source-order pin guards against
    future drift."""
    src = Path(
        "backend/core/ouroboros/governance/ide_observability.py",
    ).read_text(encoding="utf-8")
    recent_idx = src.index('"/observability/repair-tree"')
    op_idx = src.index('"/observability/repair-tree/op/{op_id}"')
    branch_idx = src.index('"/observability/repair-tree/branch/{ref}"')
    assert recent_idx < op_idx
    assert op_idx < branch_idx


# ===========================================================================
# Master-flag gate enforcement
# ===========================================================================


def test_recent_returns_403_when_ide_disabled(monkeypatch):
    monkeypatch.setenv(_IDE_FLAG, "false")
    monkeypatch.setenv(ARCHIVE_FLAG, "true")
    router = IDEObservabilityRouter()
    req = _make_request("/observability/repair-tree")
    resp = _run_async(router._handle_repair_tree_recent(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.disabled"


def test_recent_returns_403_when_archive_disabled(monkeypatch):
    monkeypatch.setenv(_IDE_FLAG, "true")
    monkeypatch.delenv(ARCHIVE_FLAG, raising=False)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/repair-tree")
    resp = _run_async(router._handle_repair_tree_recent(req))
    assert resp.status == 403
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == "ide_observability.repair_tree_disabled"


def test_by_op_returns_400_on_missing_op_id(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/op/", match_info={"op_id": ""},
    )
    resp = _run_async(router._handle_repair_tree_by_op(req))
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.repair_tree_missing_op_id"
    )


def test_by_ref_returns_400_on_missing_ref(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/branch/", match_info={"ref": ""},
    )
    resp = _run_async(router._handle_repair_tree_by_ref(req))
    assert resp.status == 400
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.repair_tree_missing_ref"
    )


# ===========================================================================
# Payload shapes — happy paths
# ===========================================================================


def test_recent_returns_archived_branches(monkeypatch):
    _enable(monkeypatch)
    archive = get_default_archive()
    archive.record_result(_make_result(op_id="op-A", branches_per_layer=3))
    router = IDEObservabilityRouter()
    req = _make_request("/observability/repair-tree")
    resp = _run_async(router._handle_repair_tree_recent(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 3
    assert "snapshot" in body
    assert "branches" in body
    assert len(body["branches"]) == 3
    # First entry has b-3 (newest first via .recent())
    assert body["branches"][0]["ref"] == "b-3"


def test_recent_respects_limit_query(monkeypatch):
    _enable(monkeypatch)
    archive = get_default_archive()
    for i in range(5):
        archive.record_result(_make_result(
            op_id=f"op-{i}", branches_per_layer=1,
        ))
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree", query={"limit": "2"},
    )
    resp = _run_async(router._handle_repair_tree_recent(req))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 2


def test_recent_empty_archive_returns_zero(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request("/observability/repair-tree")
    resp = _run_async(router._handle_repair_tree_recent(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 0
    assert body["branches"] == []


def test_by_op_returns_op_branches(monkeypatch):
    _enable(monkeypatch)
    archive = get_default_archive()
    archive.record_result(_make_result(op_id="op-target", branches_per_layer=2))
    archive.record_result(_make_result(op_id="op-other", branches_per_layer=3))
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/op/op-target",
        match_info={"op_id": "op-target"},
    )
    resp = _run_async(router._handle_repair_tree_by_op(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["op_id"] == "op-target"
    assert body["count"] == 2
    assert body["total_archived"] == 2


def test_by_op_unknown_op_returns_zero(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/op/never-existed",
        match_info={"op_id": "never-existed"},
    )
    resp = _run_async(router._handle_repair_tree_by_op(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["count"] == 0


def test_by_ref_returns_branch(monkeypatch):
    _enable(monkeypatch)
    archive = get_default_archive()
    archive.record_result(_make_result(branches_per_layer=2))
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/branch/b-1",
        match_info={"ref": "b-1"},
    )
    resp = _run_async(router._handle_repair_tree_by_ref(req))
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert body["ref"] == "b-1"
    assert body["branch"]["ref"] == "b-1"
    assert "branch" in body["branch"]  # ArchivedBranch.to_dict has nested "branch"


def test_by_ref_returns_404_for_unknown_ref(monkeypatch):
    _enable(monkeypatch)
    router = IDEObservabilityRouter()
    req = _make_request(
        "/observability/repair-tree/branch/b-9999",
        match_info={"ref": "b-9999"},
    )
    resp = _run_async(router._handle_repair_tree_by_ref(req))
    assert resp.status == 404
    body = json.loads(resp.body.decode("utf-8"))
    assert body["reason_code"] == (
        "ide_observability.repair_tree_ref_not_found"
    )


# ===========================================================================
# Authority asymmetry — read-only consumer
# ===========================================================================


def test_repair_tree_routes_do_not_mutate_archive(monkeypatch):
    """The IDE GET surface MUST be read-only. Verify by checking the
    archive's next_seq doesn't advance after a GET."""
    _enable(monkeypatch)
    archive = get_default_archive()
    archive.record_result(_make_result(branches_per_layer=2))
    pre_snap = archive.snapshot()
    router = IDEObservabilityRouter()
    req = _make_request("/observability/repair-tree")
    _run_async(router._handle_repair_tree_recent(req))
    post_snap = archive.snapshot()
    assert pre_snap.next_seq == post_snap.next_seq
    assert pre_snap.size == post_snap.size

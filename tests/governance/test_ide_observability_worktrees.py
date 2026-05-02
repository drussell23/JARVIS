"""Gap #3 Slice 2 — IDE observability /observability/worktrees regression suite.

Covers:

  §1   constructor accepts optional scheduler + worktree_manager
  §2   503 graceful degradation when scheduler ref not wired
  §3   403 when ide_observability master is off
  §4   429 on rate limit
  §5   200 with topology projection (scheduler wired, no wm)
  §6   200 with topology + worktree paths (both refs wired)
  §7   GET /observability/worktrees/{graph_id} happy path
  §8   GET /observability/worktrees/{graph_id} 404 on unknown
  §9   GET /observability/worktrees/{graph_id} 400 on malformed id
  §10  WorktreeManager.list_worktree_paths returns flat path list
  §11  schema_version stamped + cache-control no-store
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.ide_observability import (
    IDEObservabilityRouter,
)


def _spec(uid: str, *, deps: tuple = ()) -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=uid, repo="primary",
        goal=f"goal-{uid}",
        target_files=("file.py",),
        dependency_ids=deps,
    )


def _make_state(graph_id: str = "g1", op_id: str = "op-1") -> GraphExecutionState:
    units = (_spec("a"), _spec("b", deps=("a",)))
    graph = ExecutionGraph(
        graph_id=graph_id, op_id=op_id,
        planner_id="test", schema_version="1.0",
        units=units, concurrency_limit=4,
    )
    return GraphExecutionState(
        graph=graph, phase=GraphExecutionPhase.RUNNING,
        running_units=("b",),
        completed_units=("a",),
    )


class _StubScheduler:
    def __init__(self, graphs: Dict[str, GraphExecutionState]):
        self._graphs = graphs


class _StubWorktreeManager:
    def __init__(self, paths: Optional[List[str]] = None,
                 raises: bool = False):
        self._paths = paths or []
        self._raises = raises

    async def list_worktree_paths(self) -> List[str]:
        if self._raises:
            raise RuntimeError("git unavailable")
        return list(self._paths)


def _make_request(
    path: str, *, match_info: Optional[Dict[str, str]] = None,
    remote: str = "127.0.0.1",
):
    req = make_mocked_request("GET", path)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]
    return req


def _enable_observability(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WORKTREE_TOPOLOGY_ENABLED", "true")


# ============================================================================
# §1 — Constructor accepts optional scheduler + worktree_manager
# ============================================================================


class TestConstructor:
    def test_default_no_refs(self):
        r = IDEObservabilityRouter()
        assert r._scheduler is None
        assert r._worktree_manager is None

    def test_only_scheduler(self):
        r = IDEObservabilityRouter(scheduler=_StubScheduler({}))
        assert r._scheduler is not None
        assert r._worktree_manager is None

    def test_both_refs(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({}),
            worktree_manager=_StubWorktreeManager(),
        )
        assert r._scheduler is not None
        assert r._worktree_manager is not None

    def test_session_dir_kwarg_still_works(self, tmp_path):
        # Backward-compat with the existing constructor
        r = IDEObservabilityRouter(session_dir=tmp_path)
        assert r._session_dir == tmp_path


# ============================================================================
# §2 — 503 graceful degradation when scheduler ref not wired
# ============================================================================


class TestNoSchedulerWired:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_list_returns_503(self):
        r = IDEObservabilityRouter()  # no scheduler wired
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        assert resp.status == 503
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_observability.worktrees_scheduler_not_wired"
        )

    def test_detail_returns_503(self):
        r = IDEObservabilityRouter()
        req = _make_request(
            "/observability/worktrees/g1",
            match_info={"graph_id": "g1"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        assert resp.status == 503


# ============================================================================
# §3 — 403 when ide_observability master is off
# ============================================================================


class TestMasterDisabled:
    def test_list_returns_403_when_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        assert resp.status == 403

    def test_detail_returns_403_when_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "false",
        )
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request(
            "/observability/worktrees/g1",
            match_info={"graph_id": "g1"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        assert resp.status == 403


# ============================================================================
# §4 — 429 on rate limit
# ============================================================================


class TestRateLimit:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "2",
        )

    def test_third_request_429(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        for _ in range(2):
            req = _make_request("/observability/worktrees")
            asyncio.run(r._handle_worktrees_list(req))
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        assert resp.status == 429


# ============================================================================
# §5 — 200 with topology projection (scheduler wired, no wm)
# ============================================================================


class TestListHappyPathSchedulerOnly:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_returns_200_with_topology(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "topology" in body
        topo = body["topology"]
        assert topo["outcome"] == "ok"
        assert topo["summary"]["total_graphs"] == 1
        assert topo["summary"]["total_units"] == 2
        # No worktree manager → all units have_worktree=false
        assert topo["summary"]["units_with_worktree"] == 0

    def test_empty_scheduler_returns_empty_outcome(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({}),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["topology"]["outcome"] == "empty"


# ============================================================================
# §6 — 200 with topology + worktree paths (both refs wired)
# ============================================================================


class TestListHappyPathBothRefs:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_worktree_correspondence(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
            worktree_manager=_StubWorktreeManager(paths=[
                "/x/.worktrees/unit-a",
                "/x/.worktrees/unit-b",
            ]),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["topology"]["summary"]["units_with_worktree"] == 2

    def test_orphan_worktree_detection(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
            worktree_manager=_StubWorktreeManager(paths=[
                "/x/.worktrees/unit-a",
                "/x/.worktrees/unit-b",
                "/x/.worktrees/unit-orphan",
            ]),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        body = json.loads(resp.body)
        assert body["topology"]["summary"]["orphan_worktree_count"] == 1
        orphans = body["topology"]["summary"]["orphan_worktree_paths"]
        assert any("unit-orphan" in p for p in orphans)

    def test_worktree_manager_failure_degrades_to_empty_paths(self):
        # WM raises → paths fall through to empty list, projection
        # still succeeds (no units have_worktree, no orphans).
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
            worktree_manager=_StubWorktreeManager(raises=True),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["topology"]["outcome"] == "ok"
        assert body["topology"]["summary"]["units_with_worktree"] == 0


# ============================================================================
# §7 — GET /observability/worktrees/{graph_id} happy path
# ============================================================================


class TestDetailHappyPath:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_returns_specific_graph(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({
                "g1": _make_state("g1", "op-1"),
                "g2": _make_state("g2", "op-2"),
            }),
        )
        req = _make_request(
            "/observability/worktrees/g1",
            match_info={"graph_id": "g1"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        body = json.loads(resp.body)
        assert resp.status == 200
        assert body["graph"]["graph_id"] == "g1"
        assert body["graph"]["op_id"] == "op-1"
        assert len(body["graph"]["nodes"]) == 2

    def test_graph_carries_dependency_edges(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request(
            "/observability/worktrees/g1",
            match_info={"graph_id": "g1"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        body = json.loads(resp.body)
        edges = body["graph"]["edges"]
        # _make_state defines a→b dependency
        dep_edges = [e for e in edges if e["edge_kind"] == "dependency"]
        assert len(dep_edges) == 1
        assert dep_edges[0]["from_unit_id"] == "a"
        assert dep_edges[0]["to_unit_id"] == "b"


# ============================================================================
# §8 — 404 on unknown graph_id
# ============================================================================


class TestDetail404:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_unknown_graph_id_returns_404(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request(
            "/observability/worktrees/g-does-not-exist",
            match_info={"graph_id": "g-does-not-exist"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        body = json.loads(resp.body)
        assert resp.status == 404
        assert body["reason_code"] == (
            "ide_observability.worktree_graph_not_found"
        )


# ============================================================================
# §9 — 400 on malformed graph_id
# ============================================================================


class TestDetail400:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_malformed_id_with_spaces_400(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({}),
        )
        req = _make_request(
            "/observability/worktrees/has spaces",
            match_info={"graph_id": "has spaces"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["reason_code"] == (
            "ide_observability.malformed_graph_id"
        )

    def test_malformed_id_with_special_chars_400(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({}),
        )
        req = _make_request(
            "/observability/worktrees/!@#$%",
            match_info={"graph_id": "!@#$%"},
        )
        resp = asyncio.run(r._handle_worktree_detail(req))
        assert resp.status == 400


# ============================================================================
# §10 — WorktreeManager.list_worktree_paths
# ============================================================================


class TestWorktreeManagerListMethod:
    def test_list_returns_at_least_main_worktree(self, tmp_path):
        """The repo we're running in always has at least its
        own worktree — no real assertion on count, just that the
        method returns a list of strings without crashing."""
        from backend.core.ouroboros.governance.worktree_manager import (
            WorktreeManager,
        )
        wm = WorktreeManager(repo_root=Path("."))
        paths = asyncio.run(wm.list_worktree_paths())
        assert isinstance(paths, list)
        assert all(isinstance(p, str) for p in paths)

    def test_list_returns_empty_when_no_repo(self, tmp_path):
        from backend.core.ouroboros.governance.worktree_manager import (
            WorktreeManager,
        )
        wm = WorktreeManager(repo_root=tmp_path)  # tmp dir, not a git repo
        paths = asyncio.run(wm.list_worktree_paths())
        assert paths == []


# ============================================================================
# §11 — Schema version + cache control
# ============================================================================


class TestEnvelopeShape:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        _enable_observability(monkeypatch)

    def test_response_carries_schema_version(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        body = json.loads(resp.body)
        assert "schema_version" in body
        # ide_observability uses "1.0" — distinct from substrate's
        # "worktree_topology.1" which is nested under topology
        assert body["schema_version"] == "1.0"
        assert body["topology"]["schema_version"] == "worktree_topology.1"

    def test_response_has_no_store_cache_control(self):
        r = IDEObservabilityRouter(
            scheduler=_StubScheduler({"g1": _make_state()}),
        )
        req = _make_request("/observability/worktrees")
        resp = asyncio.run(r._handle_worktrees_list(req))
        assert resp.headers.get("Cache-Control") == "no-store"

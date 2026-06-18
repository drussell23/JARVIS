"""Tests for the Cross-Repo Scope Promoter — ignition wire for the dormant multi-repo Saga mesh.

Covers `cross_repo_scope_promoter.py` + the `OperationContext.with_cross_repo_promotion` elevation:
1. Cross-boundary lineage detection (jarvis fault cone reaching a reactor node → promote).
2. No promotion when the cone stays within the primary repo.
3. Topological cascade shield: deep blast into the sibling → sharded to boundary-interface files.
4. Elevation re-derives cross_repo=True + forces Orange-tier (APPROVAL_REQUIRED).
5. Gating (default OFF) + fail-soft (no graph / errors → no promotion).
6. Structural-delta report rendering.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.cross_repo_scope_promoter import (
    CrossRepoScopePromoter,
    PromotionReport,
    promoter_enabled,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.risk_engine import RiskTier


# --------------------------------------------------------------------------- fakes
class _Node:
    def __init__(self, key: str) -> None:
        self._k = key

    def __str__(self) -> str:
        return self._k


class _Blast:
    def __init__(self, trans: List[str]) -> None:
        self.directly_affected: set = set()
        self.transitively_affected = {_Node(k) for k in trans}
        self.risk_level = "medium"


class _Graph:
    """Unified-graph fake keyed by NodeID str 'repo:file:name'."""

    def __init__(self, file_nodes: Dict[str, List[str]], deps: Dict[str, List[str]],
                 dependents: Optional[Dict[str, List[str]]] = None,
                 blast: Optional[Dict[str, List[str]]] = None) -> None:
        self._file_nodes = file_nodes
        self._deps = deps
        self._dependents = dependents or {}
        self._blast = blast or {}

    def find_nodes_in_file(self, f: str) -> List[_Node]:
        return [_Node(k) for k in self._file_nodes.get(f, [])]

    def get_dependencies(self, k: str) -> List[_Node]:
        return [_Node(x) for x in self._deps.get(str(k), [])]

    def get_dependents(self, k: str) -> List[_Node]:
        return [_Node(x) for x in self._dependents.get(str(k), [])]

    def compute_blast_radius(self, k: str, max_depth: int = 2) -> _Blast:
        return _Blast(self._blast.get(str(k), []))


def _ctx(target=("backend/x.py",)):
    return OperationContext.create(op_id="op1", target_files=target, description="fix x")


# --------------------------------------------------------------------------- detection
class TestDetection:
    def test_cross_boundary_promotes(self) -> None:
        # jarvis:backend/x.py:foo depends on reactor:core/api.py:bar → cross boundary
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["reactor:core/api.py:bar"]},
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis")
        r = p.analyze(("backend/x.py",), "jarvis")
        assert r.promoted is True
        assert r.cross_repos == ["reactor"]
        assert ("jarvis:backend/x.py:foo", "reactor:core/api.py:bar") in r.boundary_edges
        assert r.elevated_scope == ("jarvis", "reactor")

    def test_intra_repo_no_promotion(self) -> None:
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["jarvis:backend/y.py:baz"]},  # same repo
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis")
        r = p.analyze(("backend/x.py",), "jarvis")
        assert r.promoted is False
        assert r.cross_repos == []

    def test_no_graph_no_promotion(self) -> None:
        p = CrossRepoScopePromoter(graph=None, primary_repo="jarvis")
        # _get_graph will try the real oracle; if unavailable → empty report (no raise)
        r = p.analyze(("does/not/exist.py",), "jarvis")
        assert r.promoted is False


# --------------------------------------------------------------------------- cascade shield
class TestCascadeShield:
    def test_deep_blast_shards_to_boundary(self) -> None:
        # direct boundary edge + DEEP transitive reactor nodes → shield engages
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["reactor:core/api.py:bar"]},
            blast={"jarvis:backend/x.py:foo": [
                "reactor:core/deep1.py:d1", "reactor:core/deep2.py:d2",
            ]},
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis", max_cascade_depth=1)
        r = p.analyze(("backend/x.py",), "jarvis")
        assert r.promoted is True
        assert r.sharded is True
        assert r.cascade_depth > 1
        assert r.shielded_internal  # deep internal nodes recorded as shielded
        assert "reactor:core/api.py:bar" not in r.shielded_internal  # boundary kept, not shielded

    def test_shallow_blast_no_shard(self) -> None:
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["reactor:core/api.py:bar"]},
            blast={"jarvis:backend/x.py:foo": []},
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis", max_cascade_depth=2)
        r = p.analyze(("backend/x.py",), "jarvis")
        assert r.promoted is True
        assert r.sharded is False


# --------------------------------------------------------------------------- elevation + gating
class TestElevationAndGating:
    def test_elevation_derives_cross_repo_and_forces_orange(self) -> None:
        c = _ctx()
        assert c.cross_repo is False
        e = c.with_cross_repo_promotion(
            repo_scope=("jarvis", "reactor"),
            dependency_edges=(("jarvis", "reactor"),),
            apply_plan=("jarvis", "reactor"),
            risk_tier=RiskTier.APPROVAL_REQUIRED,
        )
        assert e.cross_repo is True
        assert e.repo_scope == ("jarvis", "reactor")
        assert e.risk_tier == RiskTier.APPROVAL_REQUIRED
        assert e.context_hash != c.context_hash  # hash chain advanced

    @pytest.mark.asyncio
    async def test_disabled_no_promotion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_CROSS_REPO_PROMOTER_ENABLED", raising=False)
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["reactor:core/api.py:bar"]},
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis")
        ctx, report = await p.maybe_promote(_ctx())
        assert report is None
        assert ctx.cross_repo is False

    @pytest.mark.asyncio
    async def test_enabled_promotes_and_elevates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_CROSS_REPO_PROMOTER_ENABLED", "true")
        g = _Graph(
            file_nodes={"backend/x.py": ["jarvis:backend/x.py:foo"]},
            deps={"jarvis:backend/x.py:foo": ["reactor:core/api.py:bar"]},
        )
        p = CrossRepoScopePromoter(graph=g, primary_repo="jarvis")
        ctx, report = await p.maybe_promote(_ctx())
        assert report is not None and report.promoted is True
        assert ctx.cross_repo is True
        assert ctx.repo_scope == ("jarvis", "reactor")
        assert ctx.risk_tier == RiskTier.APPROVAL_REQUIRED

    def test_promoter_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_CROSS_REPO_PROMOTER_ENABLED", raising=False)
        assert promoter_enabled() is False


# --------------------------------------------------------------------------- report render
class TestReport:
    def test_render_contains_delta(self) -> None:
        r = PromotionReport(
            promoted=True, primary_repo="jarvis", cross_repos=["reactor"],
            boundary_edges=[("jarvis:x.py:foo", "reactor:api.py:bar")],
            boundary_files=["reactor:api.py"], elevated_scope=("jarvis", "reactor"),
            cascade_depth=2, sharded=True, shielded_internal=["reactor:deep.py:d"],
            reason="boundary crossed",
        )
        out = r.render()
        assert "CROSS-REPO SCOPE ELEVATION" in out
        assert "CASCADE SHIELD ENGAGED" in out
        assert "APPROVAL_REQUIRED" in out

    def test_empty_report_renders_empty(self) -> None:
        assert PromotionReport().render() == ""

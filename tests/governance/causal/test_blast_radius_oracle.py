"""Tests for BlastRadiusOracle intra-repo path (Domain-1 Staging-2 Task 2).

Strict TheOracle delegation: the BlastRadiusOracle re-derives NOTHING about
import resolution -- it forwards to ``get_oracle().get_blast_radius(symbol)``
and maps the resulting ``BlastRadius`` into an ``IntraRepoImpact``. These
tests pin: exact mapping, delegation (recording fake), fail-soft on
raise/None, no re-derived import walking (Mandate 3), and determinism.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.causal.blast_radius_oracle import (
    BlastRadiusOracle,
    IntraRepoImpact,
)
from backend.core.ouroboros.governance.causal.causal_graph import CausalGraph


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------
class _FakeNodeID:
    """Stands in for oracle.NodeID -- str() -> "repo:file:name"."""

    def __init__(self, s: str) -> None:
        self._s = s

    def __str__(self) -> str:  # noqa: D401 - trivial
        return self._s


class _FakeBlastRadius:
    """BlastRadius-shaped: the only fields the mapper reads."""

    def __init__(self, directly, transitively, risk_level: str) -> None:
        self.source_node = _FakeNodeID("jarvis:x.py:src")
        self.directly_affected = set(directly)
        self.transitively_affected = set(transitively)
        self.broken_imports: List[Any] = []
        self.broken_calls: List[Any] = []
        self.risk_level = risk_level


class _RecordingOracle:
    """Records the exact target passed to get_blast_radius."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: List[str] = []

    def get_blast_radius(self, target: str) -> Any:
        self.calls.append(target)
        return self._result


class _RaisingOracle:
    def get_blast_radius(self, target: str) -> Any:
        raise RuntimeError("oracle exploded during graph walk")


def _make_oracle(graph: CausalGraph, oracle_obj: Any) -> BlastRadiusOracle:
    return BlastRadiusOracle(graph, oracle_fn=lambda: oracle_obj)


# --------------------------------------------------------------------------
# Mapping + delegation
# --------------------------------------------------------------------------
def test_maps_blast_radius_fields_exactly_sorted() -> None:
    graph = CausalGraph()
    directly = [_FakeNodeID("jarvis:b.py:b"), _FakeNodeID("jarvis:a.py:a")]
    transitively = [_FakeNodeID("jarvis:z.py:z"), _FakeNodeID("jarvis:m.py:m")]
    br = _FakeBlastRadius(directly, transitively, "high")
    oracle = _RecordingOracle(br)
    bro = _make_oracle(graph, oracle)

    impact = asyncio.run(bro.intra_repo("jarvis:x.py:src"))

    assert isinstance(impact, IntraRepoImpact)
    assert impact.source_symbol == "jarvis:x.py:src"
    assert impact.directly_affected == ("jarvis:a.py:a", "jarvis:b.py:b")
    assert impact.transitively_affected == ("jarvis:m.py:m", "jarvis:z.py:z")
    assert impact.risk_level == "high"


def test_delegation_pin_calls_get_blast_radius_with_exact_symbol() -> None:
    graph = CausalGraph()
    oracle = _RecordingOracle(_FakeBlastRadius([], [], "low"))
    bro = _make_oracle(graph, oracle)

    asyncio.run(bro.intra_repo("jarvis:mod.py:target_symbol"))

    assert oracle.calls == ["jarvis:mod.py:target_symbol"]


def test_default_resolver_calls_get_oracle() -> None:
    # When no oracle_fn is injected, the intra_repo path must resolve the
    # per-repo Oracle via ``blast_radius_oracle.get_oracle`` (the module-level
    # import the code actually calls). Patch that symbol with a recording fake
    # and assert the default path invoked it -- a real behavioral pin, not the
    # tautological ``_oracle_fn is None`` identity check it replaces.
    import backend.core.ouroboros.governance.causal.blast_radius_oracle as mod

    called = {"n": 0}
    recording = _RecordingOracle(_FakeBlastRadius([], [], "low"))

    def _fake_get_oracle():
        called["n"] += 1
        return recording

    orig = mod.get_oracle
    mod.get_oracle = _fake_get_oracle
    try:
        graph = CausalGraph()
        bro = BlastRadiusOracle(graph)  # no oracle_fn -> default resolver
        asyncio.run(bro.intra_repo("jarvis:mod.py:sym"))
    finally:
        mod.get_oracle = orig

    assert called["n"] == 1, "default path must call blast_radius_oracle.get_oracle"
    assert recording.calls == ["jarvis:mod.py:sym"]


# --------------------------------------------------------------------------
# Fail-soft
# --------------------------------------------------------------------------
def test_oracle_raising_yields_empty_impact_no_raise() -> None:
    graph = CausalGraph()
    bro = _make_oracle(graph, _RaisingOracle())

    impact = asyncio.run(bro.intra_repo("jarvis:x.py:src"))

    assert impact == IntraRepoImpact("jarvis:x.py:src", (), (), "low")


def test_oracle_returning_none_yields_empty_impact() -> None:
    graph = CausalGraph()
    bro = _make_oracle(graph, _RecordingOracle(None))

    impact = asyncio.run(bro.intra_repo("jarvis:x.py:src"))

    assert impact == IntraRepoImpact("jarvis:x.py:src", (), (), "low")


def test_oracle_fn_itself_raising_yields_empty_impact() -> None:
    def _boom():
        raise RuntimeError("get_oracle failed to build")

    graph = CausalGraph()
    bro = BlastRadiusOracle(graph, oracle_fn=_boom)

    impact = asyncio.run(bro.intra_repo("jarvis:x.py:src"))

    assert impact == IntraRepoImpact("jarvis:x.py:src", (), (), "low")


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------
def test_determinism_identical_impact_for_same_input() -> None:
    graph = CausalGraph()
    directly = [_FakeNodeID("jarvis:b.py:b"), _FakeNodeID("jarvis:a.py:a")]
    br = _FakeBlastRadius(directly, [], "medium")
    bro = _make_oracle(graph, _RecordingOracle(br))

    first = asyncio.run(bro.intra_repo("jarvis:x.py:src"))
    second = asyncio.run(bro.intra_repo("jarvis:x.py:src"))

    assert first == second


# --------------------------------------------------------------------------
# Mandate 3: NO re-derived import resolution in the module
# --------------------------------------------------------------------------
def test_module_contains_no_import_resolution() -> None:
    import backend.core.ouroboros.governance.causal.blast_radius_oracle as mod

    src = inspect.getsource(mod)
    assert "ast.parse" not in src
    assert "import ast" not in src
    assert "_build_forward_import_graph" not in src
    # The ONLY dependency-source is the Oracle's get_blast_radius / get_dependents.
    assert "get_blast_radius" in src


def test_result_type_is_frozen_dataclass() -> None:
    import dataclasses

    assert dataclasses.is_dataclass(IntraRepoImpact)
    with pytest.raises(dataclasses.FrozenInstanceError):
        impact = IntraRepoImpact("s", (), (), "low")
        impact.risk_level = "high"  # type: ignore[misc]

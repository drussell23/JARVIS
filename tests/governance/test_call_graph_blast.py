"""Tests for call_graph_blast — symbol-scoped blast radius over the oracle
call graph (Sovereign Call-Graph Risk Matrix, C1 root-cause).

The unit under test, ``symbol_blast_radius``, resolves ``file::Symbol``
strings to oracle ``NodeID``s and BFS-counts the transitive caller closure
(who CALLS the symbols), bounded + dedup'd, capped at 50. It is pure,
fail-soft, and NEVER raises.

These tests drive a FAKE oracle that exposes ``find_nodes_by_name`` +
``get_callers`` with controllable caller sets, so we can prove:
  * a symbol with few callers → low blast
  * a hub symbol → high/capped blast
  * an unresolved symbol → None (caller falls back to file-level)
  * a transitively-deep chain is counted via BFS
  * bound (depth/fan) is respected
  * any oracle error → None (fail-soft, never raises)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.call_graph_blast import (
    symbol_blast_radius,
)


# ---------------------------------------------------------------------------
# Fake oracle scaffolding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeNode:
    """Minimal NodeID stand-in — only the attributes the resolver reads."""

    name: str
    file_path: str

    def __str__(self) -> str:  # the BFS dedups by str(node)
        return f"jarvis:{self.file_path}:{self.name}"


class FakeOracle:
    """Controllable call graph.

    ``name_index`` maps a symbol leaf name → list of FakeNode.
    ``callers_map`` maps str(node) → list of caller FakeNode.
    """

    def __init__(
        self,
        name_index: Dict[str, List[FakeNode]],
        callers_map: Dict[str, List[FakeNode]],
        *,
        raise_on_callers: bool = False,
        raise_on_find: bool = False,
    ) -> None:
        self._name_index = name_index
        self._callers_map = callers_map
        self._raise_on_callers = raise_on_callers
        self._raise_on_find = raise_on_find

    def find_nodes_by_name(self, name: str, fuzzy: bool = False) -> List[FakeNode]:
        if self._raise_on_find:
            raise RuntimeError("boom-find")
        return list(self._name_index.get(name, []))

    def get_callers(self, node) -> List[FakeNode]:  # noqa: ANN001
        if self._raise_on_callers:
            raise RuntimeError("boom-callers")
        return list(self._callers_map.get(str(node), []))


def _node(name: str, file_path: str = "mod.py") -> FakeNode:
    return FakeNode(name=name, file_path=file_path)


# ---------------------------------------------------------------------------
# Resolution + low/high blast
# ---------------------------------------------------------------------------


def test_few_callers_yields_low_blast():
    target = _node("helper", "util.py")
    c1 = _node("caller_a", "a.py")
    c2 = _node("caller_b", "b.py")
    oracle = FakeOracle(
        name_index={"helper": [target]},
        callers_map={str(target): [c1, c2]},
    )
    radius = symbol_blast_radius(("util.py::helper",), oracle=oracle)
    assert radius == 2


def test_hub_symbol_caps_at_50():
    hub = _node("hub", "core.py")
    callers = [_node(f"c{i}", f"f{i}.py") for i in range(120)]
    oracle = FakeOracle(
        name_index={"hub": [hub]},
        callers_map={str(hub): callers},
    )
    radius = symbol_blast_radius(("core.py::hub",), oracle=oracle)
    assert radius == 50  # capped for comparability with legacy ceiling


def test_unresolved_symbol_returns_none():
    oracle = FakeOracle(name_index={}, callers_map={})
    radius = symbol_blast_radius(("core.py::nope",), oracle=oracle)
    assert radius is None


def test_mixed_resolution_one_unresolved_returns_none():
    resolved = _node("known", "k.py")
    oracle = FakeOracle(
        name_index={"known": [resolved]},
        callers_map={str(resolved): [_node("c", "c.py")]},
    )
    # Second symbol can't resolve → whole result is None (conservative).
    radius = symbol_blast_radius(
        ("k.py::known", "x.py::unknown"), oracle=oracle
    )
    assert radius is None


# ---------------------------------------------------------------------------
# Transitive BFS
# ---------------------------------------------------------------------------


def test_transitive_callers_counted_via_bfs():
    leaf = _node("leaf", "leaf.py")
    mid = _node("mid", "mid.py")
    top = _node("top", "top.py")
    # leaf <- mid <- top  (top calls mid calls leaf)
    oracle = FakeOracle(
        name_index={"leaf": [leaf]},
        callers_map={
            str(leaf): [mid],
            str(mid): [top],
            str(top): [],
        },
    )
    radius = symbol_blast_radius(("leaf.py::leaf",), oracle=oracle)
    assert radius == 2  # mid + top, deduped, transitive


def test_bfs_dedups_diamond():
    leaf = _node("leaf", "leaf.py")
    a = _node("a", "a.py")
    b = _node("b", "b.py")
    apex = _node("apex", "apex.py")
    # diamond: a<-apex, b<-apex, leaf<-a, leaf<-b → distinct callers {a,b,apex}
    oracle = FakeOracle(
        name_index={"leaf": [leaf]},
        callers_map={
            str(leaf): [a, b],
            str(a): [apex],
            str(b): [apex],
            str(apex): [],
        },
    )
    radius = symbol_blast_radius(("leaf.py::leaf",), oracle=oracle)
    assert radius == 3  # a, b, apex — apex counted once


# ---------------------------------------------------------------------------
# Fail-soft
# ---------------------------------------------------------------------------


def test_oracle_get_callers_raises_returns_none():
    target = _node("sym", "m.py")
    oracle = FakeOracle(
        name_index={"sym": [target]},
        callers_map={},
        raise_on_callers=True,
    )
    radius = symbol_blast_radius(("m.py::sym",), oracle=oracle)
    assert radius is None  # never raises


def test_oracle_find_raises_returns_none():
    oracle = FakeOracle(
        name_index={}, callers_map={}, raise_on_find=True
    )
    radius = symbol_blast_radius(("m.py::sym",), oracle=oracle)
    assert radius is None


def test_none_oracle_returns_none():
    assert symbol_blast_radius(("m.py::sym",), oracle=None) is None


def test_empty_symbols_returns_none():
    oracle = FakeOracle(name_index={}, callers_map={})
    assert symbol_blast_radius((), oracle=oracle) is None


def test_whole_file_marker_symbol_skipped_resolves_none():
    # "file::" with empty symbol is the scoper's whole-file fallback marker;
    # it carries no symbol → cannot resolve on the call graph → None.
    oracle = FakeOracle(name_index={}, callers_map={})
    assert symbol_blast_radius(("m.py::",), oracle=oracle) is None


# ---------------------------------------------------------------------------
# Bounding — a pathological deep chain must not explode the Advisor budget
# ---------------------------------------------------------------------------


def test_depth_bound_respected(monkeypatch):
    # Build a long linear caller chain leaf<-n1<-n2<-...<-n10.
    monkeypatch.setenv("JARVIS_CALLGRAPH_BLAST_MAX_DEPTH", "2")
    leaf = _node("leaf", "leaf.py")
    chain = [_node(f"n{i}", f"n{i}.py") for i in range(10)]
    callers_map: Dict[str, List[FakeNode]] = {str(leaf): [chain[0]]}
    for i in range(len(chain) - 1):
        callers_map[str(chain[i])] = [chain[i + 1]]
    callers_map[str(chain[-1])] = []
    oracle = FakeOracle(
        name_index={"leaf": [leaf]}, callers_map=callers_map
    )
    radius = symbol_blast_radius(("leaf.py::leaf",), oracle=oracle)
    # depth 1 → n0, depth 2 → n1; n2.. beyond the depth bound are not visited.
    assert radius == 2


def test_fan_bound_respected(monkeypatch):
    monkeypatch.setenv("JARVIS_CALLGRAPH_BLAST_MAX_FAN", "3")
    hub = _node("hub", "hub.py")
    callers = [_node(f"c{i}", f"c{i}.py") for i in range(20)]
    oracle = FakeOracle(
        name_index={"hub": [hub]},
        callers_map={str(hub): callers},
    )
    radius = symbol_blast_radius(("hub.py::hub",), oracle=oracle)
    # Only the first 3 callers per node are expanded/counted.
    assert radius == 3


# ---------------------------------------------------------------------------
# file_path disambiguation — same leaf name in two files
# ---------------------------------------------------------------------------


def test_file_path_disambiguates_same_name():
    # Two symbols named "run" in different files; we scope to a.py::run.
    run_a = _node("run", "a.py")
    run_b = _node("run", "b.py")
    oracle = FakeOracle(
        name_index={"run": [run_a, run_b]},
        callers_map={
            str(run_a): [_node("ca", "ca.py")],
            str(run_b): [_node("cb1", "x.py"), _node("cb2", "y.py")],
        },
    )
    radius = symbol_blast_radius(("a.py::run",), oracle=oracle)
    # Should resolve ONLY run_a (1 caller), not run_b.
    assert radius == 1


def test_qualified_method_name_resolves_via_leaf():
    # scoper emits "file::Class.method"; oracle indexes by ".method" suffix.
    method = _node("Widget.render", "widget.py")
    oracle = FakeOracle(
        name_index={"Widget.render": [method]},
        callers_map={str(method): [_node("caller", "c.py")]},
    )
    radius = symbol_blast_radius(("widget.py::Widget.render",), oracle=oracle)
    assert radius == 1

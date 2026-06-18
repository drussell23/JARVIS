"""Tests for the Repair Context Bridge — Slice 2 graph-informed cognitive context.

Covers `repair_context_bridge.py`:
1. Adaptive fault-key resolution (Slice-1 evidence > file > failing tests).
2. Cone assembly composing blast-radius + dependencies + call-chain.
3. Top-K-by-proximity truncation honors the env-tunable cap.
4. render_clause emits the boundary clause with the right sections.
5. Async build() self-gates on the master flag and offloads (to_thread).
6. Fail-soft: a raising graph / no graph / no fault keys → None (never raises).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.repair_context_bridge import (
    RepairCone,
    RepairContextBridge,
    _file_of,
    _parse_fault_keys,
    _short_symbol,
)


# --------------------------------------------------------------------------- fakes
class _Node:
    def __init__(self, repo: str, file_path: str, name: str) -> None:
        self.repo, self.file_path, self.name = repo, file_path, name

    def __str__(self) -> str:
        return f"{self.repo}:{self.file_path}:{self.name}"

    def __hash__(self) -> int:
        return hash(str(self))

    def __eq__(self, o: object) -> bool:
        return str(self) == str(o)


class _Blast:
    def __init__(self, direct: List[_Node], trans: List[_Node], risk: str) -> None:
        self.directly_affected = set(direct)
        self.transitively_affected = set(trans)
        self.risk_level = risk
        self.broken_imports: List[Any] = []
        self.broken_calls: List[Any] = []


class _Graph:
    """GraphBackend/CKG-shaped fake with the cone primitives."""

    def __init__(
        self,
        blast: Optional[_Blast] = None,
        deps: Optional[List[_Node]] = None,
        chain: Optional[List[_Node]] = None,
        file_nodes: Optional[Dict[str, List[_Node]]] = None,
        name_nodes: Optional[Dict[str, List[_Node]]] = None,
        raise_blast: bool = False,
    ) -> None:
        self._blast = blast
        self._deps = deps or []
        self._chain = chain
        self._file_nodes = file_nodes or {}
        self._name_nodes = name_nodes or {}
        self._raise_blast = raise_blast

    def compute_blast_radius(self, key: str, max_depth: int = 2) -> _Blast:
        if self._raise_blast:
            raise RuntimeError("graph down")
        return self._blast or _Blast([], [], "low")

    def get_dependencies(self, key: str) -> List[_Node]:
        return list(self._deps)

    def find_call_chain(self, src: str, tgt: str) -> Optional[List[_Node]]:
        return self._chain

    def find_nodes_in_file(self, path: str) -> List[_Node]:
        return list(self._file_nodes.get(path, []))

    def find_nodes_by_name(self, name: str) -> List[_Node]:
        return list(self._name_nodes.get(name, []))


_FAULT = "jarvis:src/calc.py:parse"
_DEP_UP = _Node("jarvis", "src/util.py", "helper")
_DEP_DOWN = _Node("jarvis", "src/app.py", "main")


def _graph_full() -> _Graph:
    return _Graph(
        blast=_Blast(direct=[_DEP_DOWN], trans=[], risk="medium"),
        deps=[_DEP_UP],
        chain=[_Node("jarvis", "tests/test_calc.py", "test_parse"), _Node("jarvis", "src/calc.py", "parse")],
        file_nodes={"src/calc.py": [_Node("jarvis", "src/calc.py", "parse")]},
        name_nodes={"test_parse": [_Node("jarvis", "tests/test_calc.py", "test_parse")]},
    )


# --------------------------------------------------------------------------- helpers
class TestHelpers:
    def test_parse_fault_keys(self) -> None:
        ev = '{"fault_node_keys": ["a:b:c", "d:e:f"], "x": 1}'
        assert _parse_fault_keys(ev) == ["a:b:c", "d:e:f"]

    def test_parse_fault_keys_empty(self) -> None:
        assert _parse_fault_keys("") == []
        assert _parse_fault_keys("not json") == []
        assert _parse_fault_keys('{"other": 1}') == []

    def test_file_and_symbol_extraction(self) -> None:
        assert _file_of("jarvis:src/calc.py:parse") == "src/calc.py"
        assert _short_symbol("jarvis:src/calc.py:parse") == "src/calc.py:parse"


# --------------------------------------------------------------------------- resolution
class TestFaultKeyResolution:
    def test_evidence_wins(self) -> None:
        b = RepairContextBridge(oracle_graph=_graph_full())
        keys = b.resolve_fault_keys('{"fault_node_keys": ["x:y:z"]}', "src/calc.py", ())
        assert keys == ["x:y:z"]

    def test_falls_back_to_file(self) -> None:
        b = RepairContextBridge(oracle_graph=_graph_full())
        keys = b.resolve_fault_keys("", "src/calc.py", ())
        assert keys == [_FAULT]

    def test_falls_back_to_failing_tests(self) -> None:
        b = RepairContextBridge(oracle_graph=_graph_full())
        keys = b.resolve_fault_keys("", "", ("tests/test_calc.py::test_parse",))
        assert keys == ["jarvis:tests/test_calc.py:test_parse"]


# --------------------------------------------------------------------------- cone build
class TestConeBuild:
    def test_full_cone(self) -> None:
        b = RepairContextBridge(oracle_graph=_graph_full())
        cone = b._build_sync('{"fault_node_keys": ["%s"]}' % _FAULT, "src/calc.py",
                             ("tests/test_calc.py::test_parse",))
        assert cone is not None
        assert _FAULT in cone.fault_keys
        assert str(_DEP_DOWN) in cone.dependents
        assert str(_DEP_UP) in cone.dependencies
        assert cone.call_chain and "→" in cone.call_chain[0]
        assert cone.risk_level == "medium"

    def test_no_fault_keys_returns_none(self) -> None:
        b = RepairContextBridge(oracle_graph=_Graph())  # empty graph, no file/name nodes
        assert b._build_sync("", "", ()) is None

    def test_truncation_respects_cap(self) -> None:
        many = [_Node("jarvis", f"src/f{i}.py", f"sym{i}") for i in range(20)]
        g = _Graph(blast=_Blast(direct=many, trans=[], risk="high"),
                   file_nodes={"src/calc.py": [_Node("jarvis", "src/calc.py", "parse")]})
        b = RepairContextBridge(oracle_graph=g, max_symbols=5)
        cone = b._build_sync("", "src/calc.py", ())
        assert cone is not None
        assert len(cone.dependents) == 5
        assert cone.truncated is True


# --------------------------------------------------------------------------- render
class TestRender:
    def test_clause_sections(self) -> None:
        b = RepairContextBridge(oracle_graph=_graph_full())
        cone = b._build_sync('{"fault_node_keys": ["%s"]}' % _FAULT, "src/calc.py",
                             ("tests/test_calc.py::test_parse",))
        clause = RepairContextBridge.render_clause(cone)  # type: ignore[arg-type]
        assert "DEPENDENCY CONE" in clause
        assert "Downstream dependents" in clause
        assert "Upstream dependencies" in clause
        assert "structural gate" in clause  # honest steer-not-enforce note

    def test_empty_cone_renders_empty(self) -> None:
        assert RepairContextBridge.render_clause(RepairCone()) == ""


# --------------------------------------------------------------------------- async gate
class TestAsyncBuild:
    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # graduated default-ON → set the kill-switch explicitly to exercise the OFF path
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "false")
        b = RepairContextBridge(oracle_graph=_graph_full())
        cone = await b.build(evidence_json='{"fault_node_keys": ["%s"]}' % _FAULT,
                             target_file="src/calc.py")
        assert cone is None

    @pytest.mark.asyncio
    async def test_enabled_builds_cone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")
        b = RepairContextBridge(oracle_graph=_graph_full())
        cone = await b.build(evidence_json='{"fault_node_keys": ["%s"]}' % _FAULT,
                             target_file="src/calc.py",
                             failing_tests=("tests/test_calc.py::test_parse",))
        assert cone is not None
        assert str(_DEP_DOWN) in cone.dependents

    @pytest.mark.asyncio
    async def test_failsoft_on_raising_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")
        g = _Graph(raise_blast=True,
                   file_nodes={"src/calc.py": [_Node("jarvis", "src/calc.py", "parse")]})
        b = RepairContextBridge(oracle_graph=g)
        # blast raises but is caught per-fault; deps/file still resolve → cone non-empty, no raise
        cone = await b.build(evidence_json="", target_file="src/calc.py")
        assert cone is not None
        assert cone.dependents == []  # blast failed → no dependents, but fault_keys present

    @pytest.mark.asyncio
    async def test_no_graph_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")
        b = RepairContextBridge(oracle_graph=None)
        # _get_graph will try get_oracle(); in the test env that may succeed but have no nodes,
        # or fail — either way must not raise and must return None when no fault keys resolve.
        cone = await b.build(evidence_json="", target_file="does/not/exist.py")
        assert cone is None

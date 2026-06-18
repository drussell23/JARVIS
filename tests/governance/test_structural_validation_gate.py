"""Tests for the Sovereign Structural Validation Gate — Slice 3 (the enforce).

Covers `structural_validation_gate.py`:
1. Isolated delta build + the three structural proofs.
2. Acyclicity Guard — a new cycle is a HARD reject with the closed loop in coordinates.
3. Path Reachability Matrix (§3.1) — live-reachability sever → reject; dead-only sever → prune.
4. Boundary Invariant Verification — changed signature with un-updated callers → soft divergence.
5. Structured DivergenceSignature feedback + stable signature hashes (Phase 3).
6. Gating + fail-soft (disabled → accept; analyze unavailable → accept; never raises).
7. _DeltaGraph + OracleConeReader units.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.structural_validation_gate import (
    DivergenceSignature,
    OracleConeReader,
    StructuralValidationGate,
    StructuralVerdict,
    _DeltaGraph,
)


# --------------------------------------------------------------------------- analyzer fakes
class _OutOK:
    value = "ok"


class _ET:
    def __init__(self, v: str) -> None:
        self.value = v


class _ED:
    def __init__(self, v: str) -> None:
        self.edge_type = _ET(v)


class _ND:
    def __init__(self, key: str, sig: Optional[str] = None) -> None:
        self.node_id = key
        self.signature = sig


class _Result:
    def __init__(self, edges: List[Tuple[str, str, Any]], nodes: Tuple[Any, ...] = ()) -> None:
        self.outcome = _OutOK()
        self.edges = edges
        self.nodes = nodes


def _analyzer(edges: List[Tuple[str, str, str]], nodes: Tuple[Any, ...] = ()):
    _e = [(s, d, _ED(t)) for s, d, t in edges]

    async def _a(caller, source, *, filename="", repo_name="", relative_path=""):  # noqa: ANN001
        return _Result(_e, nodes)

    return _a


def _analyzer_fail():
    async def _a(caller, source, *, filename="", repo_name="", relative_path=""):  # noqa: ANN001
        class _Bad:
            outcome = _ET("syntax_error")
            edges: Tuple = ()
            nodes: Tuple = ()
        return _Bad()

    return _a


class _Reader:
    def __init__(self, edges: List[Tuple[str, str, str]],
                 sigs: Optional[Dict[str, Optional[str]]] = None,
                 roots: Optional[List[str]] = None) -> None:
        self._edges = edges
        self._sigs = sigs or {}
        self._roots = roots or []

    def cone_edges(self) -> List[Tuple[str, str, str]]:
        return list(self._edges)

    def node_signature(self, key: str) -> Optional[str]:
        return self._sigs.get(key)

    def roots(self) -> List[str]:
        return list(self._roots)


@pytest.fixture(autouse=True)
def _enable_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_REPAIR_STRUCTURAL_SOFT_BLOCKS", raising=False)


# --------------------------------------------------------------------------- _DeltaGraph
class TestDeltaGraph:
    def test_cycle_detection(self) -> None:
        g = _DeltaGraph()
        g.add_edge("a", "b"); g.add_edge("b", "c"); g.add_edge("c", "a")
        cyc = g.cycles()
        assert len(cyc) == 1 and set(cyc[0]) == {"a", "b", "c"}

    def test_no_cycle(self) -> None:
        g = _DeltaGraph()
        g.add_edge("a", "b"); g.add_edge("b", "c")
        assert g.cycles() == []

    def test_reachability(self) -> None:
        g = _DeltaGraph()
        g.add_edge("root", "a"); g.add_edge("a", "b"); g.add_edge("x", "y")
        assert g.reachable(["root"]) == {"root", "a", "b"}


# --------------------------------------------------------------------------- acyclicity
class TestAcyclicity:
    @pytest.mark.asyncio
    async def test_new_cycle_hard_reject(self) -> None:
        pre = [("r:f.py:a", "r:g.py:b", "calls")]
        new = [("r:f.py:a", "r:f.py:c", "calls"), ("r:f.py:c", "r:f.py:a", "calls")]
        gate = StructuralValidationGate(analyzer=_analyzer(new))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=_Reader(pre))
        assert v.accepted is False
        kinds = [d.kind for d in v.divergences]
        assert "new_cycle" in kinds
        cyc = next(d for d in v.divergences if d.kind == "new_cycle")
        assert set(cyc.coordinates["cycle"]) == {"r:f.py:a", "r:f.py:c"}
        assert cyc.severity == "hard"
        assert cyc.signature_hash  # Phase 3 stable hash populated

    @pytest.mark.asyncio
    async def test_clean_candidate_accepted(self) -> None:
        pre = [("r:f.py:a", "r:g.py:b", "calls")]
        new = [("r:f.py:a", "r:g.py:b", "calls")]  # same shape, no cycle
        gate = StructuralValidationGate(analyzer=_analyzer(new))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=_Reader(pre))
        assert v.accepted is True
        assert v.divergences == ()


# --------------------------------------------------------------------------- reachability matrix
class TestReachabilityMatrix:
    @pytest.mark.asyncio
    async def test_live_sever_rejected(self) -> None:
        # root → a (f.py) → b ; patch drops a→b → b becomes unreachable
        pre = [("r:t.py:test", "r:f.py:a", "calls"), ("r:f.py:a", "r:g.py:b", "calls")]
        new: List[Tuple[str, str, str]] = []  # candidate removed the a→b call
        gate = StructuralValidationGate(analyzer=_analyzer(new))
        v = await gate.validate(candidate_source="x", file_path="f.py", repo_name="r",
                                reader=_Reader(pre, roots=["r:t.py:test"]))
        assert v.accepted is False
        sev = next(d for d in v.divergences if d.kind == "severed_reachability")
        assert "r:g.py:b" in sev.coordinates["unreachable"]

    @pytest.mark.asyncio
    async def test_dead_only_sever_is_pruned_not_rejected(self) -> None:
        # root → a (g.py, kept) reachable; f.py holds dead→orphan (never reachable)
        pre = [("r:t.py:test", "r:g.py:a", "calls"),
               ("r:f.py:dead", "r:f.py:orphan", "calls")]
        new: List[Tuple[str, str, str]] = []  # patch removed the dead code
        gate = StructuralValidationGate(analyzer=_analyzer(new))
        v = await gate.validate(candidate_source="x", file_path="f.py", repo_name="r",
                                reader=_Reader(pre, roots=["r:t.py:test"]))
        assert v.accepted is True            # authorized pruning, not a regression
        assert not any(d.kind == "severed_reachability" for d in v.divergences)
        assert len(v.prunes) == 1
        assert v.prunes[0].severed_edge == ("r:f.py:dead", "r:f.py:orphan")

    @pytest.mark.asyncio
    async def test_no_roots_skips_reachability(self) -> None:
        pre = [("r:f.py:a", "r:g.py:b", "calls")]
        gate = StructuralValidationGate(analyzer=_analyzer([]))
        v = await gate.validate(candidate_source="x", file_path="f.py", repo_name="r",
                                reader=_Reader(pre, roots=[]))
        assert v.accepted is True
        assert not any(d.kind == "severed_reachability" for d in v.divergences)


# --------------------------------------------------------------------------- boundary invariant
class TestBoundaryInvariant:
    @pytest.mark.asyncio
    async def test_signature_change_with_callers_soft(self) -> None:
        nodes = (_ND("r:f.py:func", sig="func(a, b)"),)
        pre = [("r:f.py:caller", "r:f.py:func", "calls")]
        reader = _Reader(pre, sigs={"r:f.py:func": "func(a)"})
        gate = StructuralValidationGate(analyzer=_analyzer([], nodes=nodes))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=reader)
        bd = next(d for d in v.divergences if d.kind == "boundary_signature_mismatch")
        assert bd.coordinates["symbol"] == "r:f.py:func"
        assert bd.coordinates["callers"] == ["r:f.py:caller"]
        assert bd.severity == "soft"
        assert v.accepted is True  # soft does not block by default

    @pytest.mark.asyncio
    async def test_soft_blocks_env_makes_boundary_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_STRUCTURAL_SOFT_BLOCKS", "true")
        nodes = (_ND("r:f.py:func", sig="func(a, b)"),)
        pre = [("r:f.py:caller", "r:f.py:func", "calls")]
        reader = _Reader(pre, sigs={"r:f.py:func": "func(a)"})
        gate = StructuralValidationGate(analyzer=_analyzer([], nodes=nodes))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=reader)
        assert v.accepted is False

    @pytest.mark.asyncio
    async def test_unchanged_signature_no_divergence(self) -> None:
        nodes = (_ND("r:f.py:func", sig="func(a)"),)
        pre = [("r:f.py:caller", "r:f.py:func", "calls")]
        reader = _Reader(pre, sigs={"r:f.py:func": "func(a)"})
        gate = StructuralValidationGate(analyzer=_analyzer([], nodes=nodes))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=reader)
        assert v.divergences == ()


# --------------------------------------------------------------------------- gating + fail-soft
class TestGatingAndFailSoft:
    @pytest.mark.asyncio
    async def test_disabled_accepts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", raising=False)
        gate = StructuralValidationGate(analyzer=_analyzer([]))
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=_Reader([]))
        assert v.accepted is True and v.analyzed is False

    @pytest.mark.asyncio
    async def test_no_source_accepts(self) -> None:
        gate = StructuralValidationGate(analyzer=_analyzer([]))
        v = await gate.validate(candidate_source="", file_path="f.py",
                                repo_name="r", reader=_Reader([]))
        assert v.accepted is True and v.analyzed is False

    @pytest.mark.asyncio
    async def test_analyze_failure_accepts(self) -> None:
        gate = StructuralValidationGate(analyzer=_analyzer_fail())
        v = await gate.validate(candidate_source="x", file_path="f.py",
                                repo_name="r", reader=_Reader([]))
        assert v.accepted is True and v.analyzed is False


# --------------------------------------------------------------------------- feedback (Phase 3)
class TestDivergenceFeedback:
    def test_cycle_feedback(self) -> None:
        d = DivergenceSignature(kind="new_cycle", severity="hard", detail="d",
                                coordinates={"cycle": ["a", "b"]})
        fb = d.to_feedback()
        assert "dependency cycle" in fb and "a → b" in fb

    def test_reachability_feedback(self) -> None:
        d = DivergenceSignature(kind="severed_reachability", severity="hard", detail="d",
                                coordinates={"unreachable": ["x"], "broken_path": ["a", "x"],
                                             "root": "t"})
        fb = d.to_feedback()
        assert "severs" in fb.lower() and "x" in fb

    def test_verdict_feedback_concats(self) -> None:
        v = StructuralVerdict(accepted=False, divergences=(
            DivergenceSignature(kind="new_cycle", severity="hard", detail="d",
                                coordinates={"cycle": ["a", "b"]}),
        ))
        assert "STRUCTURAL VIOLATION" in v.feedback()
        assert "accepted=False" in v.telemetry()


# --------------------------------------------------------------------------- OracleConeReader
class _FakeCKG:
    def __init__(self) -> None:
        self._from = {"r:f.py:a": [("r:g.py:b", {"edge_type": "calls"})]}
        self._to = {"r:f.py:a": [("r:t.py:test", {"edge_type": "calls"})]}
        self._sig = {"r:f.py:a": "a(self)"}
        self._names = {"test_x": ["r:t.py:test_x"]}

    def get_edges_from(self, k: str):
        return self._from.get(k, [])

    def get_edges_to(self, k: str):
        return self._to.get(k, [])

    def get_node(self, k: str):
        return {"signature": self._sig.get(k)}

    def find_nodes_by_name(self, name: str):
        return self._names.get(name, [])


class _FakeCone:
    fault_keys = ["r:f.py:a"]
    dependents = []
    dependencies = []
    call_chain = ["r:t.py:test → r:f.py:a"]


class TestOracleConeReader:
    def test_cone_edges_union(self) -> None:
        r = OracleConeReader(_FakeCKG(), _FakeCone(), failing_tests=())
        edges = r.cone_edges()
        assert ("r:f.py:a", "r:g.py:b", "calls") in edges
        assert ("r:t.py:test", "r:f.py:a", "calls") in edges

    def test_node_signature(self) -> None:
        r = OracleConeReader(_FakeCKG(), _FakeCone(), failing_tests=())
        assert r.node_signature("r:f.py:a") == "a(self)"

    def test_roots_from_tests_and_chain(self) -> None:
        r = OracleConeReader(_FakeCKG(), _FakeCone(),
                             failing_tests=("tests/test_x.py::test_x",))
        roots = r.roots()
        assert "r:t.py:test_x" in roots          # from failing test name
        assert "r:t.py:test" in roots            # from call-chain head

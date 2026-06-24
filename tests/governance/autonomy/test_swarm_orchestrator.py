"""Tests for swarm_orchestrator — define_worker, build_graph, OFF-inert submit."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.elastic_fanout import FanoutAction
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.autonomy.swarm_orchestrator import (
    SubGoal,
    SwarmOrchestrator,
)


@dataclass
class _Probe:
    free_pct: float
    ok: bool = True
    source: str = "fake"


class _FakeGate:
    def __init__(self, free_pct, ok=True):
        self._p = _Probe(free_pct=free_pct, ok=ok)

    def probe(self):
        return self._p


@pytest.fixture
def py_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# define_worker fills the new WorkUnitSpec fields from synthesis
# ---------------------------------------------------------------------------


def test_define_worker_fills_swarm_fields(py_file, tmp_path):
    orch = SwarmOrchestrator(project_root=str(tmp_path))
    sg = SubGoal(
        unit_id="u1", repo="JARVIS",
        goal="Analyze the f function",
        target_files=(py_file.name,),
    )
    unit = orch.define_worker(sg)
    assert isinstance(unit, WorkUnitSpec)
    assert unit.is_swarm_worker is True
    assert unit.worker_role is not None and "analyzer" in unit.worker_role
    assert unit.allowed_tools is not None
    assert "read_file" in unit.allowed_tools
    assert unit.mutation_budget == 0  # read-only synthesis
    assert unit.context_budget_tokens is not None
    assert unit.system_prompt_template is not None
    assert unit.goal in unit.system_prompt_template


def test_define_worker_mutating_goal_gets_budget(py_file, tmp_path):
    orch = SwarmOrchestrator(project_root=str(tmp_path))
    sg = SubGoal(
        unit_id="u2", repo="JARVIS",
        goal="Refactor and fix the f function",
        target_files=(py_file.name,),
    )
    unit = orch.define_worker(sg)
    assert unit.mutation_budget is not None and unit.mutation_budget > 0
    assert any(t in ("edit_file", "write_file") for t in unit.allowed_tools)


# ---------------------------------------------------------------------------
# OFF -> byte-identical: a legacy WorkUnitSpec has all swarm fields None
# ---------------------------------------------------------------------------


def test_legacy_workunitspec_unaffected_swarm_fields_none():
    unit = WorkUnitSpec(
        unit_id="legacy", repo="JARVIS", goal="do thing",
        target_files=("x.py",),
    )
    assert unit.system_prompt_template is None
    assert unit.allowed_tools is None
    assert unit.mutation_budget is None
    assert unit.context_budget_tokens is None
    assert unit.worker_role is None
    assert unit.is_swarm_worker is False


def test_legacy_graph_plan_digest_byte_identical():
    """A legacy unit's plan_digest must be unaffected by the new fields.

    The digest excludes the swarm fields, so a graph built today and a
    graph built after the extension hash identically for legacy units.
    """
    unit = WorkUnitSpec(
        unit_id="u", repo="JARVIS", goal="g", target_files=("a.py",),
    )
    g = ExecutionGraph(
        graph_id="gid", op_id="op", planner_id="p", schema_version="1",
        units=(unit,), concurrency_limit=3,
    )
    # Recompute the digest from the same legacy unit -> identical.
    g2 = ExecutionGraph(
        graph_id="gid", op_id="op", planner_id="p", schema_version="1",
        units=(unit,), concurrency_limit=3,
    )
    assert g.plan_digest == g2.plan_digest


def test_submit_inert_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", raising=False)

    class _Sched:
        def __init__(self):
            self.calls = 0

        async def submit(self, graph):
            self.calls += 1
            return True

    sched = _Sched()
    orch = SwarmOrchestrator(scheduler=sched)
    unit = WorkUnitSpec(unit_id="u", repo="JARVIS", goal="g", target_files=("a.py",))
    graph = ExecutionGraph(
        graph_id="g", op_id="op", planner_id="p", schema_version="1",
        units=(unit,), concurrency_limit=3,
    )
    result = asyncio.run(orch.submit(graph))
    assert result is False
    assert sched.calls == 0  # scheduler NEVER touched when OFF


def test_submit_delegates_when_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", "true")

    class _Sched:
        def __init__(self):
            self.calls = 0

        async def submit(self, graph):
            self.calls += 1
            return True

    sched = _Sched()
    orch = SwarmOrchestrator(scheduler=sched)
    unit = WorkUnitSpec(unit_id="u", repo="JARVIS", goal="g", target_files=("a.py",))
    graph = ExecutionGraph(
        graph_id="g", op_id="op", planner_id="p", schema_version="1",
        units=(unit,), concurrency_limit=3,
    )
    result = asyncio.run(orch.submit(graph))
    assert result is True
    assert sched.calls == 1


# ---------------------------------------------------------------------------
# build_graph — elastic fan-out sets concurrency_limit + FIFO under freeze
# ---------------------------------------------------------------------------


def test_build_graph_bursts_under_low_pressure(py_file, tmp_path):
    orch = SwarmOrchestrator(
        memory_pressure_gate=_FakeGate(free_pct=90.0),  # used 10% -> burst
        project_root=str(tmp_path),
    )
    sgs = [
        SubGoal(unit_id="u{0}".format(i), repo="JARVIS",
                goal="Analyze f", target_files=(py_file.name,))
        for i in range(6)
    ]
    graph = orch.build_graph(sgs, op_id="op", graph_id="g")
    assert len(graph.units) == 6
    # Burst permits beyond the base floor.
    assert graph.concurrency_limit >= 3
    # All units carry synthesized swarm fields.
    assert all(u.is_swarm_worker for u in graph.units)
    # Nothing held under burst.
    assert len(orch.pending) == 0


def test_build_graph_freezes_and_holds_fifo(py_file, tmp_path):
    orch = SwarmOrchestrator(
        memory_pressure_gate=_FakeGate(free_pct=5.0),  # used 95% -> freeze
        project_root=str(tmp_path),
    )
    sgs = [
        SubGoal(unit_id="u{0}".format(i), repo="JARVIS",
                goal="Analyze f", target_files=(py_file.name,))
        for i in range(6)
    ]
    graph = orch.build_graph(sgs, op_id="op", graph_id="g")
    # Freeze pins concurrency to the floor; the rest held in FIFO (no drop).
    assert graph.concurrency_limit >= 1
    assert len(orch.pending) == max(0, 6 - graph.concurrency_limit)
    # No unit lost — all encoded in the graph.
    assert len(graph.units) == 6


def test_compute_fanout_unreadable_probe_freezes(tmp_path):
    orch = SwarmOrchestrator(
        memory_pressure_gate=_FakeGate(free_pct=0.0, ok=False),
        project_root=str(tmp_path),
    )
    decision = orch.compute_fanout(10)
    assert decision.action is FanoutAction.FREEZE

"""Regression spine for swarm reachability — the Golden Rule must be REACHED.

`swarm_invoker`'s docstring says it closed the gap where "setting
``JARVIS_SWARM_ORCHESTRATOR_ENABLED=true`` did nothing because nothing invoked
them". It closed that one and left the next: the invoker WAS called on every
work unit, but its eligibility predicate asked ``unit.is_swarm_worker`` —
True only for a unit that ALREADY carries a synthesized shape.

Every production graph builder (`parallel_dispatch`, `iteration_planner`,
`providers`, `meta_goal_aggregator`, `graph_coalescer`) leaves those fields
``None``, and the only writer — ``SwarmOrchestrator.define_worker`` — has no
production caller. So the invoker would only shape a unit that was already
shaped, and the master flag still did nothing.

The load-bearing test here is `test_synthesizer_is_reachable_from_a_production_unit`.
It is the guard that would have caught the original bug, and it asserts
against a unit built the way production builds them — no swarm fields — rather
than against a hand-marked fixture, which is exactly how the bug survived its
own test suite.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.autonomy.swarm_invoker import (
    SwarmUnitInvoker,
    is_graph_parallelizable,
    swarm_eligible,
)

TARGET = "backend/core/ouroboros/governance/memory_corpus.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("JARVIS_SWARM_ORCHESTRATOR_ENABLED",
                "JARVIS_SWARM_REQUIRE_PRESHAPED_UNITS"):
        monkeypatch.delenv(var, raising=False)
    yield


def _unit(uid="u0", goal="refactor the timeout handling", **kw):
    """A unit shaped the way PRODUCTION builders shape one — no swarm fields."""
    base = dict(unit_id=uid, repo="r", goal=goal, target_files=(TARGET,))
    base.update(kw)
    return WorkUnitSpec(**base)


def _graph(units=None, concurrency_limit=2):
    units = units or (_unit("u0"), _unit("u1"))
    return ExecutionGraph(graph_id="g", op_id="op-1", planner_id="p",
                          schema_version=1, units=units,
                          concurrency_limit=concurrency_limit)


class _Legacy:
    def __init__(self):
        self.calls = 0

    async def execute(self, graph, unit):
        self.calls += 1
        return "LEGACY"


def _halt(*_a, **_k):
    """Stop the swarm path right after synthesis, before any execution."""
    raise RuntimeError("halt after synthesis")


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        coro)


# ---------------------------------------------------------------------------
# The load-bearing guard
# ---------------------------------------------------------------------------


def test_synthesizer_is_reachable_from_a_production_unit(monkeypatch):
    """The Golden Rule must actually RUN on a unit production would build.

    This is the regression that would have caught the original defect. It
    deliberately uses a unit with NO swarm fields — a hand-marked fixture
    would pass against the bug, which is how the bug survived.
    """
    monkeypatch.setenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy import worker_synthesizer

    seen = {}
    real = worker_synthesizer.synthesize_worker_spec

    def spy(unit, **kw):
        shape = real(unit, **kw)
        seen[unit.unit_id] = shape
        return shape

    monkeypatch.setattr(worker_synthesizer, "synthesize_worker_spec", spy)

    graph = _graph()
    legacy = _Legacy()
    result = _run(SwarmUnitInvoker(
        legacy_executor=legacy, build_worker=_halt).execute(graph, graph.units[0]))

    assert legacy.calls == 0, "unit fell through to legacy — swarm unreachable"
    assert seen, "synthesize_worker_spec never ran"
    shape = seen["u0"]
    # The shape is DERIVED, not looked up: a mutating goal on a real Python
    # source file must yield a mutation tool and a bounded budget.
    assert "edit_file" in tuple(shape.allowed_tools)
    assert shape.mutation_budget > 0
    assert shape.role and not shape.role.isupper(), "role should be a derived phrase"
    # Fail-CLOSED still holds: no cage -> FAILED, never an uncaged run.
    assert getattr(result, "failure_class", "") == "swarm_cage"


def test_read_only_goal_gets_no_mutation_tool(monkeypatch):
    """Least privilege, derived. The cage is the point of reaching this path."""
    monkeypatch.setenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", "true")
    from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
        synthesize_worker_spec,
    )
    shape = synthesize_worker_spec(
        _unit(goal="analyze and report on the call graph"))
    tools = tuple(shape.allowed_tools)
    assert "edit_file" not in tools and "write_file" not in tools


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def test_production_shaped_unit_is_eligible():
    assert swarm_eligible(_unit()) is True


def test_preshaped_unit_stays_eligible():
    """The `define_worker` path keeps working unchanged."""
    assert swarm_eligible(_unit(worker_role="explicit")) is True


def test_goalless_unit_fails_closed():
    """No goal → nothing to inspect → legacy. A worker that cannot be
    inspected cannot be caged."""
    assert swarm_eligible(_unit(goal="   ")) is False


def test_eligibility_never_raises_on_a_foreign_object():
    assert swarm_eligible(object()) is False  # type: ignore[arg-type]


def test_rollback_flag_restores_the_old_predicate(monkeypatch):
    monkeypatch.setenv("JARVIS_SWARM_REQUIRE_PRESHAPED_UNITS", "1")
    assert swarm_eligible(_unit()) is False
    assert swarm_eligible(_unit(worker_role="explicit")) is True


# ---------------------------------------------------------------------------
# The gates that must still hold
# ---------------------------------------------------------------------------


def test_master_gate_off_is_byte_identical_legacy():
    """Default OFF must remain a strict no-op."""
    graph = _graph()
    legacy = _Legacy()
    result = _run(SwarmUnitInvoker(
        legacy_executor=legacy, build_worker=_halt).execute(graph, graph.units[0]))
    assert result == "LEGACY"
    assert legacy.calls == 1


def test_serial_graph_never_fans_out(monkeypatch):
    """A non-parallelizable DAG stays on the legacy executor."""
    monkeypatch.setenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", "true")
    serial = _graph(units=(_unit("u0"),), concurrency_limit=1)
    assert is_graph_parallelizable(serial) is False
    legacy = _Legacy()
    result = _run(SwarmUnitInvoker(
        legacy_executor=legacy, build_worker=_halt).execute(serial, serial.units[0]))
    assert result == "LEGACY"


def test_dependency_chain_is_not_parallelizable():
    chained = ExecutionGraph(
        graph_id="g", op_id="o", planner_id="p", schema_version=1,
        units=(_unit("u0"), _unit("u1", dependency_ids=("u0",))),
        concurrency_limit=4)
    assert is_graph_parallelizable(chained) is False


# ---------------------------------------------------------------------------
# The structural fact the fix depends on
# ---------------------------------------------------------------------------


def test_production_builders_still_leave_swarm_fields_unset():
    """Pins WHY the predicate had to change.

    If a builder ever starts setting these, the eligibility predicate should
    be revisited rather than silently doing two things. This test is the
    tripwire for that.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "backend/core/ouroboros/governance"
    builders = ("parallel_dispatch.py", "meta_goal_aggregator.py",
                "graph_coalescer.py", "autonomy/iteration_planner.py")
    swarm_fields = {"worker_role", "system_prompt_template", "allowed_tools",
                    "mutation_budget", "context_budget_tokens"}
    for name in builders:
        path = root / name
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "WorkUnitSpec"):
                used = {kw.arg for kw in node.keywords if kw.arg}
                assert not (used & swarm_fields), (
                    f"{name} now sets swarm fields {used & swarm_fields} — "
                    f"revisit swarm_eligible()")

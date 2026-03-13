from __future__ import annotations

import pytest


def _make_graph():
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        WorkUnitSpec,
    )

    return ExecutionGraph(
        graph_id="graph-001",
        op_id="op-001",
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=2,
        units=(
            WorkUnitSpec(
                unit_id="jarvis-api",
                repo="jarvis",
                goal="update jarvis api",
                target_files=("backend/api.py",),
                owned_paths=("backend/api.py",),
            ),
            WorkUnitSpec(
                unit_id="prime-router",
                repo="prime",
                goal="update routing",
                target_files=("router.py",),
                dependency_ids=("jarvis-api",),
                owned_paths=("router.py",),
            ),
        ),
    )


def test_execution_graph_auto_computes_digest() -> None:
    graph = _make_graph()
    assert graph.plan_digest
    assert graph.causal_trace_id.startswith("graph-001:")


def test_duplicate_unit_ids_rejected() -> None:
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        WorkUnitSpec,
    )

    with pytest.raises(ValueError, match="duplicate unit_id"):
        ExecutionGraph(
            graph_id="graph-dup",
            op_id="op-dup",
            planner_id="planner-v1",
            schema_version="2d.1",
            concurrency_limit=1,
            units=(
                WorkUnitSpec(
                    unit_id="u1",
                    repo="jarvis",
                    goal="a",
                    target_files=("a.py",),
                ),
                WorkUnitSpec(
                    unit_id="u1",
                    repo="prime",
                    goal="b",
                    target_files=("b.py",),
                ),
            ),
        )


def test_cycle_rejected() -> None:
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        WorkUnitSpec,
    )

    with pytest.raises(ValueError, match="cycle"):
        ExecutionGraph(
            graph_id="graph-cycle",
            op_id="op-cycle",
            planner_id="planner-v1",
            schema_version="2d.1",
            concurrency_limit=1,
            units=(
                WorkUnitSpec(
                    unit_id="u1",
                    repo="jarvis",
                    goal="a",
                    target_files=("a.py",),
                    dependency_ids=("u2",),
                ),
                WorkUnitSpec(
                    unit_id="u2",
                    repo="prime",
                    goal="b",
                    target_files=("b.py",),
                    dependency_ids=("u1",),
                ),
            ),
        )


def test_work_unit_effective_owned_paths_defaults_to_target_files() -> None:
    from backend.core.ouroboros.governance.autonomy.subagent_types import WorkUnitSpec

    unit = WorkUnitSpec(
        unit_id="u1",
        repo="jarvis",
        goal="goal",
        target_files=("backend/a.py",),
    )
    assert unit.effective_owned_paths == ("backend/a.py",)

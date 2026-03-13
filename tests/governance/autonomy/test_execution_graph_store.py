from __future__ import annotations

import json


def _make_state():
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        GraphExecutionState,
        WorkUnitSpec,
    )

    graph = ExecutionGraph(
        graph_id="graph-store",
        op_id="op-store",
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=2,
        units=(
            WorkUnitSpec(
                unit_id="u1",
                repo="jarvis",
                goal="goal",
                target_files=("backend/a.py",),
                owned_paths=("backend/a.py",),
            ),
        ),
    )
    return GraphExecutionState(graph=graph, ready_units=("u1",))


def test_save_and_get_roundtrip(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )

    store = ExecutionGraphStore(tmp_path)
    state = _make_state()
    store.save(state)

    loaded = store.get(state.graph_id)
    assert loaded is not None
    assert loaded.graph.graph_id == "graph-store"
    assert loaded.ready_units == ("u1",)


def test_load_inflight_skips_corrupt_files(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )

    (tmp_path / "graph_bad.json").write_text("not json{{")
    store = ExecutionGraphStore(tmp_path)
    assert store.load_inflight() == {}


def test_save_is_atomic_json(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )

    store = ExecutionGraphStore(tmp_path)
    state = _make_state()
    store.save(state)
    payload = json.loads((tmp_path / "graph_graph-store.json").read_text())
    assert payload["graph"]["graph_id"] == "graph-store"
    assert payload["phase"] == "created"

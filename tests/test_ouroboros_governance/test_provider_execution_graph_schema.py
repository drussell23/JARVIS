from __future__ import annotations

import json

import pytest


def _ctx():
    from backend.core.ouroboros.governance.op_context import OperationContext

    return OperationContext.create(
        target_files=("backend/a.py", "prime/router.py"),
        description="parallel cross-repo update",
        op_id="op-l3-001",
        repo_scope=("jarvis", "prime"),
        primary_repo="jarvis",
    ).with_execution_graph_metadata(
        execution_graph_id="",
        execution_plan_digest="",
        subagent_count=0,
        parallelism_budget=2,
        causal_trace_id="",
    )


def test_parse_valid_2d1_execution_graph() -> None:
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    raw = json.dumps(
        {
            "schema_version": "2d.1",
            "execution_graph": {
                "graph_id": "graph-001",
                "planner_id": "jprime-dag-v1",
                "concurrency_limit": 2,
                "units": [
                    {
                        "unit_id": "u1",
                        "repo": "jarvis",
                        "goal": "update jarvis",
                        "target_files": ["backend/a.py"],
                        "owned_paths": ["backend/a.py"],
                    },
                    {
                        "unit_id": "u2",
                        "repo": "prime",
                        "goal": "update prime",
                        "target_files": ["router.py"],
                        "owned_paths": ["router.py"],
                    },
                ],
            },
            "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
        }
    )

    result = _parse_generation_response(raw, "gcp-jprime", 0.5, _ctx(), "", "")
    assert len(result.candidates) == 1
    graph = result.candidates[0]["execution_graph"]
    assert graph.graph_id == "graph-001"
    assert graph.concurrency_limit == 2
    assert len(graph.units) == 2


def test_parse_2d1_rejects_duplicate_unit_ids() -> None:
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    raw = json.dumps(
        {
            "schema_version": "2d.1",
            "execution_graph": {
                "graph_id": "graph-dup",
                "planner_id": "jprime-dag-v1",
                "concurrency_limit": 2,
                "units": [
                    {
                        "unit_id": "u1",
                        "repo": "jarvis",
                        "goal": "a",
                        "target_files": ["backend/a.py"],
                    },
                    {
                        "unit_id": "u1",
                        "repo": "prime",
                        "goal": "b",
                        "target_files": ["router.py"],
                    },
                ],
            },
        }
    )

    with pytest.raises(RuntimeError, match="duplicate unit_id"):
        _parse_generation_response(raw, "gcp-jprime", 0.5, _ctx(), "", "")


def test_parse_2d1_rejects_cycles() -> None:
    from backend.core.ouroboros.governance.providers import _parse_generation_response

    raw = json.dumps(
        {
            "schema_version": "2d.1",
            "execution_graph": {
                "graph_id": "graph-cycle",
                "planner_id": "jprime-dag-v1",
                "concurrency_limit": 2,
                "units": [
                    {
                        "unit_id": "u1",
                        "repo": "jarvis",
                        "goal": "a",
                        "target_files": ["backend/a.py"],
                        "dependency_ids": ["u2"],
                    },
                    {
                        "unit_id": "u2",
                        "repo": "prime",
                        "goal": "b",
                        "target_files": ["router.py"],
                        "dependency_ids": ["u1"],
                    },
                ],
            },
        }
    )

    with pytest.raises(RuntimeError, match="cycle"):
        _parse_generation_response(raw, "gcp-jprime", 0.5, _ctx(), "", "")

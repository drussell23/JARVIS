"""Tests for Wave 3 (6) Slice 4b -- DAGComposer.

Closes the Slice-4b gap: a *successful* WAVE3 parallel L3 fan-out's per-unit
patches must be map-reduced into ONE unified multi-file candidate that walks
VALIDATE -> GATE -> APPLY via the EXISTING orchestrator multi-file path
(``_iter_candidate_files`` / ``_apply_multi_file_candidate`` + batch
rollback) -- instead of being stashed + ignored while APPLY re-runs serially.

Invariants pinned here:

(a) N disjoint successful units -> ONE composed multi-file candidate in the
    existing ``{file_path, full_content, files:[{...}]}`` shape, carrying the
    parent op_id, deterministic file ordering.
(b) ANY unit failure -> ComposeFailure -> caller uses the legacy serial path
    (no partial compose, no silent data loss).
(c) Two units claiming the SAME file -> fail-CLOSED
    ``collision_invariant_violated`` (never a silent merge).
(d) The composed candidate matches the shape ``_iter_candidate_files``
    accepts (asserted against the REAL consumer).
(e) OFF flag (default) -> phase_dispatcher hook stashes unchanged
    (byte-identical); composer is a pure function callable regardless.
"""
from __future__ import annotations

import time
from typing import Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)
from backend.core.ouroboros.governance import dag_composer
from backend.core.ouroboros.governance.dag_composer import (
    COMPOSER_ID,
    ComposeFailure,
    ComposeFailureReason,
    ComposedCandidate,
    compose_fanout_result,
    dag_compose_enabled,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _spec(unit_id: str, file_path: str, goal: str = "") -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="jarvis",
        goal=goal or f"edit {file_path}",
        target_files=(file_path,),
        owned_paths=(file_path,),
    )


def _graph(specs: List[WorkUnitSpec], op_id: str = "op-deadbeef") -> ExecutionGraph:
    return ExecutionGraph(
        graph_id="graph-test01",
        op_id=op_id,
        planner_id="parallel_dispatch.v1",
        schema_version="wave3_item6_slice2.v1",
        units=tuple(specs),
        concurrency_limit=max(2, len(specs)),
    )


def _result(
    unit_id: str,
    file_path: str,
    content: str,
    *,
    status: WorkUnitState = WorkUnitState.COMPLETED,
    with_patch: bool = True,
) -> WorkUnitResult:
    patch = None
    if with_patch:
        patch = RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
            new_content=((file_path, content.encode("utf-8")),),
        )
    now = time.monotonic_ns()
    return WorkUnitResult(
        unit_id=unit_id,
        repo="jarvis",
        status=status,
        patch=patch,
        attempt_count=1,
        started_at_ns=now,
        finished_at_ns=now,
    )


def _three_disjoint() -> Tuple[ExecutionGraph, Dict[str, WorkUnitResult]]:
    specs = [
        _spec("unit-a", "pkg/a.py", goal="add a"),
        _spec("unit-b", "pkg/b.py", goal="add b"),
        _spec("unit-c", "pkg/c.py", goal="add c"),
    ]
    graph = _graph(specs)
    results = {
        "unit-a": _result("unit-a", "pkg/a.py", "# a\npass\n"),
        "unit-b": _result("unit-b", "pkg/b.py", "# b\npass\n"),
        "unit-c": _result("unit-c", "pkg/c.py", "# c\npass\n"),
    }
    return graph, results


# ---------------------------------------------------------------------------
# (a) N disjoint successful units -> ONE composed multi-file candidate
# ---------------------------------------------------------------------------


def test_three_disjoint_units_compose_into_one_multifile_candidate():
    graph, results = _three_disjoint()
    out = compose_fanout_result(graph, results)

    assert isinstance(out, ComposedCandidate)
    assert out.is_failure is False
    assert out.op_id == "op-deadbeef"

    cand = out.candidate
    # Multi-file shape present.
    assert isinstance(cand["files"], list)
    assert len(cand["files"]) == 3
    # Every entry carries file_path + full_content + rationale.
    for entry in cand["files"]:
        assert set(("file_path", "full_content", "rationale")).issubset(entry.keys())
        assert isinstance(entry["file_path"], str) and entry["file_path"]
        assert isinstance(entry["full_content"], str)

    # All three files present, content preserved.
    by_path = {e["file_path"]: e["full_content"] for e in cand["files"]}
    assert by_path == {
        "pkg/a.py": "# a\npass\n",
        "pkg/b.py": "# b\npass\n",
        "pkg/c.py": "# c\npass\n",
    }
    # Lineage stamp.
    assert cand["composed_by"] == COMPOSER_ID
    assert cand["composed_op_id"] == "op-deadbeef"


def test_composition_is_deterministic_in_graph_order():
    graph, results = _three_disjoint()
    out1 = compose_fanout_result(graph, results)
    out2 = compose_fanout_result(graph, results)
    assert isinstance(out1, ComposedCandidate)
    assert isinstance(out2, ComposedCandidate)
    # File ordering follows graph unit order -> stable across calls.
    assert out1.file_paths == ("pkg/a.py", "pkg/b.py", "pkg/c.py")
    assert out1.file_paths == out2.file_paths
    assert out1.candidate["files"] == out2.candidate["files"]


def test_primary_file_mirrors_first_files_entry():
    graph, results = _three_disjoint()
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposedCandidate)
    cand = out.candidate
    assert cand["file_path"] == cand["files"][0]["file_path"]
    assert cand["full_content"] == cand["files"][0]["full_content"]


# ---------------------------------------------------------------------------
# (b) ANY unit failure -> ComposeFailure -> caller uses legacy serial path
# ---------------------------------------------------------------------------


def test_any_unit_failed_yields_compose_failure_no_partial():
    graph, results = _three_disjoint()
    # Flip unit-b to FAILED.
    results["unit-b"] = _result(
        "unit-b", "pkg/b.py", "", status=WorkUnitState.FAILED, with_patch=False
    )
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.is_failure is True
    assert out.reason == ComposeFailureReason.UNIT_NOT_SUCCESS
    assert out.offending_unit_id == "unit-b"


def test_missing_unit_result_yields_compose_failure():
    graph, results = _three_disjoint()
    del results["unit-c"]
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.UNIT_NOT_SUCCESS
    assert out.offending_unit_id == "unit-c"


def test_success_unit_with_no_patch_fails_closed():
    graph, results = _three_disjoint()
    # SUCCESS but no patch content -> never fabricate.
    results["unit-a"] = _result(
        "unit-a", "pkg/a.py", "", with_patch=False
    )
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.UNIT_MISSING_PATCH
    assert out.offending_unit_id == "unit-a"


def test_cancelled_unit_treated_as_failure():
    graph, results = _three_disjoint()
    results["unit-b"] = _result(
        "unit-b", "pkg/b.py", "x", status=WorkUnitState.CANCELLED
    )
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.UNIT_NOT_SUCCESS


# ---------------------------------------------------------------------------
# (c) Two units claiming the SAME file -> fail-CLOSED collision_invariant_violated
# ---------------------------------------------------------------------------


def test_two_units_same_file_fail_closed_collision():
    specs = [
        _spec("unit-a", "pkg/shared.py"),
        _spec("unit-b", "pkg/shared.py"),
    ]
    graph = _graph(specs)
    results = {
        "unit-a": _result("unit-a", "pkg/shared.py", "# from a\n"),
        "unit-b": _result("unit-b", "pkg/shared.py", "# from b\n"),
    }
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.COLLISION_INVARIANT_VIOLATED
    assert out.offending_unit_id == "unit-b"
    assert "pkg/shared.py" in out.detail


def test_empty_graph_units_fail_closed():
    # Build a graph then strip units defensively -- compose must not crash.
    specs = [_spec("unit-a", "pkg/a.py"), _spec("unit-b", "pkg/b.py")]
    graph = _graph(specs)
    object.__setattr__(graph, "units", ())
    out = compose_fanout_result(graph, {})
    assert isinstance(out, ComposeFailure)
    assert out.reason == ComposeFailureReason.NO_UNITS


# ---------------------------------------------------------------------------
# (d) Composed candidate matches the REAL _iter_candidate_files consumer
# ---------------------------------------------------------------------------


def test_composed_candidate_consumed_by_real_iter_candidate_files(monkeypatch):
    """The composed candidate must feed the EXISTING orchestrator multi-file
    consumer (reuse, no new apply path). Drive the REAL static method."""
    from backend.core.ouroboros.governance.orchestrator import Orchestrator

    monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "true")

    graph, results = _three_disjoint()
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposedCandidate)

    pairs = Orchestrator._iter_candidate_files(out.candidate)
    # All three (file_path, full_content) pairs surface through the real
    # multi-file unpacker, in deterministic order.
    assert pairs == [
        ("pkg/a.py", "# a\npass\n"),
        ("pkg/b.py", "# b\npass\n"),
        ("pkg/c.py", "# c\npass\n"),
    ]


def test_iter_candidate_files_off_falls_back_to_primary(monkeypatch):
    """With multi-file gen OFF, the consumer uses the primary mirror -- proving
    the composed candidate is back-compatible with the legacy single-file
    contract too."""
    from backend.core.ouroboros.governance.orchestrator import Orchestrator

    monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "false")

    graph, results = _three_disjoint()
    out = compose_fanout_result(graph, results)
    assert isinstance(out, ComposedCandidate)

    pairs = Orchestrator._iter_candidate_files(out.candidate)
    assert pairs == [("pkg/a.py", "# a\npass\n")]


# ---------------------------------------------------------------------------
# (e) OFF flag default + gating
# ---------------------------------------------------------------------------


def test_dag_compose_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", raising=False)
    assert dag_compose_enabled() is False


def test_dag_compose_enable_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "true")
    assert dag_compose_enabled() is True
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "0")
    assert dag_compose_enabled() is False


def test_compose_is_pure_regardless_of_flag(monkeypatch):
    """compose_fanout_result is a pure function -- it does NOT read the gate;
    only the phase_dispatcher wiring does. Same inputs -> same output with the
    flag in either state."""
    graph, results = _three_disjoint()
    monkeypatch.delenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", raising=False)
    out_off = compose_fanout_result(graph, results)
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "true")
    out_on = compose_fanout_result(graph, results)
    assert isinstance(out_off, ComposedCandidate)
    assert isinstance(out_on, ComposedCandidate)
    assert out_off.candidate == out_on.candidate


# ---------------------------------------------------------------------------
# phase_dispatcher integration — _maybe_compose_fanout wiring seam
#   COMPLETED fan-out + compose-on -> pctx.generation replaced with the
#   unified multi-file candidate that VALIDATE/GATE then see.
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field  # noqa: E402

from backend.core.ouroboros.governance.op_context import GenerationResult  # noqa: E402
from backend.core.ouroboros.governance.parallel_dispatch import (  # noqa: E402
    FanoutOutcome,
    FanoutResult,
)
from backend.core.ouroboros.governance import phase_dispatcher  # noqa: E402


@dataclass
class _Ctx:
    op_id: str = "op-deadbeef"


@dataclass
class _Pctx:
    generation: object = None
    extras: dict = field(default_factory=dict)


def _completed_fanout(graph, results) -> FanoutResult:
    state = GraphExecutionState(
        graph=graph,
        phase=GraphExecutionPhase.COMPLETED,
        completed_units=tuple(results.keys()),
        results=dict(results),
    )
    return FanoutResult(
        outcome=FanoutOutcome.COMPLETED,
        graph=graph,
        state=state,
        n_units_requested=len(results),
        n_units_completed=len(results),
    )


def test_dispatcher_completed_fanout_compose_on_replaces_generation(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "true")

    graph, results = _three_disjoint()
    fr = _completed_fanout(graph, results)

    prior_gen = GenerationResult(
        candidates=({"file_path": "stale.py", "full_content": "# stale\n"},),
        provider_name="doubleword",
        generation_duration_s=1.5,
        model_id="dw-397b",
    )
    pctx = _Pctx(generation=prior_gen)
    ctx = _Ctx()

    replacement = phase_dispatcher._maybe_compose_fanout(
        ctx=ctx, pctx=pctx, fanout_result=fr
    )
    assert replacement is not None
    assert isinstance(replacement, GenerationResult)

    # The replacement carries ONE unified multi-file candidate with all 3 files.
    assert len(replacement.candidates) == 1
    cand = replacement.candidates[0]
    assert len(cand["files"]) == 3

    # Drive the REAL VALIDATE-facing consumer against the replacement to prove
    # VALIDATE/GATE/APPLY would see the unified multi-file set.
    from backend.core.ouroboros.governance.orchestrator import Orchestrator

    pairs = Orchestrator._iter_candidate_files(replacement.candidates[0])
    assert pairs == [
        ("pkg/a.py", "# a\npass\n"),
        ("pkg/b.py", "# b\npass\n"),
        ("pkg/c.py", "# c\npass\n"),
    ]
    # Provider lineage shows composition; prior duration preserved.
    assert "dag_composer" in replacement.provider_name
    assert replacement.generation_duration_s == 1.5


def test_dispatcher_compose_off_returns_none_byte_identical(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", raising=False)
    graph, results = _three_disjoint()
    fr = _completed_fanout(graph, results)
    pctx = _Pctx(generation="SENTINEL")
    ctx = _Ctx()
    out = phase_dispatcher._maybe_compose_fanout(ctx=ctx, pctx=pctx, fanout_result=fr)
    assert out is None
    # Caller would NOT replace -> generation untouched, no compose stash.
    assert pctx.generation == "SENTINEL"
    assert "dag_composed_candidate" not in pctx.extras


def test_dispatcher_failed_unit_compose_on_falls_through_to_serial(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "true")
    graph, results = _three_disjoint()
    results["unit-b"] = _result(
        "unit-b", "pkg/b.py", "", status=WorkUnitState.FAILED, with_patch=False
    )
    fr = _completed_fanout(graph, results)  # graph COMPLETED but a unit FAILED
    pctx = _Pctx(generation="SENTINEL")
    ctx = _Ctx()
    out = phase_dispatcher._maybe_compose_fanout(ctx=ctx, pctx=pctx, fanout_result=fr)
    # Fail-CLOSED: composer declines -> None -> legacy serial (generation kept).
    assert out is None
    assert pctx.generation == "SENTINEL"


def test_dispatcher_non_completed_outcome_no_compose(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_DAG_COMPOSE_ENABLED", "true")
    graph, results = _three_disjoint()
    state = GraphExecutionState(
        graph=graph, phase=GraphExecutionPhase.FAILED, results=dict(results)
    )
    fr = FanoutResult(outcome=FanoutOutcome.FAILED, graph=graph, state=state)
    pctx = _Pctx(generation="SENTINEL")
    out = phase_dispatcher._maybe_compose_fanout(
        ctx=_Ctx(), pctx=pctx, fanout_result=fr
    )
    assert out is None
    assert pctx.generation == "SENTINEL"

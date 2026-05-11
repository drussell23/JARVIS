"""Regression spine for §41.4 Phase 1 — Goal Decomposition Planner."""
from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    goal_decomposition_planner as gdp,
)
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    GOAL_DECOMPOSITION_SCHEMA_VERSION,
    CompletionRecord,
    CompletionStatus,
    DecomposedPlan,
    DecompositionReport,
    DecompositionVerdict,
    ParentProgress,
    SubGoal,
    SubGoalEmitOutcome,
    SubGoalKind,
    _ENV_ENVELOPE_SOURCE,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_DAG_DEPTH,
    _ENV_MAX_SUB_GOALS,
    _ENV_PERSIST,
    _coerce_kind,
    _coerce_status,
    _make_envelope_for_sub_goal,
    _topological_sort,
    decompose_and_emit,
    decompose_and_emit_sync,
    decompose_goal,
    emit_sub_goal_envelopes,
    envelope_source,
    format_decomposition_panel,
    get_parent_progress,
    heuristic_decompose,
    kind_glyph,
    ledger_path,
    mark_sub_goal_status,
    master_enabled,
    max_dag_depth,
    max_sub_goals,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    repo_name,
    status_glyph,
    verdict_glyph,
)


@dataclass
class _FakeRoadmapGoal:
    """Duck-typed RoadmapGoal for tests."""
    goal_id: str = "g1"
    title: str = "Test Goal"
    description: str = "test description"
    target_files: Tuple[str, ...] = field(default_factory=tuple)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_MAX_SUB_GOALS,
        _ENV_MAX_DAG_DEPTH, _ENV_LEDGER_PATH,
        _ENV_ENVELOPE_SOURCE,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"),
    )
    yield


def _run(coro):
    return asyncio.run(coro)


# Defaults


def test_schema():
    assert GOAL_DECOMPOSITION_SCHEMA_VERSION == "goal_decomposition.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_max_sub_goals_default():
    assert max_sub_goals() == 20


def test_max_dag_depth_default():
    assert max_dag_depth() == 10


def test_repo_name_default():
    assert repo_name() == "jarvis"


def test_envelope_source_default():
    assert envelope_source() == "roadmap"


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in DecompositionVerdict} == {
        "no_goal", "valid", "too_complex",
        "decomposition_failed",
    }


def test_kind_taxonomy_closed():
    assert {k.value for k in SubGoalKind} == {
        "atomic", "sequential", "parallel", "exploratory",
    }


def test_status_taxonomy_closed():
    assert {s.value for s in CompletionStatus} == {
        "proposed", "in_progress", "completed", "failed",
    }


@pytest.mark.parametrize("v", list(DecompositionVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("k", list(SubGoalKind))
def test_kind_glyph(k):
    assert kind_glyph(k) != "?"


@pytest.mark.parametrize("s", list(CompletionStatus))
def test_status_glyph(s):
    assert status_glyph(s) != "?"


# Coercion


def test_coerce_kind_enum():
    assert _coerce_kind(SubGoalKind.PARALLEL) is SubGoalKind.PARALLEL


def test_coerce_kind_string():
    assert _coerce_kind("exploratory") is SubGoalKind.EXPLORATORY


def test_coerce_kind_unknown():
    assert _coerce_kind("garbage") is SubGoalKind.ATOMIC


def test_coerce_status_enum():
    assert (
        _coerce_status(CompletionStatus.COMPLETED)
        is CompletionStatus.COMPLETED
    )


def test_coerce_status_unknown_defaults_proposed():
    assert _coerce_status("garbage") is CompletionStatus.PROPOSED


# Heuristic decomposer


def test_heuristic_empty_goal():
    assert heuristic_decompose(None) == ()


def test_heuristic_no_id_or_title():
    g = _FakeRoadmapGoal(goal_id="", title="")
    assert heuristic_decompose(g) == ()


def test_heuristic_single_file_atomic():
    g = _FakeRoadmapGoal(
        goal_id="g1", title="t", description="plain",
        target_files=("x.py",),
    )
    subs = heuristic_decompose(g)
    assert len(subs) == 1
    assert subs[0].kind in (SubGoalKind.ATOMIC, SubGoalKind.SEQUENTIAL)


def test_heuristic_multi_file_one_per_file():
    g = _FakeRoadmapGoal(
        goal_id="g1", title="multi",
        description="x",
        target_files=("a.py", "b.py", "c.py"),
    )
    subs = heuristic_decompose(g)
    assert len(subs) == 3
    files_seen = {s.target_files[0] for s in subs}
    assert files_seen == {"a.py", "b.py", "c.py"}


def test_heuristic_enumerated_bullets():
    g = _FakeRoadmapGoal(
        goal_id="g1", title="t",
        description=(
            "Steps:\n"
            "1. First step\n"
            "2. Second step\n"
            "3. Third step\n"
        ),
        target_files=("x.py",),
    )
    subs = heuristic_decompose(g)
    assert len(subs) == 3
    # Each step depends on the previous (sequential)
    assert subs[0].depends_on_sub_ids == ()
    assert subs[1].depends_on_sub_ids == (subs[0].sub_goal_id,)
    assert subs[2].depends_on_sub_ids == (subs[1].sub_goal_id,)


def test_heuristic_dash_bullets():
    g = _FakeRoadmapGoal(
        goal_id="g1", title="t",
        description=(
            "- alpha\n"
            "- beta\n"
            "- gamma\n"
        ),
    )
    subs = heuristic_decompose(g)
    assert len(subs) == 3


def test_heuristic_cage_touch_sequential():
    g = _FakeRoadmapGoal(
        goal_id="g1", title="t",
        target_files=(
            "backend/core/ouroboros/governance/orchestrator.py",
            "tests/test.py",
        ),
    )
    subs = heuristic_decompose(g)
    cage_subs = [s for s in subs if s.boundary_crossed]
    if cage_subs:
        # Cage-touching sub-goal should be SEQUENTIAL
        assert cage_subs[0].kind is SubGoalKind.SEQUENTIAL


# Topological sort


def test_topo_sort_empty():
    valid, order, depth = _topological_sort([])
    assert valid is True
    assert order == ()
    assert depth == 0


def test_topo_sort_no_deps():
    subs = [
        SubGoal(
            sub_goal_id=f"s{i}", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.ATOMIC,
            target_files=(), depends_on_sub_ids=(),
            estimated_complexity="m",
            boundary_crossed=False,
        )
        for i in range(3)
    ]
    valid, order, depth = _topological_sort(subs)
    assert valid is True
    assert set(order) == {"s0", "s1", "s2"}
    assert depth == 0


def test_topo_sort_linear_chain():
    subs = [
        SubGoal(
            sub_goal_id="s0", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(), depends_on_sub_ids=(),
            estimated_complexity="m", boundary_crossed=False,
        ),
        SubGoal(
            sub_goal_id="s1", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(),
            depends_on_sub_ids=("s0",),
            estimated_complexity="m", boundary_crossed=False,
        ),
        SubGoal(
            sub_goal_id="s2", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(),
            depends_on_sub_ids=("s1",),
            estimated_complexity="m", boundary_crossed=False,
        ),
    ]
    valid, order, depth = _topological_sort(subs)
    assert valid is True
    assert order == ("s0", "s1", "s2")
    assert depth == 2


def test_topo_sort_cycle():
    subs = [
        SubGoal(
            sub_goal_id="s0", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(),
            depends_on_sub_ids=("s1",),
            estimated_complexity="m", boundary_crossed=False,
        ),
        SubGoal(
            sub_goal_id="s1", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(),
            depends_on_sub_ids=("s0",),
            estimated_complexity="m", boundary_crossed=False,
        ),
    ]
    valid, order, depth = _topological_sort(subs)
    assert valid is False


def test_topo_sort_unknown_dep():
    subs = [
        SubGoal(
            sub_goal_id="s0", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.SEQUENTIAL,
            target_files=(),
            depends_on_sub_ids=("nonexistent",),
            estimated_complexity="m", boundary_crossed=False,
        ),
    ]
    valid, order, depth = _topological_sort(subs)
    assert valid is False


def test_topo_sort_duplicate_ids():
    subs = [
        SubGoal(
            sub_goal_id="s0", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.ATOMIC,
            target_files=(), depends_on_sub_ids=(),
            estimated_complexity="m", boundary_crossed=False,
        ),
        SubGoal(
            sub_goal_id="s0", parent_goal_id="p",
            title="t2", description="d",
            kind=SubGoalKind.ATOMIC,
            target_files=(), depends_on_sub_ids=(),
            estimated_complexity="m", boundary_crossed=False,
        ),
    ]
    valid, order, depth = _topological_sort(subs)
    assert valid is False


# decompose_goal


def test_decompose_none():
    verdict, plan, diag = decompose_goal(None)
    assert verdict is DecompositionVerdict.NO_GOAL


def test_decompose_empty_id():
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(goal_id="", title=""),
    )
    assert verdict is DecompositionVerdict.NO_GOAL


def test_decompose_valid():
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(
            goal_id="g1", title="test",
            target_files=("a.py",),
        ),
    )
    assert verdict is DecompositionVerdict.VALID
    assert plan is not None
    assert len(plan.sub_goals) >= 1


def test_decompose_too_many(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_SUB_GOALS, "2")
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(
            goal_id="g1", title="huge",
            target_files=("a.py", "b.py", "c.py", "d.py"),
        ),
    )
    assert verdict is DecompositionVerdict.TOO_COMPLEX


def test_decompose_too_deep(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_DAG_DEPTH, "1")
    # Custom decomposer producing a deep chain
    def _deep(goal):
        subs = []
        prev = ""
        for i in range(5):
            sid = f"chain-{i}"
            deps = (prev,) if prev else ()
            subs.append(SubGoal(
                sub_goal_id=sid, parent_goal_id="g1",
                title="t", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=(), depends_on_sub_ids=deps,
                estimated_complexity="m",
                boundary_crossed=False,
            ))
            prev = sid
        return tuple(subs)
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(goal_id="g1", title="t"),
        decomposer=_deep,
    )
    assert verdict is DecompositionVerdict.TOO_COMPLEX


def test_decompose_decomposer_raises():
    def _broken(goal):
        raise RuntimeError("broken")
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(goal_id="g1", title="t"),
        decomposer=_broken,
    )
    assert verdict is DecompositionVerdict.DECOMPOSITION_FAILED


def test_decompose_empty_decomposer_output():
    def _empty(goal):
        return ()
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(goal_id="g1", title="t"),
        decomposer=_empty,
    )
    assert verdict is DecompositionVerdict.DECOMPOSITION_FAILED


def test_decompose_cycle():
    def _cyclic(goal):
        return (
            SubGoal(
                sub_goal_id="a", parent_goal_id="g1",
                title="t", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=(),
                depends_on_sub_ids=("b",),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
            SubGoal(
                sub_goal_id="b", parent_goal_id="g1",
                title="t", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=(),
                depends_on_sub_ids=("a",),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
        )
    verdict, plan, diag = decompose_goal(
        _FakeRoadmapGoal(goal_id="g1", title="t"),
        decomposer=_cyclic,
    )
    assert verdict is DecompositionVerdict.DECOMPOSITION_FAILED


# Envelope construction


def test_envelope_for_sub_goal_valid():
    sub = SubGoal(
        sub_goal_id="s1", parent_goal_id="p",
        title="t", description="d",
        kind=SubGoalKind.ATOMIC,
        target_files=("x.py",),
        depends_on_sub_ids=(),
        estimated_complexity="moderate",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env is not None
    assert env.source == "roadmap"
    assert env.target_files == ("x.py",)
    ev = env.evidence
    assert ev["sub_goal_id"] == "s1"
    assert ev["parent_goal_id"] == "p"


def test_envelope_no_target_files_has_placeholder():
    sub = SubGoal(
        sub_goal_id="s1", parent_goal_id="p",
        title="t", description="d",
        kind=SubGoalKind.EXPLORATORY,
        target_files=(),
        depends_on_sub_ids=(),
        estimated_complexity="m",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(sub)
    # IntentEnvelope requires non-empty target_files for
    # non-vision sources; substrate inserts placeholder.
    assert env is not None
    assert env.target_files != ()


def test_envelope_sequential_urgency():
    sub = SubGoal(
        sub_goal_id="s1", parent_goal_id="p",
        title="t", description="d",
        kind=SubGoalKind.SEQUENTIAL,
        target_files=("x.py",),
        depends_on_sub_ids=(),
        estimated_complexity="m",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env.urgency == "high"


def test_envelope_exploratory_urgency():
    sub = SubGoal(
        sub_goal_id="s1", parent_goal_id="p",
        title="t", description="d",
        kind=SubGoalKind.EXPLORATORY,
        target_files=("x.py",),
        depends_on_sub_ids=(),
        estimated_complexity="m",
        boundary_crossed=False,
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env.urgency == "low"


# emit_sub_goal_envelopes


def test_emit_empty_plan():
    plan = DecomposedPlan(
        parent_goal_id="p", sub_goals=(),
        dag_valid=True, dag_depth=0,
        topological_order=(), diagnostic="",
    )
    outcomes = _run(emit_sub_goal_envelopes(plan))
    assert outcomes == ()


def test_emit_dry_run():
    plan = DecomposedPlan(
        parent_goal_id="p",
        sub_goals=(SubGoal(
            sub_goal_id="s1", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.ATOMIC,
            target_files=("x.py",),
            depends_on_sub_ids=(),
            estimated_complexity="m",
            boundary_crossed=False,
        ),),
        dag_valid=True, dag_depth=0,
        topological_order=("s1",), diagnostic="",
    )
    outcomes = _run(emit_sub_goal_envelopes(plan))
    assert len(outcomes) == 1
    assert outcomes[0].emitted is False
    assert "dry-run" in outcomes[0].error.lower()


def test_emit_with_mock_router():
    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return f"key-{env.signal_id}"
    router = _Mock()
    plan = DecomposedPlan(
        parent_goal_id="p",
        sub_goals=(
            SubGoal(
                sub_goal_id="s1", parent_goal_id="p",
                title="t", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=("x.py",),
                depends_on_sub_ids=(),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
            SubGoal(
                sub_goal_id="s2", parent_goal_id="p",
                title="t", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=("y.py",),
                depends_on_sub_ids=("s1",),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
        ),
        dag_valid=True, dag_depth=1,
        topological_order=("s1", "s2"), diagnostic="",
    )
    outcomes = _run(emit_sub_goal_envelopes(plan, router=router))
    assert len(outcomes) == 2
    assert all(o.emitted for o in outcomes)
    assert len(router.calls) == 2
    # Verify topological emit order
    sources_signal_ids = [
        getattr(c, "signal_id", "") for c in router.calls
    ]
    assert "s1" in sources_signal_ids[0]
    assert "s2" in sources_signal_ids[1]


def test_emit_router_exception():
    class _Broken:
        async def ingest(self, env):
            raise RuntimeError("ingest fail")
    plan = DecomposedPlan(
        parent_goal_id="p",
        sub_goals=(SubGoal(
            sub_goal_id="s1", parent_goal_id="p",
            title="t", description="d",
            kind=SubGoalKind.ATOMIC,
            target_files=("x.py",),
            depends_on_sub_ids=(),
            estimated_complexity="m",
            boundary_crossed=False,
        ),),
        dag_valid=True, dag_depth=0,
        topological_order=("s1",), diagnostic="",
    )
    outcomes = _run(emit_sub_goal_envelopes(plan, router=_Broken()))
    assert outcomes[0].emitted is False
    assert "ingest fail" in outcomes[0].error


# decompose_and_emit (top-level)


def test_decompose_and_emit_master_off():
    report = _run(decompose_and_emit(_FakeRoadmapGoal()))
    assert report.master_enabled is False
    assert report.verdict is DecompositionVerdict.NO_GOAL


def test_decompose_and_emit_valid(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "k"
    router = _Mock()
    report = _run(decompose_and_emit(
        _FakeRoadmapGoal(
            goal_id="g1", title="multi",
            target_files=("a.py", "b.py"),
        ),
        router=router,
    ))
    assert report.verdict is DecompositionVerdict.VALID
    assert len(router.calls) == 2
    assert len(report.emit_outcomes) == 2
    assert all(o.emitted for o in report.emit_outcomes)


def test_decompose_and_emit_no_goal_with_master(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(decompose_and_emit(None))
    assert report.verdict is DecompositionVerdict.NO_GOAL


# Sync wrapper


def test_sync_wrapper_outside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = decompose_and_emit_sync(
        _FakeRoadmapGoal(target_files=("x.py",)),
    )
    assert isinstance(report, DecompositionReport)


def test_sync_wrapper_inside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    async def inner():
        return decompose_and_emit_sync(_FakeRoadmapGoal())
    report = asyncio.run(inner())
    assert report.verdict is DecompositionVerdict.NO_GOAL
    assert "event loop" in report.diagnostic.lower()


# Completion tracking


def test_mark_sub_goal_status_empty_ids():
    assert mark_sub_goal_status(
        sub_goal_id="", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
    ) is None


def test_mark_sub_goal_status_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    rec = mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
        note="all tests passed",
    )
    assert isinstance(rec, CompletionRecord)
    assert ledger_path().exists()


def test_get_parent_progress_master_off():
    assert get_parent_progress("p") is None


def test_get_parent_progress_empty(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    # No ledger rows yet
    assert get_parent_progress("nonexistent") is None


def test_get_parent_progress_aggregates(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.PROPOSED,
    )
    mark_sub_goal_status(
        sub_goal_id="s2", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
    )
    mark_sub_goal_status(
        sub_goal_id="s3", parent_goal_id="p",
        status=CompletionStatus.FAILED,
    )
    progress = get_parent_progress("p")
    assert progress is not None
    assert progress.total_sub_goals == 3
    assert progress.completed_count == 1
    assert progress.proposed_count == 1
    assert progress.failed_count == 1
    assert progress.completion_ratio == pytest.approx(1/3)


def test_get_parent_progress_uses_latest_status(monkeypatch):
    """Append-only ledger; latest status for sub_goal_id wins."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.PROPOSED,
        now_unix=1.0,
    )
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.IN_PROGRESS,
        now_unix=2.0,
    )
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
        now_unix=3.0,
    )
    progress = get_parent_progress("p")
    assert progress.total_sub_goals == 1
    assert progress.completed_count == 1
    assert progress.proposed_count == 0


def test_get_parent_progress_filters_by_parent(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="parent-a",
        status=CompletionStatus.COMPLETED,
    )
    mark_sub_goal_status(
        sub_goal_id="s2", parent_goal_id="parent-b",
        status=CompletionStatus.COMPLETED,
    )
    progress = get_parent_progress("parent-a")
    assert progress.total_sub_goals == 1


# Persistence


def test_persist_valid_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    _run(decompose_and_emit(
        _FakeRoadmapGoal(
            goal_id="g1", title="t",
            target_files=("x.py",),
        ),
    ))
    assert ledger_path().exists()


def test_persist_no_goal_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    _run(decompose_and_emit(None))
    # NO_GOAL verdict skips persistence
    assert not ledger_path().exists()


def test_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    _run(decompose_and_emit(
        _FakeRoadmapGoal(target_files=("x.py",)),
    ))
    assert not ledger_path().exists()


# Renderer


def test_format_master_off():
    out = format_decomposition_panel()
    assert "disabled" in out


def test_format_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(decompose_and_emit(
        _FakeRoadmapGoal(
            goal_id="g1", title="test",
            target_files=("x.py",),
        ),
    ))
    out = format_decomposition_panel(report)
    assert "Goal Decomposition" in out


def test_format_with_progress(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    mark_sub_goal_status(
        sub_goal_id="s1", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
    )
    progress = get_parent_progress("p")
    out = format_decomposition_panel(progress=progress)
    assert "Progress" in out


# to_dict


def test_sub_goal_to_dict():
    s = SubGoal(
        sub_goal_id="s", parent_goal_id="p",
        title="t", description="d",
        kind=SubGoalKind.PARALLEL,
        target_files=("x.py",), depends_on_sub_ids=(),
        estimated_complexity="m", boundary_crossed=False,
    )
    d = s.to_dict()
    assert d["schema_version"] == GOAL_DECOMPOSITION_SCHEMA_VERSION
    assert d["kind"] == "parallel"


def test_plan_to_dict():
    p = DecomposedPlan(
        parent_goal_id="p", sub_goals=(),
        dag_valid=True, dag_depth=0,
        topological_order=(), diagnostic="",
    )
    d = p.to_dict()
    assert d["schema_version"] == GOAL_DECOMPOSITION_SCHEMA_VERSION


def test_completion_record_to_dict():
    r = CompletionRecord(
        sub_goal_id="s", parent_goal_id="p",
        status=CompletionStatus.COMPLETED,
        note="ok", transitioned_at_unix=1.0,
    )
    d = r.to_dict()
    assert d["kind"] == "completion"
    assert d["status"] == "completed"


def test_progress_to_dict():
    p = ParentProgress(
        parent_goal_id="p", total_sub_goals=5,
        proposed_count=1, in_progress_count=1,
        completed_count=3, failed_count=0,
        completion_ratio=0.6,
    )
    d = p.to_dict()
    assert d["completion_ratio"] == 0.6


def test_report_to_dict():
    r = DecompositionReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=DecompositionVerdict.VALID, plan=None,
        emit_outcomes=(), diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == GOAL_DECOMPOSITION_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "goal_decomposition_planner.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 6


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "kind_taxonomy_closed",
        "status_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_authority_forbids_plan_generator():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.plan_generator "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_forbids_tool_executor():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.tool_executor "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 6


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# SSE


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert ios.EVENT_TYPE_GOAL_DECOMPOSED == "goal_decomposed"
    assert "goal_decomposed" in ios._VALID_EVENT_TYPES


# End-to-end with RoadmapReader integration (smoke)


def test_end_to_end_from_roadmap_goal(monkeypatch):
    """RoadmapGoal → decomposed → envelopes flow through the
    canonical path. Verifies the substrate plugs into the
    actual roadmap_reader.RoadmapGoal artifact."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    from backend.core.ouroboros.governance.roadmap_reader import (
        GoalPriority,
        RoadmapGoal,
    )
    goal = RoadmapGoal(
        goal_id="real-g1",
        title="Implement feature X",
        description=(
            "Steps:\n"
            "1. Write the data structure\n"
            "2. Add the API endpoint\n"
            "3. Add the tests\n"
        ),
        priority=GoalPriority.HIGH,
        target_files=("backend/foo.py",),
        success_criteria="all tests pass",
        depends_on=(),
        max_duration_s=3600,
    )

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "ok"
    router = _Mock()
    report = _run(decompose_and_emit(goal, router=router))
    assert report.verdict is DecompositionVerdict.VALID
    # 3 enumerated steps → 3 sub-goals → 3 envelopes
    assert len(router.calls) == 3

"""Regression spine for §41.4 Phase 1 fourth arc — Multi-step Plan Orchestrator."""
from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    multi_step_orchestrator as mso,
)
from backend.core.ouroboros.governance.multi_step_orchestrator import (
    MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION,
    OrchestrationEmitOutcome,
    OrchestrationReport,
    OrchestrationVerdict,
    SubGoalRunRecord,
    SubGoalRunState,
    _ENV_COMPLETION_LEDGER_PATH,
    _ENV_ENVELOPE_SOURCE,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_EMITS_PER_TICK,
    _ENV_PERSIST,
    _ENV_REPO_NAME,
    _load_completion_status,
    _make_envelope_for_sub_goal,
    advance_orchestration,
    advance_orchestration_sync,
    completion_ledger_path,
    compute_ready_set,
    compute_run_state,
    envelope_source,
    format_orchestration_panel,
    is_plan_completed,
    is_plan_stalled,
    ledger_path,
    master_enabled,
    max_emits_per_tick,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    repo_name,
    run_state_glyph,
    verdict_glyph,
)


@dataclass
class _FakeSubGoal:
    sub_goal_id: str
    parent_goal_id: str = "parent-1"
    title: str = "title"
    description: str = "desc"
    target_files: Tuple[str, ...] = field(default_factory=tuple)
    depends_on_sub_ids: Tuple[str, ...] = field(default_factory=tuple)
    estimated_complexity: str = "moderate"
    boundary_crossed: bool = False
    kind_value: str = "atomic"

    @property
    def kind(self):
        return self  # duck-type — has .value

    @property
    def value(self):
        return self.kind_value


@dataclass
class _FakePlan:
    parent_goal_id: str = "parent-1"
    sub_goals: Tuple[Any, ...] = field(default_factory=tuple)
    topological_order: Tuple[str, ...] = field(default_factory=tuple)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_MAX_EMITS_PER_TICK,
        _ENV_COMPLETION_LEDGER_PATH, _ENV_LEDGER_PATH,
        _ENV_REPO_NAME, _ENV_ENVELOPE_SOURCE,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_COMPLETION_LEDGER_PATH,
        str(tmp_path / "completion.jsonl"),
    )
    monkeypatch.setenv(
        _ENV_LEDGER_PATH,
        str(tmp_path / "orch.jsonl"),
    )
    yield


def _run(coro):
    return asyncio.run(coro)


# Defaults


def test_schema():
    assert MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION == "multi_step_orchestrator.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_max_emits_per_tick_default():
    assert max_emits_per_tick() == 5


def test_repo_name_default():
    assert repo_name() == "jarvis"


def test_envelope_source_default():
    assert envelope_source() == "roadmap"


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in OrchestrationVerdict} == {
        "no_plan", "progressing", "stalled", "completed",
    }


def test_run_state_taxonomy_closed():
    assert {s.value for s in SubGoalRunState} == {
        "blocked", "ready", "emitted", "done",
    }


@pytest.mark.parametrize("v", list(OrchestrationVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(SubGoalRunState))
def test_run_state_glyph(s):
    assert run_state_glyph(s) != "?"


# compute_run_state


def test_run_state_no_deps_no_status_is_ready():
    sub = _FakeSubGoal(sub_goal_id="s1")
    state, unmet = compute_run_state(sub, {})
    assert state is SubGoalRunState.READY
    assert unmet == ()


def test_run_state_with_unmet_dep_is_blocked():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        depends_on_sub_ids=("dep1",),
    )
    state, unmet = compute_run_state(sub, {})
    assert state is SubGoalRunState.BLOCKED
    assert "dep1" in unmet


def test_run_state_with_completed_dep_is_ready():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        depends_on_sub_ids=("dep1",),
    )
    state, unmet = compute_run_state(
        sub, {"dep1": "completed"},
    )
    assert state is SubGoalRunState.READY


def test_run_state_with_failed_dep_is_blocked():
    """Failed dep means we can never become ready."""
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        depends_on_sub_ids=("dep1",),
    )
    state, unmet = compute_run_state(
        sub, {"dep1": "failed"},
    )
    assert state is SubGoalRunState.BLOCKED
    assert "dep1" in unmet


def test_run_state_own_completed_is_done():
    sub = _FakeSubGoal(sub_goal_id="s1")
    state, _ = compute_run_state(sub, {"s1": "completed"})
    assert state is SubGoalRunState.DONE


def test_run_state_own_failed_is_done():
    sub = _FakeSubGoal(sub_goal_id="s1")
    state, _ = compute_run_state(sub, {"s1": "failed"})
    assert state is SubGoalRunState.DONE


def test_run_state_own_in_progress_is_emitted():
    sub = _FakeSubGoal(sub_goal_id="s1")
    state, _ = compute_run_state(sub, {"s1": "in_progress"})
    assert state is SubGoalRunState.EMITTED


def test_run_state_own_proposed_is_emitted():
    sub = _FakeSubGoal(sub_goal_id="s1")
    state, _ = compute_run_state(sub, {"s1": "proposed"})
    assert state is SubGoalRunState.EMITTED


def test_run_state_empty_sub_goal_is_blocked():
    state, _ = compute_run_state(_FakeSubGoal(sub_goal_id=""), {})
    assert state is SubGoalRunState.BLOCKED


# compute_ready_set


def test_ready_set_empty_plan():
    plan = _FakePlan(sub_goals=())
    assert compute_ready_set(plan, {}) == ()


def test_ready_set_all_ready():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
            _FakeSubGoal(sub_goal_id="c"),
        ),
        topological_order=("a", "b", "c"),
    )
    assert compute_ready_set(plan, {}) == ("a", "b", "c")


def test_ready_set_respects_topological_order():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="c"),
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
        topological_order=("a", "b", "c"),
    )
    assert compute_ready_set(plan, {}) == ("a", "b", "c")


def test_ready_set_excludes_blocked():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(
                sub_goal_id="b", depends_on_sub_ids=("a",),
            ),
        ),
        topological_order=("a", "b"),
    )
    assert compute_ready_set(plan, {}) == ("a",)


def test_ready_set_unblocks_after_dep_complete():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(
                sub_goal_id="b", depends_on_sub_ids=("a",),
            ),
        ),
        topological_order=("a", "b"),
    )
    assert compute_ready_set(
        plan, {"a": "completed"},
    ) == ("b",)


def test_ready_set_excludes_emitted():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
        topological_order=("a", "b"),
    )
    assert compute_ready_set(
        plan, {"a": "in_progress"},
    ) == ("b",)


def test_ready_set_excludes_done():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
        topological_order=("a", "b"),
    )
    assert compute_ready_set(
        plan, {"a": "completed"},
    ) == ("b",)


# is_plan_completed


def test_completed_empty_is_false():
    assert is_plan_completed(_FakePlan(), {}) is False


def test_completed_all_done():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
    )
    assert is_plan_completed(
        plan, {"a": "completed", "b": "completed"},
    ) is True


def test_completed_one_missing_is_false():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
    )
    assert is_plan_completed(plan, {"a": "completed"}) is False


def test_completed_one_failed_is_false():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
        ),
    )
    assert is_plan_completed(plan, {"a": "failed"}) is False


# is_plan_stalled


def test_stalled_empty_is_false():
    assert is_plan_stalled(_FakePlan(), {}) is False


def test_stalled_with_failed_dep():
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(
                sub_goal_id="b", depends_on_sub_ids=("a",),
            ),
        ),
    )
    # a is failed → b is blocked by failed dep → stalled
    assert is_plan_stalled(plan, {"a": "failed"}) is True


def test_stalled_with_ready_set_is_false():
    plan = _FakePlan(
        sub_goals=(_FakeSubGoal(sub_goal_id="a"),),
    )
    assert is_plan_stalled(plan, {}) is False


# Envelope construction


def test_envelope_for_sub_goal_basic():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        parent_goal_id="p",
        target_files=("x.py",),
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env is not None
    assert env.source == "roadmap"
    assert env.target_files == ("x.py",)
    ev = env.evidence
    assert ev["sub_goal_id"] == "s1"
    assert ev["multi_step_orchestrated"] is True


def test_envelope_no_target_files_has_placeholder():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        target_files=(),
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env is not None
    assert env.target_files != ()


def test_envelope_sequential_urgency_high():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        target_files=("x.py",),
        kind_value="sequential",
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env.urgency == "high"


def test_envelope_exploratory_urgency_low():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        target_files=("x.py",),
        kind_value="exploratory",
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env.urgency == "low"


def test_envelope_boundary_crossed_high_urgency():
    sub = _FakeSubGoal(
        sub_goal_id="s1",
        target_files=("x.py",),
        kind_value="atomic",
        boundary_crossed=True,
    )
    env = _make_envelope_for_sub_goal(sub)
    assert env.urgency == "high"


# Completion ledger reading


def test_load_completion_status_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(
        _ENV_COMPLETION_LEDGER_PATH,
        str(tmp_path / "absent.jsonl"),
    )
    assert _load_completion_status("p") == {}


def test_load_completion_status_filters_parent(monkeypatch, tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        "\n".join([
            json.dumps({
                "kind": "completion",
                "sub_goal_id": "s1",
                "parent_goal_id": "p1",
                "status": "completed",
            }),
            json.dumps({
                "kind": "completion",
                "sub_goal_id": "s2",
                "parent_goal_id": "p2",
                "status": "completed",
            }),
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_COMPLETION_LEDGER_PATH, str(target))
    out = _load_completion_status("p1")
    assert out == {"s1": "completed"}


def test_load_completion_status_latest_wins(monkeypatch, tmp_path):
    """Append-only: latest status for sub_goal_id wins."""
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        "\n".join([
            json.dumps({
                "kind": "completion",
                "sub_goal_id": "s1",
                "parent_goal_id": "p1",
                "status": "proposed",
            }),
            json.dumps({
                "kind": "completion",
                "sub_goal_id": "s1",
                "parent_goal_id": "p1",
                "status": "completed",
            }),
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_COMPLETION_LEDGER_PATH, str(target))
    out = _load_completion_status("p1")
    assert out == {"s1": "completed"}


def test_load_completion_status_skips_malformed(monkeypatch, tmp_path):
    target = tmp_path / "ledger.jsonl"
    target.write_text(
        "\n".join([
            "{not json}",
            json.dumps({
                "kind": "completion",
                "sub_goal_id": "s1",
                "parent_goal_id": "p1",
                "status": "completed",
            }),
            "[]",  # not a dict
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_COMPLETION_LEDGER_PATH, str(target))
    out = _load_completion_status("p1")
    assert out == {"s1": "completed"}


# advance_orchestration


def test_advance_master_off():
    report = _run(advance_orchestration(_FakePlan()))
    assert report.master_enabled is False
    assert report.verdict is OrchestrationVerdict.NO_PLAN


def test_advance_empty_plan(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = _run(advance_orchestration(_FakePlan()))
    assert report.verdict is OrchestrationVerdict.NO_PLAN


def test_advance_all_ready_dry_run(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
            _FakeSubGoal(
                sub_goal_id="b", target_files=("y.py",),
            ),
        ),
        topological_order=("a", "b"),
    )
    report = _run(advance_orchestration(
        plan, completion_status_override={},
    ))
    assert report.verdict is OrchestrationVerdict.PROGRESSING
    # Dry run — no router → no emits
    assert all(
        not o.emitted for o in report.emit_outcomes
    )


def test_advance_emits_ready_via_mock_router(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return f"key-{env.signal_id}"
    router = _Mock()
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
            _FakeSubGoal(
                sub_goal_id="b", target_files=("y.py",),
                depends_on_sub_ids=("a",),
            ),
        ),
        topological_order=("a", "b"),
    )
    # Initial tick: only a is ready (b waits on a)
    report = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={},
    ))
    assert report.verdict is OrchestrationVerdict.PROGRESSING
    assert len(router.calls) == 1  # only a emitted
    # Find the emitted outcome
    emitted = [o for o in report.emit_outcomes if o.emitted]
    assert emitted[0].sub_goal_id == "a"


def test_advance_subsequent_tick_unblocks_dependent(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "k"
    router = _Mock()
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
            _FakeSubGoal(
                sub_goal_id="b", target_files=("y.py",),
                depends_on_sub_ids=("a",),
            ),
        ),
        topological_order=("a", "b"),
    )
    # Mark a completed; b should now be ready
    report = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={"a": "completed"},
    ))
    assert len(router.calls) == 1
    emitted = [o for o in report.emit_outcomes if o.emitted]
    assert emitted[0].sub_goal_id == "b"


def test_advance_completed_plan_verdict(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(sub_goal_id="b"),
        ),
        topological_order=("a", "b"),
    )
    report = _run(advance_orchestration(
        plan,
        completion_status_override={
            "a": "completed", "b": "completed",
        },
    ))
    assert report.verdict is OrchestrationVerdict.COMPLETED
    assert report.completion_ratio == 1.0


def test_advance_stalled_verdict_failed_dep(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(sub_goal_id="a"),
            _FakeSubGoal(
                sub_goal_id="b", depends_on_sub_ids=("a",),
            ),
        ),
        topological_order=("a", "b"),
    )
    report = _run(advance_orchestration(
        plan,
        completion_status_override={"a": "failed"},
    ))
    assert report.verdict is OrchestrationVerdict.STALLED


def test_advance_respects_max_emits_per_tick(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MAX_EMITS_PER_TICK, "2")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "k"
    router = _Mock()
    plan = _FakePlan(
        sub_goals=tuple(
            _FakeSubGoal(
                sub_goal_id=f"s{i}",
                target_files=(f"x{i}.py",),
            )
            for i in range(5)
        ),
        topological_order=tuple(f"s{i}" for i in range(5)),
    )
    report = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={},
    ))
    # Cap = 2, so only 2 envelopes submitted this tick
    assert len(router.calls) == 2


def test_advance_router_exception_caught(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    class _Broken:
        async def ingest(self, env):
            raise RuntimeError("ingest fail")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
        ),
        topological_order=("a",),
    )
    report = _run(advance_orchestration(
        plan, router=_Broken(),
        completion_status_override={},
    ))
    assert report.emit_outcomes[0].emitted is False
    assert "ingest fail" in report.emit_outcomes[0].error


def test_advance_idempotent_emitted_not_reemitted(monkeypatch):
    """Calling advance twice when no status change → no
    re-emit of EMITTED sub-goals (they show as emitted in
    run_records but no new envelope is dispatched)."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "k"
    router = _Mock()
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
        ),
        topological_order=("a",),
    )
    # First tick: emit
    _run(advance_orchestration(
        plan, router=router,
        completion_status_override={},
    ))
    # Second tick: a is now "proposed" (just emitted)
    _run(advance_orchestration(
        plan, router=router,
        completion_status_override={"a": "proposed"},
    ))
    # Total calls should be 1 (idempotent)
    assert len(router.calls) == 1


# Sync wrapper


def test_sync_wrapper_outside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = advance_orchestration_sync(_FakePlan())
    assert isinstance(report, OrchestrationReport)


def test_sync_wrapper_inside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    async def inner():
        return advance_orchestration_sync(_FakePlan())
    report = asyncio.run(inner())
    assert report.verdict is OrchestrationVerdict.NO_PLAN
    assert "event loop" in report.diagnostic.lower()


# Persistence


def test_persist_progressing_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
        ),
        topological_order=("a",),
    )
    _run(advance_orchestration(
        plan, completion_status_override={},
    ))
    assert ledger_path().exists()


def test_persist_no_plan_skips(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    _run(advance_orchestration(_FakePlan()))
    assert not ledger_path().exists()


def test_persist_disabled(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
        ),
        topological_order=("a",),
    )
    _run(advance_orchestration(
        plan, completion_status_override={},
    ))
    assert not ledger_path().exists()


# Renderer


def test_format_master_off():
    out = format_orchestration_panel()
    assert "disabled" in out


def test_format_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    plan = _FakePlan(
        sub_goals=(
            _FakeSubGoal(
                sub_goal_id="a", target_files=("x.py",),
            ),
        ),
        topological_order=("a",),
    )
    report = _run(advance_orchestration(
        plan, completion_status_override={},
    ))
    out = format_orchestration_panel(report)
    assert "Multi-step Orchestrator" in out


# to_dict


def test_run_record_to_dict():
    r = SubGoalRunRecord(
        sub_goal_id="s1",
        run_state=SubGoalRunState.READY,
        completion_status="",
        unmet_deps=(),
        emitted_at_unix=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION
    assert d["run_state"] == "ready"


def test_emit_outcome_to_dict():
    o = OrchestrationEmitOutcome(
        sub_goal_id="s1", emitted=True,
        idempotency_key="k", error="",
    )
    d = o.to_dict()
    assert d["kind"] == "emit"


def test_report_to_dict():
    r = OrchestrationReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=OrchestrationVerdict.PROGRESSING,
        parent_goal_id="p", total_sub_goals=1,
        blocked_count=0, ready_count=1, emitted_count=0,
        done_count=0, failed_count=0, completion_ratio=0.0,
        emit_outcomes=(), run_records=(),
        diagnostic="", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == MULTI_STEP_ORCHESTRATOR_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/"
        "multi_step_orchestrator.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "run_state_taxonomy_closed",
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


def test_pin_authority_forbids_orchestrator():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_forbids_roadmap_reader():
    """Upstream substrate — must not be imported (this
    substrate is downstream of roadmap_reader)."""
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.roadmap_reader "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(ast.parse("# x\n"), "# x\n")


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 7


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
    assert (
        ios.EVENT_TYPE_MULTI_STEP_ORCHESTRATED
        == "multi_step_orchestrated"
    )
    assert "multi_step_orchestrated" in ios._VALID_EVENT_TYPES


# End-to-end with REAL goal_decomposition_planner


def test_end_to_end_with_real_decomposed_plan(monkeypatch, tmp_path):
    """Compose real DecomposedPlan from goal_decomposition_planner
    → flow into orchestrator → emit phased."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    from backend.core.ouroboros.governance.goal_decomposition_planner import (
        SubGoal as RealSubGoal,
        SubGoalKind,
        DecomposedPlan as RealDecomposedPlan,
    )
    plan = RealDecomposedPlan(
        parent_goal_id="real-p",
        sub_goals=(
            RealSubGoal(
                sub_goal_id="real-a",
                parent_goal_id="real-p",
                title="A", description="d",
                kind=SubGoalKind.ATOMIC,
                target_files=("x.py",),
                depends_on_sub_ids=(),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
            RealSubGoal(
                sub_goal_id="real-b",
                parent_goal_id="real-p",
                title="B", description="d",
                kind=SubGoalKind.SEQUENTIAL,
                target_files=("y.py",),
                depends_on_sub_ids=("real-a",),
                estimated_complexity="m",
                boundary_crossed=False,
            ),
        ),
        dag_valid=True, dag_depth=1,
        topological_order=("real-a", "real-b"),
        diagnostic="",
    )

    class _Mock:
        def __init__(self):
            self.calls = []
        async def ingest(self, env):
            self.calls.append(env)
            return "k"
    router = _Mock()

    # First tick: only real-a is ready
    report1 = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={},
    ))
    assert len(router.calls) == 1
    assert report1.verdict is OrchestrationVerdict.PROGRESSING

    # Second tick: real-a completed → real-b ready
    report2 = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={"real-a": "completed"},
    ))
    assert len(router.calls) == 2  # one new
    assert report2.verdict is OrchestrationVerdict.PROGRESSING

    # Third tick: both completed
    report3 = _run(advance_orchestration(
        plan, router=router,
        completion_status_override={
            "real-a": "completed",
            "real-b": "completed",
        },
    ))
    assert report3.verdict is OrchestrationVerdict.COMPLETED
    assert report3.completion_ratio == 1.0

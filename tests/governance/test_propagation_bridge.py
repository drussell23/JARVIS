"""Sovereign State-Propagation Bridge — the forward-progress gate must read the
GROUND-TRUTH dispatch count (emitted_this_tick), not the lagging emitted_count
aggregate, so a freshly-decomposed-and-dispatched GOAL is never false-DLQ'd."""
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.governance import multi_step_orchestrator as mso
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    SubGoal, SubGoalKind, DecomposedPlan,
)


def _sub(sid: str, deps=()):
    return SubGoal(
        sub_goal_id=sid, parent_goal_id="parent", title=sid, description="do " + sid,
        kind=SubGoalKind.ATOMIC, target_files=("f.py",), depends_on_sub_ids=tuple(deps),
        estimated_complexity="moderate", boundary_crossed=False, scoped_symbols=(),
    )


def _plan(*subs):
    return DecomposedPlan(
        parent_goal_id="parent", sub_goals=tuple(subs), dag_valid=True, dag_depth=1,
        topological_order=tuple(s.sub_goal_id for s in subs),
        diagnostic="test",
    )


class _OkRouter:
    def __init__(self):
        self.ingested = []

    async def ingest(self, env):
        self.ingested.append(env)
        return "idem-key"


class _FailRouter:
    async def ingest(self, env):
        raise RuntimeError("ingest exploded")


# -- made_forward_progress predicate ---------------------------------------- #
def _report(**over):
    base = dict(
        evaluated_at_unix=0.0, master_enabled=True,
        verdict=mso.OrchestrationVerdict.PROGRESSING, parent_goal_id="p",
        total_sub_goals=2, blocked_count=1, ready_count=1, emitted_count=0,
        done_count=0, failed_count=0, completion_ratio=0.0,
        emit_outcomes=(), run_records=(), diagnostic="", elapsed_s=0.0,
    )
    base.update(over)
    return mso.OrchestrationReport(**base)


def test_forward_progress_true_on_dispatch_this_tick():
    # THE bug: aggregate emitted_count=0 (lagging ledger) but a sub-goal WAS
    # dispatched this tick -> must read as forward progress.
    assert _report(emitted_count=0, emitted_this_tick=1).made_forward_progress is True


def test_forward_progress_true_on_inflight_aggregate():
    assert _report(emitted_count=2, emitted_this_tick=0).made_forward_progress is True


def test_forward_progress_false_when_nothing_moved():
    assert _report(emitted_count=0, emitted_this_tick=0).made_forward_progress is False


def test_report_to_dict_exposes_new_fields():
    d = _report(emitted_count=0, emitted_this_tick=1).to_dict()
    assert d["emitted_this_tick"] == 1 and d["made_forward_progress"] is True


# -- advance_orchestration populates the ground truth (reproduces the bug) ---- #
@pytest.mark.asyncio
async def test_fresh_decompose_reports_dispatch_despite_zero_aggregate(monkeypatch):
    monkeypatch.setattr(mso, "master_enabled", lambda: True)
    monkeypatch.setattr(mso, "_make_envelope_for_sub_goal",
                        lambda s: types.SimpleNamespace(idempotency_key="k"))
    monkeypatch.setattr(mso, "_mark_emitted_via_goal_decomposition",
                        lambda **kw: None)
    router = _OkRouter()
    # completion_status empty -> the just-emitted READY sub-goal cannot show as
    # EMITTED in this tick's aggregate (the structural lag this fix addresses).
    report = await mso.advance_orchestration(
        _plan(_sub("s1")), router=router, completion_status_override={},
    )
    assert len(router.ingested) == 1           # actually dispatched
    assert report.emitted_count == 0           # aggregate lags (the trap)
    assert report.emitted_this_tick == 1       # ground truth
    assert report.made_forward_progress is True  # -> gate passes, no false-DLQ


@pytest.mark.asyncio
async def test_real_drop_is_loud_not_silent(monkeypatch, caplog):
    monkeypatch.setattr(mso, "master_enabled", lambda: True)
    monkeypatch.setattr(mso, "_make_envelope_for_sub_goal",
                        lambda s: types.SimpleNamespace(idempotency_key="k"))
    import logging
    with caplog.at_level(logging.CRITICAL):
        report = await mso.advance_orchestration(
            _plan(_sub("s1")), router=_FailRouter(), completion_status_override={},
        )
    assert report.emitted_this_tick == 0
    assert report.made_forward_progress is False    # a genuine drop
    assert any("[SovereignPropagation] REAL DROP" in r.getMessage() for r in caplog.records)

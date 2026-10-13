"""Operator-first priority: the operator's own ops outrank all autonomous work.

Found live (2026-10-13): a /session apply op queued 18th in the background
pool. /goal and /session apply must submit ``source="roadmap"`` (classify
re-derives the operator's authority from the signed roadmap), so the source
alone could not tell them from the Sentinel's self-signed dispatches -- which
share that source AND the same injector. The operator paths now mark their
goals (``OPERATOR_ORIGIN_KEY``) and one predicate (``is_sovereign``) decides
human primacy for intake order, pool order, preemption and the lane.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import background_agent_pool as bap
from backend.core.ouroboros.governance import generation_admission as ga
from backend.core.ouroboros.governance import preemption as pre
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool, BackgroundOp,
)
from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance.intake.intent_envelope import (
    OPERATOR_ORIGIN_KEY, context_is_sovereign, is_sovereign, make_envelope,
)
from backend.core.ouroboros.governance.op_context import OperationContext


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_HUMAN_PREEMPTION_ENABLED", raising=False)
    pre.reset_preemptions()
    ga.reset_for_tests()
    yield
    pre.reset_preemptions()
    ga.reset_for_tests()


def _ctx(desc, *, operator=False, op_id=None):
    ev = {"goal_id": f"g-{desc}"}
    if operator:
        ev[OPERATOR_ORIGIN_KEY] = True
    c = OperationContext.create(
        target_files=("backend/x.py",), description=desc, signal_source="roadmap",
        intake_evidence_json=json.dumps(ev),
    )
    return dataclasses.replace(c, op_id=op_id) if op_id else c


def _running(pool, desc, started_at, *, operator=False):
    ctx = _ctx(desc, operator=operator, op_id=f"op-{desc}")
    op = BackgroundOp(op_id=f"bg-{desc}", goal=desc, context=ctx, status="running")
    op.started_at = started_at
    pool._ops[op.op_id] = op
    return ctx.op_id


# ---- the predicate ----------------------------------------------------------------

def test_an_operator_sanctioned_roadmap_goal_is_sovereign_and_a_self_signed_one_is_not():
    assert is_sovereign("roadmap", {OPERATOR_ORIGIN_KEY: True})
    assert not is_sovereign("roadmap", {"operator_goal": True})   # the Sentinel's shape
    assert not is_sovereign("roadmap", {OPERATOR_ORIGIN_KEY: "true"})  # only a real True
    assert is_sovereign("operator_chat", None)
    assert context_is_sovereign(_ctx("a", operator=True))
    assert not context_is_sovereign(_ctx("b"))


def test_intake_ranks_the_operators_goal_above_a_resurrected_survivor():
    def env(evidence):
        return make_envelope(source="roadmap", description="x", target_files=("a.py",),
                             repo="jarvis", confidence=0.95, urgency="high",
                             evidence=evidence, requires_human_ack=False)
    mine, _ = uir._compute_priority(env({OPERATOR_ORIGIN_KEY: True}))
    survivor, _ = uir._compute_priority(env({}), resurrected=True)
    autonomous, _ = uir._compute_priority(env({"operator_goal": True}))
    assert mine == uir._sovereign_human_priority() < survivor < autonomous


async def test_the_pool_runs_the_operators_op_first():
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
    pool._running = True
    try:
        await pool.submit(_ctx("autonomous-1"))
        await pool.submit(_ctx("autonomous-2"))
        await pool.submit(_ctx("MINE", operator=True))
        order = []
        while not pool._queue.empty():
            _prio, _seq, op = pool._queue.get_nowait()
            order.append(op.context.description)
    finally:
        pool._running = False
    assert order[0] == "MINE"


# ---- yielding a lane -----------------------------------------------------------------

async def test_with_every_lane_busy_the_youngest_autonomous_op_is_asked_to_yield():
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=2, queue_size=16)
    older = _running(pool, "older", 1.0)
    younger = _running(pool, "younger", 2.0)
    pool._running = True
    try:
        bg = await pool.submit(_ctx("MINE", operator=True))
    finally:
        pool._running = False
    assert pre.is_preemption_requested(younger) and not pre.is_preemption_requested(older)
    assert pool._yield_for == {bg: younger}


async def test_a_free_lane_means_nobody_yields():
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=3, queue_size=16)
    a = _running(pool, "a", 1.0)
    b = _running(pool, "b", 2.0)
    pool._running = True
    try:
        await pool.submit(_ctx("MINE", operator=True))
    finally:
        pool._running = False
    assert not pre.is_preemption_requested(a) and not pre.is_preemption_requested(b)


async def test_an_operator_op_never_yields_to_another_and_one_op_is_asked_once():
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=2, queue_size=16)
    mine_running = _running(pool, "mine-running", 5.0, operator=True)
    auto = _running(pool, "auto", 1.0)
    pool._running = True
    try:
        await pool.submit(_ctx("MINE-2", operator=True))
        await pool.submit(_ctx("MINE-3", operator=True))
    finally:
        pool._running = False
    assert not pre.is_preemption_requested(mine_running)
    assert list(pool._yield_for.values()) == [auto]       # asked once, not twice


async def test_the_request_is_withdrawn_when_a_lane_frees_first():
    pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
    victim = _running(pool, "victim", 1.0)
    pool._running = True
    try:
        bg = await pool.submit(_ctx("MINE", operator=True))
    finally:
        pool._running = False
    assert pre.is_preemption_requested(victim)
    pool._withdraw_yield(bg)                     # the operator op got a lane elsewhere
    assert not pre.is_preemption_requested(victim) and not pool._yield_for


# ---- the lane: generation priority ----------------------------------------------------

async def test_the_operators_op_holds_a_foreground_claim_on_the_local_lane():
    seen = {}

    class _Orch:
        async def run(self, ctx):
            seen[ctx.description] = (ga.current_priority(), ga.admission().snapshot()["turns"])
            return "ok"

    assert await bap._run_with_priority(_Orch(), _ctx("MINE", operator=True)) == "ok"
    assert await bap._run_with_priority(_Orch(), _ctx("auto")) == "ok"
    assert seen["MINE"] == (ga.FOREGROUND, 1)
    assert seen["auto"] == (ga.BACKGROUND, 0)
    assert ga.admission().snapshot()["turns"] == 0      # released after the op

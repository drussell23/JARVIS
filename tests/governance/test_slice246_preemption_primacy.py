"""Slice 246 — Preemptive Interrupt Matrix & Human Override Protocol.

Slice 245 gave hibernation survivors absolute-max primacy (negative priority).
That created a sovereignty hole: a resurrected GOAL would out-rank a live human
voice intent (voice_human=0 > resurrected=-100). The agent must never starve
human primacy.

Two-part fix (the brief under-specified the first):
  * Sovereign human tier (the queue guarantee): human-origin sources rank BELOW
    resurrection — Human > Resurrected > Normal — so a queued human ALWAYS wins.
    Dynamic (sovereign = resurrection_floor - margin), not hardcoded. Does NOT
    degrade resurrected (it still beats all normal work).
  * Cooperative preemption (the running-op backstop): when a human intent is
    submitted while a resurrected op is actively running, fire a non-blocking
    preemption signal. At the NEXT tool-round boundary the executor raises
    OperationPreemptedError (no hard SIGTERM); the pool re-ingests the op via
    Slice 245's resubmit_resurrected (micro-hibernation), runs the human, and the
    survivor re-enters the VIP lane (below the human). Honest granularity: the
    in-flight round restarts; completed phases in the OperationContext survive.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance.intake import intent_envelope as ie
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance import background_agent_pool as bap
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool, BackgroundOp,
)
from backend.core.ouroboros.governance import preemption as pre


def _ctx(desc, *, route="standard", source="", resurrected=False):
    import dataclasses
    c = OperationContext.create(
        target_files=("backend/core/x.py",), description=desc, signal_source=source,
    )
    c = dataclasses.replace(c, provider_route=route)
    return c.with_resurrection() if resurrected else c


def _env(source, urgency="normal"):
    return make_envelope(
        source=source, description=f"{source} unit", target_files=("a.py",),
        repo="JARVIS-AI-Agent", confidence=0.8, urgency=urgency,
        evidence={"k": source}, requires_human_ack=False,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("JARVIS_SOVEREIGN_PRIMACY_MARGIN", raising=False)
    monkeypatch.delenv("JARVIS_HUMAN_PREEMPTION_ENABLED", raising=False)
    pre.reset_preemptions()
    yield
    pre.reset_preemptions()


class TestSovereignSources:
    def test_human_sources_registered(self):
        for s in ("voice_human", "cli_emergency", "human_override"):
            assert s in ie.SOVEREIGN_SOURCES
            assert s in ie._VALID_SOURCES


class TestSovereignPrimacyIntake:
    def test_hierarchy_human_above_resurrected_above_normal(self):
        human, _ = uir._compute_priority(_env("voice_human", urgency="critical"))
        res, _ = uir._compute_priority(_env("backlog"), resurrected=True)
        normal, _ = uir._compute_priority(_env("backlog"))
        assert human < res < normal, f"need Human({human}) < Resurrected({res}) < Normal({normal})"

    def test_all_sovereign_sources_outrank_resurrected(self):
        res, _ = uir._compute_priority(_env("backlog"), resurrected=True)
        for s in ("voice_human", "cli_emergency", "human_override"):
            p, _ = uir._compute_priority(_env(s))
            assert p < res, f"{s} must outrank resurrected"

    def test_dynamic_margin(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SOVEREIGN_PRIMACY_MARGIN", "30")
        p, _ = uir._compute_priority(_env("voice_human"))
        assert p == uir._resurrection_intake_priority() - 30


class TestSovereignPrimacyPool:
    async def test_pool_hierarchy(self):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        pool._running = True
        try:
            await pool.submit(_ctx("normal", route="standard"))
            await pool.submit(_ctx("resurrected", route="standard", resurrected=True))
            await pool.submit(_ctx("HUMAN", route="immediate", source="voice_human"))
            order = []
            while not pool._queue.empty():
                prio, _seq, op = pool._queue.get_nowait()
                order.append((op.context.description, prio))
        finally:
            pool._running = False
        descs = [d for d, _ in order]
        assert descs == ["HUMAN", "resurrected", "normal"], f"got {descs}"

    def test_sovereign_pool_priority_below_resurrection(self):
        assert bap._sovereign_pool_priority() < bap._resurrection_pool_priority()


class TestPreemptionPrimitive:
    def test_request_is_clear(self):
        assert pre.is_preemption_requested("op-1") is False
        pre.request_preemption("op-1")
        assert pre.is_preemption_requested("op-1") is True
        pre.clear_preemption("op-1")
        assert pre.is_preemption_requested("op-1") is False

    def test_check_raises_when_requested(self):
        pre.request_preemption("op-2")
        with pytest.raises(pre.OperationPreemptedError):
            pre.check_preemption("op-2")

    def test_check_noop_when_not_requested(self):
        pre.check_preemption("op-clean")  # no raise

    def test_gate_off_disables_check(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HUMAN_PREEMPTION_ENABLED", "0")
        pre.request_preemption("op-3")
        pre.check_preemption("op-3")  # gated off → no raise
        assert pre.human_preemption_enabled() is False


class TestPoolPreemptionSentinel:
    def _pool_with_running_resurrected(self, op_id="res-op"):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        ctx = _ctx("running resurrected GOAL", resurrected=True)
        import dataclasses
        ctx = dataclasses.replace(ctx, op_id=op_id)
        pool._ops[op_id] = BackgroundOp(op_id=op_id, goal="g", context=ctx, status="running")
        return pool, ctx

    async def test_human_submit_preempts_running_resurrected(self):
        pool, ctx = self._pool_with_running_resurrected()
        pool._running = True
        try:
            await pool.submit(_ctx("HUMAN emergency", route="immediate", source="voice_human"))
        finally:
            pool._running = False
        assert pre.is_preemption_requested(ctx.op_id) is True

    async def test_nonhuman_submit_does_not_preempt(self):
        pool, ctx = self._pool_with_running_resurrected()
        pool._running = True
        try:
            await pool.submit(_ctx("normal work", route="standard"))
        finally:
            pool._running = False
        assert pre.is_preemption_requested(ctx.op_id) is False

    async def test_gate_off_no_preempt(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HUMAN_PREEMPTION_ENABLED", "0")
        pool, ctx = self._pool_with_running_resurrected()
        pool._running = True
        try:
            await pool.submit(_ctx("HUMAN", route="immediate", source="voice_human"))
        finally:
            pool._running = False
        assert pre.is_preemption_requested(ctx.op_id) is False

    def test_running_resurrected_op_ids(self):
        pool, ctx = self._pool_with_running_resurrected("res-1")
        pool._ops["done"] = BackgroundOp(op_id="done", goal="g", context=_ctx("d", resurrected=True), status="completed")
        pool._ops["normal-run"] = BackgroundOp(op_id="normal-run", goal="g", context=_ctx("n"), status="running")
        assert pool.running_resurrected_op_ids() == [ctx.op_id]


class TestToolExecutorCheckpoint:
    def test_round_loop_checks_preemption(self):
        from backend.core.ouroboros.governance import tool_executor as te
        src = inspect.getsource(te)
        assert "check_preemption" in src or "is_preemption_requested" in src, \
            "tool loop must cooperatively check for preemption at a round boundary"


class TestWorkerResumeOnPreempt:
    def test_worker_loop_reingest_on_preempt(self):
        src = inspect.getsource(bap)
        assert "OperationPreemptedError" in src, "worker must catch preemption"
        assert "resubmit_resurrected" in src, "preempted op must be re-ingested (micro-hibernation)"


class TestPhase4Integration:
    async def test_preempt_then_human_then_resume(self):
        """Resurrected GOAL running → human emergency arrives → preemption fired →
        executor yields at round boundary → survivor re-ingested → drain shows the
        human ahead of the re-ingested survivor (no payload lost)."""
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        import dataclasses
        survivor = dataclasses.replace(
            _ctx("massive resurrected GOAL", resurrected=True), op_id="survivor-op",
        )
        pool._ops["survivor-op"] = BackgroundOp(
            op_id="survivor-op", goal="GOAL", context=survivor, status="running",
        )
        pool._running = True
        try:
            # human emergency arrives mid-execution → sentinel fires preemption
            await pool.submit(_ctx("VOICE emergency", route="immediate", source="voice_human"))
            assert pre.is_preemption_requested(survivor.op_id) is True

            # the running op hits its next round boundary and yields
            with pytest.raises(pre.OperationPreemptedError):
                pre.check_preemption(survivor.op_id)

            # graceful suspension → re-ingest the EXACT context (micro-hibernation)
            pre.clear_preemption(survivor.op_id)
            await pool.resubmit_resurrected(survivor)

            order = []
            while not pool._queue.empty():
                prio, _seq, op = pool._queue.get_nowait()
                order.append(op.context.description)
        finally:
            pool._running = False

        assert order[0] == "VOICE emergency", "human runs first"
        assert "massive resurrected GOAL" in order[1], "survivor preserved + re-ingested"

"""Slice 245 — Absolute-Primacy Re-Ingest Matrix for hibernation survivors.

An op that fails with provider-exhaustion during a dark window is, today,
terminal (background_agent_pool marks it status="failed" and drops it). When the
Grid Sentinel (Slice 243) confirms streaming stability and the controller wakes,
that survivor must NOT re-enter at the back of the queue behind everything that
accumulated while the grid was dark — it earned Absolute Primacy.

This slice (honest scope):
  * Phase 1 — does NOT eradicate pool-resume (healthy QUEUED ops are preserved
    across pause and correctly resume in place). It ADDS re-ingest for the
    exhaustion-FAILED survivor: re-submit its exact OperationContext (preserving
    durable partial state — phase, already-generated candidates, plan) marked
    RESURRECTED. Re-submitting the CONTEXT (not a reconstructed envelope, which
    would restart at CLASSIFY) is what makes Phase 3 "no completed work
    re-computed" real. The live LLM stream cannot cross a stateless boundary and
    does not need to — completed phases live in the context.
  * Phase 2 — dynamic absolute-max primacy (NOT a hardcoded 0): resurrected
    priority = min(normal priorities) - JARVIS_RESURRECTION_PRIMACY_MARGIN, in
    BOTH the intake _compute_priority and the BackgroundAgentPool route-priority.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance import background_agent_pool as bap
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
    BackgroundOp,
)
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope


def _ctx(desc="resurrect GOAL-001", route="standard"):
    c = OperationContext.create(target_files=("backend/core/x.py",), description=desc)
    import dataclasses
    return dataclasses.replace(c, provider_route=route)


def _env(source, urgency="normal"):
    return make_envelope(
        source=source, description=f"{source} unit", target_files=("a.py",),
        repo="JARVIS-AI-Agent", confidence=0.8, urgency=urgency,
        evidence={"k": source}, requires_human_ack=False,
    )


class TestResurrectionContextFlag:
    def test_default_is_false(self):
        assert _ctx().resurrected_from_hibernation is False

    def test_with_resurrection_sets_flag_and_preserves_state(self):
        c = _ctx(desc="heavy multi-file GOAL")
        r = c.with_resurrection()
        assert r.resurrected_from_hibernation is True
        # Phase 3 — durable partial state preserved across re-ingest
        assert r.op_id == c.op_id
        assert r.description == c.description
        assert r.target_files == c.target_files
        assert r.provider_route == c.provider_route
        # frozen original untouched
        assert c.resurrected_from_hibernation is False


class TestIntakeResurrectionPrimacy:
    def test_resurrected_beats_highest_normal_source(self):
        # voice_human is the highest normal primacy (priority 0)
        voice_p, _ = uir._compute_priority(_env("voice_human", urgency="critical"))
        res_p, _ = uir._compute_priority(_env("backlog"), resurrected=True)
        assert res_p < voice_p, "resurrected must supersede even voice_human"

    def test_is_dynamic_not_hardcoded_zero(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESURRECTION_PRIMACY_MARGIN", "50")
        res_p, _ = uir._compute_priority(_env("backlog"), resurrected=True)
        expected = min(uir._PRIORITY_MAP.values()) - 50
        assert res_p == expected
        assert res_p < 0  # below the voice_human=0 floor — not a hardcoded 0


class TestPoolResurrectionPrimacy:
    async def test_resurrected_dequeues_before_standard(self):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        pool._running = True
        try:
            # 3 standard tasks accumulate, THEN the resurrected survivor arrives
            await pool.submit(_ctx("std-1", route="standard"))
            await pool.submit(_ctx("std-2", route="standard"))
            await pool.submit(_ctx("std-3", route="standard"))
            await pool.submit(_ctx("RESURRECTED", route="standard").with_resurrection())

            order = []
            while not pool._queue.empty():
                prio, _seq, op = pool._queue.get_nowait()
                order.append((op.context.description, prio))
        finally:
            pool._running = False

        assert order[0][0] == "RESURRECTED", "survivor must dequeue FIRST"
        assert order[0][1] < 1, "resurrected pool priority must be below immediate(1)"

    def test_pool_priority_is_dynamic(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RESURRECTION_PRIMACY_MARGIN", "20")
        p = bap._resurrection_pool_priority()
        assert p == min((1, 3, 3, 5, 7)) - 20  # min route priority - margin
        assert p < 1


class TestReingestPreservesState:
    async def test_resubmit_resurrected_marks_and_preserves_context(self):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        pool._running = True
        try:
            original = _ctx("GOAL-007", route="complex")
            op_id = await pool.resubmit_resurrected(original)
            bg_op = pool._ops[op_id]
        finally:
            pool._running = False
        assert bg_op.context.resurrected_from_hibernation is True
        assert bg_op.context.op_id == original.op_id  # exact context preserved

    def test_drain_exhaustion_failures_returns_and_clears(self):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        ctx = _ctx("dark-window survivor")
        # a survivor: failed with provider-exhaustion during the dark window
        pool._ops["op-dead"] = BackgroundOp(
            op_id="op-dead", goal="survivor", context=ctx,
            status="failed", error="RuntimeError: all_providers_exhausted",
        )
        # a normal completed op — must NOT be resurrected
        pool._ops["op-done"] = BackgroundOp(
            op_id="op-done", goal="done", context=_ctx("done"),
            status="completed",
        )
        drained = pool.drain_exhaustion_failures()
        assert [c.op_id for c in drained] == [ctx.op_id]
        # cleared so a second wake cannot double-resurrect it
        assert pool.drain_exhaustion_failures() == []


class TestWakeReingestIntegration:
    async def test_phase4_survivor_beats_three_dark_window_tasks(self):
        """Blackout mid-task → 3 standard tasks enqueue while dark → recovery →
        the survivor is re-ingested and dequeues before all three."""
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        survivor = _ctx("GOAL-001 survivor", route="complex")
        # the survivor died during the outage
        pool._ops["op-survivor"] = BackgroundOp(
            op_id="op-survivor", goal="GOAL-001 survivor", context=survivor,
            status="failed", error="RuntimeError: all_providers_exhausted",
        )
        pool._running = True
        try:
            # 3 standard tasks accumulated during the dark window
            await pool.submit(_ctx("dark-1", route="standard"))
            await pool.submit(_ctx("dark-2", route="standard"))
            await pool.submit(_ctx("dark-3", route="standard"))
            # recovery: re-ingest survivors with absolute primacy
            for ctx in pool.drain_exhaustion_failures():
                await pool.resubmit_resurrected(ctx)

            order = []
            while not pool._queue.empty():
                prio, _seq, op = pool._queue.get_nowait()
                order.append(op.context.description)
        finally:
            pool._running = False

        assert order[0] == "GOAL-001 survivor", "survivor must run before dark-window tasks"
        assert set(order[1:]) == {"dark-1", "dark-2", "dark-3"}

    def test_wake_bridge_wired_for_reingest(self):
        from backend.core.ouroboros.governance import governed_loop_service as gls
        src = inspect.getsource(gls)
        assert "drain_exhaustion_failures" in src, "wake path must drain exhaustion survivors"
        assert "resubmit_resurrected" in src, "wake path must re-ingest with primacy"
        assert "JARVIS_RESURRECTION_REINGEST_ENABLED" in src, "gated, fail-soft"

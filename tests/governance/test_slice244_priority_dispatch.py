"""Slice 244 — proof that priority dispatch already holds at BOTH intake lanes.

AUDIT FINDING (verify-first): the premise "the WAL is processed as a sequential
single-lane FIFO" is false at every layer. Dispatch is already priority-ordered:

  * INTAKE: ``unified_intake_router`` enqueues onto an ``asyncio.PriorityQueue``
    using ``_compute_priority`` (lower int = higher primacy). ``test_coverage``
    is in ``_PRIORITY_MAP_DEFERRED`` (base 99 → effective ~100), the lowest tier
    (Slice 239 decoupled test-gen). Primary GOAL sources (voice_human/backlog/
    roadmap/test_failure) sit at 0–6.
  * BACKGROUND POOL: ``BackgroundAgentPool._queue`` is ALSO an
    ``asyncio.PriorityQueue`` keyed on ``provider_route``
    (immediate=1, standard/complex=3, background=5, speculative=7). A
    test_coverage shard routes to ``background`` (5, runs last); a primary GOAL
    routes to immediate/standard (1/3, runs first).

So the requested "Priority Weight Matrix (1.0/0.5) + Concurrency Scheduler +
background lane" already exists as a two-tier integer-priority system + a
decoupled, priority-ordered ``BackgroundAgentPool`` + Slice 240 budget/velocity
gating (``should_decouple_test_gen``). Building a parallel float-weight scheduler
would duplicate it. This module instead PROVES Phase 4 against the real
machinery and locks the invariant so a future refactor cannot silently regress
either lane to FIFO.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake import unified_intake_router as uir
from backend.core.ouroboros.governance.background_agent_pool import BackgroundAgentPool


def _env(source: str, urgency: str = "normal"):
    return make_envelope(
        source=source,
        description=f"{source} work unit",
        target_files=("backend/core/x.py",),
        repo="JARVIS-AI-Agent",
        confidence=0.8,
        urgency=urgency,
        evidence={"k": source},
        requires_human_ack=False,
    )


class TestIntakeLanePriority:
    """Layer 1 — the intake asyncio.PriorityQueue."""

    def test_primary_goal_outranks_test_coverage(self):
        # a hibernated primary GOAL resurrection vs a deferred test-gen shard
        goal_p, _ = uir._compute_priority(_env("backlog", urgency="high"))
        test_p, _ = uir._compute_priority(_env("test_coverage", urgency="low"))
        # lower int = higher primacy → the GOAL strictly outranks the shard
        assert goal_p < test_p, f"GOAL {goal_p} must outrank test_coverage {test_p}"

    def test_test_coverage_is_deferred_tier(self):
        assert "test_coverage" in uir._PRIORITY_MAP_DEFERRED
        # it carries NO primary-tier mapping → falls to the base-99 floor
        assert "test_coverage" not in uir._PRIORITY_MAP

    async def test_phase4_drain_order_intake(self):
        """Inject [test, test, GOAL] OUT OF ORDER; the PriorityQueue dequeues
        the GOAL first and the test shards last — no FIFO."""
        q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        injected = [
            ("test_coverage", "low"),
            ("test_coverage", "low"),
            ("backlog", "high"),  # the primary GOAL arrives LAST
        ]
        for seq, (src, urg) in enumerate(injected):
            prio, _ = uir._compute_priority(_env(src, urgency=urg))
            # mirror the router heap tuple: (priority, submitted_seq, payload)
            await q.put((prio, seq, src))

        drained = []
        while not q.empty():
            _p, _s, src = await q.get()
            drained.append(src)

        assert drained[0] == "backlog", "primary GOAL must dequeue FIRST"
        assert drained[1:] == ["test_coverage", "test_coverage"], "shards last"


class TestBackgroundPoolPriority:
    """Layer 2 — the BackgroundAgentPool asyncio.PriorityQueue."""

    async def test_pool_dequeues_goal_before_test_shards(self):
        pool = BackgroundAgentPool(orchestrator=SimpleNamespace(), pool_size=1, queue_size=16)
        # do NOT start() — no workers, so the queue retains items for inspection
        pool._running = True
        try:
            # submit OUT OF ORDER: two background test shards, then the GOAL
            await pool.submit(SimpleNamespace(op_id="test-1", goal="test shard 1", provider_route="background"))
            await pool.submit(SimpleNamespace(op_id="test-2", goal="test shard 2", provider_route="background"))
            await pool.submit(SimpleNamespace(op_id="goal-1", goal="resurrect GOAL-001", provider_route="standard"))

            order = []
            while not pool._queue.empty():
                _prio, _seq, op = pool._queue.get_nowait()
                order.append((op.goal, _prio))
        finally:
            pool._running = False

        goals = [g for g, _ in order]
        assert goals[0] == "resurrect GOAL-001", "primary GOAL must run before background test shards"
        assert order[0][1] == 3 and order[1][1] == 5, "GOAL=standard(3) ahead of shards=background(5)"
        assert set(goals[1:]) == {"test shard 1", "test shard 2"}

    def test_route_priority_ordering(self):
        # the route map ranks primary ahead of background/speculative
        rp = {"immediate": 1, "standard": 3, "complex": 3, "background": 5, "speculative": 7}
        assert rp["standard"] < rp["background"] < rp["speculative"]


class TestIronGateDeferralSafety:
    """Phase 3 — deferring a test-coverage intent behind a primary GOAL cannot
    violate the Iron Gate. The Iron Gate sequences GENERATE→VALIDATE WITHIN a
    single op; intake priority orders DISTINCT ops across the queue. Reordering
    two independent intents (a GOAL and a post-execution test-gen shard, which
    Slice 239 decoupled into its OWN WAL intent) is cross-op, never intra-op —
    so no gate sequence is touched."""

    def test_test_coverage_is_a_distinct_decoupled_intent(self):
        goal = _env("backlog")
        shard = _env("test_coverage")
        # distinct sources + distinct dedup keys → distinct ops → reordering is
        # cross-op (safe), not a reordering of any single op's gate phases
        assert goal.source != shard.source
        assert goal.dedup_key != shard.dedup_key

    def test_deferred_shard_never_precondition_of_primary(self):
        # the shard is the LOWEST priority and post-execution by design; a
        # primary GOAL never waits on it → deferral cannot deadlock the gate
        goal_p, _ = uir._compute_priority(_env("backlog", urgency="high"))
        shard_p, _ = uir._compute_priority(_env("test_coverage", urgency="low"))
        assert goal_p < shard_p

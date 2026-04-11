"""Unit tests for MinerGraphCoalescer (Phase 2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import ExecutionGraph
from backend.core.ouroboros.governance.graph_coalescer import (
    CoalescedBatch,
    MinerGraphCoalescer,
)


@dataclass
class _FakeAnalysis:
    file_path: str
    composite_score: float = 0.5


def _analyses(n: int) -> List[_FakeAnalysis]:
    return [
        _FakeAnalysis(file_path=f"backend/core/ouroboros/f{i}.py", composite_score=0.3 + i * 0.05)
        for i in range(n)
    ]


class _StubScheduler:
    def __init__(self, accept: bool = True, raise_: bool = False) -> None:
        self.accept = accept
        self.raise_ = raise_
        self.submitted: List[ExecutionGraph] = []

    async def submit(self, graph: ExecutionGraph) -> bool:
        if self.raise_:
            raise RuntimeError("boom")
        self.submitted.append(graph)
        return self.accept


# ---------------------------------------------------------------------------
# should_coalesce
# ---------------------------------------------------------------------------


class TestShouldCoalesce:
    def test_disabled_never_coalesces(self) -> None:
        c = MinerGraphCoalescer(enabled=False)
        assert c.should_coalesce(_analyses(5)) is False

    def test_single_candidate_not_coalesced(self) -> None:
        c = MinerGraphCoalescer(enabled=True, min_units=2)
        assert c.should_coalesce(_analyses(1)) is False

    def test_min_units_threshold(self) -> None:
        c = MinerGraphCoalescer(enabled=True, min_units=3)
        assert c.should_coalesce(_analyses(2)) is False
        assert c.should_coalesce(_analyses(3)) is True

    def test_default_min_units_is_two(self) -> None:
        c = MinerGraphCoalescer(enabled=True)
        assert c.should_coalesce(_analyses(2)) is True


# ---------------------------------------------------------------------------
# coalesce() returns CoalescedBatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCoalesceOutput:
    async def test_returns_none_when_below_min(self) -> None:
        c = MinerGraphCoalescer(min_units=3)
        result = await c.coalesce(_analyses(1), strategy="complexity")
        assert result is None

    async def test_builds_one_unit_per_file(self) -> None:
        c = MinerGraphCoalescer(max_units=16)
        result = await c.coalesce(_analyses(5), strategy="complexity")
        assert isinstance(result, CoalescedBatch)
        assert len(result.graph.units) == 5
        assert len(result.target_files) == 5

    async def test_respects_max_units(self) -> None:
        c = MinerGraphCoalescer(max_units=3)
        result = await c.coalesce(_analyses(10), strategy="complexity")
        assert result is not None
        assert len(result.graph.units) == 3

    async def test_concurrency_limit_capped_to_unit_count(self) -> None:
        c = MinerGraphCoalescer(concurrency_limit=8)
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.graph.concurrency_limit == 3

    async def test_concurrency_limit_honored(self) -> None:
        c = MinerGraphCoalescer(concurrency_limit=2)
        result = await c.coalesce(_analyses(10), strategy="complexity")
        assert result is not None
        assert result.graph.concurrency_limit == 2

    async def test_unit_ids_unique(self) -> None:
        c = MinerGraphCoalescer()
        result = await c.coalesce(_analyses(5), strategy="complexity")
        assert result is not None
        ids = [u.unit_id for u in result.graph.units]
        assert len(set(ids)) == len(ids)

    async def test_evidence_payload_shape(self) -> None:
        c = MinerGraphCoalescer()
        result = await c.coalesce(_analyses(4), strategy="long_functions")
        assert result is not None
        ev = result.envelope_evidence
        assert ev["coalesced_graph"] is True
        assert ev["unit_count"] == 4
        assert ev["strategy"] == "long_functions"
        assert "plan_digest" in ev
        assert len(ev["unit_specs"]) == 4

    async def test_confidence_is_average(self) -> None:
        c = MinerGraphCoalescer()
        result = await c.coalesce(_analyses(4), strategy="complexity")
        assert result is not None
        # Scores: 0.3, 0.35, 0.4, 0.45 → avg 0.375
        assert abs(result.confidence - 0.375) < 0.01

    async def test_empty_file_paths_dropped(self) -> None:
        c = MinerGraphCoalescer()
        bad = [_FakeAnalysis(file_path="", composite_score=0.5), *_analyses(3)]
        result = await c.coalesce(bad, strategy="complexity")
        assert result is not None
        # Only 3 valid analyses
        assert len(result.graph.units) == 3


# ---------------------------------------------------------------------------
# Scheduler submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSchedulerSubmission:
    async def test_auto_submit_false_skips_scheduler(self) -> None:
        sched = _StubScheduler()
        c = MinerGraphCoalescer(scheduler=sched, auto_submit=False)
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.submitted_to_scheduler is False
        assert len(sched.submitted) == 0

    async def test_auto_submit_true_submits_once(self) -> None:
        sched = _StubScheduler(accept=True)
        c = MinerGraphCoalescer(scheduler=sched, auto_submit=True)
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.submitted_to_scheduler is True
        assert len(sched.submitted) == 1
        assert sched.submitted[0].graph_id == result.graph.graph_id

    async def test_scheduler_rejection_recorded(self) -> None:
        sched = _StubScheduler(accept=False)
        c = MinerGraphCoalescer(scheduler=sched, auto_submit=True)
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.submitted_to_scheduler is False

    async def test_scheduler_exception_swallowed(self) -> None:
        sched = _StubScheduler(raise_=True)
        c = MinerGraphCoalescer(scheduler=sched, auto_submit=True)
        # Should not raise — exception is caught.
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.submitted_to_scheduler is False

    async def test_no_scheduler_with_auto_submit_noop(self) -> None:
        c = MinerGraphCoalescer(scheduler=None, auto_submit=True)
        result = await c.coalesce(_analyses(3), strategy="complexity")
        assert result is not None
        assert result.submitted_to_scheduler is False

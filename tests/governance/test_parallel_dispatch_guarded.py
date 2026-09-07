"""The fan-out boundary fails CLOSED.

``enforce_evaluate_fanout`` keeps its loud contract (unexpected exceptions
propagate — see test_parallel_dispatch_enforce.py). The FSM calls the GUARDED
wrapper, which turns every such fault into one deterministic outcome —
WARNING with traceback, graphs collapsed to CANCELLED, lesson recorded,
``FanoutOutcome.CRASHED`` — so an op proceeds on the legacy serial path
instead of aborting, and a timed-out graph never outlives the decision made
about it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutOutcome,
    enforce_evaluate_fanout_guarded,
)
from backend.core.ouroboros.governance.posture import Posture


@dataclass
class _FakeGeneration:
    candidates: Tuple[Dict[str, Any], ...] = ()


def _multi_file_candidates(n: int = 3) -> Tuple[Dict[str, Any], ...]:
    return ({
        "files": [
            {"file_path": f"pkg/mod_{i}.py", "full_content": f"# module {i}\npass\n", "rationale": f"unit {i}"}
            for i in range(n)
        ],
    },)


def _ok_gate() -> MemoryPressureGate:
    gate = MagicMock(spec=MemoryPressureGate)

    def _cf(n: int) -> MemoryFanoutDecision:
        return MemoryFanoutDecision(
            allowed=True, n_requested=n, n_allowed=n, level=PressureLevel.OK,
            free_pct=60.0, reason_code="mock_ok", source="test",
        )

    gate.can_fanout.side_effect = _cf
    return gate


def _posture() -> Tuple[Optional[Posture], Optional[float]]:
    return Posture.MAINTAIN, 0.9


class _Scheduler:
    def __init__(
        self, *, submit_raises: Optional[BaseException] = None,
        wait_raises: Optional[BaseException] = None, wait_delay_s: float = 0.0,
        cancel_raises: bool = False,
    ) -> None:
        self.submit_raises, self.wait_raises, self.wait_delay_s = submit_raises, wait_raises, wait_delay_s
        self.cancel_raises = cancel_raises
        self.submitted: List[ExecutionGraph] = []
        self.cancel_calls: List[str] = []

    async def submit(self, graph: ExecutionGraph) -> bool:
        self.submitted.append(graph)
        if self.submit_raises is not None:
            raise self.submit_raises
        return True

    async def wait_for_graph(self, graph_id: str, timeout_s: Optional[float] = None) -> GraphExecutionState:
        if self.wait_delay_s:
            # the real scheduler bounds the wait with asyncio.wait_for(timeout_s)
            if timeout_s is not None and self.wait_delay_s > timeout_s:
                await asyncio.sleep(timeout_s)
                raise asyncio.TimeoutError()
            await asyncio.sleep(self.wait_delay_s)
        if self.wait_raises is not None:
            raise self.wait_raises
        g = self.submitted[-1]
        return GraphExecutionState(
            graph=g, phase=GraphExecutionPhase.COMPLETED,
            completed_units=tuple(u.unit_id for u in g.units),
        )

    async def cancel_graphs_for_op(self, op_id: str) -> int:
        self.cancel_calls.append(op_id)
        if self.cancel_raises:
            raise RuntimeError("cannot cancel")
        return 2


@pytest.fixture
def enforce_on(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    monkeypatch.setenv("JARVIS_SUBAGENT_DIAGNOSTICS_ENABLED", "1")


async def _run(op_id: str, sched: _Scheduler, n: int = 3):
    return await enforce_evaluate_fanout_guarded(
        op_id=op_id, generation=_FakeGeneration(candidates=_multi_file_candidates(n)),
        scheduler=sched, gate=_ok_gate(), posture_fn=_posture,
    )


@pytest.mark.asyncio
async def test_completed_passes_through_unchanged(enforce_on) -> None:
    sched = _Scheduler()
    res = await _run("op-ok", sched)
    assert res.outcome is FanoutOutcome.COMPLETED
    assert sched.cancel_calls == []


@pytest.mark.asyncio
async def test_submit_crash_becomes_crashed_outcome_not_exception(enforce_on, caplog, monkeypatch) -> None:
    recorded: List[dict] = []

    async def _rec(**kw: Any) -> str:
        recorded.append(kw)
        return "recorded"

    from backend.core.ouroboros.governance import subagent_diagnostic_interceptor as sdi
    monkeypatch.setattr(sdi, "_record", lambda kw, r: _rec(**kw))
    sched = _Scheduler(submit_raises=ValueError("graph validator: cycle detected"))
    with caplog.at_level(logging.WARNING):
        res = await _run("op-crash", sched)
    assert res.outcome is FanoutOutcome.CRASHED
    assert res.skip_reason == "unexpected_exception"
    assert "ValueError: graph validator: cycle detected" in res.error
    assert "collapsed 2 graph(s)" in res.error
    assert sched.cancel_calls == ["op-crash"]
    warn = [r for r in caplog.records if "enforce_crashed" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING and warn[0].exc_info
    assert recorded and recorded[0]["error_class"] == "fanout_crash"
    assert recorded[0]["target_files"] == ("pkg/mod_0.py", "pkg/mod_1.py", "pkg/mod_2.py")


@pytest.mark.asyncio
async def test_wait_crash_collapses_even_when_cancel_degrades(enforce_on, caplog) -> None:
    sched = _Scheduler(wait_raises=RuntimeError("scheduler wedged"), cancel_raises=True)
    with caplog.at_level(logging.WARNING):
        res = await _run("op-wedge", sched, 2)
    assert res.outcome is FanoutOutcome.CRASHED
    assert "RuntimeError: scheduler wedged" in res.error
    assert "collapsed" not in res.error  # cancel degraded → nothing claimed
    assert any("collapse_degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cancelled_error_is_never_swallowed(enforce_on) -> None:
    sched = _Scheduler(wait_raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _run("op-cancel", sched, 2)
    assert sched.cancel_calls == []


@pytest.mark.asyncio
async def test_timeout_collapses_the_graph_and_keeps_its_outcome(enforce_on, monkeypatch, caplog) -> None:
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", "0.05")
    sched = _Scheduler(wait_delay_s=0.5)
    with caplog.at_level(logging.WARNING):
        res = await _run("op-slow", sched, 2)
    assert res.outcome is FanoutOutcome.TIMEOUT
    assert sched.cancel_calls == ["op-slow"]
    assert any("enforce_timeout_collapsed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_scheduler_without_cancel_api_still_returns_crashed(enforce_on) -> None:
    class _Bare(_Scheduler):
        cancel_graphs_for_op = None  # type: ignore[assignment]

    res = await _run("op-bare", _Bare(submit_raises=KeyError("unit_map")), 2)
    assert res.outcome is FanoutOutcome.CRASHED
    assert "KeyError" in res.error


def test_crashed_is_distinct_and_stable() -> None:
    """Every consumer branches on ``!= COMPLETED``; the string is grep-stable."""
    assert FanoutOutcome.CRASHED is not FanoutOutcome.COMPLETED
    assert FanoutOutcome("crashed") is FanoutOutcome.CRASHED

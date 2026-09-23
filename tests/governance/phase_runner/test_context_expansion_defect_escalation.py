"""A code defect in CONTEXT_EXPANSION ends the op instead of generating blind.

bt-2026-09-21..23: ``index_age_s`` on the isolated Oracle raised an
AttributeError on 113 of 113 ops; each was a WARNING and every op went to
GENERATE unexpanded. Transient faults still degrade, and every ending settles
the reachability ledger so a streak raises a HEALTH ALARM.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runners import context_expansion_runner as CER
from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
    ContextExpansionRunner,
)
from tests.governance.phase_runner.test_context_expansion_runner_parity import (
    _FakeConfig, _FakeOrchestrator, _FakeStack, _ctx_at_ctx_phase,
)


@dataclass
class _Orch(_FakeOrchestrator):
    def __post_init__(self):
        self.ledger = []

    async def _record_ledger(self, ctx, state, data):
        self.ledger.append((state.name, data))


@pytest.fixture
def settles(monkeypatch):
    got = []

    async def _settle(op_id, exc):
        got.append(None if exc is None else type(exc).__name__)

    monkeypatch.setattr(CER, "settle_expansion", _settle)
    return got


async def _run(tmp_path, raises=None):
    orch = _Orch(_stack=_FakeStack(), _config=_FakeConfig(project_root=tmp_path))

    async def _expand(ctx, deadline):
        if raises is not None:
            raise raises
        return ctx

    with patch("backend.core.ouroboros.governance.orchestrator.ContextExpander") as MockExp:
        inst = MagicMock()
        inst.expand = AsyncMock(side_effect=_expand)
        MockExp.return_value = inst
        result = await ContextExpansionRunner(orch, None).run(_ctx_at_ctx_phase(tmp_path))
    return orch, result


async def test_the_measured_defect_ends_the_op(tmp_path, settles, caplog):
    exc = AttributeError("'index_age_s' is not available on the process-isolated Oracle")
    orch, result = await _run(tmp_path, exc)
    assert result.status == "fail" and result.next_phase is None
    assert result.reason == "context_expansion_defect"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert orch.ledger == [("FAILED", {"reason": "context_expansion_defect",
                                       "detail": f"AttributeError: {exc}"})]
    assert settles == ["AttributeError"]
    assert any(r.levelname == "ERROR" and "context_expansion_defect" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), OSError("ipc reset"), RuntimeError("x")])
async def test_a_transient_fault_degrades_loudly(tmp_path, settles, caplog, exc):
    _orch, result = await _run(tmp_path, exc)
    assert result.status == "ok" and result.next_phase is OperationPhase.PLAN
    assert settles == [type(exc).__name__]
    assert any(r.levelname == "ERROR" and "transient" in r.getMessage() for r in caplog.records)


async def test_success_settles_healthy(tmp_path, settles):
    _orch, result = await _run(tmp_path)
    assert result.status == "ok" and settles == [None]


async def test_the_switch_makes_defects_degrade(tmp_path, settles, monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_EXPANSION_DEFECTS_FATAL", "false")
    _orch, result = await _run(tmp_path, TypeError("'bool' object is not callable"))
    assert result.status == "ok" and result.next_phase is OperationPhase.PLAN


async def test_a_defect_streak_raises_the_health_alarm(monkeypatch, caplog):
    from backend.core.ouroboros.governance import reachability_ledger as RL
    monkeypatch.setenv("JARVIS_REACHABILITY_FAILING_STREAK", "3")
    book = RL.ReachabilityLedger()   # in-memory: no path
    monkeypatch.setattr(RL, "default_ledger", lambda: book)
    for _ in range(3):
        await CER.settle_expansion("op", AttributeError("index_age_s"))
    assert any("HEALTH ALARM" in r.getMessage() and "context_expansion" in r.getMessage()
               for r in caplog.records)

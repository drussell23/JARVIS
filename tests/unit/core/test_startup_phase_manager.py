"""tests/unit/core/test_startup_phase_manager.py — Diseases 1+3 phase manager tests."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.startup_phase_manager import (
    ComponentResult,
    PhaseConfig,
    PhasePolicy,
    PhaseResult,
    StartupPhaseManager,
    TaskOutcome,
)


async def _ok():
    """Coroutine that succeeds instantly."""


async def _fail():
    raise RuntimeError("init failed")


async def _slow(seconds: float = 99.0):
    await asyncio.sleep(seconds)


class TestPhaseResult:
    def test_success_pct_all_ok(self):
        r = PhaseResult(
            phase="x",
            policy=PhasePolicy.BEST_EFFORT,
            succeeded=[ComponentResult("a", TaskOutcome.SUCCESS, 0.1)],
        )
        assert r.success_pct == 100.0

    def test_success_pct_half(self):
        r = PhaseResult(
            phase="x",
            policy=PhasePolicy.BEST_EFFORT,
            succeeded=[ComponentResult("a", TaskOutcome.SUCCESS, 0.1)],
            failed=[ComponentResult("b", TaskOutcome.FAILED, 0.1, error="boom")],
        )
        assert r.success_pct == 50.0

    def test_success_pct_empty_is_100(self):
        r = PhaseResult(phase="x", policy=PhasePolicy.BEST_EFFORT)
        assert r.success_pct == 100.0

    def test_total_count(self):
        r = PhaseResult(
            phase="x",
            policy=PhasePolicy.BEST_EFFORT,
            succeeded=[ComponentResult("a", TaskOutcome.SUCCESS, 0.1)] * 2,
            failed=[ComponentResult("b", TaskOutcome.FAILED, 0.1, error="e")] * 3,
        )
        assert r.total_count == 5


class TestExecutePhase:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("infra", timeout_s=5.0, policy=PhasePolicy.REQUIRED_ALL),
            {"a": _ok, "b": _ok},
        )
        assert result.success_count == 2
        assert result.failure_count == 0

    @pytest.mark.asyncio
    async def test_single_failure_captured(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("infra", timeout_s=5.0, policy=PhasePolicy.REQUIRED_ALL),
            {"ok": _ok, "bad": _fail},
        )
        assert result.success_count == 1
        assert result.failure_count == 1
        failed = result.failed[0]
        assert failed.outcome == TaskOutcome.FAILED
        assert "init failed" in (failed.error or "")

    @pytest.mark.asyncio
    async def test_component_timeout_produces_timed_out_outcome(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig(
                "infra", timeout_s=5.0,
                component_timeout_s=0.05,
                policy=PhasePolicy.BEST_EFFORT,
            ),
            {"slow": _slow},
        )
        assert result.failed[0].outcome == TaskOutcome.TIMED_OUT

    @pytest.mark.asyncio
    async def test_phase_timeout_marks_all_timed_out(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig(
                "infra", timeout_s=0.05,
                component_timeout_s=99.0,
                policy=PhasePolicy.BEST_EFFORT,
            ),
            {"slow1": _slow, "slow2": _slow},
        )
        assert result.failure_count == 2
        assert all(r.outcome == TaskOutcome.TIMED_OUT for r in result.failed)

    @pytest.mark.asyncio
    async def test_callable_factory_invoked(self):
        called = []

        async def factory_coro():
            called.append(1)

        m = StartupPhaseManager()
        await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0),
            {"svc": factory_coro},
        )
        assert called

    @pytest.mark.asyncio
    async def test_duration_recorded(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0),
            {"a": _ok},
        )
        assert result.duration_s >= 0.0

    @pytest.mark.asyncio
    async def test_memory_gate_refused_outcome_is_shed(self):
        class MemoryGateRefused(Exception):
            pass

        async def shed_task():
            raise MemoryGateRefused("OOM")

        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("intel", timeout_s=5.0),
            {"neural": shed_task},
        )
        assert result.failed[0].outcome == TaskOutcome.SHED


class TestCanProceed:
    @pytest.mark.asyncio
    async def test_best_effort_always_proceeds(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0, policy=PhasePolicy.BEST_EFFORT),
            {"a": _fail, "b": _fail},
        )
        assert m.can_proceed(result) is True

    @pytest.mark.asyncio
    async def test_required_all_passes_when_all_succeed(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0, policy=PhasePolicy.REQUIRED_ALL),
            {"a": _ok, "b": _ok},
        )
        assert m.can_proceed(result) is True

    @pytest.mark.asyncio
    async def test_required_all_fails_with_any_failure(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0, policy=PhasePolicy.REQUIRED_ALL),
            {"ok": _ok, "bad": _fail},
        )
        assert m.can_proceed(result) is False

    @pytest.mark.asyncio
    async def test_required_quorum_passes_when_above_pct(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig(
                "x", timeout_s=5.0,
                policy=PhasePolicy.REQUIRED_QUORUM, quorum_pct=50.0,
            ),
            {"a": _ok, "b": _ok, "c": _fail},
        )
        # 2/3 = 67% > 50% quorum
        assert m.can_proceed(result) is True

    @pytest.mark.asyncio
    async def test_required_quorum_fails_when_below_pct(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig(
                "x", timeout_s=5.0,
                policy=PhasePolicy.REQUIRED_QUORUM, quorum_pct=75.0,
            ),
            {"a": _ok, "b": _fail, "c": _fail, "d": _fail},
        )
        # 1/4 = 25% < 75% quorum
        assert m.can_proceed(result) is False

    @pytest.mark.asyncio
    async def test_degradation_level_accumulates(self):
        m = StartupPhaseManager()
        result1 = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0, policy=PhasePolicy.BEST_EFFORT),
            {"a": _fail},
        )
        result2 = await m.execute_phase(
            PhaseConfig("y", timeout_s=5.0, policy=PhasePolicy.BEST_EFFORT),
            {"b": _fail, "c": _fail},
        )
        m.can_proceed(result1)
        m.can_proceed(result2)
        assert m.degradation_level == 3

    @pytest.mark.asyncio
    async def test_phase_history_populated(self):
        m = StartupPhaseManager()
        await m.execute_phase(PhaseConfig("p1", timeout_s=5.0), {"a": _ok})
        await m.execute_phase(PhaseConfig("p2", timeout_s=5.0), {"b": _ok})
        assert len(m.phase_history) == 2
        assert m.phase_history[0].phase == "p1"


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        m = StartupPhaseManager()
        result = await m.execute_phase(
            PhaseConfig("x", timeout_s=5.0, policy=PhasePolicy.BEST_EFFORT),
            {"a": _fail},
        )
        m.can_proceed(result)
        m.reset()
        assert m.degradation_level == 0
        assert m.phase_history == []

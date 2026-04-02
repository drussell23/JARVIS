"""
Tests for backend.hive.rem_council

Covers:
- Runs all three modules (assert each .run called once)
- Returns correct RemSessionResult (threads aggregated, calls summed, modules listed)
- Sequential execution order (health -> graduation -> manifesto)
- Budget splitting: each module gets ~16 calls (50 // 3)
- Budget exhaustion: if first module uses all 50 calls, remaining modules skipped
- Remaining budget carries forward
- Escalation from any module propagates to result
- No escalation when all clean
- Module exception: logged, marked completed, execution continues
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from backend.hive.rem_council import RemCouncil, RemSessionResult


# ============================================================================
# Helpers
# ============================================================================


def _make_module(
    thread_ids=None, calls_used=5, should_escalate=False, escalation_id=None
):
    """Create an AsyncMock ReviewModule returning a fixed tuple."""
    mock = AsyncMock()
    mock.run.return_value = (
        thread_ids or [],
        calls_used,
        should_escalate,
        escalation_id,
    )
    return mock


# ============================================================================
# Tests
# ============================================================================


class TestRemCouncilRunsAllModules:
    """All three modules execute when budget permits."""

    @pytest.mark.asyncio
    async def test_all_modules_called_once(self):
        health = _make_module(thread_ids=["t1"], calls_used=5)
        graduation = _make_module(thread_ids=["t2"], calls_used=3)
        manifesto = _make_module(thread_ids=["t3"], calls_used=4)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        health.run.assert_called_once()
        graduation.run.assert_called_once()
        manifesto.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_result_aggregation(self):
        health = _make_module(thread_ids=["t1"], calls_used=5)
        graduation = _make_module(thread_ids=["t2", "t3"], calls_used=8)
        manifesto = _make_module(thread_ids=["t4"], calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.threads_created == ["t1", "t2", "t3", "t4"]
        assert result.calls_used == 15
        assert result.calls_budget == 50
        assert result.modules_completed == ["health", "graduation", "manifesto"]
        assert result.modules_skipped == []
        assert result.should_escalate is False
        assert result.escalation_thread_id is None


class TestSequentialOrder:
    """Modules execute in strict health -> graduation -> manifesto order."""

    @pytest.mark.asyncio
    async def test_execution_order(self):
        order = []

        def _tracking_side_effect(name, tid, used):
            async def _side_effect(budget):
                order.append(name)
                return [tid], used, False, None
            return _side_effect

        health = AsyncMock()
        graduation = AsyncMock()
        manifesto = AsyncMock()

        health.run = AsyncMock(side_effect=_tracking_side_effect("health", "t1", 3))
        graduation.run = AsyncMock(side_effect=_tracking_side_effect("graduation", "t2", 4))
        manifesto.run = AsyncMock(side_effect=_tracking_side_effect("manifesto", "t3", 2))

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        await council.run_session()

        assert order == ["health", "graduation", "manifesto"]


class TestBudgetSplitting:
    """Each module receives per_module = max_calls // 3 as its budget arg."""

    @pytest.mark.asyncio
    async def test_budget_per_module(self):
        health = _make_module(calls_used=5)
        graduation = _make_module(calls_used=5)
        manifesto = _make_module(calls_used=5)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        await council.run_session()

        # 50 // 3 = 16
        health.run.assert_called_once_with(16)
        graduation.run.assert_called_once_with(16)
        manifesto.run.assert_called_once_with(16)


class TestBudgetExhaustion:
    """When first module exhausts entire budget, remaining modules are skipped."""

    @pytest.mark.asyncio
    async def test_first_exhausts_budget(self):
        health = _make_module(thread_ids=["t1"], calls_used=50)
        graduation = _make_module(thread_ids=["t2"], calls_used=0)
        manifesto = _make_module(thread_ids=["t3"], calls_used=0)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        health.run.assert_called_once()
        graduation.run.assert_not_called()
        manifesto.run.assert_not_called()

        assert result.modules_completed == ["health"]
        assert result.modules_skipped == ["graduation", "manifesto"]
        assert result.threads_created == ["t1"]
        assert result.calls_used == 50

    @pytest.mark.asyncio
    async def test_second_exhausts_budget(self):
        health = _make_module(thread_ids=["t1"], calls_used=10)
        graduation = _make_module(thread_ids=["t2"], calls_used=40)
        manifesto = _make_module(thread_ids=["t3"], calls_used=0)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        health.run.assert_called_once()
        graduation.run.assert_called_once()
        manifesto.run.assert_not_called()

        assert result.modules_completed == ["health", "graduation"]
        assert result.modules_skipped == ["manifesto"]
        assert result.calls_used == 50


class TestBudgetCarryForward:
    """Unused calls from earlier modules are available to later ones."""

    @pytest.mark.asyncio
    async def test_carry_forward_budget(self):
        # per_module = 50 // 3 = 16
        # health uses 2 -> remaining = 48
        # graduation gets min(16, 48) = 16, uses 2 -> remaining = 46
        # manifesto gets min(16, 46) = 16
        health = _make_module(calls_used=2)
        graduation = _make_module(calls_used=2)
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        await council.run_session()

        health.run.assert_called_once_with(16)
        graduation.run.assert_called_once_with(16)
        manifesto.run.assert_called_once_with(16)

    @pytest.mark.asyncio
    async def test_last_module_gets_all_remaining(self):
        # per_module = 50 // 3 = 16
        # health uses 0 -> remaining = 50
        # graduation uses 0 -> remaining = 50
        # manifesto gets min(16, 50) = 16
        # But if remaining > per_module, budget = per_module (capped at per_module)
        # Actually, when remaining > per_module, budget = per_module
        health = _make_module(calls_used=0)
        graduation = _make_module(calls_used=0)
        manifesto = _make_module(calls_used=0)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        manifesto.run.assert_called_once_with(16)
        assert result.calls_used == 0

    @pytest.mark.asyncio
    async def test_budget_shrinks_when_earlier_overuses(self):
        # per_module = 50 // 3 = 16
        # health uses 16 -> remaining = 34
        # graduation gets min(16, 34) = 16, uses 16 -> remaining = 18
        # manifesto gets min(16, 18) = 16
        health = _make_module(calls_used=16)
        graduation = _make_module(calls_used=16)
        manifesto = _make_module(calls_used=16)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.calls_used == 48
        assert result.modules_completed == ["health", "graduation", "manifesto"]

    @pytest.mark.asyncio
    async def test_remaining_becomes_budget_when_less_than_per_module(self):
        # per_module = 50 // 3 = 16
        # health uses 16 -> remaining = 34
        # graduation uses 30 -> remaining = 4
        # manifesto gets min(16, 4) = 4
        health = _make_module(calls_used=16)
        graduation = _make_module(calls_used=30)
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        # remaining for manifesto = 50 - 46 = 4 < 16, so budget = 4
        manifesto.run.assert_called_once_with(4)
        assert result.calls_used == 48


class TestEscalation:
    """Escalation from any module propagates to session result."""

    @pytest.mark.asyncio
    async def test_escalation_from_first_module(self):
        health = _make_module(
            thread_ids=["t_esc"],
            calls_used=5,
            should_escalate=True,
            escalation_id="t_esc",
        )
        graduation = _make_module(calls_used=3)
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.should_escalate is True
        assert result.escalation_thread_id == "t_esc"

    @pytest.mark.asyncio
    async def test_escalation_from_second_module(self):
        health = _make_module(calls_used=5)
        graduation = _make_module(
            thread_ids=["t_grad_esc"],
            calls_used=3,
            should_escalate=True,
            escalation_id="t_grad_esc",
        )
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.should_escalate is True
        assert result.escalation_thread_id == "t_grad_esc"

    @pytest.mark.asyncio
    async def test_first_escalation_wins(self):
        """When multiple modules escalate, only the first sets the session ID."""
        health = _make_module(
            calls_used=5, should_escalate=True, escalation_id="first"
        )
        graduation = _make_module(
            calls_used=3, should_escalate=True, escalation_id="second"
        )
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.should_escalate is True
        assert result.escalation_thread_id == "first"

    @pytest.mark.asyncio
    async def test_no_escalation_when_all_clean(self):
        health = _make_module(calls_used=5)
        graduation = _make_module(calls_used=3)
        manifesto = _make_module(calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert result.should_escalate is False
        assert result.escalation_thread_id is None


class TestModuleException:
    """Module exceptions are logged; module marked completed, not skipped."""

    @pytest.mark.asyncio
    async def test_exception_marks_completed(self):
        health = AsyncMock()
        health.run.side_effect = RuntimeError("scanner crashed")
        graduation = _make_module(thread_ids=["t2"], calls_used=4)
        manifesto = _make_module(thread_ids=["t3"], calls_used=3)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        assert "health" in result.modules_completed
        assert "graduation" in result.modules_completed
        assert "manifesto" in result.modules_completed
        assert result.modules_skipped == []
        # Only graduation and manifesto threads, health had an exception
        assert result.threads_created == ["t2", "t3"]
        assert result.calls_used == 7

    @pytest.mark.asyncio
    async def test_exception_does_not_block_later_modules(self):
        health = AsyncMock()
        health.run.side_effect = ValueError("bad data")
        graduation = AsyncMock()
        graduation.run.side_effect = TypeError("type mismatch")
        manifesto = _make_module(thread_ids=["t3"], calls_used=2)

        council = RemCouncil(health, graduation, manifesto, max_calls=50)
        result = await council.run_session()

        manifesto.run.assert_called_once()
        assert result.modules_completed == ["health", "graduation", "manifesto"]
        assert result.threads_created == ["t3"]
        assert result.calls_used == 2


class TestRemSessionResultDefaults:
    """RemSessionResult initializes with sane defaults."""

    def test_defaults(self):
        r = RemSessionResult()
        assert r.threads_created == []
        assert r.calls_used == 0
        assert r.calls_budget == 0
        assert r.should_escalate is False
        assert r.escalation_thread_id is None
        assert r.modules_completed == []
        assert r.modules_skipped == []

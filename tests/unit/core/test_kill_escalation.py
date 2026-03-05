"""Tests for kill escalation ladder: drain -> term -> group_kill."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.root_authority_types import (
    ProcessIdentity, LifecycleAction, LifecycleVerdict, ExecutionResult,
    TimeoutPolicy, RestartPolicy,
)


@pytest.fixture
def identity():
    return ProcessIdentity(pid=100, start_time_ns=0, session_id="s1", exec_fingerprint="f1")


@pytest.fixture
def success_result():
    return ExecutionResult(True, True, "success", None, None, "c1")


@pytest.fixture
def timeout_result():
    return ExecutionResult(True, True, "timeout", None, None, "c1")


class TestEscalationEngine:
    @pytest.mark.asyncio
    async def test_drain_success_stops_escalation(self, identity, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "drain_success"
        executor.execute_drain.assert_called_once()
        executor.execute_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_drain_timeout_escalates_to_term(self, identity, timeout_result, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=timeout_result)
        executor.execute_term = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "term_success"
        executor.execute_drain.assert_called_once()
        executor.execute_term.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_escalation_to_group_kill(self, identity, timeout_result, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        executor.execute_drain = AsyncMock(return_value=timeout_result)
        executor.execute_term = AsyncMock(return_value=timeout_result)
        executor.execute_group_kill = AsyncMock(return_value=success_result)
        executor.get_current_identity = MagicMock(return_value=identity)

        engine = EscalationEngine(TimeoutPolicy(drain_timeout_s=5.0, term_timeout_s=2.0))
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "group_kill_success"

    @pytest.mark.asyncio
    async def test_stale_identity_aborts(self, identity, success_result):
        from backend.core.root_authority import EscalationEngine
        executor = AsyncMock()
        different = ProcessIdentity(pid=200, start_time_ns=0, session_id="s1", exec_fingerprint="f1")
        executor.get_current_identity = MagicMock(return_value=different)

        engine = EscalationEngine(TimeoutPolicy())
        result = await engine.escalate(
            "jarvis-prime", identity, executor, correlation_id="c1"
        )
        assert result == "stale_identity"
        executor.execute_drain.assert_not_called()

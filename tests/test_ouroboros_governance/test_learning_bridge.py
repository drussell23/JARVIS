# tests/test_ouroboros_governance/test_learning_bridge.py
"""Tests for the governance learning feedback bridge."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.learning_bridge import (
    LearningBridge,
    OperationOutcome,
)
from backend.core.ouroboros.governance.ledger import OperationState


@pytest.fixture
def mock_memory():
    mem = AsyncMock()
    mem.record_attempt = AsyncMock()
    mem.get_known_solution = AsyncMock(return_value=None)
    mem.should_skip_pattern = AsyncMock(return_value=False)
    return mem


@pytest.fixture
def bridge(mock_memory):
    return LearningBridge(learning_memory=mock_memory)


class TestOperationOutcome:
    def test_outcome_fields(self):
        """OperationOutcome has required fields."""
        outcome = OperationOutcome(
            op_id="op-test-001",
            goal="fix bug in foo.py",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
            error_pattern=None,
        )
        assert outcome.op_id == "op-test-001"
        assert outcome.success is True

    def test_failed_outcome(self):
        """Failed outcome has success=False."""
        outcome = OperationOutcome(
            op_id="op-test-002",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.FAILED,
            error_pattern="syntax_error",
        )
        assert outcome.success is False

    def test_rolled_back_is_failure(self):
        """ROLLED_BACK is treated as failure."""
        outcome = OperationOutcome(
            op_id="op-test-003",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.ROLLED_BACK,
            error_pattern="verify_failed",
        )
        assert outcome.success is False


class TestLearningBridge:
    @pytest.mark.asyncio
    async def test_publish_success(self, bridge, mock_memory):
        """Successful operation recorded with success=True."""
        outcome = OperationOutcome(
            op_id="op-test-010",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
        )
        await bridge.publish(outcome)
        mock_memory.record_attempt.assert_called_once()
        call_kwargs = mock_memory.record_attempt.call_args
        assert call_kwargs[1]["success"] is True

    @pytest.mark.asyncio
    async def test_publish_failure(self, bridge, mock_memory):
        """Failed operation recorded with error pattern."""
        outcome = OperationOutcome(
            op_id="op-test-011",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.FAILED,
            error_pattern="syntax_error",
        )
        await bridge.publish(outcome)
        mock_memory.record_attempt.assert_called_once()
        call_kwargs = mock_memory.record_attempt.call_args
        assert call_kwargs[1]["success"] is False
        assert call_kwargs[1]["error_pattern"] == "syntax_error"

    @pytest.mark.asyncio
    async def test_should_skip_delegates_to_memory(self, bridge, mock_memory):
        """should_skip() delegates to LearningMemory."""
        mock_memory.should_skip_pattern.return_value = True
        result = await bridge.should_skip("fix bug", "foo.py", "syntax_error")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_solution_delegates_to_memory(self, bridge, mock_memory):
        """get_known_solution() delegates to LearningMemory."""
        mock_memory.get_known_solution.return_value = "use try/except"
        result = await bridge.get_known_solution("fix bug", "foo.py", "runtime_error")
        assert result == "use try/except"

    @pytest.mark.asyncio
    async def test_fault_isolation_on_publish(self, bridge, mock_memory):
        """Memory failure on publish does not propagate."""
        mock_memory.record_attempt.side_effect = RuntimeError("disk full")
        outcome = OperationOutcome(
            op_id="op-test-012",
            goal="fix bug",
            target_files=["foo.py"],
            final_state=OperationState.APPLIED,
        )
        # Should not raise
        await bridge.publish(outcome)

    @pytest.mark.asyncio
    async def test_no_memory_returns_defaults(self):
        """Bridge without memory returns safe defaults."""
        bridge = LearningBridge(learning_memory=None)
        assert await bridge.should_skip("goal", "file", "err") is False
        assert await bridge.get_known_solution("goal", "file", "err") is None

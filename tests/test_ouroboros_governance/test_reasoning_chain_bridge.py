"""Tests for the ReasoningChainBridge governance integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.comm_protocol import (
    CommProtocol,
    LogTransport,
)
from backend.core.ouroboros.governance.reasoning_chain_bridge import (
    ReasoningChainBridge,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chain_result(
    *,
    handled: bool = True,
    expanded_intents: list | None = None,
    needs_confirmation: bool = False,
    phase_value: str = "soft_enable",
    success_rate: float = 0.9,
):
    """Build a mock ChainResult-like object."""
    result = MagicMock()
    result.handled = handled
    result.expanded_intents = expanded_intents or ["intent_a", "intent_b"]
    result.needs_confirmation = needs_confirmation
    result.success_rate = success_rate
    result.phase = MagicMock()
    result.phase.value = phase_value
    return result


def _make_inactive_orchestrator():
    """Build a mock orchestrator whose config reports inactive."""
    orch = MagicMock()
    orch._config.is_active.return_value = False
    return orch


def _make_active_orchestrator(process_return=None, timeout: float = 2.0):
    """Build a mock orchestrator whose config reports active."""
    orch = MagicMock()
    orch._config.is_active.return_value = True
    orch._config.expansion_timeout = timeout
    orch.process = AsyncMock(return_value=process_return)
    return orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBridgeInactive:
    """Bridge behaviour when reasoning chain is not available."""

    @pytest.mark.asyncio
    async def test_returns_none_when_orchestrator_unavailable(self):
        """classify_with_reasoning returns None when import fails."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=None,
        ):
            bridge = ReasoningChainBridge(comm)

        assert not bridge.is_active
        result = await bridge.classify_with_reasoning(
            command="fix tests", op_id="op-test-1"
        )
        assert result is None
        assert len(transport.messages) == 0

    @pytest.mark.asyncio
    async def test_returns_none_when_chain_inactive(self):
        """classify_with_reasoning returns None when config is inactive."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=None,
        ):
            bridge = ReasoningChainBridge(comm)

        assert bridge.is_active is False
        result = await bridge.classify_with_reasoning(
            command="run linter", op_id="op-test-2"
        )
        assert result is None


class TestBridgeActive:
    """Bridge behaviour when reasoning chain is active and available."""

    @pytest.mark.asyncio
    async def test_returns_result_dict_when_chain_handles(self):
        """classify_with_reasoning returns a result dict when chain handles command."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        chain_result = _make_chain_result(
            handled=True,
            expanded_intents=["refactor module", "update tests"],
            needs_confirmation=True,
            phase_value="soft_enable",
            success_rate=0.85,
        )
        mock_orch = _make_active_orchestrator(process_return=chain_result)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        assert bridge.is_active is True

        result = await bridge.classify_with_reasoning(
            command="refactor and test", op_id="op-test-3"
        )

        assert result is not None
        assert result["expanded_intents"] == ["refactor module", "update tests"]
        assert result["phase"] == "soft_enable"
        assert result["success_rate"] == 0.85
        assert result["needs_confirmation"] is True
        assert result["intent_count"] == 2

    @pytest.mark.asyncio
    async def test_emits_plan_message_via_comm(self):
        """classify_with_reasoning emits a PLAN message through CommProtocol."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        chain_result = _make_chain_result(
            expanded_intents=["step_one", "step_two"],
        )
        mock_orch = _make_active_orchestrator(process_return=chain_result)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        await bridge.classify_with_reasoning(
            command="multi task", op_id="op-test-4"
        )

        # A PLAN message should have been emitted
        plan_msgs = [m for m in transport.messages if m.msg_type.value == "PLAN"]
        assert len(plan_msgs) == 1
        assert plan_msgs[0].payload["steps"] == ["step_one", "step_two"]
        assert plan_msgs[0].payload["rollback_strategy"] == "reasoning_chain_rollback"

    @pytest.mark.asyncio
    async def test_returns_none_when_chain_not_handled(self):
        """classify_with_reasoning returns None when chain.process returns not handled."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        chain_result = _make_chain_result(handled=False)
        mock_orch = _make_active_orchestrator(process_return=chain_result)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        result = await bridge.classify_with_reasoning(
            command="simple cmd", op_id="op-test-5"
        )
        assert result is None
        assert len(transport.messages) == 0

    @pytest.mark.asyncio
    async def test_returns_none_when_process_returns_none(self):
        """classify_with_reasoning returns None when chain.process returns None."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        mock_orch = _make_active_orchestrator(process_return=None)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        result = await bridge.classify_with_reasoning(
            command="pass through", op_id="op-test-6"
        )
        assert result is None


class TestBridgeErrorHandling:
    """Bridge resilience against timeouts and exceptions."""

    @pytest.mark.asyncio
    async def test_handles_timeout_gracefully(self):
        """classify_with_reasoning returns None on asyncio.TimeoutError."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        mock_orch = _make_active_orchestrator(timeout=0.5)
        mock_orch.process = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        result = await bridge.classify_with_reasoning(
            command="slow command", op_id="op-test-7"
        )
        assert result is None
        assert len(transport.messages) == 0

    @pytest.mark.asyncio
    async def test_handles_orchestrator_exception_gracefully(self):
        """classify_with_reasoning returns None on arbitrary orchestrator error."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        mock_orch = _make_active_orchestrator()
        mock_orch.process = AsyncMock(side_effect=RuntimeError("boom"))

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        result = await bridge.classify_with_reasoning(
            command="bad command", op_id="op-test-8"
        )
        assert result is None
        assert len(transport.messages) == 0

    @pytest.mark.asyncio
    async def test_plan_emit_failure_does_not_suppress_result(self):
        """Result dict is returned even if CommProtocol.emit_plan fails."""
        transport = LogTransport()
        comm = CommProtocol(transports=[transport])
        chain_result = _make_chain_result(
            expanded_intents=["a", "b"],
        )
        mock_orch = _make_active_orchestrator(process_return=chain_result)

        with patch(
            "backend.core.ouroboros.governance.reasoning_chain_bridge"
            ".ReasoningChainBridge._try_load_orchestrator",
            return_value=mock_orch,
        ):
            bridge = ReasoningChainBridge(comm)

        # Sabotage emit_plan to raise
        comm.emit_plan = AsyncMock(side_effect=RuntimeError("emit failed"))

        result = await bridge.classify_with_reasoning(
            command="multi", op_id="op-test-9"
        )
        # Result should still be returned even though PLAN emission failed
        assert result is not None
        assert result["intent_count"] == 2

"""Tests for the three connective-tissue wiring items in GovernedOrchestrator.

asyncio_mode = auto — never add @pytest.mark.asyncio.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.risk_engine import (
    RiskClassification,
    RiskTier,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    SagaApplyResult,
    SagaTerminalState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        project_root=tmp_path,
        generation_timeout_s=5.0,
        validation_timeout_s=5.0,
        approval_timeout_s=5.0,
        max_generate_retries=1,
        max_validate_retries=2,
    )


def _make_ctx(tmp_path: Path, op_id: str = "op-001") -> OperationContext:
    return OperationContext.create(
        target_files=(str(tmp_path / "backend/core/utils.py"),),
        description="Add utility function",
        op_id=op_id,
    )


def _advance_to_apply(ctx: OperationContext) -> OperationContext:
    """Walk the context through the required phase sequence to reach APPLY.

    CLASSIFY -> ROUTE -> GENERATE -> VALIDATE -> GATE -> APPLY
    """
    ctx = ctx.advance(OperationPhase.ROUTE)
    ctx = ctx.advance(OperationPhase.GENERATE)
    ctx = ctx.advance(OperationPhase.VALIDATE)
    ctx = ctx.advance(OperationPhase.GATE)
    ctx = ctx.advance(OperationPhase.APPLY)
    return ctx


def _mock_stack() -> MagicMock:
    stack = MagicMock()
    stack.can_write.return_value = (True, "ok")
    stack.risk_engine.classify.return_value = RiskClassification(
        tier=RiskTier.SAFE_AUTO, reason_code="safe"
    )
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.controller = MagicMock()
    stack.controller.pause = AsyncMock()
    stack.canary = MagicMock()
    stack.canary.record_operation = MagicMock()
    stack.change_engine = AsyncMock()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=True, rolled_back=False, op_id="op-001")
    )
    return stack


def _mock_generator(tmp_path: Path) -> MagicMock:
    gen = MagicMock()
    candidate = {
        "file_path": str(tmp_path / "backend/core/utils.py"),
        "full_content": "def hello():\n    pass\n",
    }
    gen.generate = AsyncMock(
        return_value=GenerationResult(
            candidates=(candidate,),
            provider_name="mock",
            generation_duration_s=0.1,
        )
    )
    gen.validate = AsyncMock(
        return_value=ValidationResult(
            passed=True,
            best_candidate=candidate,
            validation_duration_s=0.05,
            error=None,
        )
    )
    return gen


def _mock_approval_provider() -> MagicMock:
    provider = MagicMock()
    provider.request = AsyncMock(return_value="op-001")
    provider.await_decision = AsyncMock(
        return_value=ApprovalResult(
            status=ApprovalStatus.APPROVED,
            approver="test-operator",
            reason=None,
            decided_at=None,
            request_id="op-001",
        )
    )
    return provider


def _make_orchestrator(tmp_path: Path, stack=None) -> GovernedOrchestrator:
    if stack is None:
        stack = _mock_stack()
    return GovernedOrchestrator(
        stack=stack,
        generator=_mock_generator(tmp_path),
        approval_provider=_mock_approval_provider(),
        config=_config(tmp_path),
    )


def _make_stuck_result(op_id: str = "op-001") -> SagaApplyResult:
    """Build a SagaApplyResult that represents SAGA_STUCK."""
    return SagaApplyResult(
        terminal_state=SagaTerminalState.SAGA_STUCK,
        saga_id=f"saga-{op_id}",
        saga_step_index=2,
        error="compensation failed: disk full",
        reason_code="compensation_failed",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_saga_stuck_triggers_controller_pause(tmp_path):
    """SAGA_STUCK must await controller.pause() exactly once."""
    stack = _mock_stack()
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)
    ctx_apply = _advance_to_apply(ctx)

    stuck_result = _make_stuck_result(op_id="op-001")

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strat = MagicMock()
        mock_strat.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strat

        result_ctx = await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    stack.controller.pause.assert_awaited_once()
    assert result_ctx is not None
    assert result_ctx.phase == OperationPhase.POSTMORTEM


async def test_saga_stuck_pause_failure_does_not_reraise(tmp_path):
    """If controller.pause() raises, _execute_saga_apply must still return a ctx."""
    stack = _mock_stack()
    stack.controller.pause = AsyncMock(side_effect=RuntimeError("controller down"))
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)
    ctx_apply = _advance_to_apply(ctx)

    stuck_result = _make_stuck_result(op_id="op-001")

    with patch(
        "backend.core.ouroboros.governance.orchestrator.SagaApplyStrategy"
    ) as MockStrategy:
        mock_strat = MagicMock()
        mock_strat.execute = AsyncMock(return_value=stuck_result)
        MockStrategy.return_value = mock_strat

        # Must not raise even though pause() raises
        result_ctx = await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    assert result_ctx is not None
    assert result_ctx.phase == OperationPhase.POSTMORTEM

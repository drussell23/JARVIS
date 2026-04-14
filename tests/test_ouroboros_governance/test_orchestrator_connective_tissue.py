"""Tests for the three connective-tissue wiring items in GovernedOrchestrator.

asyncio_mode = auto — never add @pytest.mark.asyncio.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _disable_iron_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable post-GENERATE Iron Gates for this file's connective-tissue tests.

    **Intentional and scoped to this suite — do not copy blindly.**

    Mock generators here return ``GenerationResult`` objects with an **empty**
    ``tool_execution_records`` tuple — the mock path does not exercise the
    Venom tool loop at all. With ``JARVIS_EXPLORATION_GATE`` enabled, the
    Iron Gate fires ``exploration_insufficient: 0/N`` on every attempt, the
    retry loop trips forward-progress on the second identical failure, and
    the pipeline cancels before APPLY — so every canary / learning-bridge /
    saga-apply assertion below fires against a ``CANCELLED`` terminal state
    instead of the success path it's trying to exercise.
    ``JARVIS_ASCII_GATE`` is disabled for the same "no candidate content"
    reason.

    **Do not re-enable these gates here.** Gate behaviour has dedicated
    integration tests that drive real tool records; these tests only care
    about the connective wiring (canary, learning_bridge, saga pause).
    Re-enabling gates in this file would turn the suite red again for
    reasons unrelated to the wiring under test.
    """
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    monkeypatch.setenv("JARVIS_ASCII_GATE", "false")

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,  # used in Task 2/3 tests below
    ApprovalStatus,  # used in Task 2/3 tests below
)
from backend.core.ouroboros.governance.ledger import OperationState  # used in Task 3 tests below
from backend.core.ouroboros.governance.learning_bridge import OperationOutcome  # Task 3
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
from backend.core.ouroboros.governance.test_runner import MultiAdapterResult


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
        # PatchBenchmarker has nothing to measure in tmp_path and returns a
        # zero-metric result; the VERIFY regression gate would then route
        # every happy-path op to POSTMORTEM.
        benchmark_enabled=False,
    )


def _make_ctx(tmp_path: Path, op_id: str = "op-001") -> OperationContext:
    return OperationContext.create(
        target_files=(str(tmp_path / "backend/core/utils.py"),),
        description="Add utility function",
        op_id=op_id,
    )


def _advance_to_apply(ctx: OperationContext) -> OperationContext:
    """Advance a context from its initial CLASSIFY phase through to APPLY.

    Precondition: ctx must be in CLASSIFY (as returned by OperationContext.create()).
    Phase walk: CLASSIFY → ROUTE → GENERATE → VALIDATE → GATE → APPLY
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
    stack.learning_bridge = None  # Task 3: default None so existing tests are unaffected
    # _is_cancel_requested() reads stack.governed_loop_service.is_cancel_requested;
    # naked MagicMock is truthy → every pipeline cancels at the first check.
    stack.governed_loop_service.is_cancel_requested.return_value = False
    # SecurityReviewer awaits stack.security_reviewer.review; MagicMock isn't awaitable.
    stack.security_reviewer = MagicMock()
    stack.security_reviewer.review = AsyncMock(return_value=None)
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


def _mock_validation_runner() -> MagicMock:
    """Return a duck-typed validation runner that always passes."""
    runner = MagicMock()
    runner.run = AsyncMock(
        return_value=MultiAdapterResult(
            passed=True,
            adapter_results=(),
            dominant_failure=None,
            total_duration_s=0.01,
        )
    )
    return runner


def _make_orchestrator(tmp_path: Path, stack=None) -> GovernedOrchestrator:
    if stack is None:
        stack = _mock_stack()
    return GovernedOrchestrator(
        stack=stack,
        generator=_mock_generator(tmp_path),
        approval_provider=_mock_approval_provider(),
        config=_config(tmp_path),
        validation_runner=_mock_validation_runner(),
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

    stack.controller.pause.assert_awaited_once()
    assert result_ctx is not None
    assert result_ctx.phase == OperationPhase.POSTMORTEM


async def test_canary_record_on_single_repo_success(tmp_path):
    """CanaryController.record_operation must be called with success=True after a clean apply."""
    stack = _mock_stack()
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch.run(ctx)

    assert stack.canary.record_operation.called
    call = stack.canary.record_operation.call_args
    assert call.kwargs["success"] is True


async def test_canary_record_on_single_repo_failure(tmp_path):
    """CanaryController.record_operation must be called with success=False when change_engine fails."""
    stack = _mock_stack()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=False, rolled_back=True)
    )
    orch = _make_orchestrator(tmp_path, stack=stack)
    ctx = _make_ctx(tmp_path)

    await orch.run(ctx)

    assert stack.canary.record_operation.called
    call = stack.canary.record_operation.call_args
    assert call.kwargs["success"] is False
    assert call.kwargs["rolled_back"] is True


async def test_canary_record_on_saga_stuck(tmp_path):
    """CanaryController.record_operation must be called with success=False for SAGA_STUCK."""
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

        await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    assert stack.canary.record_operation.called
    call = stack.canary.record_operation.call_args
    assert call.kwargs["success"] is False


# ---------------------------------------------------------------------------
# Task 3: LearningBridge outcome publishing
# ---------------------------------------------------------------------------


async def test_learning_bridge_publish_on_single_repo_success(tmp_path):
    """learning_bridge.publish called with APPLIED outcome on single-repo success."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
    orch = _make_orchestrator(tmp_path, stack=stack)

    await orch.run(_make_ctx(tmp_path))

    stack.learning_bridge.publish.assert_awaited_once()
    outcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.APPLIED
    assert outcome.op_id == "op-001"


async def test_learning_bridge_publish_on_single_repo_failure(tmp_path):
    """learning_bridge.publish called with FAILED outcome when change_engine fails."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(success=False, rolled_back=False)
    )
    orch = _make_orchestrator(tmp_path, stack=stack)

    await orch.run(_make_ctx(tmp_path))

    stack.learning_bridge.publish.assert_awaited_once()
    outcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.FAILED
    assert outcome.error_pattern == "change_engine_failed"


async def test_learning_bridge_skipped_when_none(tmp_path):
    """No AttributeError when learning_bridge is None (default in _mock_stack)."""
    stack = _mock_stack()
    assert stack.learning_bridge is None
    orch = _make_orchestrator(tmp_path, stack=stack)

    # Must not raise
    await orch.run(_make_ctx(tmp_path))


async def test_learning_bridge_publish_on_saga_stuck(tmp_path):
    """learning_bridge.publish called with FAILED/saga_stuck on SAGA_STUCK."""
    stack = _mock_stack()
    stack.learning_bridge = AsyncMock()
    stack.learning_bridge.publish = AsyncMock()
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

        await orch._execute_saga_apply(ctx_apply, {"mock_candidate": {}})

    stack.learning_bridge.publish.assert_awaited_once()
    outcome = stack.learning_bridge.publish.call_args.args[0]
    assert outcome.final_state == OperationState.FAILED
    assert outcome.error_pattern == "saga_stuck"

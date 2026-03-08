"""Tests for GovernedOrchestrator — the central pipeline coordinator.

The orchestrator ties together all governance components:
- OperationContext (state machine with advance())
- RiskEngine (classify into SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED)
- CandidateGenerator (generate code candidates)
- ApprovalProvider (human-in-the-loop gate)
- ChangeEngine (transactional apply with rollback)
- OperationLedger (append-only state log)

All async tests use ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.change_engine import ChangeRequest
from backend.core.ouroboros.governance.ledger import LedgerEntry, OperationState
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
    ChangeType,
    OperationProfile,
    RiskClassification,
    RiskTier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add utility function",
    target_files: Tuple[str, ...] = ("backend/core/utils.py",),
) -> OperationContext:
    """Build a deterministic OperationContext for testing."""
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


def _mock_stack(
    can_write_result: Tuple[bool, str] = (True, "ok"),
    risk_tier: RiskTier = RiskTier.SAFE_AUTO,
    change_success: bool = True,
) -> MagicMock:
    """Build a mock GovernanceStack with sensible defaults."""
    stack = MagicMock()
    stack.can_write.return_value = can_write_result
    stack.risk_engine.classify.return_value = RiskClassification(
        tier=risk_tier, reason_code="default_safe"
    )
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.change_engine = AsyncMock()
    stack.change_engine.execute = AsyncMock(
        return_value=MagicMock(
            success=change_success,
            rolled_back=False,
            op_id="op-test-001",
        )
    )
    return stack


def _mock_generator(
    candidates: Optional[Tuple[Dict, ...]] = None,
    raise_exc: Optional[Exception] = None,
) -> MagicMock:
    """Build a mock CandidateGenerator."""
    gen = MagicMock()
    if raise_exc is not None:
        gen.generate = AsyncMock(side_effect=raise_exc)
    else:
        if candidates is None:
            candidates = (
                {"file": "backend/core/utils.py", "content": "def hello():\n    pass\n"},
            )
        result = GenerationResult(
            candidates=candidates,
            provider_name="mock-provider",
            generation_duration_s=1.5,
        )
        gen.generate = AsyncMock(return_value=result)
    return gen


def _mock_approval_provider(
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> MagicMock:
    """Build a mock ApprovalProvider."""
    provider = MagicMock()
    provider.request = AsyncMock(return_value="op-test-001")
    provider.await_decision = AsyncMock(
        return_value=ApprovalResult(
            status=status,
            approver="test-operator",
            reason=None,
            decided_at=_FIXED_TS,
            request_id="op-test-001",
        )
    )
    return provider


def _default_config() -> OrchestratorConfig:
    """Build an OrchestratorConfig with tight timeouts for tests."""
    return OrchestratorConfig(
        project_root=Path("/tmp/test-project"),
        generation_timeout_s=5.0,
        validation_timeout_s=5.0,
        approval_timeout_s=5.0,
        max_generate_retries=1,
        max_validate_retries=2,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    """Tests for the OrchestratorConfig frozen dataclass."""

    def test_frozen(self) -> None:
        cfg = _default_config()
        with pytest.raises(AttributeError):
            cfg.generation_timeout_s = 999.0  # type: ignore[misc]

    def test_defaults(self) -> None:
        cfg = OrchestratorConfig(project_root=Path("/tmp"))
        assert cfg.generation_timeout_s == 120.0
        assert cfg.validation_timeout_s == 60.0
        assert cfg.approval_timeout_s == 600.0
        assert cfg.max_generate_retries == 1
        assert cfg.max_validate_retries == 2


@pytest.mark.asyncio
class TestHappyPath:
    """Happy-path tests where the entire pipeline completes."""

    async def test_happy_path_safe_auto(self) -> None:
        """SAFE_AUTO op goes all the way to COMPLETE."""
        stack = _mock_stack()
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.COMPLETE
        # Risk engine was called
        stack.risk_engine.classify.assert_called_once()
        # Generator was called
        generator.generate.assert_called_once()
        # can_write was called (GATE phase)
        stack.can_write.assert_called_once()
        # Change engine was called (APPLY phase)
        stack.change_engine.execute.assert_called_once()
        # Ledger was appended at least once (APPLIED entry at VERIFY)
        assert stack.ledger.append.call_count >= 1


@pytest.mark.asyncio
class TestCanWriteGate:
    """Tests for the GATE phase where can_write blocks the pipeline."""

    async def test_can_write_false_cancels(self) -> None:
        """can_write returns (False, reason) -> CANCELLED."""
        stack = _mock_stack(can_write_result=(False, "canary_not_promoted:f.py"))
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED
        # Change engine must NOT be called
        stack.change_engine.execute.assert_not_called()


@pytest.mark.asyncio
class TestApprovalFlow:
    """Tests for the APPROVE phase with ApprovalProvider."""

    async def test_approval_required_pauses_then_approves(self) -> None:
        """APPROVAL_REQUIRED -> pause -> approve -> continue to COMPLETE."""
        stack = _mock_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
        generator = _mock_generator()
        approval = _mock_approval_provider(status=ApprovalStatus.APPROVED)
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=approval,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.COMPLETE
        approval.request.assert_called_once()
        approval.await_decision.assert_called_once()

    async def test_approval_timeout_expires(self) -> None:
        """APPROVAL_REQUIRED -> timeout -> EXPIRED."""
        stack = _mock_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
        generator = _mock_generator()
        approval = _mock_approval_provider(status=ApprovalStatus.EXPIRED)
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=approval,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.EXPIRED
        # Change engine must NOT be called after expiry
        stack.change_engine.execute.assert_not_called()

    async def test_approval_rejected_cancels(self) -> None:
        """APPROVAL_REQUIRED -> rejected -> CANCELLED."""
        stack = _mock_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
        generator = _mock_generator()
        approval = _mock_approval_provider(status=ApprovalStatus.REJECTED)
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=approval,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED

    async def test_approval_required_no_provider_skips_to_apply(self) -> None:
        """APPROVAL_REQUIRED with no provider still proceeds (GATE -> APPLY)."""
        # When there is no approval_provider, APPROVAL_REQUIRED ops skip
        # the approval phase. The GATE phase transition goes to APPLY directly
        # if risk_tier doesn't require approval or there's no provider.
        # Actually per spec: if APPROVAL_REQUIRED and no provider -> CANCELLED.
        stack = _mock_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        # No approval provider when approval is required -> CANCELLED
        assert result.phase is OperationPhase.CANCELLED


@pytest.mark.asyncio
class TestGenerationFailure:
    """Tests for generation phase failures."""

    async def test_generation_failure_cancels(self) -> None:
        """Generator raises -> retries exhausted -> CANCELLED."""
        stack = _mock_stack()
        generator = _mock_generator(raise_exc=RuntimeError("model OOM"))
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED
        # Generator was called 1 (initial) + 1 (retry) = 2 times
        assert generator.generate.call_count == 2

    async def test_generation_returns_empty_cancels(self) -> None:
        """Generator returns empty candidates -> CANCELLED."""
        stack = _mock_stack()
        generator = _mock_generator(candidates=())
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED


@pytest.mark.asyncio
class TestBlockedTier:
    """Tests for BLOCKED risk tier."""

    async def test_blocked_tier_cancels_immediately(self) -> None:
        """BLOCKED -> immediate CANCELLED without proceeding."""
        stack = _mock_stack(risk_tier=RiskTier.BLOCKED)
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED
        # Generator should NOT be called for blocked ops
        generator.generate.assert_not_called()
        # Change engine should NOT be called
        stack.change_engine.execute.assert_not_called()


@pytest.mark.asyncio
class TestPostmortem:
    """Tests for crash/error -> POSTMORTEM transitions."""

    async def test_crash_in_pipeline_goes_to_postmortem(self) -> None:
        """change_engine raises -> POSTMORTEM."""
        stack = _mock_stack()
        stack.change_engine.execute = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.POSTMORTEM

    async def test_change_engine_returns_failure_goes_to_postmortem(self) -> None:
        """change_engine.execute returns success=False -> POSTMORTEM."""
        stack = _mock_stack(change_success=False)
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.POSTMORTEM


@pytest.mark.asyncio
class TestValidationRetries:
    """Tests for validation phase with retries."""

    async def test_validation_all_candidates_invalid_retries_then_cancels(self) -> None:
        """All candidates fail AST parse, retries exhausted -> CANCELLED."""
        stack = _mock_stack()
        # Provide a candidate with invalid Python syntax
        generator = _mock_generator(
            candidates=(
                {"file": "utils.py", "content": "def broken(\n"},
            )
        )
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED

    async def test_valid_candidate_passes_validation(self) -> None:
        """A syntactically valid candidate passes AST validation."""
        stack = _mock_stack()
        generator = _mock_generator(
            candidates=(
                {"file": "utils.py", "content": "def hello():\n    return 42\n"},
            )
        )
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.COMPLETE


@pytest.mark.asyncio
class TestLedgerRecording:
    """Tests that ledger entries are recorded at key lifecycle points."""

    async def test_blocked_records_in_ledger(self) -> None:
        """BLOCKED tier records a BLOCKED entry in ledger."""
        stack = _mock_stack(risk_tier=RiskTier.BLOCKED)
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        await orch.run(ctx)

        # Ledger.append was called at least once for the BLOCKED state
        assert stack.ledger.append.call_count >= 1
        # Check that at least one call had BLOCKED state
        blocked_calls = [
            call
            for call in stack.ledger.append.call_args_list
            if isinstance(call.args[0], LedgerEntry)
            and call.args[0].state is OperationState.BLOCKED
        ]
        assert len(blocked_calls) >= 1

    async def test_complete_records_applied_in_ledger(self) -> None:
        """Successful completion records APPLIED entry in ledger."""
        stack = _mock_stack()
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        await orch.run(ctx)

        # Ledger.append was called with APPLIED state
        applied_calls = [
            call
            for call in stack.ledger.append.call_args_list
            if isinstance(call.args[0], LedgerEntry)
            and call.args[0].state is OperationState.APPLIED
        ]
        assert len(applied_calls) >= 1


class TestOrchestratorTerminalInvariant:
    """Verify the top-level handler always returns a terminal state."""

    @pytest.mark.asyncio
    async def test_exception_in_classify_phase_returns_terminal(self):
        """Exception during risk classification -> CANCELLED (not stuck in CLASSIFY)."""
        stack = _mock_stack()
        stack.risk_engine.classify.side_effect = RuntimeError("classify exploded")
        generator = _mock_generator()
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=None,
            config=config,
        )
        result = await orch.run(ctx)
        assert result.phase in (
            OperationPhase.CANCELLED,
            OperationPhase.POSTMORTEM,
        ), f"Expected terminal state, got {result.phase}"

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
from backend.core.ouroboros.governance.plan_generator import PlanResult
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


@pytest.fixture(autouse=True)
def _disable_iron_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable post-GENERATE Iron Gates for this file's pipeline unit tests.

    **Intentional and scoped to this suite — do not copy blindly.**

    These tests drive ``GovernedOrchestrator.run()`` with mock generators that
    return ``GenerationResult`` objects with an **empty**
    ``tool_execution_records`` tuple — there is no Venom tool loop at all in
    the mock path. If the exploration gate (``JARVIS_EXPLORATION_GATE``) is
    left enabled, every op fails post-GENERATE with
    ``Iron Gate — exploration_insufficient: 0/N``, the retry loop trips the
    forward-progress guard on the second identical failure, and the pipeline
    cancels before APPLY — so every happy-path test ends in ``CANCELLED``
    instead of ``COMPLETE``. Same reasoning for ``JARVIS_ASCII_GATE`` (no
    candidate content means no codepoints to scan, but the gate's import path
    still runs and pre-pollutes state).

    **Do not "fix" this by re-enabling the gates here.** Gate behaviour has
    dedicated integration tests that drive real tool records; these tests
    only care about the pipeline state machine. Re-enabling gates in this
    file would turn the suite red again for reasons unrelated to the code
    under test.
    """
    monkeypatch.setenv("JARVIS_EXPLORATION_GATE", "false")
    monkeypatch.setenv("JARVIS_ASCII_GATE", "false")


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
    # _is_cancel_requested() reads stack.governed_loop_service.is_cancel_requested;
    # without an explicit return_value a bare MagicMock is truthy and every
    # pipeline cancels immediately. Force a bool so the cancel check is a no-op.
    stack.governed_loop_service.is_cancel_requested.return_value = False
    # _publish_outcome() awaits learning_bridge.publish; MagicMock's default
    # return is not awaitable and raises TypeError in POSTMORTEM path.
    stack.learning_bridge = MagicMock()
    stack.learning_bridge.publish = AsyncMock(return_value=None)
    # SecurityReviewer awaits stack.security_reviewer.review; same issue.
    stack.security_reviewer = MagicMock()
    stack.security_reviewer.review = AsyncMock(return_value=None)
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
                {
                    "candidate_id": "c1",
                    "file_path": "backend/core/utils.py",
                    "full_content": "def hello():\n    pass\n",
                    "rationale": "default mock",
                },
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
        # Disable the post-apply benchmark. With no real project on disk,
        # PatchBenchmarker returns a zero-metric BenchmarkResult and the
        # VERIFY regression gate (pass_rate < 1.0) routes every happy-path
        # op to POSTMORTEM. Pipeline unit tests don't exercise benchmarking.
        benchmark_enabled=False,
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
        assert cfg.generation_timeout_s == 180.0
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
class TestPlanReviewMode:
    """Session-level /plan mode should force plan review before execution."""

    async def test_plan_review_mode_gates_even_trivial_ops(self) -> None:
        stack = _mock_stack()
        generator = _mock_generator()
        approval = _mock_approval_provider(status=ApprovalStatus.APPROVED)
        approval.request_plan = AsyncMock(return_value="op-test-001::plan")
        config = OrchestratorConfig(
            project_root=Path("/tmp/test-project"),
            generation_timeout_s=5.0,
            validation_timeout_s=5.0,
            approval_timeout_s=5.0,
            max_generate_retries=1,
            max_validate_retries=2,
            context_expansion_enabled=False,
        )
        ctx = _make_context(description="Fix typo")
        plan_result = PlanResult(
            plan_json='{"schema_version":"plan.1"}',
            approach="Make the small change directly and verify it.",
            complexity="trivial",
            ordered_changes=[
                {
                    "file_path": "backend/core/utils.py",
                    "change_type": "modify",
                    "description": "Update the utility implementation.",
                    "dependencies": [],
                    "estimated_scope": "small",
                }
            ],
            test_strategy="Run focused unit tests.",
        )

        with patch.dict("os.environ", {"JARVIS_SHOW_PLAN_BEFORE_EXECUTE": "1"}, clear=False):
            with patch("backend.core.ouroboros.governance.plan_generator.PlanGenerator") as mock_plan_gen:
                mock_plan_gen.return_value.generate_plan = AsyncMock(return_value=plan_result)
                orch = GovernedOrchestrator(
                    stack=stack,
                    generator=generator,
                    approval_provider=approval,
                    config=config,
                )
                result = await orch.run(ctx)

        approval.request_plan.assert_awaited_once()
        approval.await_decision.assert_awaited_once()
        assert result.terminal_reason_code not in {
            "plan_required_unavailable",
            "plan_review_unavailable",
            "plan_rejected",
            "plan_approval_expired",
        }

    async def test_plan_review_mode_fails_closed_without_provider(self) -> None:
        stack = _mock_stack()
        generator = _mock_generator()
        config = OrchestratorConfig(
            project_root=Path("/tmp/test-project"),
            generation_timeout_s=5.0,
            validation_timeout_s=5.0,
            approval_timeout_s=5.0,
            max_generate_retries=1,
            max_validate_retries=2,
            context_expansion_enabled=False,
        )
        ctx = _make_context(description="Fix typo")
        plan_result = PlanResult(
            plan_json='{"schema_version":"plan.1"}',
            approach="Make the small change directly and verify it.",
            complexity="trivial",
            ordered_changes=[
                {
                    "file_path": "backend/core/utils.py",
                    "change_type": "modify",
                    "description": "Update the utility implementation.",
                    "dependencies": [],
                    "estimated_scope": "small",
                }
            ],
            test_strategy="Run focused unit tests.",
        )

        with patch.dict("os.environ", {"JARVIS_SHOW_PLAN_BEFORE_EXECUTE": "1"}, clear=False):
            with patch("backend.core.ouroboros.governance.plan_generator.PlanGenerator") as mock_plan_gen:
                mock_plan_gen.return_value.generate_plan = AsyncMock(return_value=plan_result)
                orch = GovernedOrchestrator(
                    stack=stack,
                    generator=generator,
                    approval_provider=None,
                    config=config,
                )
                result = await orch.run(ctx)

        assert result.phase is OperationPhase.CANCELLED
        assert result.terminal_reason_code == "plan_review_unavailable"
        generator.generate.assert_not_awaited()


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
        assert result.terminal_reason_code == "change_engine_error"
        assert result.rollback_occurred is False

    async def test_change_engine_returns_failure_goes_to_postmortem(self) -> None:
        """change_engine.execute returns success=False -> POSTMORTEM."""
        stack = _mock_stack(change_success=False)
        stack.change_engine.execute = AsyncMock(
            return_value=MagicMock(
                success=False,
                rolled_back=True,
                op_id="op-test-001",
            )
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
        assert result.terminal_reason_code == "change_engine_failed"
        assert result.rollback_occurred is True


@pytest.mark.asyncio
class TestValidationRetries:
    """Tests for validation phase with retries."""

    async def test_validation_all_candidates_invalid_retries_then_cancels(self) -> None:
        """All candidates fail AST parse, retries exhausted -> CANCELLED."""
        stack = _mock_stack()
        # Provide a candidate with invalid Python syntax
        generator = _mock_generator(
            candidates=(
                {"candidate_id": "c1", "file_path": "utils.py", "full_content": "def broken(\n", "rationale": "broken"},
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
                {"candidate_id": "c1", "file_path": "utils.py", "full_content": "def hello():\n    return 42\n", "rationale": "valid"},
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


@pytest.mark.asyncio
class TestApprovalNotifications:
    """Tests that APPROVAL_REQUIRED ops emit comm messages."""

    async def test_approval_required_emits_heartbeat(self) -> None:
        """APPROVAL_REQUIRED -> comm.emit_heartbeat called with APPROVE phase."""
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
        # Verify comm was called with approval notification (lowercase convention)
        stack.comm.emit_heartbeat.assert_any_call(
            op_id="op-test-001",
            phase="approve",
            progress_pct=0.0,
        )


class TestContextExpansionPhaseWiring:
    def test_orchestrator_config_expansion_defaults(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
        config = OrchestratorConfig(project_root=tmp_path)
        assert config.context_expansion_enabled is True
        assert config.context_expansion_timeout_s == 30.0

    def test_orchestrator_config_expansion_can_be_disabled(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
        config = OrchestratorConfig(project_root=tmp_path, context_expansion_enabled=False)
        assert config.context_expansion_enabled is False

    async def test_context_expansion_called_when_enabled(self, tmp_path):
        """ContextExpander.expand() must be called when context_expansion_enabled=True."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator, OrchestratorConfig,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, GenerationResult,
        )
        from backend.core.ouroboros.governance.risk_engine import RiskTier

        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        mock_classification = MagicMock()
        mock_classification.tier = MagicMock()
        mock_classification.tier.name = "SAFE_AUTO"
        mock_classification.tier.__eq__ = lambda self, other: False  # not BLOCKED, not APPROVAL_REQUIRED
        mock_classification.reason_code = "safe"
        stack.risk_engine.classify.return_value = mock_classification
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="op-test"
        ))
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()

        mock_gen = MagicMock()
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x = 1\n", "rationale": "test",
                         "candidate_hash": "abc", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock",
            generation_duration_s=0.1,
        ))

        config = OrchestratorConfig(
            project_root=tmp_path,
            context_expansion_enabled=True,
            context_expansion_timeout_s=5.0,
        )
        orch = GovernedOrchestrator(
            stack=stack, generator=mock_gen, approval_provider=None, config=config
        )

        expand_called = []

        async def fake_expand(ctx, deadline):
            expand_called.append(True)
            return ctx

        with patch(
            "backend.core.ouroboros.governance.orchestrator.ContextExpander"
        ) as MockExpander:
            instance = MagicMock()
            instance.expand = AsyncMock(side_effect=fake_expand)
            MockExpander.return_value = instance

            ctx = OperationContext.create(
                target_files=("foo.py",), description="test expansion wiring"
            )
            await orch.run(ctx)

        assert expand_called, "ContextExpander.expand() was never called"

    async def test_context_expansion_skipped_when_disabled(self, tmp_path):
        """ContextExpander must NOT be instantiated when context_expansion_enabled=False."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator, OrchestratorConfig,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext, GenerationResult,
        )

        stack = MagicMock()
        stack.can_write.return_value = (True, "ok")
        mock_classification = MagicMock()
        mock_classification.tier = MagicMock()
        mock_classification.tier.name = "SAFE_AUTO"
        mock_classification.tier.__eq__ = lambda self, other: False
        mock_classification.reason_code = "safe"
        stack.risk_engine.classify.return_value = mock_classification
        stack.ledger.append = AsyncMock(return_value=True)
        stack.comm = AsyncMock()
        stack.change_engine.execute = AsyncMock(return_value=MagicMock(
            success=True, rolled_back=False, op_id="op-test"
        ))
        stack.learning_bridge = None
        stack.canary.record_operation = MagicMock()

        mock_gen = MagicMock()
        mock_gen.generate = AsyncMock(return_value=GenerationResult(
            candidates=({"candidate_id": "c1", "file_path": "foo.py",
                         "full_content": "x = 1\n", "rationale": "test",
                         "candidate_hash": "abc", "source_hash": "", "source_path": "foo.py"},),
            provider_name="mock",
            generation_duration_s=0.1,
        ))

        config = OrchestratorConfig(
            project_root=tmp_path,
            context_expansion_enabled=False,
        )
        orch = GovernedOrchestrator(
            stack=stack, generator=mock_gen, approval_provider=None, config=config
        )

        with patch(
            "backend.core.ouroboros.governance.orchestrator.ContextExpander"
        ) as MockExpander:
            ctx = OperationContext.create(
                target_files=("foo.py",), description="test no expansion"
            )
            await orch.run(ctx)

        MockExpander.assert_not_called()


# ---------------------------------------------------------------------------
# TestBenchmarkWiring
# ---------------------------------------------------------------------------


class TestBenchmarkWiring:
    """Tests for _run_benchmark and _persist_performance_record wiring."""

    async def test_run_benchmark_disabled_returns_ctx_unchanged(self):
        """When benchmark_enabled=False, ctx is returned unmodified."""
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from unittest.mock import MagicMock, AsyncMock
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = False
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        result = await orch._run_benchmark(ctx, [])
        assert result is ctx

    async def test_run_benchmark_enabled_calls_benchmarker(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        from unittest.mock import MagicMock, AsyncMock, patch
        from pathlib import Path
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = True
        config.benchmark_timeout_s = 5.0
        config.project_root = Path("/tmp")
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        ctx.pre_apply_snapshots = {}
        br = BenchmarkResult(
            pass_rate=1.0, lint_violations=0, coverage_pct=80.0,
            complexity_delta=0.0, patch_hash="h", quality_score=0.9,
            task_type="code_improvement", timed_out=False, error=None,
        )
        ctx.with_benchmark_result.return_value = ctx
        with patch(
            "backend.core.ouroboros.governance.patch_benchmarker.PatchBenchmarker"
        ) as MockBenchmarker:
            MockBenchmarker.return_value.benchmark = AsyncMock(return_value=br)
            result = await orch._run_benchmark(ctx, [])
            ctx.with_benchmark_result.assert_called_once_with(br)

    async def test_run_benchmark_never_raises_on_exception(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from unittest.mock import MagicMock, AsyncMock, patch
        from pathlib import Path
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = True
        config.benchmark_timeout_s = 5.0
        config.project_root = Path("/tmp")
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        ctx.pre_apply_snapshots = {}
        ctx.op_id = "op-x"
        with patch(
            "backend.core.ouroboros.governance.patch_benchmarker.PatchBenchmarker"
        ) as MockBenchmarker:
            MockBenchmarker.return_value.benchmark = AsyncMock(side_effect=RuntimeError("boom"))
            result = await orch._run_benchmark(ctx, [])
            assert result is ctx  # original ctx returned on failure

    async def test_run_benchmark_never_raises_on_cancelled_error(self):
        """CancelledError during benchmark must be swallowed — benchmark is non-blocking."""
        import asyncio
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from unittest.mock import MagicMock, AsyncMock, patch
        from pathlib import Path
        config = MagicMock(spec=OrchestratorConfig)
        config.benchmark_enabled = True
        config.benchmark_timeout_s = 5.0
        config.project_root = Path("/tmp")
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        ctx = MagicMock()
        ctx.pre_apply_snapshots = {}
        ctx.op_id = "op-cancel"
        with patch(
            "backend.core.ouroboros.governance.patch_benchmarker.PatchBenchmarker"
        ) as MockBenchmarker:
            MockBenchmarker.return_value.benchmark = AsyncMock(
                side_effect=asyncio.CancelledError()
            )
            result = await orch._run_benchmark(ctx, [])
            assert result is ctx  # ctx returned, CancelledError swallowed

    async def test_persist_performance_record_no_persistence_is_noop(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator
        from unittest.mock import MagicMock
        orch = Orchestrator.__new__(Orchestrator)
        stack = MagicMock()
        stack.performance_persistence = None
        orch._stack = stack
        ctx = MagicMock()
        ctx.op_id = "op-x"
        await orch._persist_performance_record(ctx)  # must not raise

    async def test_persist_performance_record_calls_save_record(self):
        from backend.core.ouroboros.governance.orchestrator import Orchestrator
        from backend.core.ouroboros.governance.patch_benchmarker import BenchmarkResult
        from backend.core.ouroboros.governance.op_context import OperationPhase
        from unittest.mock import MagicMock, AsyncMock
        orch = Orchestrator.__new__(Orchestrator)
        stack = MagicMock()
        stack.performance_persistence = MagicMock()
        stack.performance_persistence.save_record = AsyncMock()
        orch._stack = stack
        ctx = MagicMock()
        ctx.op_id = "op-x"
        ctx.phase = OperationPhase.COMPLETE
        ctx.model_id = "m1"
        ctx.difficulty = MagicMock()
        ctx.elapsed_ms = 500.0
        ctx.iterations_used = 1
        ctx.benchmark_result = BenchmarkResult(
            pass_rate=0.9, lint_violations=0, coverage_pct=75.0,
            complexity_delta=0.0, patch_hash="p", quality_score=0.85,
            task_type="bug_fix", timed_out=False, error=None,
        )
        await orch._persist_performance_record(ctx)
        stack.performance_persistence.save_record.assert_called_once()


class TestOracleIncrementalUpdate:
    """_oracle_incremental_update is fault-isolated and calls oracle correctly."""

    async def test_incremental_update_called_with_resolved_paths(self):
        """_oracle_incremental_update calls oracle.incremental_update with the given paths."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from pathlib import Path

        config = MagicMock(spec=OrchestratorConfig)
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        orch._oracle_update_lock = asyncio.Lock()

        mock_oracle = MagicMock()
        mock_oracle.incremental_update = AsyncMock()
        stack = MagicMock()
        stack.oracle = mock_oracle
        orch._stack = stack

        applied = [Path("/tmp/foo.py"), Path("/tmp/bar.py")]
        await orch._oracle_incremental_update(applied)

        mock_oracle.incremental_update.assert_called_once_with(applied)

    async def test_oracle_update_exception_does_not_raise(self):
        """RuntimeError from incremental_update is caught and never re-raised."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from pathlib import Path

        config = MagicMock(spec=OrchestratorConfig)
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        orch._oracle_update_lock = asyncio.Lock()

        mock_oracle = MagicMock()
        mock_oracle.incremental_update = AsyncMock(side_effect=RuntimeError("oracle exploded"))
        stack = MagicMock()
        stack.oracle = mock_oracle
        orch._stack = stack

        applied = [Path("/tmp/foo.py")]
        # Must not raise
        await orch._oracle_incremental_update(applied)

    async def test_oracle_update_noop_when_oracle_is_none(self):
        """When stack.oracle is None, _oracle_incremental_update is a no-op."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import Orchestrator, OrchestratorConfig
        from pathlib import Path

        config = MagicMock(spec=OrchestratorConfig)
        orch = Orchestrator.__new__(Orchestrator)
        orch._config = config
        stack = MagicMock()
        stack.oracle = None
        orch._stack = stack

        # Must not raise and must complete instantly (no oracle to call)
        await orch._oracle_incremental_update([Path("/tmp/foo.py")])


# ---------------------------------------------------------------------------
# TestObserveTierGateCheck
# ---------------------------------------------------------------------------


class TestObserveTierGateCheck:
    """frozen_autonomy_tier='observe' forces APPROVAL_REQUIRED at GATE phase."""

    def _make_orchestrator(self):
        """Build a GovernedOrchestrator with minimal mocked stack."""
        stack = _mock_stack(
            can_write_result=(True, "ok"),
            risk_tier=RiskTier.SAFE_AUTO,
        )
        config = OrchestratorConfig(
            project_root=Path("/tmp/test"),
            benchmark_enabled=False,
        )
        orch = GovernedOrchestrator(
            stack=stack,
            generator=_mock_generator(),
            approval_provider=None,
            config=config,
        )
        return orch, stack

    async def test_observe_tier_triggers_approval_when_provider_missing(self):
        """observe tier → APPROVAL_REQUIRED → pipeline exits (no approval provider)."""
        ctx = _make_context(
            target_files=("backend/core/some_module.py",),
        ).with_frozen_autonomy_tier("observe")

        orch, _ = self._make_orchestrator()

        # No approval provider → APPROVAL_REQUIRED path cancels
        result = await orch.run(ctx)

        assert result.phase in (
            OperationPhase.CANCELLED,
            OperationPhase.POSTMORTEM,
            OperationPhase.COMPLETE,
        ), (
            f"Expected non-trivial terminal phase for observe tier, got {result.phase}"
        )
        # The key check: it must NOT reach COMPLETE without going through approval
        # (with approval_provider=None, APPROVAL_REQUIRED path should cancel, not complete)
        assert result.phase is not OperationPhase.COMPLETE, (
            "observe tier with no approval provider should not reach COMPLETE"
        )

    async def test_governed_tier_does_not_force_approval(self):
        """governed tier with SAFE_AUTO risk does NOT force APPROVAL_REQUIRED at GATE."""
        ctx = _make_context(
            target_files=("tests/test_foo.py",),
        ).with_frozen_autonomy_tier("governed")

        orch, _ = self._make_orchestrator()

        result = await orch.run(ctx)

        # With governed tier + SAFE_AUTO risk + mock change_engine success → COMPLETE
        assert result.phase is OperationPhase.COMPLETE, (
            f"Expected COMPLETE for governed tier + SAFE_AUTO, got {result.phase}"
        )


# ---------------------------------------------------------------------------
# TestOracleUpdateLock
# ---------------------------------------------------------------------------


class TestOracleUpdateLock:
    """_oracle_incremental_update serializes concurrent oracle updates via asyncio.Lock."""

    async def test_oracle_update_lock_exists(self):
        """GovernedOrchestrator.__init__ creates _oracle_update_lock."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        from pathlib import Path
        import asyncio

        stack = MagicMock()
        config = OrchestratorConfig(project_root=Path("/tmp/test"))
        orch = GovernedOrchestrator(stack=stack, generator=None, approval_provider=None, config=config)

        assert hasattr(orch, "_oracle_update_lock")
        assert isinstance(orch._oracle_update_lock, asyncio.Lock)

    async def test_oracle_updates_serialized(self):
        """Concurrent _oracle_incremental_update calls are serialized (not concurrent)."""
        from unittest.mock import MagicMock
        from backend.core.ouroboros.governance.orchestrator import (
            GovernedOrchestrator,
            OrchestratorConfig,
        )
        from pathlib import Path
        import asyncio

        stack = MagicMock()
        config = OrchestratorConfig(project_root=Path("/tmp/test"))
        orch = GovernedOrchestrator(stack=stack, generator=None, approval_provider=None, config=config)

        call_order = []
        async def mock_incremental_update(_):
            call_order.append("start")
            await asyncio.sleep(0)  # yield control
            call_order.append("end")

        oracle = MagicMock()
        oracle.incremental_update = mock_incremental_update
        stack.oracle = oracle
        orch._stack = stack

        # Launch two concurrent update calls
        from pathlib import Path as P
        await asyncio.gather(
            orch._oracle_incremental_update([P("/tmp/a.py")]),
            orch._oracle_incremental_update([P("/tmp/b.py")]),
        )

        # With a lock, calls are serialized: start-end-start-end (not start-start-end-end)
        assert call_order == ["start", "end", "start", "end"], (
            f"Oracle updates were not serialized. Order: {call_order}"
        )


@pytest.mark.asyncio
async def test_cross_repo_execution_graph_materializes_into_saga_patches() -> None:
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        GraphExecutionPhase,
        GraphExecutionState,
        WorkUnitResult,
        WorkUnitSpec,
        WorkUnitState,
    )
    from backend.core.ouroboros.governance.saga.saga_types import (
        FileOp,
        PatchedFile,
        RepoPatch,
    )

    class _FakeScheduler:
        def __init__(self, graph, patches, state):
            self._graph = graph
            self._patches = patches
            self._state = state
            self.submit = AsyncMock(return_value=True)
            self.wait_for_graph = AsyncMock(return_value=state)

        def has_graph(self, graph_id):
            return graph_id == self._graph.graph_id

        def get_merged_patches(self, graph_id):
            assert graph_id == self._graph.graph_id
            return dict(self._patches)

    graph = ExecutionGraph(
        graph_id="graph-l3-001",
        op_id="op-l3-001",
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=2,
        units=(
            WorkUnitSpec(
                unit_id="u1",
                repo="jarvis",
                goal="update jarvis file",
                target_files=("backend/core/utils.py",),
                owned_paths=("backend/core/utils.py",),
            ),
            WorkUnitSpec(
                unit_id="u2",
                repo="prime",
                goal="update prime file",
                target_files=("prime/router.py",),
                owned_paths=("prime/router.py",),
            ),
        ),
    )
    patches = {
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/core/utils.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/core/utils.py", b"def jarvis_patch():\n    return True\n"),),
        ),
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="prime/router.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("prime/router.py", b"def route():\n    return 'prime'\n"),),
        ),
    }
    state = GraphExecutionState(
        graph=graph,
        phase=GraphExecutionPhase.COMPLETED,
        completed_units=("u1", "u2"),
        results={
            "u1": WorkUnitResult(
                unit_id="u1",
                repo="jarvis",
                status=WorkUnitState.COMPLETED,
                patch=patches["jarvis"],
                attempt_count=1,
                started_at_ns=1,
                finished_at_ns=2,
                causal_parent_id=graph.causal_trace_id,
            ),
            "u2": WorkUnitResult(
                unit_id="u2",
                repo="prime",
                status=WorkUnitState.COMPLETED,
                patch=patches["prime"],
                attempt_count=1,
                started_at_ns=1,
                finished_at_ns=2,
                causal_parent_id=graph.causal_trace_id,
            ),
        },
    )
    scheduler = _FakeScheduler(graph, patches, state)

    generator = _mock_generator(candidates=(
        {
            "candidate_id": "l3-graph",
            "execution_graph": graph,
            "rationale": "parallel plan",
        },
    ))
    config = OrchestratorConfig(
        project_root=Path("/tmp/test-project"),
        generation_timeout_s=5.0,
        validation_timeout_s=5.0,
        approval_timeout_s=5.0,
        max_generate_retries=1,
        max_validate_retries=2,
        execution_graph_scheduler=scheduler,
    )
    stack = _mock_stack()
    orch = GovernedOrchestrator(
        stack=stack,
        generator=generator,
        approval_provider=None,
        config=config,
    )

    captured: Dict[str, Dict] = {}

    async def _fake_saga_apply(ctx, candidate):
        captured["candidate"] = candidate
        captured["ctx"] = ctx
        return ctx.advance(OperationPhase.VERIFY).advance(OperationPhase.COMPLETE)

    orch._execute_saga_apply = AsyncMock(side_effect=_fake_saga_apply)

    ctx = OperationContext.create(
        target_files=("backend/core/utils.py", "prime/router.py"),
        description="parallel cross-repo patch",
        op_id="op-l3-001",
        _timestamp=_FIXED_TS,
        primary_repo="jarvis",
        repo_scope=("jarvis", "prime"),
    )
    result = await orch.run(ctx)

    assert result.phase is OperationPhase.COMPLETE
    assert result.execution_graph_id == graph.graph_id
    assert result.execution_plan_digest == graph.plan_digest
    scheduler.submit.assert_awaited_once_with(graph)
    scheduler.wait_for_graph.assert_awaited_once()
    assert captured["candidate"]["patches"] == patches

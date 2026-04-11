# tests/test_ouroboros_governance/test_orchestrator_l2.py
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine, RepairResult
from backend.core.ouroboros.governance.op_context import OperationPhase, ValidationResult


def _failing_val():
    return ValidationResult(
        passed=False,
        best_candidate=None,
        validation_duration_s=0.0,
        error="boom",
        failure_class="test",
    )


def _deadline(seconds: float = 300.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class TestOrchestratorRepairEngineField:
    def test_default_repair_engine_is_none(self, tmp_path):
        cfg = OrchestratorConfig(project_root=tmp_path)
        assert cfg.repair_engine is None

    def test_can_set_repair_engine(self, tmp_path):
        budget = RepairBudget(enabled=True)
        engine = RepairEngine(budget=budget, prime_provider=MagicMock(), repo_root=tmp_path)
        cfg = OrchestratorConfig(project_root=tmp_path, repair_engine=engine)
        assert cfg.repair_engine is engine

    def test_repair_engine_none_means_no_l2(self, tmp_path):
        """Explicit invariant: repair_engine=None means L2 is disabled."""
        cfg = OrchestratorConfig(project_root=tmp_path)
        assert cfg.repair_engine is None

    def test_l2_converged_result_carries_candidate(self):
        candidate = {"candidate_id": "c1", "file_path": "f.py"}
        result = RepairResult(
            terminal="L2_CONVERGED", candidate=candidate,
            stop_reason=None, summary={}, iterations=(),
        )
        assert result.terminal == "L2_CONVERGED"
        assert result.candidate is candidate

    def test_l2_stopped_result_has_stop_reason(self):
        result = RepairResult(
            terminal="L2_STOPPED", candidate=None,
            stop_reason="max_iterations_exhausted", summary={}, iterations=(),
        )
        assert result.stop_reason == "max_iterations_exhausted"


class TestOrchestratorL2Hook:
    """Unit tests for GovernedOrchestrator._l2_hook (added in Step 4b)."""

    def _make_orchestrator(self, tmp_path, engine):
        cfg = OrchestratorConfig(project_root=tmp_path, repair_engine=engine)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        orch._record_ledger = AsyncMock()
        return orch

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self, tmp_path):
        """When engine.run raises CancelledError, _l2_hook re-raises it."""
        engine = MagicMock()
        engine.run = AsyncMock(side_effect=asyncio.CancelledError())
        orch = self._make_orchestrator(tmp_path, engine)
        ctx = MagicMock()
        ctx.advance = MagicMock(return_value=ctx)
        with pytest.raises(asyncio.CancelledError):
            await orch._l2_hook(ctx, _failing_val(), _deadline())

    @pytest.mark.asyncio
    async def test_l2_stopped_returns_cancel_directive(self, tmp_path):
        """When engine returns L2_STOPPED, _l2_hook returns ('cancel',)."""
        engine = MagicMock()
        engine.run = AsyncMock(return_value=RepairResult(
            terminal="L2_STOPPED", candidate=None,
            stop_reason="max_iterations_exhausted", summary={}, iterations=(),
        ))
        orch = self._make_orchestrator(tmp_path, engine)
        ctx = MagicMock()
        ctx.advance = MagicMock(return_value=ctx)
        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())
        assert directive[0] == "cancel"

    @pytest.mark.asyncio
    async def test_l2_converged_canonical_pass_returns_break(self, tmp_path):
        """When engine converges and canonical VALIDATE passes, returns ('break', candidate, val)."""
        candidate = {"candidate_id": "c1", "file_path": "f.py",
                     "unified_diff": "@@ -1 +1 @@\n-x=1\n+x=2"}
        engine = MagicMock()
        engine.run = AsyncMock(return_value=RepairResult(
            terminal="L2_CONVERGED", candidate=candidate,
            stop_reason=None, summary={}, iterations=(),
        ))
        canonical_val = ValidationResult(passed=True, best_candidate=candidate,
                                         validation_duration_s=0.1, error=None)
        orch = self._make_orchestrator(tmp_path, engine)
        orch._run_validation = AsyncMock(return_value=canonical_val)
        ctx = MagicMock()
        ctx.advance = MagicMock(return_value=ctx)
        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())
        assert directive[0] == "break"
        assert directive[1] is candidate
        assert directive[2] is canonical_val


class TestL2EscapeTerminalSelection:
    """Phase-aware terminal selection for L2 escapes (regression for the
    ``VERIFY -> CANCELLED`` illegal-transition bug).

    Rule: once code has touched disk (APPLY / VERIFY), an L2 escape is a
    regression requiring forensics → POSTMORTEM. Before that, the op hasn't
    altered any files, so a graceful abort is a user-level CANCELLED.
    """

    @pytest.mark.parametrize(
        "phase, expected",
        [
            # Pre-apply phases → graceful CANCELLED
            (OperationPhase.CLASSIFY, OperationPhase.CANCELLED),
            (OperationPhase.ROUTE, OperationPhase.CANCELLED),
            (OperationPhase.CONTEXT_EXPANSION, OperationPhase.CANCELLED),
            (OperationPhase.PLAN, OperationPhase.CANCELLED),
            (OperationPhase.GENERATE, OperationPhase.CANCELLED),
            (OperationPhase.GENERATE_RETRY, OperationPhase.CANCELLED),
            (OperationPhase.VALIDATE, OperationPhase.CANCELLED),
            (OperationPhase.VALIDATE_RETRY, OperationPhase.CANCELLED),
            (OperationPhase.GATE, OperationPhase.CANCELLED),
            (OperationPhase.APPROVE, OperationPhase.CANCELLED),
            # Post-apply phases → forensic POSTMORTEM
            (OperationPhase.APPLY, OperationPhase.POSTMORTEM),
            (OperationPhase.VERIFY, OperationPhase.POSTMORTEM),
        ],
    )
    def test_escape_terminal_for_each_phase(self, phase, expected):
        """Every non-terminal phase maps to the correct escape terminal."""
        result = GovernedOrchestrator._l2_escape_terminal(phase)
        assert result == expected, (
            f"From {phase.name}, expected escape -> {expected.name}, "
            f"got {result.name}"
        )

    def test_post_apply_phases_set_is_exhaustive(self):
        """The POST_APPLY_PHASES frozen-set must include every post-disk phase.

        If a new phase is added after APPLY (e.g. a future POST_VERIFY), it
        must be added to _POST_APPLY_PHASES or the escape classification will
        silently misroute regressions as cancellations.
        """
        assert OperationPhase.APPLY in GovernedOrchestrator._POST_APPLY_PHASES
        assert OperationPhase.VERIFY in GovernedOrchestrator._POST_APPLY_PHASES
        # Terminal phases should never appear in the set — they never call L2.
        assert OperationPhase.COMPLETE not in GovernedOrchestrator._POST_APPLY_PHASES
        assert OperationPhase.POSTMORTEM not in GovernedOrchestrator._POST_APPLY_PHASES


class TestL2HookPhaseAwareTerminal:
    """Integration tests: _l2_hook emits the right terminal based on entry phase."""

    def _make_orchestrator(self, tmp_path, engine):
        cfg = OrchestratorConfig(project_root=tmp_path, repair_engine=engine)
        orch = GovernedOrchestrator(
            stack=MagicMock(),
            generator=MagicMock(),
            approval_provider=MagicMock(),
            config=cfg,
        )
        orch._record_ledger = AsyncMock()
        return orch

    def _make_ctx(self, phase: OperationPhase):
        """Make a mock ctx whose advance() records the target phase."""
        ctx = MagicMock()
        ctx.phase = phase
        ctx.op_id = "op-test"
        advanced = MagicMock()
        advanced.phase = phase  # will be overridden by advance() call
        ctx.advance = MagicMock(
            side_effect=lambda new_phase, **_: MagicMock(
                phase=new_phase, op_id="op-test",
            )
        )
        return ctx

    @pytest.mark.asyncio
    async def test_l2_stopped_from_verify_emits_postmortem(self, tmp_path):
        """L2_STOPPED from VERIFY advances ctx to POSTMORTEM (not CANCELLED)."""
        engine = MagicMock()
        engine.run = AsyncMock(return_value=RepairResult(
            terminal="L2_STOPPED", candidate=None,
            stop_reason="max_iterations_exhausted", summary={}, iterations=(),
        ))
        orch = self._make_orchestrator(tmp_path, engine)
        ctx = self._make_ctx(OperationPhase.VERIFY)

        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())

        assert directive[0] == "cancel"
        # ctx.advance was called with POSTMORTEM (post-apply rule)
        ctx.advance.assert_called_once()
        call_args = ctx.advance.call_args
        assert call_args.args[0] == OperationPhase.POSTMORTEM

    @pytest.mark.asyncio
    async def test_l2_stopped_from_validate_emits_cancelled(self, tmp_path):
        """L2_STOPPED from VALIDATE advances ctx to CANCELLED (pre-apply rule)."""
        engine = MagicMock()
        engine.run = AsyncMock(return_value=RepairResult(
            terminal="L2_STOPPED", candidate=None,
            stop_reason="max_iterations_exhausted", summary={}, iterations=(),
        ))
        orch = self._make_orchestrator(tmp_path, engine)
        ctx = self._make_ctx(OperationPhase.VALIDATE)

        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())

        assert directive[0] == "cancel"
        ctx.advance.assert_called_once()
        call_args = ctx.advance.call_args
        assert call_args.args[0] == OperationPhase.CANCELLED

    @pytest.mark.asyncio
    async def test_l2_canonical_validate_failed_from_verify_emits_postmortem(
        self, tmp_path,
    ):
        """Converged but canonical-validate-failed from VERIFY → POSTMORTEM."""
        candidate = {"candidate_id": "c1", "file_path": "f.py"}
        engine = MagicMock()
        engine.run = AsyncMock(return_value=RepairResult(
            terminal="L2_CONVERGED", candidate=candidate,
            stop_reason=None, summary={}, iterations=(),
        ))
        orch = self._make_orchestrator(tmp_path, engine)
        # canonical validation fails
        orch._run_validation = AsyncMock(return_value=ValidationResult(
            passed=False, best_candidate=candidate,
            validation_duration_s=0.1, error="still failing",
        ))
        ctx = self._make_ctx(OperationPhase.VERIFY)

        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())

        assert directive[0] == "cancel"
        call_args = ctx.advance.call_args
        assert call_args.args[0] == OperationPhase.POSTMORTEM

    @pytest.mark.asyncio
    async def test_l2_fatal_always_emits_postmortem_regardless_of_phase(
        self, tmp_path,
    ):
        """Engine exceptions are always forensic → POSTMORTEM from any phase."""
        engine = MagicMock()
        engine.run = AsyncMock(side_effect=RuntimeError("engine exploded"))
        orch = self._make_orchestrator(tmp_path, engine)
        ctx = self._make_ctx(OperationPhase.VALIDATE)

        directive = await orch._l2_hook(ctx, _failing_val(), _deadline())

        assert directive[0] == "fatal"
        call_args = ctx.advance.call_args
        assert call_args.args[0] == OperationPhase.POSTMORTEM

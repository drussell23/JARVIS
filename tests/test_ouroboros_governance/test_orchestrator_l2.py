# tests/test_ouroboros_governance/test_orchestrator_l2.py
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.repair_engine import RepairBudget, RepairEngine, RepairResult
from backend.core.ouroboros.governance.op_context import ValidationResult


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

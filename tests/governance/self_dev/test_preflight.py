"""Tests for Phase 2B connectivity preflight in GovernedLoopService.submit()."""
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    MIN_GENERATION_BUDGET_S,
)
from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_service(fsm_state_name="PRIMARY_READY", primary_health=True):
    """Build a GovernedLoopService with mocked internals."""
    config = GovernedLoopConfig(
        project_root=REPO_ROOT,
        pipeline_timeout_s=300.0,
    )
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._config = config
    svc._started = True

    # Mock generator with FSM state and primary.health_probe
    mock_generator = MagicMock()
    mock_generator._fsm_state = MagicMock()
    mock_generator._fsm_state.name = fsm_state_name
    mock_generator.primary = MagicMock()
    mock_generator.primary.health_probe = AsyncMock(return_value=primary_health)
    svc._generator = mock_generator

    # Mock orchestrator that returns a terminal ctx
    mock_orch = MagicMock()
    mock_orch.run = AsyncMock(side_effect=lambda ctx: ctx.advance(OperationPhase.COMPLETE))
    svc._orchestrator = mock_orch

    # Mock ledger
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    svc._ledger = mock_ledger

    return svc


def _ctx(deadline_s=300.0):
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=deadline_s)
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="test op",
        pipeline_deadline=dl,
    )


# ── MIN_GENERATION_BUDGET_S constant ─────────────────────────────────────

def test_min_generation_budget_s_is_float():
    assert isinstance(MIN_GENERATION_BUDGET_S, float)
    assert MIN_GENERATION_BUDGET_S > 0


# ── Budget pre-check ──────────────────────────────────────────────────────

async def test_budget_exhausted_pre_generation_cancels():
    """remaining_s < MIN_GENERATION_BUDGET_S → CANCELLED before generation."""
    svc = _make_service()
    # Deadline already very close (1 second remaining)
    ctx = _ctx(deadline_s=1.0)
    terminal = await svc._preflight_check(ctx)
    assert terminal is not None
    assert terminal.phase == OperationPhase.CANCELLED


# ── QUEUE_ONLY preflight ──────────────────────────────────────────────────

async def test_queue_only_state_cancels():
    """FSM in QUEUE_ONLY + primary unhealthy → CANCELLED."""
    svc = _make_service(fsm_state_name="QUEUE_ONLY", primary_health=False)
    ctx = _ctx(deadline_s=300.0)
    terminal = await svc._preflight_check(ctx)
    assert terminal is not None
    assert terminal.phase == OperationPhase.CANCELLED


# ── Primary unavailable with fallback ────────────────────────────────────

async def test_primary_unavailable_fallback_active_continues():
    """Primary unhealthy, FSM != QUEUE_ONLY → returns None (continue)."""
    svc = _make_service(fsm_state_name="FALLBACK_ACTIVE", primary_health=False)
    ctx = _ctx(deadline_s=300.0)
    result = await svc._preflight_check(ctx)
    # None means "proceed — no early exit"
    assert result is None


# ── Healthy primary ───────────────────────────────────────────────────────

async def test_healthy_primary_continues():
    """Primary healthy → returns None (continue)."""
    svc = _make_service(fsm_state_name="PRIMARY_READY", primary_health=True)
    ctx = _ctx(deadline_s=300.0)
    result = await svc._preflight_check(ctx)
    assert result is None

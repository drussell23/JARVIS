"""Tests for Phase 2B connectivity preflight in GovernedLoopService.submit()."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.candidate_generator import FailbackState
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    MIN_GENERATION_BUDGET_S,
)
from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_service(fsm_state: FailbackState = FailbackState.PRIMARY_READY, primary_health: bool = True):
    """Build a GovernedLoopService with mocked internals matching real CandidateGenerator API."""
    config = GovernedLoopConfig(
        project_root=REPO_ROOT,
        pipeline_timeout_s=300.0,
    )
    svc = GovernedLoopService.__new__(GovernedLoopService)
    svc._config = config

    # Mock generator using real CandidateGenerator attribute names:
    #   ._primary  → primary provider (has .health_probe())
    #   .fsm.state → FailbackState enum value
    mock_primary = MagicMock()
    mock_primary.health_probe = AsyncMock(return_value=primary_health)

    mock_fsm = MagicMock()
    mock_fsm.state = fsm_state

    mock_generator = MagicMock()
    mock_generator._primary = mock_primary
    mock_generator.fsm = mock_fsm
    svc._generator = mock_generator

    # Mock orchestrator
    mock_orch = MagicMock()
    mock_orch.run = AsyncMock(side_effect=lambda ctx: ctx.advance(OperationPhase.COMPLETE))
    svc._orchestrator = mock_orch

    # Mock ledger
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    svc._ledger = mock_ledger

    # Attributes initialised by __init__ that _preflight_check reads
    import collections as _coll
    svc._file_touch_cache = _coll.defaultdict(_coll.deque)
    svc._vm_capability = None  # no GPU VM in unit tests
    svc._fsm_executor = None   # FSM not started in unit tests

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
    svc = _make_service(fsm_state=FailbackState.QUEUE_ONLY, primary_health=False)
    ctx = _ctx(deadline_s=300.0)
    terminal = await svc._preflight_check(ctx)
    assert terminal is not None
    assert terminal.phase == OperationPhase.CANCELLED


# ── Primary unavailable with fallback ────────────────────────────────────

async def test_primary_unavailable_fallback_active_continues():
    """Primary unhealthy, FSM != QUEUE_ONLY → returns None (continue)."""
    svc = _make_service(fsm_state=FailbackState.FALLBACK_ACTIVE, primary_health=False)
    ctx = _ctx(deadline_s=300.0)
    result = await svc._preflight_check(ctx)
    # None means "proceed — no early exit"
    assert result is None


# ── Healthy primary ───────────────────────────────────────────────────────

async def test_healthy_primary_continues():
    """Primary healthy → returns None (continue)."""
    svc = _make_service(fsm_state=FailbackState.PRIMARY_READY, primary_health=True)
    ctx = _ctx(deadline_s=300.0)
    result = await svc._preflight_check(ctx)
    assert result is None

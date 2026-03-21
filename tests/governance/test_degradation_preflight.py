"""
Tests for DegradationController gate in GovernedLoopService._preflight_check().

Gates under test:
- READ_ONLY_PLANNING / EMERGENCY_STOP  -> cancel ANY op
- REDUCED_AUTONOMY                     -> cancel ops without SAFE_AUTO risk tier
- FULL_AUTONOMY                        -> pass through (return None)
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.degradation import (
    DegradationController,
    DegradationMode,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAR_FUTURE = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=600)


def _make_gls_with_degradation(mode: DegradationMode):
    """Create a minimal GovernedLoopService instance with a forced degradation mode."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService

    deg = DegradationController()
    deg._mode = mode  # force internal state

    mock_stack = MagicMock()
    mock_stack.degradation = deg

    gls = object.__new__(GovernedLoopService)
    gls._stack = mock_stack
    gls._file_touch_cache = {}
    gls._generator = None
    gls._ledger = None
    gls._vm_capability = None
    # FSM attributes accessed after probe failure — set to None to skip those paths
    gls._fsm_executor = None
    gls._fsm_contexts = {}
    gls._fsm_checkpoint_seq = {}
    gls._brain_selector = None
    return gls


def _make_ctx(*, risk_tier: "RiskTier | None" = None) -> OperationContext:
    """Create a minimal OperationContext with a far-future deadline and optional risk_tier."""
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="test op",
        primary_repo="jarvis",
    )
    # Stamp a far-future deadline so the budget check does not cancel the op.
    ctx = ctx.with_pipeline_deadline(_FAR_FUTURE)
    if risk_tier is not None:
        # Use dataclasses.replace directly — advance() validates phase transitions
        # and CLASSIFY->CLASSIFY is illegal.  The risk_tier field is mutable via
        # replace() without a phase change (same pattern as with_pipeline_deadline).
        ctx = dataclasses.replace(ctx, risk_tier=risk_tier)
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emergency_stop_cancels_op():
    """EMERGENCY_STOP mode must cancel any op regardless of risk tier."""
    gls = _make_gls_with_degradation(DegradationMode.EMERGENCY_STOP)
    ctx = _make_ctx()

    result = asyncio.get_event_loop().run_until_complete(gls._preflight_check(ctx))

    assert result is not None, "Expected a cancelled context, got None (pass-through)"
    assert result.phase == OperationPhase.CANCELLED


def test_read_only_planning_cancels_op():
    """READ_ONLY_PLANNING mode must cancel any op."""
    gls = _make_gls_with_degradation(DegradationMode.READ_ONLY_PLANNING)
    ctx = _make_ctx()

    result = asyncio.get_event_loop().run_until_complete(gls._preflight_check(ctx))

    assert result is not None, "Expected a cancelled context, got None (pass-through)"
    assert result.phase == OperationPhase.CANCELLED


def test_full_autonomy_passes_through():
    """FULL_AUTONOMY mode must not block — _preflight_check returns None."""
    gls = _make_gls_with_degradation(DegradationMode.FULL_AUTONOMY)
    ctx = _make_ctx()

    result = asyncio.get_event_loop().run_until_complete(gls._preflight_check(ctx))

    assert result is None, (
        f"FULL_AUTONOMY should not cancel the op, but got "
        f"phase={result.phase if result else 'None'}"
    )


def test_reduced_autonomy_blocks_non_safe_op():
    """REDUCED_AUTONOMY mode must cancel ops whose risk_tier is not SAFE_AUTO.

    At preflight time risk_tier is typically None (set during CLASSIFY).
    None is treated as 'not SAFE_AUTO' and must be blocked (fail-safe).
    """
    gls = _make_gls_with_degradation(DegradationMode.REDUCED_AUTONOMY)
    # risk_tier=None is the real-world preflight scenario
    ctx = _make_ctx(risk_tier=None)

    result = asyncio.get_event_loop().run_until_complete(gls._preflight_check(ctx))

    assert result is not None, (
        "REDUCED_AUTONOMY with no risk tier should cancel (fail-safe), got None"
    )
    assert result.phase == OperationPhase.CANCELLED


def test_reduced_autonomy_allows_safe_auto_op():
    """REDUCED_AUTONOMY mode must allow ops pre-stamped with SAFE_AUTO tier."""
    gls = _make_gls_with_degradation(DegradationMode.REDUCED_AUTONOMY)
    ctx = _make_ctx(risk_tier=RiskTier.SAFE_AUTO)

    result = asyncio.get_event_loop().run_until_complete(gls._preflight_check(ctx))

    assert result is None, (
        f"REDUCED_AUTONOMY with SAFE_AUTO tier should pass through, "
        f"but got phase={result.phase if result else 'None'}"
    )

"""Tests for HUD governance boot (Sub-project E)."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HudGovernanceContext
# ---------------------------------------------------------------------------

def test_hud_gov_context_is_active_when_gls_active():
    """is_active returns True when GLS state is ACTIVE."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "ACTIVE"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=MagicMock())
    assert ctx.is_active is True


def test_hud_gov_context_is_active_when_degraded():
    """is_active returns True when GLS state is DEGRADED."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "DEGRADED"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=MagicMock())
    assert ctx.is_active is True


def test_hud_gov_context_inactive_when_gls_none():
    """is_active returns False when GLS is None."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    ctx = HudGovernanceContext(stack=None, gls=None, intake=None)
    assert ctx.is_active is False


def test_hud_gov_context_inactive_when_gls_failed():
    """is_active returns False when GLS state is FAILED."""
    from backend.core.ouroboros.governance.hud_governance_boot import HudGovernanceContext
    gls = MagicMock()
    gls.state = MagicMock()
    gls.state.name = "FAILED"
    ctx = HudGovernanceContext(stack=MagicMock(), gls=gls, intake=None)
    assert ctx.is_active is False


# ---------------------------------------------------------------------------
# stop_hud_governance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_hud_governance_handles_none_components():
    """Shutdown with all-None components does not raise."""
    from backend.core.ouroboros.governance.hud_governance_boot import (
        HudGovernanceContext, stop_hud_governance,
    )
    ctx = HudGovernanceContext(stack=None, gls=None, intake=None)
    await stop_hud_governance(ctx)  # must not raise


@pytest.mark.asyncio
async def test_stop_hud_governance_calls_stop_reverse_order():
    """Shutdown calls stop in reverse order: intake → gls → stack."""
    from backend.core.ouroboros.governance.hud_governance_boot import (
        HudGovernanceContext, stop_hud_governance,
    )
    call_order = []
    intake = MagicMock()
    intake.stop = AsyncMock(side_effect=lambda: call_order.append("intake"))
    gls = MagicMock()
    gls.stop = AsyncMock(side_effect=lambda: call_order.append("gls"))
    stack = MagicMock()
    stack.stop = AsyncMock(side_effect=lambda: call_order.append("stack"))
    stack.governed_loop_service = gls

    ctx = HudGovernanceContext(stack=stack, gls=gls, intake=intake)
    await stop_hud_governance(ctx)
    assert call_order == ["intake", "gls", "stack"]

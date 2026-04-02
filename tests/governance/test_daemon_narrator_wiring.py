"""Tests for Sub-project D: DaemonNarrator wiring."""
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Task 1: GovernedLoopService say_fn wiring
# ---------------------------------------------------------------------------

def test_gls_accepts_say_fn():
    """GLS constructor accepts and stores say_fn."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    mock_say = AsyncMock(return_value=True)
    gls = GovernedLoopService(say_fn=mock_say)
    assert gls._say_fn is mock_say


def test_gls_say_fn_default_none():
    """GLS defaults say_fn to None for tests/headless."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    gls = GovernedLoopService()
    assert gls._say_fn is None

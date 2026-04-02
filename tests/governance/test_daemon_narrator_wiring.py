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


# ---------------------------------------------------------------------------
# Task 3: CommProtocol target_files extension
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_decision_target_files():
    """emit_decision includes target_files in payload when provided."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_decision(
        op_id="op-1",
        outcome="blocked",
        reason_code="duplication",
        target_files=["module.py"],
    )
    assert len(emitted) == 1
    assert emitted[0].payload["target_files"] == ["module.py"]


@pytest.mark.asyncio
async def test_emit_postmortem_target_files():
    """emit_postmortem includes target_files in payload when provided."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_postmortem(
        op_id="op-1",
        root_cause="verify_regression: pass_rate=0.85",
        failed_phase="VERIFY",
        target_files=["module.py"],
    )
    assert len(emitted) == 1
    assert emitted[0].payload["target_files"] == ["module.py"]


@pytest.mark.asyncio
async def test_emit_decision_without_target_files():
    """emit_decision without target_files does not include it in payload."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_decision(
        op_id="op-1",
        outcome="applied",
        reason_code="success",
    )
    assert "target_files" not in emitted[0].payload

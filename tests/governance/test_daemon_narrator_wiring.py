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


# ---------------------------------------------------------------------------
# Task 4: VoiceNarrator routing + narration templates
# ---------------------------------------------------------------------------

def test_map_phase_duplication():
    """reason_code=duplication → 'duplication_blocked'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "blocked", "reason_code": "duplication"},
    )
    assert VoiceNarrator._map_phase(msg) == "duplication_blocked"


def test_map_phase_similarity():
    """reason_code=similarity_escalation → 'similarity_escalated'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "escalated", "reason_code": "similarity_escalation"},
    )
    assert VoiceNarrator._map_phase(msg) == "similarity_escalated"


def test_map_phase_verify_regression():
    """root_cause starting with 'verify_regression' → 'verify_regression'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.POSTMORTEM,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"root_cause": "verify_regression: pass_rate=0.85", "failed_phase": "VERIFY"},
    )
    assert VoiceNarrator._map_phase(msg) == "verify_regression"


def test_map_phase_generic_decision_unchanged():
    """Existing outcome routing still works for non-gate decisions."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "applied", "reason_code": "success"},
    )
    assert VoiceNarrator._map_phase(msg) == "applied"


def test_narration_template_duplication():
    """duplication_blocked template renders with file context."""
    from backend.core.ouroboros.governance.comms.narrator_script import format_narration
    result = format_narration("duplication_blocked", {"file": "module.py", "op_id": "op-1"})
    assert result is not None
    assert "module.py" in result
    assert "duplicat" in result.lower()


def test_narration_template_verify():
    """verify_regression template renders with file and root_cause."""
    from backend.core.ouroboros.governance.comms.narrator_script import format_narration
    result = format_narration("verify_regression", {
        "file": "module.py",
        "root_cause": "pass_rate=0.85 < threshold=1.00",
        "op_id": "op-1",
    })
    assert result is not None
    assert "module.py" in result
    assert "pass_rate" in result

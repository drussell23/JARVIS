"""tests/governance/self_dev/test_notifications.py

Verify notification emission through CommProtocol to transports.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.comm_protocol import (
    CommMessage,
    CommProtocol,
    LogTransport,
    MessageType,
)
from backend.core.ouroboros.governance.comms import VoiceNarrator


def test_heartbeat_emits_approve_phase():
    """Emitting heartbeat with phase='approve' reaches transports."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_heartbeat(op_id="op-1", phase="approve", progress_pct=0.0)
    )
    assert len(transport.messages) == 1
    assert transport.messages[0].payload["phase"] == "approve"


def test_decision_emits_for_approval():
    """Decision message with outcome='escalated' reaches transports."""
    transport = LogTransport()
    comm = CommProtocol(transports=[transport])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_decision(
            op_id="op-1",
            outcome="escalated",
            reason_code="approval_required",
            diff_summary="Change to tests/test_foo.py",
        )
    )
    assert len(transport.messages) == 1
    assert transport.messages[0].payload["outcome"] == "escalated"


def test_voice_narrator_skips_heartbeat():
    """VoiceNarrator does not narrate HEARTBEAT messages."""
    say_fn = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=say_fn, debounce_s=0)
    msg = CommMessage(
        msg_type=MessageType.HEARTBEAT,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"phase": "sandbox", "progress_pct": 20.0},
    )
    asyncio.get_event_loop().run_until_complete(narrator.send(msg))
    say_fn.assert_not_called()


def test_voice_narrator_narrates_intent():
    """VoiceNarrator narrates INTENT messages."""
    say_fn = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=say_fn, debounce_s=0)
    msg = CommMessage(
        msg_type=MessageType.INTENT,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={
            "goal": "fix test",
            "target_files": ["test_foo.py"],
            "risk_tier": "SAFE_AUTO",
            "blast_radius": 1,
        },
    )
    asyncio.get_event_loop().run_until_complete(narrator.send(msg))
    say_fn.assert_called_once()


def test_transport_failure_does_not_block():
    """A failing transport does not prevent delivery to healthy ones."""
    good = LogTransport()
    bad = MagicMock()
    bad.send = AsyncMock(side_effect=RuntimeError("transport down"))
    comm = CommProtocol(transports=[bad, good])
    asyncio.get_event_loop().run_until_complete(
        comm.emit_intent(
            op_id="op-1",
            goal="fix",
            target_files=["f.py"],
            risk_tier="SAFE_AUTO",
            blast_radius=1,
        )
    )
    assert len(good.messages) == 1


def test_voice_narrator_failure_does_not_raise():
    """If say_fn fails, VoiceNarrator swallows the error."""
    say_fn = AsyncMock(side_effect=RuntimeError("TTS down"))
    narrator = VoiceNarrator(say_fn=say_fn, debounce_s=0)
    msg = CommMessage(
        msg_type=MessageType.INTENT,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={
            "goal": "fix",
            "target_files": [],
            "risk_tier": "SAFE_AUTO",
            "blast_radius": 1,
        },
    )
    # Should not raise
    asyncio.get_event_loop().run_until_complete(narrator.send(msg))

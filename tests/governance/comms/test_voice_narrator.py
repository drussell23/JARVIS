"""tests/governance/comms/test_voice_narrator.py"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_safe_say():
    return AsyncMock(return_value=True)


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestVoiceNarratorSend:
    @pytest.mark.asyncio
    async def test_narrates_intent_message(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix test",
            "target_files": ["tests/test_a.py"],
        })
        await narrator.send(msg)
        mock_safe_say.assert_called_once()
        call_text = mock_safe_say.call_args[0][0]
        assert isinstance(call_text, str)
        assert len(call_text) > 0

    @pytest.mark.asyncio
    async def test_narrates_decision_message(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("DECISION", payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
            "diff_summary": "added edge case",
        })
        await narrator.send(msg)
        mock_safe_say.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_heartbeat(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("HEARTBEAT", payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        await narrator.send(msg)
        mock_safe_say.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_plan(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("PLAN", payload={"steps": ["step1"]})
        await narrator.send(msg)
        mock_safe_say.assert_not_called()


class TestVoiceNarratorDebounce:
    @pytest.mark.asyncio
    async def test_debounce_blocks_rapid_narrations(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=60.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"],
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"],
        })
        await narrator.send(msg1)
        await narrator.send(msg2)
        assert mock_safe_say.call_count == 1  # second blocked by debounce

    @pytest.mark.asyncio
    async def test_debounce_allows_after_expiry(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"],
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"],
        })
        await narrator.send(msg1)
        await asyncio.sleep(0.01)
        await narrator.send(msg2)
        assert mock_safe_say.call_count == 2


class TestVoiceNarratorIdempotency:
    @pytest.mark.asyncio
    async def test_same_op_same_phase_not_repeated(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("DECISION", op_id="op-001", payload={
            "outcome": "applied",
        })
        await narrator.send(msg)
        await narrator.send(msg)  # same op_id + same msg_type
        assert mock_safe_say.call_count == 1


class TestVoiceNarratorFailure:
    @pytest.mark.asyncio
    async def test_say_failure_does_not_propagate(self):
        failing_say = AsyncMock(side_effect=RuntimeError("TTS broke"))
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=failing_say, debounce_s=0.0)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix", "target_files": ["a.py"],
        })
        await narrator.send(msg)  # should not raise

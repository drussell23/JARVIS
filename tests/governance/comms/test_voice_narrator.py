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
            "test_count": 3,
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
            "file": "tests/test_a.py",
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
            "goal": "fix 1", "target_files": ["a.py"], "test_count": 1,
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"], "test_count": 2,
        })
        await narrator.send(msg1)
        await narrator.send(msg2)
        assert mock_safe_say.call_count == 1  # second blocked by debounce

    @pytest.mark.asyncio
    async def test_debounce_allows_after_expiry(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"], "test_count": 1,
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"], "test_count": 2,
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
            "file": "tests/test_a.py",
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
            "goal": "fix", "target_files": ["a.py"], "test_count": 1,
        })
        await narrator.send(msg)  # should not raise


# ---------------------------------------------------------------------------
# TestSeverityAwareDebounce
# ---------------------------------------------------------------------------


class TestSeverityAwareDebounce:
    """DECISION and POSTMORTEM bypass debounce; INTENT is rate-limited."""

    def _make_narrator(self, debounce_s: float = 60.0):
        from unittest.mock import AsyncMock
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
        say = AsyncMock(return_value=True)
        narrator = VoiceNarrator(say_fn=say, debounce_s=debounce_s, source="test")
        return narrator, say

    def _make_msg(self, msg_type, op_id: str = "op-1"):
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        # Use real payloads with required context so narration is not suppressed
        if msg_type == MessageType.INTENT:
            payload = {"goal": "fix", "target_files": ["a.py"], "test_count": 1}
        elif msg_type == MessageType.POSTMORTEM:
            payload = {"file": "a.py", "root_cause": "AST parse failed"}
        else:  # DECISION
            payload = {"outcome": "applied", "file": "a.py"}
        return _make_comm_message(msg_type.name, op_id=op_id, payload=payload)

    async def test_postmortem_bypasses_debounce(self):
        """POSTMORTEM narrates even within debounce window."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, say = self._make_narrator(debounce_s=3600.0)

        # First INTENT narrates and sets _last_narration
        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        assert say.call_count == 1

        # POSTMORTEM for a different op must narrate despite debounce window
        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-2"))
        assert say.call_count == 2, (
            f"POSTMORTEM was suppressed by debounce (call_count={say.call_count})"
        )

    async def test_decision_bypasses_debounce(self):
        """DECISION narrates even within debounce window."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, say = self._make_narrator(debounce_s=3600.0)

        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        assert say.call_count == 1

        await narrator.send(self._make_msg(MessageType.DECISION, "op-2"))
        assert say.call_count == 2, (
            f"DECISION was suppressed by debounce (call_count={say.call_count})"
        )

    async def test_intent_is_debounced(self):
        """INTENT respects debounce window (second INTENT within window is dropped)."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, say = self._make_narrator(debounce_s=3600.0)

        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        assert say.call_count == 1

        await narrator.send(self._make_msg(MessageType.INTENT, "op-2"))
        assert say.call_count == 1, "Second INTENT within window should be debounced"

    async def test_idempotency_still_blocks_duplicate_postmortem(self):
        """Same op_id + same msg_type is idempotent even without debounce."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, say = self._make_narrator(debounce_s=0.0)

        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-1"))
        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-1"))  # duplicate
        assert say.call_count == 1, "Idempotency guard should block duplicate op_id+type"

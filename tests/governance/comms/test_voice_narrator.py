"""tests/governance/comms/test_voice_narrator.py

Sprint 2 Task 7: narrator_script.py's static templates are eradicated.
VoiceNarrator now drives an injected KarenSpeechSynthesizer + arbiter
(gated on JARVIS_KAREN_SYNTH_ENABLED); with no injection it is a silent
no-op. These tests exercise routing/debounce/idempotency/fault-isolation
against the new synthesizer+arbiter contract instead of asserting on
say_fn + rendered template text.
"""
import asyncio
import time
from typing import AsyncIterator, List

import pytest
from unittest.mock import AsyncMock


class _FakeSynthesizer:
    """Yields canned sentences; records the LedgerView it was given."""

    def __init__(self, sentences=None, raises: bool = False) -> None:
        self._sentences = sentences if sentences is not None else ["Update."]
        self._raises = raises
        self.calls = 0

    async def synthesize(self, view) -> AsyncIterator[str]:
        self.calls += 1
        if self._raises:
            raise RuntimeError("synth broke")
        for s in self._sentences:
            yield s


class _FakeArbiter:
    def __init__(self) -> None:
        self.filler_calls = 0
        self.submitted: List[object] = []

    def fire_filler(self) -> None:
        self.filler_calls += 1

    def submit(self, request) -> None:
        self.submitted.append(request)


@pytest.fixture
def mock_safe_say():
    return AsyncMock(return_value=True)


@pytest.fixture(autouse=True)
def _karen_synth_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_SYNTH_ENABLED", "1")


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


def _make_narrator(mock_safe_say, debounce_s=0.0, sentences=None, raises=False, source=None):
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    synth = _FakeSynthesizer(sentences=sentences, raises=raises)
    arbiter = _FakeArbiter()
    kwargs = dict(
        say_fn=mock_safe_say,
        debounce_s=debounce_s,
        synthesizer=synth,
        arbiter=arbiter,
    )
    if source is not None:
        kwargs["source"] = source
    narrator = VoiceNarrator(**kwargs)
    return narrator, synth, arbiter


class TestVoiceNarratorSend:
    @pytest.mark.asyncio
    async def test_narrates_intent_message(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix test",
            "target_files": ["tests/test_a.py"],
            "test_count": 3,
        })
        await narrator.send(msg)
        await narrator.drain()
        assert arbiter.filler_calls == 1
        assert len(arbiter.submitted) == 1
        assert arbiter.submitted[0].text == "Update."

    @pytest.mark.asyncio
    async def test_narrates_decision_message(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say)
        msg = _make_comm_message("DECISION", payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
            "diff_summary": "added edge case",
            "file": "tests/test_a.py",
        })
        await narrator.send(msg)
        await narrator.drain()
        assert arbiter.filler_calls == 1
        assert len(arbiter.submitted) == 1

    @pytest.mark.asyncio
    async def test_skips_heartbeat(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say)
        msg = _make_comm_message("HEARTBEAT", payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        await narrator.send(msg)
        assert arbiter.filler_calls == 0
        assert arbiter.submitted == []

    @pytest.mark.asyncio
    async def test_skips_plan(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say)
        msg = _make_comm_message("PLAN", payload={"steps": ["step1"]})
        await narrator.send(msg)
        assert arbiter.filler_calls == 0
        assert arbiter.submitted == []


class TestVoiceNarratorDebounce:
    @pytest.mark.asyncio
    async def test_debounce_blocks_rapid_narrations(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say, debounce_s=60.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"], "test_count": 1,
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"], "test_count": 2,
        })
        await narrator.send(msg1)
        await narrator.send(msg2)
        await narrator.drain()
        assert synth.calls == 1  # second blocked by debounce

    @pytest.mark.asyncio
    async def test_debounce_allows_after_expiry(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say, debounce_s=0.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"], "test_count": 1,
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"], "test_count": 2,
        })
        await narrator.send(msg1)
        await asyncio.sleep(0.01)
        await narrator.send(msg2)
        await narrator.drain()
        assert synth.calls == 2


class TestVoiceNarratorIdempotency:
    @pytest.mark.asyncio
    async def test_same_op_same_phase_not_repeated(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("DECISION", op_id="op-001", payload={
            "outcome": "applied",
            "file": "tests/test_a.py",
        })
        await narrator.send(msg)
        await narrator.drain()
        await narrator.send(msg)  # same op_id + same msg_type
        await narrator.drain()
        assert synth.calls == 1


class TestVoiceNarratorFailure:
    @pytest.mark.asyncio
    async def test_synth_failure_does_not_propagate(self, mock_safe_say):
        narrator, synth, arbiter = _make_narrator(mock_safe_say, debounce_s=0.0, raises=True)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix", "target_files": ["a.py"], "test_count": 1,
        })
        await narrator.send(msg)  # should not raise
        await narrator.drain()


# ---------------------------------------------------------------------------
# TestSeverityAwareDebounce
# ---------------------------------------------------------------------------


class TestSeverityAwareDebounce:
    """DECISION and POSTMORTEM bypass debounce; INTENT is rate-limited."""

    def _make_narrator(self, debounce_s: float = 60.0):
        say = AsyncMock(return_value=True)
        narrator, synth, arbiter = _make_narrator(say, debounce_s=debounce_s, source="test")
        return narrator, synth

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
        narrator, synth = self._make_narrator(debounce_s=3600.0)

        # First INTENT narrates and sets _last_narration
        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        await narrator.drain()
        assert synth.calls == 1

        # POSTMORTEM for a different op must narrate despite debounce window
        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-2"))
        await narrator.drain()
        assert synth.calls == 2, (
            f"POSTMORTEM was suppressed by debounce (calls={synth.calls})"
        )

    async def test_decision_bypasses_debounce(self):
        """DECISION narrates even within debounce window."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, synth = self._make_narrator(debounce_s=3600.0)

        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        await narrator.drain()
        assert synth.calls == 1

        await narrator.send(self._make_msg(MessageType.DECISION, "op-2"))
        await narrator.drain()
        assert synth.calls == 2, (
            f"DECISION was suppressed by debounce (calls={synth.calls})"
        )

    async def test_intent_is_debounced(self):
        """INTENT respects debounce window (second INTENT within window is dropped)."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, synth = self._make_narrator(debounce_s=3600.0)

        await narrator.send(self._make_msg(MessageType.INTENT, "op-1"))
        await narrator.drain()
        assert synth.calls == 1

        await narrator.send(self._make_msg(MessageType.INTENT, "op-2"))
        await narrator.drain()
        assert synth.calls == 1, "Second INTENT within window should be debounced"

    async def test_idempotency_still_blocks_duplicate_postmortem(self):
        """Same op_id + same msg_type is idempotent even without debounce."""
        from backend.core.ouroboros.governance.comm_protocol import MessageType
        narrator, synth = self._make_narrator(debounce_s=0.0)

        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-1"))
        await narrator.drain()
        await narrator.send(self._make_msg(MessageType.POSTMORTEM, "op-1"))  # duplicate
        await narrator.drain()
        assert synth.calls == 1, "Idempotency guard should block duplicate op_id+type"

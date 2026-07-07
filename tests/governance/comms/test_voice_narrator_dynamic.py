"""tests/governance/comms/test_voice_narrator_dynamic.py

Sprint 2 Task 7: narrator_script.py is eradicated; VoiceNarrator now drives
KarenSpeechSynthesizer + VoiceDuplexArbiter directly instead of formatting
static templates.
"""
from __future__ import annotations

import importlib
import time
from typing import AsyncIterator, List
from unittest.mock import AsyncMock

import pytest


def test_narrator_script_module_is_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(
            "backend.core.ouroboros.governance.comms.narrator_script"
        )


def test_format_narration_not_reexported():
    comms = importlib.import_module(
        "backend.core.ouroboros.governance.comms"
    )
    assert not hasattr(comms, "format_narration")
    assert not hasattr(comms, "SCRIPTS")


class _FakeSynthesizer:
    """Fake KarenSpeechSynthesizer — yields canned sentences, ignores the view."""

    def __init__(self, sentences: List[str]) -> None:
        self._sentences = sentences
        self.views_seen: List[object] = []

    async def synthesize(self, view) -> AsyncIterator[str]:
        self.views_seen.append(view)
        for s in self._sentences:
            yield s


class _FakeArbiter:
    """Fake VoiceDuplexArbiter — records fire_filler()/submit() calls."""

    def __init__(self) -> None:
        self.filler_calls = 0
        self.submitted: List[object] = []

    def fire_filler(self) -> None:
        self.filler_calls += 1

    def submit(self, request) -> None:
        self.submitted.append(request)


def _make_comm_message(msg_type_name, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type_name],
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


@pytest.mark.asyncio
async def test_narrate_one_drives_synthesizer_and_fires_filler(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_SYNTH_ENABLED", "1")
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

    fake_synth = _FakeSynthesizer(["First sentence.", "Second sentence."])
    fake_arbiter = _FakeArbiter()
    say_fn = AsyncMock(return_value=True)

    narrator = VoiceNarrator(
        say_fn=say_fn,
        debounce_s=0.0,
        synthesizer=fake_synth,
        arbiter=fake_arbiter,
    )

    msg = _make_comm_message(
        "INTENT",
        payload={"goal": "fix test", "target_files": ["a.py"], "test_count": 1},
    )
    await narrator.send(msg)
    await narrator.drain()

    assert fake_arbiter.filler_calls == 1
    assert len(fake_arbiter.submitted) == 2
    assert [r.text for r in fake_arbiter.submitted] == [
        "First sentence.",
        "Second sentence.",
    ]
    # No template fallback: say_fn must never be invoked by the new path.
    say_fn.assert_not_called()


@pytest.mark.asyncio
async def test_narrate_one_no_ops_when_synth_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JARVIS_KAREN_SYNTH_ENABLED", "0")
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

    fake_synth = _FakeSynthesizer(["Should not be spoken."])
    fake_arbiter = _FakeArbiter()
    say_fn = AsyncMock(return_value=True)

    narrator = VoiceNarrator(
        say_fn=say_fn,
        debounce_s=0.0,
        synthesizer=fake_synth,
        arbiter=fake_arbiter,
    )

    msg = _make_comm_message(
        "DECISION", payload={"outcome": "applied", "file": "a.py"},
    )
    await narrator.send(msg)
    await narrator.drain()

    assert fake_arbiter.filler_calls == 0
    assert fake_arbiter.submitted == []
    say_fn.assert_not_called()


@pytest.mark.asyncio
async def test_narrate_one_filters_code_fences_before_submit(monkeypatch):
    """FIX 1 (Important, Sprint 2 review): synthesized sentences must pass
    through the same deterministic guard (strip_code) used on ledger input
    before reaching the arbiter — a fence/traceback the model echoes must
    never reach TTS."""
    monkeypatch.setenv("JARVIS_KAREN_SYNTH_ENABLED", "1")
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

    fake_synth = _FakeSynthesizer(["Fixed it. ```rm -rf``` done."])
    fake_arbiter = _FakeArbiter()
    say_fn = AsyncMock(return_value=True)

    narrator = VoiceNarrator(
        say_fn=say_fn,
        debounce_s=0.0,
        synthesizer=fake_synth,
        arbiter=fake_arbiter,
    )

    msg = _make_comm_message(
        "INTENT", payload={"goal": "fix test", "target_files": ["a.py"], "test_count": 1},
    )
    await narrator.send(msg)
    await narrator.drain()

    assert fake_arbiter.submitted, "expected the filtered sentence to still be submitted"
    assert all("```" not in r.text for r in fake_arbiter.submitted)


@pytest.mark.asyncio
async def test_narrate_one_dedupes_second_identical_message_on_synth_outage(monkeypatch):
    """FIX 2 (Important, Sprint 2 review): if synthesize() yields nothing
    (DW outage — provider swallows errors, yields empty), the dedup id must
    still be recorded so a repeat of the SAME message does not fire another
    filler with no dedup/debounce (filler-spam)."""
    monkeypatch.setenv("JARVIS_KAREN_SYNTH_ENABLED", "1")
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

    fake_synth = _FakeSynthesizer([])  # synth outage: yields nothing
    fake_arbiter = _FakeArbiter()
    say_fn = AsyncMock(return_value=True)

    narrator = VoiceNarrator(
        say_fn=say_fn,
        debounce_s=0.0,
        synthesizer=fake_synth,
        arbiter=fake_arbiter,
    )

    msg = _make_comm_message(
        "INTENT", op_id="op-001", payload={"goal": "fix test", "target_files": ["a.py"], "test_count": 1},
    )
    await narrator.send(msg)
    await narrator.drain()

    # Identical message again (same op_id + msg_type -> same notification_id).
    msg2 = _make_comm_message(
        "INTENT", op_id="op-001", payload={"goal": "fix test", "target_files": ["a.py"], "test_count": 1},
    )
    await narrator.send(msg2)
    await narrator.drain()

    assert fake_arbiter.filler_calls == 1, "second identical message must be deduped, not re-fire a filler"


@pytest.mark.asyncio
async def test_narrate_one_no_ops_when_no_injection():
    """Default construction (no synthesizer/arbiter) is a silent no-op —
    there is no template fallback to speak instead."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

    say_fn = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=say_fn, debounce_s=0.0)

    msg = _make_comm_message(
        "INTENT",
        payload={"goal": "fix test", "target_files": ["a.py"], "test_count": 1},
    )
    await narrator.send(msg)  # must not raise
    await narrator.drain()
    say_fn.assert_not_called()

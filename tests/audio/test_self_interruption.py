"""Karen must not interrupt herself.

Observed live, mid-reply:

    [TTS] Synthesized 29 chars (latency: 5142ms)
    [BargeIn] User interrupted JARVIS (total: 1)
    ... and the reply stopped

Nobody interrupted her. Her own voice left the speakers, the microphone heard
it, and the barge-in detector read that as the operator cutting in.

The machinery to prevent it already existed — UnifiedSpeechStateManager
.start_speaking() gates AudioBus and stop_speaking() ungates it — but only the
UNGATE was ever called from the conversation pipeline. A gate whose release is
wired and whose engage is not is worse than no gate at all: it can only ever
be opened.

AEC cannot cover this alone. Playback leaves through afplay in a separate
process precisely so it is GIL-free, so the bus holds no reference signal to
subtract.
"""

from __future__ import annotations

import asyncio

import pytest


class _Mgr:
    def __init__(self, boom_on_start=False):
        self.started, self.stopped = [], 0
        self._boom = boom_on_start

    async def start_speaking(self, text="", source=None, estimated_duration_ms=None):
        if self._boom:
            raise RuntimeError("speech state unavailable")
        self.started.append(text)

    async def stop_speaking(self, *a, **k):
        self.stopped += 1


class _Engine:
    """speak_stream only — the shape UnifiedTTSEngine actually exposes."""

    def __init__(self, boom=False):
        self.spoke = []
        self._boom = boom

    async def speak_stream(self, text, play_audio=True, cancel_event=None, source=None):
        if self._boom:
            raise RuntimeError("synthesis exploded")
        self.spoke.append(text)


def _pipeline(engine, mgr, monkeypatch):
    from backend.audio import conversation_pipeline as cp

    monkeypatch.setattr(cp, "_tts_can_synthesize", lambda _e: False)  # legacy path

    async def _get_mgr():
        return mgr

    import backend.core.unified_speech_state as uss
    monkeypatch.setattr(uss, "get_speech_state_manager", _get_mgr)

    p = cp.ConversationPipeline.__new__(cp.ConversationPipeline)
    p._tts_engine = engine
    p._audio_bus = None
    return p


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


async def test_the_mic_is_gated_while_she_speaks(monkeypatch):
    """THE REGRESSION. Without the gate her voice reaches the mic and
    barge-in cancels her mid-sentence."""
    mgr, eng = _Mgr(), _Engine()
    p = _pipeline(eng, mgr, monkeypatch)

    await p._speak_sentence("Right, I'm here.", asyncio.Event())

    assert mgr.started == ["Right, I'm here."], "the mic was never gated"
    assert eng.spoke == ["Right, I'm here."]


async def test_the_gate_is_always_released(monkeypatch):
    mgr, eng = _Mgr(), _Engine()
    p = _pipeline(eng, mgr, monkeypatch)
    await p._speak_sentence("hello", asyncio.Event())
    assert mgr.stopped == 1


async def test_a_synthesis_fault_still_releases_the_gate(monkeypatch):
    """A gate left closed by a crash is a permanently deaf microphone —
    strictly worse than the bug it prevents."""
    mgr, eng = _Mgr(), _Engine(boom=True)
    p = _pipeline(eng, mgr, monkeypatch)

    await p._speak_sentence("hello", asyncio.Event())   # must not raise
    assert mgr.stopped == 1, "the microphone was left gated after a fault"


async def test_the_gate_engages_before_synthesis_not_after(monkeypatch):
    """Gating after synthesis begins leaves a window in which her first
    syllables reach the mic — and one frame is all barge-in needs."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_sentence)
    assert src.index("start_speaking") < src.index("speak_stream")


async def test_an_unavailable_speech_manager_does_not_silence_her(monkeypatch):
    """An ungated reply beats no reply: the gate is protection, not a
    precondition for speaking."""
    mgr, eng = _Mgr(boom_on_start=True), _Engine()
    p = _pipeline(eng, mgr, monkeypatch)

    await p._speak_sentence("hello", asyncio.Event())
    assert eng.spoke == ["hello"]


async def test_a_cancelled_turn_never_reaches_the_gate(monkeypatch):
    """Genuine barge-in must still work — a pre-cancelled sentence is not
    spoken and does not gate."""
    mgr, eng = _Mgr(), _Engine()
    p = _pipeline(eng, mgr, monkeypatch)

    ev = asyncio.Event()
    ev.set()
    await p._speak_sentence("hello", ev)
    assert eng.spoke == [] and mgr.started == []


def test_the_gate_and_its_release_are_symmetric():
    """The bug WAS the asymmetry: release wired, engage absent."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_sentence)
    assert src.count("start_speaking") >= 1
    assert "finally:" in src and "stop_speaking" in src

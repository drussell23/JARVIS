"""speak_immediate — the delivery half of the acoustic loop.

The measurement half worked perfectly and this half had never executed once:
`acoustic_feedback` referenced `speak_immediate` before it existed, so every
degradation logged `(unspoken)`. A dependency referenced but not defined is
the wired-but-inert trap, and these assert it is now actually wired.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from backend.audio import acoustic_feedback as af
from backend.audio.speech_scheduler import SpeechRole, reset_scheduler


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_TTS_ACOUSTIC_TAIL_S", "0.01")
    reset_scheduler()
    af.reset()
    yield
    reset_scheduler()


def test_empty_lines_are_not_spoken() -> None:
    assert af.speak_immediate("") is False
    assert af.speak_immediate("   ") is False


def test_it_reuses_macos_voice_and_the_turnstile() -> None:
    """DRY, asserted structurally: no second playback stack, no subprocess
    layer of its own — two players over one speaker is the collision the
    scheduler exists to prevent."""
    import inspect

    src = inspect.getsource(af.speak_immediate)
    assert "MacOSVoice" in src
    assert "SpeechRole.PRIMARY" in src
    assert "Popen" not in src and "subprocess" not in src


def test_it_speaks_with_no_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callable from any context — the STT rejection path is not guaranteed
    to be on a loop."""
    spoken: List[str] = []

    class _Voice:
        def speak(self, text: str) -> None:
            spoken.append(text)

    monkeypatch.setattr("backend.voice.macos_voice.MacOSVoice", _Voice)
    assert af.speak_immediate("the room is washing you out") is True
    assert spoken == ["the room is washing you out"]


@pytest.mark.asyncio
async def test_it_takes_a_primary_ticket_on_the_turnstile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion 1: it preempts. A SECONDARY utterance already waiting must
    not beat the interrupt to the speakers."""
    from backend.audio.speech_scheduler import get_scheduler, mark_hardware_busy, mark_hardware_idle

    order: List[str] = []

    class _Voice:
        def speak(self, text: str) -> None:
            order.append("interrupt")

    monkeypatch.setattr("backend.voice.macos_voice.MacOSVoice", _Voice)
    sched = get_scheduler()

    async def _queued() -> None:
        order.append("queued")

    mark_hardware_busy()                       # hold the turnstile shut
    try:
        secondary = asyncio.create_task(sched.speak(
            _queued, agent="karen", role=SpeechRole.SECONDARY,
        ))
        await asyncio.sleep(0.05)              # SECONDARY is waiting first
        assert af.speak_immediate("interrupt me") is True
        await asyncio.sleep(0.05)
    finally:
        mark_hardware_idle()

    await asyncio.wait_for(secondary, timeout=5)
    for _ in range(50):
        if "interrupt" in order:
            break
        await asyncio.sleep(0.05)

    assert "interrupt" in order, "the priority interrupt never reached the speakers"
    assert order[0] == "interrupt", f"the queued utterance won: {order}"


@pytest.mark.asyncio
async def test_it_does_not_block_the_rejection_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Called from the STT rejection path — blocking it would stall
    recognition in order to announce that recognition is failing."""
    import time

    class _SlowVoice:
        def speak(self, text: str) -> None:
            time.sleep(0.6)

    monkeypatch.setattr("backend.voice.macos_voice.MacOSVoice", _SlowVoice)
    t0 = time.monotonic()
    assert af.speak_immediate("slow line") is True
    assert time.monotonic() - t0 < 0.15, "speak_immediate blocked its caller"


def test_a_broken_voice_engine_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def speak(self, text: str) -> None:
            raise OSError("no audio device")

    monkeypatch.setattr("backend.voice.macos_voice.MacOSVoice", _Broken)
    assert af.speak_immediate("anything") is False


def test_the_controller_now_reaches_a_real_function() -> None:
    """The omission itself: the feedback controller's speak seam must resolve
    to something that exists."""
    assert callable(getattr(af, "speak_immediate", None))
    import inspect
    src = inspect.getsource(af._controller)
    assert "speak_immediate(line)" in src
    assert "(unspoken)" not in src, "the fallback log is still the real path"

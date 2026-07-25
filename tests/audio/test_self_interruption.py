"""Karen must not interrupt herself — without going deaf to interrupt her.

Observed live, mid-reply:

    [TTS] Synthesized 29 chars (latency: 5142ms)
    [BargeIn] User interrupted JARVIS (total: 1)
    ... and the reply stopped

Nobody interrupted her. Her own voice left the speakers, the microphone heard
it, and the barge-in detector read that as the operator cutting in. AEC cannot
cover it: playback leaves through afplay in a separate process precisely so it
is GIL-free, so the bus holds no reference to subtract.

The FIRST fix gated the microphone around the whole speak call and traded one
bug for a worse one: with synthesis measured at 5142ms, the operator could not
interrupt while Karen was merely THINKING. The gate now binds to PLAYBACK
only. These tests pin both halves of that — she cannot hear herself, and the
human can always cut in before she starts talking.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.audio import playback_gate as pg


@pytest.fixture(autouse=True)
def _reset():
    pg.force_open()
    yield
    pg.force_open()


@pytest.fixture
def bus(monkeypatch):
    state = {"gated": False, "log": []}

    def _set(active):
        state["gated"] = bool(active)
        state["log"].append(bool(active))
        return True

    monkeypatch.setattr(pg, "_set_bus_gate", _set)
    return state


# ---------------------------------------------------------------------------
# She cannot hear herself
# ---------------------------------------------------------------------------


async def test_the_mic_is_closed_while_sound_plays(bus):
    """THE REGRESSION. Without this her own voice triggers barge-in."""
    async with pg.playback_gate("Right, I'm here."):
        assert bus["gated"] is True
    assert bus["gated"] is False


def test_the_afplay_launch_site_is_gated():
    """The legacy path synthesizes AND plays inside the engine, so the gate
    has to live at the subprocess launch — the only place that knows when
    sound actually starts."""
    from pathlib import Path

    src = Path("backend/voice/macos_voice.py").read_text(encoding="utf-8")
    assert "playback_gate_sync" in src
    assert src.index("playback_gate_sync") < src.index('["afplay"')


def test_the_bus_playback_site_is_gated():
    """The AEC path streams through AudioBus; it needs the same protection."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_sentence)
    assert "playback_gate" in src
    assert src.index("playback_gate") < src.index("play_stream")


# ---------------------------------------------------------------------------
# The human can always interrupt her BEFORE she speaks
# ---------------------------------------------------------------------------


async def test_generation_never_closes_the_mic(bus):
    """The blindspot the first fix created: 5142ms of synthesis with the
    microphone deaf. Generation must leave it untouched."""
    async def _generate():
        await asyncio.sleep(0.2)          # stands in for LLM + synthesis
        return "some text"

    await _generate()
    assert bus["log"] == [], "the gate moved during generation"
    assert bus["gated"] is False


def test_the_pipeline_does_not_gate_around_the_whole_call():
    """Structural pin against reverting to synthesis-wide gating."""
    import ast
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    src = inspect.getsource(ConversationPipeline._speak_sentence)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    assert "start_speaking" not in ast.unparse(tree)


# ---------------------------------------------------------------------------
# The gate can never be left closed
# ---------------------------------------------------------------------------


async def test_a_playback_crash_reopens_the_mic(bus):
    """A gate left closed is a permanently DEAF microphone — strictly worse
    than the bug it prevents."""
    with pytest.raises(RuntimeError):
        async with pg.playback_gate("x"):
            raise RuntimeError("afplay died")
    assert bus["gated"] is False


async def test_cancellation_reopens_the_mic(bus):
    """Barge-in cancels the task mid-playback; the mic must come back."""
    async def _play():
        async with pg.playback_gate("x"):
            await asyncio.sleep(10)

    task = asyncio.get_running_loop().create_task(_play())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus["gated"] is False


def test_force_open_always_wins(bus):
    """The emergency release used by teardown paths."""
    pg._enter("x")
    assert bus["gated"] is True
    pg.force_open()
    assert bus["gated"] is False and pg.gate_depth() == 0

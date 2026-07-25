"""Two acoustic corrections: don't saturate the tensor, don't deafen the human.

AGC. The capture device delivers samples above full scale (measured 1.04
through 3.9998). Audio that reaches a tensor saturated flat is not speech any
more — faster-whisper reads formant structure and clipping is precisely the
operation that destroys it. The observable symptom was whisper HALLUCINATING
"I'm sorry, I'm sorry, I'm sorry" in place of "Hello Karen".

JIT GATING. Muting for the whole speak call deafens the microphone through
the entire generation phase — measured at 5142ms for one sentence, with the
LLM call before it. An assistant that cannot be interrupted while it is
merely THINKING is a worse interface than one that occasionally hears itself.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from backend.audio import playback_gate as pg
from backend.audio.audio_bus import _AGC_THRESHOLD, AudioBus


def _bus():
    b = AudioBus.__new__(AudioBus)
    b._range_peak = 1.0
    b._range_reports = 0
    return b


@pytest.fixture(autouse=True)
def _reset_gate():
    pg.force_open()
    yield
    pg.force_open()


# ---------------------------------------------------------------------------
# (1) MANDATE: a clipped 1.5 frame comes out cleanly below 1.0
# ---------------------------------------------------------------------------


def test_a_clipped_frame_is_compressed_below_full_scale():
    """(1) THE MANDATE."""
    out = _bus()._fit_to_range(np.full(320, 1.5, dtype=np.float32))
    peak = float(np.max(np.abs(out)))
    assert peak < 1.0, f"still saturating at {peak}"
    assert peak > _AGC_THRESHOLD, "compressed so hard the signal was crushed"


@pytest.mark.parametrize("amp", [1.0, 1.5, 2.36, 3.26, 4.0, 40.0])
def test_output_never_exceeds_full_scale(amp):
    """The guarantee is that nothing LEAVES above 1.0, at any input level.

    tanh is asymptotic to 1.0 in exact arithmetic; in float32 an extreme
    input rounds onto 1.0 exactly. That is the boundary, not an overshoot,
    and the honest claim is "never exceeds" rather than "never reaches"."""
    out = _bus()._fit_to_range(np.full(256, amp, dtype=np.float32))
    assert float(np.max(np.abs(out))) <= 1.0


def test_a_hot_signal_keeps_its_dynamics():
    """The property that matters for recognition: loud passages stay
    DISTINGUISHABLE from each other rather than all flattening onto the
    ceiling, which is what clipping does."""
    b = _bus()
    frame = np.concatenate([
        np.full(64, 0.9, dtype=np.float32),
        np.full(64, 1.4, dtype=np.float32),
        np.full(64, 2.2, dtype=np.float32),
    ])
    out = b._fit_to_range(frame)
    levels = [float(np.max(np.abs(out[i:i + 64]))) for i in (0, 64, 128)]
    assert levels[0] < levels[1] < levels[2], f"ordering lost: {levels}"


def test_audio_below_the_knee_is_bit_identical():
    """Normal speech peaks here run 0.2-0.6. The compressor must be a
    NO-OP for it, not a permanent tone control."""
    b = _bus()
    frame = (np.sin(np.linspace(0, 20, 320)) * 0.6).astype(np.float32)
    assert np.array_equal(b._fit_to_range(frame), frame)


def test_the_knee_is_soft_not_a_corner():
    """A hard knee puts a corner in the waveform and manufactures the very
    high-frequency artifacts a compressor exists to avoid. Continuity of the
    first derivative is what 'soft' means."""
    b = _bus()
    xs = np.linspace(_AGC_THRESHOLD - 0.05, _AGC_THRESHOLD + 0.05, 400)
    ys = np.array([
        float(b._fit_to_range(np.array([x], dtype=np.float32))[0]) for x in xs
    ])
    d2 = np.abs(np.diff(ys, 2))
    assert float(np.max(d2)) < 1e-3, "discontinuous slope at the knee"


def test_compression_preserves_waveform_shape():
    b = _bus()
    frame = (np.sin(np.linspace(0, 30, 512)) * 2.0).astype(np.float32)
    out = b._fit_to_range(frame)
    corr = float(np.corrcoef(frame, out)[0, 1])
    # Compression IS a nonlinearity — perfect correlation would mean it did
    # nothing. What matters is that the waveform is recognisably the same
    # shape rather than a flat-topped square, which clipping produces.
    assert corr > 0.95, f"waveform distorted beyond recognition (corr={corr:.3f})"


def test_sign_is_preserved():
    b = _bus()
    frame = np.array([2.0, -2.0, 0.5, -0.5], dtype=np.float32)
    out = b._fit_to_range(frame)
    assert np.all(np.sign(out) == np.sign(frame))


def test_the_compressor_never_raises():
    b = _bus()
    for junk in (np.zeros(0, np.float32), np.array([np.inf], np.float32)):
        b._fit_to_range(junk)


def test_it_runs_before_consumers_see_the_frame():
    import inspect

    src = inspect.getsource(AudioBus._on_mic_frame)
    assert src.index("_fit_to_range") < src.index("for consumer in")


# ---------------------------------------------------------------------------
# (2) MANDATE: the mic stays OPEN during a 2s synthesis
# ---------------------------------------------------------------------------


class _Bus:
    def __init__(self):
        self.gated = False
        self.transitions = []

    def set_mic_gate(self, active: bool) -> None:
        self.gated = bool(active)
        self.transitions.append(bool(active))


@pytest.fixture
def fake_bus(monkeypatch):
    b = _Bus()
    monkeypatch.setattr(pg, "_set_bus_gate",
                        lambda active: (b.set_mic_gate(active), True)[1])
    return b


async def test_the_mic_stays_open_during_synthesis(fake_bus):
    """(2) THE MANDATE. 2s of 'generation' with the microphone LIVE, so the
    operator can still interrupt a reply they already know is wrong."""
    async def _synthesize():
        await asyncio.sleep(2.0)
        return b"audio"

    t0 = asyncio.get_running_loop().time()
    await _synthesize()
    assert fake_bus.gated is False, "the mic was deafened during generation"
    assert fake_bus.transitions == [], "the gate moved before playback"
    assert asyncio.get_running_loop().time() - t0 >= 2.0


# ---------------------------------------------------------------------------
# (3) MANDATE: the gate closes exactly when playback starts
# ---------------------------------------------------------------------------


async def test_the_gate_closes_exactly_at_playback(fake_bus):
    """(3) THE MANDATE."""
    assert fake_bus.gated is False
    async with pg.playback_gate("hello"):
        assert fake_bus.gated is True, "the mic was open while sound played"
    assert fake_bus.gated is False, "the mic never reopened"
    assert fake_bus.transitions == [True, False]


def test_the_sync_gate_closes_at_the_afplay_launch(fake_bus):
    with pg.playback_gate_sync("hello"):
        assert fake_bus.gated is True
    assert fake_bus.gated is False


async def test_the_gate_reopens_after_a_playback_crash(fake_bus):
    """A gate left closed is a permanently DEAF microphone — strictly worse
    than the bug it prevents."""
    with pytest.raises(RuntimeError):
        async with pg.playback_gate("hello"):
            raise RuntimeError("afplay died")
    assert fake_bus.gated is False


async def test_nested_gates_do_not_reopen_early(fake_bus):
    """The legacy path can nest one gate inside another; the INNER exit must
    not reopen the mic while the outer body is still playing."""
    async with pg.playback_gate("outer"):
        with pg.playback_gate_sync("inner"):
            assert fake_bus.gated is True
        assert fake_bus.gated is True, "the inner exit reopened the mic early"
    assert fake_bus.gated is False


def test_no_bus_leaks_no_depth(monkeypatch):
    """With no audio mounted, nothing is closed — so nothing may be counted.
    A leaked level means a later REAL gate nests instead of engaging and the
    mic never closes again."""
    monkeypatch.setattr(pg, "_set_bus_gate", lambda active: False)
    with pg.playback_gate_sync("x") as engaged:
        assert engaged is False
    assert pg.gate_depth() == 0


def test_the_gate_can_be_disabled(monkeypatch, fake_bus):
    monkeypatch.setenv("JARVIS_PLAYBACK_GATE_ENABLED", "false")
    with pg.playback_gate_sync("x") as engaged:
        assert engaged is False
    assert fake_bus.transitions == []


# ---------------------------------------------------------------------------
# Exception discipline
# ---------------------------------------------------------------------------


def test_the_gate_uses_targeted_exceptions_not_a_broad_catch():
    """A broad `except Exception` swallowed an AttributeError from a mis-named
    enum member and the gate silently never engaged — a reference error
    wearing the costume of graceful degradation."""
    import ast
    from pathlib import Path

    src = Path("backend/audio/playback_gate.py").read_text(encoding="utf-8")
    # AST, not substring: the module DOCUMENTS why broad catches are banned,
    # and a naive scan flags its own prose. (Third time this lesson has come
    # up in this session — hence doing it properly.)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)
    assert "except Exception" not in code, "broad catch reintroduced"
    # force_open's BaseException is the one sanctioned exception; everything
    # else must be targeted.
    assert code.count("BaseException") <= 1
    assert "except (RuntimeError, ValueError, OSError)" in code


def test_a_contract_error_is_not_swallowed(monkeypatch):
    """An AttributeError means the gate contract changed. It must be LOUD."""
    def _boom(_active):
        raise AttributeError("set_mic_gate vanished")

    monkeypatch.setattr(pg, "_set_bus_gate", _boom)
    with pytest.raises(AttributeError):
        with pg.playback_gate_sync("x"):
            pass


def test_the_pipeline_no_longer_gates_around_generation():
    """Structural pin: start_speaking() around the whole call is the very
    blindspot this replaces."""
    import inspect

    from backend.audio.conversation_pipeline import ConversationPipeline

    import ast

    src = inspect.getsource(ConversationPipeline._speak_sentence)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)
    assert "start_speaking" not in code, "synthesis-wide gating is back"
    assert "playback_gate" in code


# ---------------------------------------------------------------------------
# EVERY afplay launch must be gated — there are three
# ---------------------------------------------------------------------------
#
# The gate first went into macos_voice.py, which this pipeline never reaches:
# the engine in use is UnifiedTTSEngine and it plays somewhere else entirely.
# Karen kept triggering her own barge-in 354ms into a reply because the mic
# stayed open through a subprocess nobody had gated. One un-gated playback
# site is the whole bug, so the check is exhaustive rather than per-file.


def test_every_afplay_launch_sits_inside_a_gate():
    """Exhaustive: find every afplay invocation in the audio/voice tree and
    require a gate above it in the same function."""
    import re
    from pathlib import Path

    offenders = []
    for path in list(Path("backend/voice").rglob("*.py")) + \
            list(Path("backend/audio").rglob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'\["afplay"', src):
            window = src[max(0, m.start() - 1200):m.start()]
            if "playback_gate" not in window:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{path}:{line}")
    assert not offenders, f"un-gated afplay launches: {offenders}"


def test_the_engine_playback_site_is_gated():
    """The specific site that was missed — named, so a refactor that moves it
    fails here rather than in a live conversation."""
    from pathlib import Path

    src = Path(
        "backend/voice/engines/unified_tts_engine.py",
    ).read_text(encoding="utf-8")
    assert "playback_gate_sync" in src
    assert src.index("playback_gate_sync") < src.index('["afplay", temp_path]')


def test_the_gate_wraps_the_launch_not_the_whole_method():
    """The temp-file write is not audible. Gating it would widen the window in
    which the operator cannot interrupt — the exact blindspot JIT gating
    exists to close."""
    from pathlib import Path

    src = Path(
        "backend/voice/engines/unified_tts_engine.py",
    ).read_text(encoding="utf-8")
    gate = src.index("with playback_gate_sync")
    write = src.index('f.write(audio_data)')
    assert write < gate, "the gate swallowed the file write"

"""The degradation verdict has to reach somebody.

The gate measured the room correctly and decided correctly, and then
published into nothing: `_emit` imported `backend.audio.audio_state_ipc` — a
module that does not exist, the real one being under
`governance.comms.duplex` — and called a `broadcast` that does not exist
there either. Both failures landed in `except (ImportError, ...): pass`, so
every degradation event since it shipped was swallowed silently.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.audio.acoustic_feedback import set_degradation_sink
from backend.audio.acoustic_quality import (
    DEGRADED_RUN, AcousticFeedbackController, QualitySample,
)


def _noise(seed: int = 7, n: int = 16000) -> np.ndarray:
    """White noise — no syllabic envelope, low crest. The acoustic signature
    of a fallback microphone in a room, which is what a dropped headset
    leaves behind."""
    return np.random.default_rng(seed).normal(0, 0.1, n).astype("float32")


@pytest.fixture(autouse=True)
def _clear_sink():
    yield
    set_degradation_sink(None)


def test_white_noise_is_scored_as_degraded() -> None:
    sample = QualitySample.from_audio(_noise(), 16000, device="FallbackMic")
    assert sample.degraded is True
    assert sample.crest_db < 15.0, "speech sits at 15-21dB crest"


@pytest.mark.asyncio
async def test_a_run_of_degraded_samples_reaches_the_sink() -> None:
    """One bad utterance is a cough or a chair. A RUN is a room."""
    seen: list = []
    controller = AcousticFeedbackController(emit=lambda k, p: seen.append((k, p)))
    sample = QualitySample.from_audio(_noise(), 16000, device="FallbackMic")

    for _ in range(DEGRADED_RUN + 1):
        controller.observe(sample)

    assert seen, "the degradation verdict was swallowed again"
    kind, payload = seen[0]
    assert kind == "acoustic_degradation"
    assert payload["device"] == "FallbackMic"
    assert payload["crest_db"] < 15.0
    assert payload["diagnosis"], "a badge that cannot say WHY is unactionable"
    assert payload["spoken"], "Karen has nothing to say about it"


@pytest.mark.asyncio
async def test_one_bad_sample_alone_says_nothing() -> None:
    seen: list = []
    controller = AcousticFeedbackController(emit=lambda k, p: seen.append(k))
    controller.observe(QualitySample.from_audio(_noise(), 16000))
    assert seen == []


@pytest.mark.asyncio
async def test_the_sink_is_optional_and_faults_are_contained() -> None:
    """This sits on the STT rejection path: it must not be able to break
    recognition, with no sink or a broken one."""
    def _boom(_kind, _payload):
        raise RuntimeError("cockpit died")

    set_degradation_sink(_boom)
    from backend.audio.acoustic_feedback import _notify_degradation
    _notify_degradation("acoustic_degradation", {"diagnosis": "reverb"})

    set_degradation_sink(None)
    _notify_degradation("acoustic_degradation", {"diagnosis": "reverb"})


def test_the_dead_import_is_gone() -> None:
    """The module it reached for has never existed. Pinned by NAME because
    that is what was wrong — a path-shaped string nobody resolved."""
    import backend.audio.acoustic_feedback as mod

    src = (mod.__file__ or "")
    assert src
    text = open(src).read()
    assert "from backend.audio.audio_state_ipc import" not in text
    assert "set_degradation_sink" in text


# --------------------------------------------------------------------------
# the surface that was waiting on the other end
# --------------------------------------------------------------------------

def _ui():
    import contextlib
    import io

    with contextlib.redirect_stderr(io.StringIO()):
        from backend.core.ouroboros.cli.ov import AttachUI
    return AttachUI()


def test_a_verdict_reaches_the_toolbar() -> None:
    ui = _ui()
    ui.on_acoustic({"diagnosis": "reverb", "device": "MacBook Pro Microphone",
                    "spoken": "The room's washing you out"})
    badge = ui.acoustic_badge()
    assert "reverb" in badge
    assert "MacBook Pro Microphone" in badge, (
        "a verdict with no device cannot distinguish a dropped headset from "
        "a noisy room, and those have different fixes"
    )
    assert badge in ui._key_hints()


def test_the_mic_badge_outranks_a_pending_approval() -> None:
    """Everything else on that line is still working. A degraded mic means
    the operator's words are not arriving at all."""
    ui = _ui()
    ui.on_acoustic({"diagnosis": "reverb", "device": "X"})
    ui.shield.offer({"prompt_id": "iron-gate:1", "text": "apply?"},
                    composing=True)
    hints = ui._key_hints()
    assert hints.index("🎙") < hints.index("⚠")


def test_the_badge_decays() -> None:
    """The gate fires on a RUN of bad utterances; silence afterwards is
    indistinguishable from "fixed" and from "stopped talking". A sticky badge
    would assert something the telemetry never said."""
    import time

    ui = _ui()
    ui.on_acoustic({"diagnosis": "reverb", "device": "X"})
    assert ui.acoustic_badge() != ""
    ui.acoustic = (time.monotonic() - 10_000, "reverb", "X")
    assert ui.acoustic_badge() == ""


def test_no_verdict_shows_nothing() -> None:
    assert _ui().acoustic_badge() == ""


@pytest.mark.parametrize("junk", [None, {}, "x", 42])
def test_a_junk_verdict_cannot_break_the_toolbar(junk) -> None:
    ui = _ui()
    ui.on_acoustic(junk)
    assert isinstance(ui.acoustic_badge(), str)
    assert isinstance(ui._key_hints(), str)

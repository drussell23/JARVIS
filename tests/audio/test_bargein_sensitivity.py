"""Barge-in must distinguish a person from an echo.

Karen speaks, the microphone hears her, and barge-in cancels her reply. The
threshold was `_MIN_SPEECH_FRAMES = 3` — three 20ms frames, SIXTY
MILLISECONDS. Her own voice leaking for 60ms is not a possibility, it is a
certainty, and it cut her off within 300ms every single time:

    13:29:35,639  mic CLOSED for playback
    13:29:35,978  [BargeIn] User interrupted JARVIS (total: 4)

Gating the microphone is defence in depth, not a substitute: there are three
playback paths and any one left ungated reproduces the bug. The detector
itself has to be able to tell the difference.
"""

from __future__ import annotations

import pytest

import backend.audio.barge_in_controller as bic


class _Ctl(bic.BargeInController):
    """Real logic, injected speaking-state, no audio."""

    def __init__(self, speaking: bool):
        super().__init__()
        self._speaking = speaking
        self.triggered = 0

    def _is_jarvis_speaking(self) -> bool:
        return self._speaking

    def _trigger_barge_in(self) -> None:
        self.triggered += 1


def _feed(ctl, ms: int) -> None:
    for _ in range(max(1, ms // bic._FRAME_MS)):
        ctl.on_vad_speech_detected(True)


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_a_short_echo_burst_does_not_cancel_her():
    """THE REGRESSION. 60ms used to be enough — and 60ms of her own voice in
    the microphone is guaranteed."""
    ctl = _Ctl(speaking=True)
    _feed(ctl, 60)
    assert ctl.triggered == 0, "an echo-length burst still cancels the reply"


@pytest.mark.parametrize("ms", [20, 60, 120, 300, 500])
def test_bursts_below_the_speaking_threshold_are_ignored(ms):
    ctl = _Ctl(speaking=True)
    _feed(ctl, ms)
    assert ctl.triggered == 0, f"{ms}ms of echo triggered barge-in"


def test_a_sustained_human_interruption_still_works():
    """The feature must survive the fix: someone who genuinely talks over her
    is still heard."""
    ctl = _Ctl(speaking=True)
    _feed(ctl, bic._MIN_SPEECH_MS_WHILE_SPEAKING + 200)
    assert ctl.triggered >= 1, "a real interruption was ignored"


def test_the_threshold_is_asymmetric():
    """Barge-in only matters WHILE she is speaking — which is exactly when
    every frame is echo-suspect. The bar is raised for that window alone."""
    assert bic._MIN_SPEECH_MS_WHILE_SPEAKING > bic._MIN_SPEECH_MS


def test_silence_resets_the_run():
    """A person pauses; an echo stops. Either way the count must restart, or
    unrelated bursts minutes apart would accumulate into an interruption."""
    ctl = _Ctl(speaking=True)
    _feed(ctl, 400)
    ctl.on_vad_speech_detected(False)
    _feed(ctl, 400)
    assert ctl.triggered == 0


def test_nothing_triggers_when_she_is_not_speaking():
    """There is nothing to interrupt. Firing here would cancel silence."""
    ctl = _Ctl(speaking=False)
    _feed(ctl, 5000)
    assert ctl.triggered == 0


def test_the_threshold_is_expressed_in_time_not_frames():
    """A frame COUNT silently changes meaning if the frame duration does —
    and it hid how short 3 frames really was."""
    import inspect

    src = inspect.getsource(bic.BargeInController.on_vad_speech_detected)
    assert "_FRAME_MS" in src
    assert bic._MIN_SPEECH_MS >= 100, "back to a hair trigger"


def test_the_legacy_frame_constant_still_resolves():
    """Other call sites and tests reference it; it must stay derived rather
    than becoming a second source of truth."""
    assert bic._MIN_SPEECH_FRAMES == max(1, bic._MIN_SPEECH_MS // bic._FRAME_MS)

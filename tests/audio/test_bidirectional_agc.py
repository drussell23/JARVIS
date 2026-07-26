"""Bi-directional AGC — upward normalization, gated by an adaptive floor.

The measurement that forced this
-------------------------------
The operator's speech arrives at peak 0.070 / rms 0.0029 — 27x quieter than a
played probe on the SAME microphone — and whisper cannot read it from the raw
device tap. A governor that only ever attenuates cannot help a signal that far
down. So the AGC now scales both ways.

The mandated assertions:
  * a float array peaking at 0.02 (speech) is scaled UP toward 0.5
  * a float array peaking at 0.002 (noise) is NOT amplified

One deliberate deviation, stated plainly: noise is left UNCHANGED rather than
zeroed. Muting the floor would remove the room tone the endpointer and the
echo canceller both read, and would put a discontinuity at every gate
crossing — manufacturing exactly the broadband clicks the compressor work went
to such lengths to avoid. "Do not amplify the noise floor" is the requirement;
"replace it with digital silence" is a different and worse operation.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.audio.audio_bus import (
    _AGC_MAX_BOOST,
    _AGC_NOMINAL,
    _AGC_QUIET_PEAK,
    AudioBus,
)


class _Cfg:
    internal_rate = 16000


def _bus() -> AudioBus:
    b = AudioBus.__new__(AudioBus)
    b._agc_gain = 1.0
    b._range_peak = 0.0
    b._range_reports = 99
    b._noise_floor = 0.0
    b._config = _Cfg()
    return b


def _tone(peak: float, n: int = 320, freq: float = 220.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / 16000.0
    return (np.sin(2 * np.pi * freq * t) * peak).astype(np.float32)


def _settle(bus: AudioBus, floor_peak: float, frames: int = 60) -> None:
    """Let the tracker learn the room before judging anything against it."""
    for _ in range(frames):
        bus._fit_to_range(_tone(floor_peak))


# ---------------------------------------------------------------------------
# Assertion 2a — quiet speech is scaled up
# ---------------------------------------------------------------------------


def test_quiet_speech_is_scaled_up_toward_nominal() -> None:
    bus = _bus()
    _settle(bus, 0.002)
    out = bus._fit_to_range(_tone(0.02))
    assert float(np.max(np.abs(out))) > 0.35, "quiet speech was not boosted"
    assert float(np.max(np.abs(out))) <= 1.0


def test_boost_converges_on_nominal_across_frames() -> None:
    """The first boosted frame is RAMPED (gain rising into headroom is where
    smoothness is free), so convergence is asserted over a few frames rather
    than demanded in one — a step would click."""
    bus = _bus()
    _settle(bus, 0.002)
    peaks = [float(np.max(np.abs(bus._fit_to_range(_tone(0.02))))) for _ in range(5)]
    assert peaks[-1] == pytest.approx(_AGC_NOMINAL, rel=0.10)
    assert peaks[-1] >= peaks[0], "boost did not converge upward"


def test_the_operators_measured_level_becomes_usable() -> None:
    """The actual numbers from the flight recorder: speech peak 0.070 against
    an ambient floor of 0.0019."""
    bus = _bus()
    _settle(bus, 0.0019)
    out = bus._fit_to_range(_tone(0.070))
    assert float(np.max(np.abs(out))) > 0.3, (
        "the operator's real speech level is still too quiet to transcribe"
    )


# ---------------------------------------------------------------------------
# Assertion 2b — the noise floor is not amplified
# ---------------------------------------------------------------------------


def test_the_noise_floor_is_not_amplified() -> None:
    bus = _bus()
    _settle(bus, 0.002)
    quiet = _tone(0.002)
    out = bus._fit_to_range(quiet)
    assert float(np.max(np.abs(out))) == pytest.approx(0.002, abs=1e-4), (
        "the room's noise floor was amplified — this is the deafening-static "
        "failure the squelch exists to prevent"
    )


def test_silence_is_never_amplified() -> None:
    bus = _bus()
    _settle(bus, 0.0)
    out = bus._fit_to_range(np.zeros(320, dtype=np.float32))
    assert float(np.max(np.abs(out))) == 0.0


def test_the_gate_is_relative_to_the_room_not_a_constant() -> None:
    """"Quiet" is a property of the room. The same 0.02 peak is SIGNAL in a
    silent room and NOISE in a loud one — a fixed threshold cannot express
    that, which is why the floor is tracked."""
    silent_room = _bus()
    _settle(silent_room, 0.001)
    boosted = float(np.max(np.abs(silent_room._fit_to_range(_tone(0.02)))))

    loud_room = _bus()
    _settle(loud_room, 0.02)
    same_peak = float(np.max(np.abs(loud_room._fit_to_range(_tone(0.02)))))

    assert boosted > 0.3, "signal in a silent room was not boosted"
    assert same_peak == pytest.approx(0.02, abs=5e-3), (
        "the room's own level was boosted as though it were speech"
    )


# ---------------------------------------------------------------------------
# The floor tracker
# ---------------------------------------------------------------------------


def test_the_floor_falls_faster_than_it_rises() -> None:
    """Asymmetric on purpose, opposite to the gain release: a room that gets
    noisier is learned over seconds so one cough cannot raise the gate; a room
    that goes quiet must be believed at once, or the gate stays shut through
    the next sentence."""
    rising = _bus()
    rising._noise_floor = 0.001
    for _ in range(20):
        rising._track_noise_floor(0.05, 320)
    risen = rising._noise_floor

    falling = _bus()
    falling._noise_floor = 0.05
    for _ in range(20):
        falling._track_noise_floor(0.001, 320)
    fallen = falling._noise_floor

    rise_frac = (risen - 0.001) / (0.05 - 0.001)
    fall_frac = (0.05 - fallen) / (0.05 - 0.001)
    assert fall_frac > rise_frac, "the floor does not fall faster than it rises"


def test_loud_frames_still_teach_the_floor() -> None:
    """A tracker fed only quiet frames could never learn that the room got
    louder, and would gate speech as though it were still silent. The SLOW
    rise is what stops speech dragging the floor up to meet it."""
    bus = _bus()
    bus._noise_floor = 0.001
    for _ in range(200):
        bus._track_noise_floor(0.08, 320)
    assert bus._noise_floor > 0.001


def test_boost_is_bounded() -> None:
    """A silent room multiplied without limit becomes static, and a VAD that
    fires on everything."""
    bus = _bus()
    _settle(bus, 1e-5)
    out = bus._fit_to_range(_tone(0.006))
    assert float(np.max(np.abs(out))) <= 1.0
    assert bus.agc_state()["max_boost"] == _AGC_MAX_BOOST


# ---------------------------------------------------------------------------
# Both directions still coexist
# ---------------------------------------------------------------------------


def test_downward_scaling_still_works() -> None:
    """The over-scale path this replaced must be untouched — the device still
    delivers above full scale."""
    bus = _bus()
    out = bus._fit_to_range(_tone(4.19))
    assert float(np.max(np.abs(out))) < 1.0
    assert int(np.sum(np.abs(out) >= 0.999)) == 0


def test_mid_level_audio_passes_through_untouched() -> None:
    """Between the gate and the ceiling nothing happens — a governor that
    rewrote every frame would be a permanent distortion."""
    bus = _bus()
    _settle(bus, 0.002)
    mid = _tone(0.35)
    out = bus._fit_to_range(mid)
    assert np.array_equal(out, mid)


def test_boost_preserves_waveform_shape() -> None:
    """Upward scaling is a scalar multiply, exactly as downward is — every
    harmonic ratio and zero crossing intact."""
    bus = _bus()
    _settle(bus, 0.002)
    src = _tone(0.02)
    for _ in range(6):                       # let the ramp settle
        out = bus._fit_to_range(src)
    nz = np.abs(src) > 1e-9
    ratios = out[nz] / src[nz]
    assert float(ratios.max() - ratios.min()) < 1e-3, "boost varied within the frame"


@pytest.mark.parametrize(
    "frame",
    [np.zeros(0, dtype=np.float32),
     np.full(320, np.nan, dtype=np.float32),
     np.full(320, np.inf, dtype=np.float32)],
    ids=["empty", "nan", "inf"],
)
def test_degenerate_frames_never_raise(frame: np.ndarray) -> None:
    out = _bus()._fit_to_range(frame)
    assert out is not None
    if out.size:
        assert np.all(np.isfinite(out))


def test_agc_state_exposes_the_floor() -> None:
    bus = _bus()
    _settle(bus, 0.003)
    state = bus.agc_state()
    assert state["noise_floor"] > 0.0
    assert state["quiet_peak"] == _AGC_QUIET_PEAK
    assert state["nominal"] == _AGC_NOMINAL

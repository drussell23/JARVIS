"""Pre-quantization floating-point AGC — the three mandated assertions.

1. A float array peaking at 4.19 scales to below 1.0.
2. The waveform's relative shape is preserved — no hard clipping.
3. A following array peaking at 0.1 releases back up over time.

Assertion 2 is the one with teeth, and it is written to catch the failure the
previous implementation had rather than a proxy for it. "Peak is under 1.0" is
satisfied equally by a scaler and by a tanh saturator; only comparing the
SHAPE separates them. So it asserts the operation is a pure scalar multiply —
every sample divided by the same constant — which no compressor can pass.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.audio.audio_bus import (
    _AGC_CEILING,
    _AGC_TARGET,
    AudioBus,
)


class _Cfg:
    internal_rate = 16000


def _bus() -> AudioBus:
    """A bus with only the governor's state.

    __new__ deliberately: __init__ opens a device, and the AGC is a pure
    function of a frame and three floats."""
    b = AudioBus.__new__(AudioBus)
    b._agc_gain = 1.0
    b._range_peak = 0.0
    b._range_reports = 99          # suppress the over-scale log in tests
    b._config = _Cfg()
    return b


def _tone(peak: float, n: int = 320, freq: float = 220.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / 16000.0
    return (np.sin(2 * np.pi * freq * t) * peak).astype(np.float32)


# ---------------------------------------------------------------------------
# Assertion 1 — the 4.19 peak comes down
# ---------------------------------------------------------------------------


def test_the_observed_4_19_peak_scales_below_full_scale() -> None:
    """4.19 is not hypothetical — it is what the capture device delivered
    while the operator spoke, recorded by the capture forensics."""
    out = _bus()._fit_to_range(_tone(4.19))
    assert float(np.max(np.abs(out))) < 1.0
    assert float(np.max(np.abs(out))) == pytest.approx(_AGC_TARGET, abs=1e-3)


@pytest.mark.parametrize("peak", [0.95, 1.0, 1.7, 4.19, 12.0, 50.0])
def test_any_over_scale_input_lands_under_full_scale(peak: float) -> None:
    """Unbounded input: float PCM has no ceiling, so neither can the test."""
    out = _bus()._fit_to_range(_tone(peak))
    assert float(np.max(np.abs(out))) <= 1.0


def test_quiet_frames_pass_through_bit_identical() -> None:
    """The common case must cost nothing AND change nothing. A governor that
    rewrote every frame would be a permanent distortion in exchange for an
    occasional one."""
    quiet = _tone(0.35)
    out = _bus()._fit_to_range(quiet)
    assert np.array_equal(out, quiet)
    assert out is quiet, "a below-ceiling frame was copied rather than returned"


# ---------------------------------------------------------------------------
# Assertion 2 — shape preserved, no hard clipping
# ---------------------------------------------------------------------------


def test_the_waveform_shape_is_mathematically_preserved() -> None:
    """The distinction that matters.

    A tanh soft-knee also keeps the peak under 1.0 — and compresses loud
    samples against quiet ones, manufacturing harmonics that were never
    spoken. Speech recognition reads formant structure, so that is the wrong
    correction. A pure scalar multiply changes amplitude and nothing else."""
    src = _tone(4.19)
    out = _bus()._fit_to_range(src)

    nz = np.abs(src) > 1e-6
    ratios = out[nz] / src[nz]
    assert float(ratios.max() - ratios.min()) < 1e-6, (
        "gain varied across the frame — this is a compressor, not a scaler"
    )
    cos = float(np.dot(out, src) / (np.linalg.norm(out) * np.linalg.norm(src)))
    assert cos == pytest.approx(1.0, abs=1e-6)


def test_nothing_is_hard_clipped() -> None:
    src = _tone(4.19)
    out = _bus()._fit_to_range(src)
    assert int(np.sum(np.abs(out) >= 0.999)) == 0


def test_a_compressor_would_fail_this_suite() -> None:
    """Guards the guard. If someone reinstates a soft knee, the shape
    assertion above must actually catch it — so prove the assertion has
    teeth by running it against the very curve that was replaced."""
    src = _tone(4.19)
    t, span = 0.75, 0.25
    mag = np.abs(src)
    tanh_out = src.copy()
    over = mag > t
    tanh_out[over] = np.sign(src[over]) * (t + span * np.tanh((mag[over] - t) / span))

    nz = np.abs(src) > 1e-6
    ratios = tanh_out[nz] / src[nz]
    assert float(ratios.max() - ratios.min()) > 1e-3, (
        "the shape assertion cannot distinguish a compressor from a scaler"
    )


def test_harmonic_content_is_not_manufactured() -> None:
    """A pure tone scaled is still a pure tone. Put through a nonlinearity it
    grows harmonics — which is the corruption, stated in the frequency domain
    where a recogniser actually reads it."""
    src = _tone(4.19, n=1600, freq=250.0)
    out = _bus()._fit_to_range(src)

    def harmonic_energy(sig: np.ndarray) -> float:
        spec = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
        freqs = np.fft.rfftfreq(len(sig), 1 / 16000.0)
        fund = spec[(freqs > 200) & (freqs < 300)].sum()
        harm = spec[(freqs > 400)].sum()
        return float(harm / max(fund, 1e-9))

    assert harmonic_energy(out) == pytest.approx(harmonic_energy(src), rel=1e-3)


# ---------------------------------------------------------------------------
# Assertion 3 — asymmetric release
# ---------------------------------------------------------------------------


def test_gain_releases_back_up_over_time() -> None:
    """Assertion 3. After the loud passage, quiet frames must recover — and
    recover SLOWLY, or the noise floor rises inside every pause between words
    (audible pumping, and a moving floor for the endpointer to chase)."""
    bus = _bus()
    bus._fit_to_range(_tone(4.19))
    attacked = bus._agc_gain
    assert attacked < 0.3, "attack did not engage"

    quiet = _tone(0.1)
    trace = []
    for _ in range(200):                      # 200 frames x 20ms = 4s
        bus._fit_to_range(quiet)
        trace.append(bus._agc_gain)

    assert trace[0] > attacked, "gain did not release at all"
    assert all(b >= a - 1e-9 for a, b in zip(trace, trace[1:])), (
        "release was not monotonic"
    )
    assert trace[-1] > trace[0], "release stalled"
    assert trace[-1] <= 1.0, "gain overshot unity"


def test_release_is_slower_than_attack() -> None:
    """The asymmetry itself, stated as a comparison rather than a constant:
    attack completes in ONE frame, release must not."""
    bus = _bus()
    bus._fit_to_range(_tone(4.19))
    attacked = bus._agc_gain

    bus._fit_to_range(_tone(0.1))
    after_one_frame = bus._agc_gain

    recovered = (after_one_frame - attacked) / max(1.0 - attacked, 1e-9)
    assert recovered < 0.10, (
        f"release recovered {recovered:.0%} in a single frame — that is a "
        f"step, not a decay"
    )


def test_release_is_time_based_not_frame_based() -> None:
    """The time constant is in SECONDS. Two buses fed the same DURATION of
    quiet audio in different frame sizes must land in the same place, or the
    governor's behaviour would silently change with the device's buffer."""
    big, small = _bus(), _bus()
    big._fit_to_range(_tone(4.19))
    small._fit_to_range(_tone(4.19))

    for _ in range(10):                       # 10 x 640 samples
        big._fit_to_range(_tone(0.1, n=640))
    for _ in range(20):                       # 20 x 320 samples — same seconds
        small._fit_to_range(_tone(0.1, n=320))

    assert big._agc_gain == pytest.approx(small._agc_gain, rel=0.05)


# ---------------------------------------------------------------------------
# Edge cases — the governor runs on the audio thread and may never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        np.zeros(0, dtype=np.float32),
        np.zeros(320, dtype=np.float32),
        np.full(320, np.nan, dtype=np.float32),
        np.full(320, np.inf, dtype=np.float32),
        np.array([1e30, -1e30] * 160, dtype=np.float32),
    ],
    ids=["empty", "silence", "nan", "inf", "absurd"],
)
def test_degenerate_frames_never_raise(frame: np.ndarray) -> None:
    out = _bus()._fit_to_range(frame)
    assert out is not None
    if out.size:
        assert np.all(np.isfinite(out))
        assert float(np.max(np.abs(out))) <= 1.0


def test_non_finite_input_does_not_poison_later_frames() -> None:
    """A NaN reaching the gain state would silence every frame after it —
    a permanent fault from one bad buffer."""
    bus = _bus()
    bus._fit_to_range(np.full(320, np.nan, dtype=np.float32))
    assert np.isfinite(bus._agc_gain)

    out = bus._fit_to_range(_tone(0.35))
    assert np.all(np.isfinite(out))
    assert float(np.max(np.abs(out))) > 0.0, "a NaN frame muted the signal"


def test_gain_never_falls_below_the_floor() -> None:
    """One freak transient must not attenuate the whole session toward
    silence with no way back inside the release constant."""
    bus = _bus()
    bus._fit_to_range(_tone(1e6))
    assert bus._agc_gain >= 0.0
    assert bus.agc_state()["gain"] > 0.0


def test_agc_state_is_observable() -> None:
    bus = _bus()
    bus._fit_to_range(_tone(4.19))
    state = bus.agc_state()
    assert state["gain"] < 1.0
    assert state["ceiling"] == _AGC_CEILING
    assert state["peak_seen"] == pytest.approx(4.19, abs=1e-2)


def test_sustained_loud_input_does_not_ratchet_toward_silence() -> None:
    """Steady loud speech must settle at ONE gain, not keep attenuating: the
    scaled output sits under the ceiling, so the next frame must not read its
    own correction as a fresh excursion."""
    bus = _bus()
    gains = []
    for _ in range(50):
        bus._fit_to_range(_tone(4.19))
        gains.append(bus._agc_gain)
    assert gains[-1] == pytest.approx(gains[0], rel=1e-6), (
        "gain ratcheted downward on steady input"
    )

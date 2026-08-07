"""
One correct formant estimator, replacing two copies of the same wrong one.

WHAT WAS WRONG
--------------
Two independent implementations claimed LPC in their docstrings and did
something else entirely::

    # enroll_voice.VoiceCharacteristicsAnalyzer._analyze_formants
    peaks, _ = find_peaks(magnitude, height=np.max(magnitude) * 0.1, distance=20)
    f1 = float(freqs[peaks[0]])      # "first two peaks approximate F1 and F2"
    f2 = float(freqs[peaks[1]])

    # advanced_feature_extraction._extract_formants_sync
    formants = [float(freqs[p]) for p in peaks[:4]]

Both take peaks of the whole-utterance magnitude spectrum **in frequency
order** and call the lowest ones formants. Two things break at once:

1. ``freqs`` comes from ``rfftfreq(len(audio), 1/16000)``, so bin spacing is
   ``16000/N``. Over a 4-second sample that is 0.25 Hz, and ``distance=20``
   bins is a 5 Hz exclusion window — no protection at all against picking
   several "peaks" out of the same low-frequency lobe.
2. The lowest spectral peaks of a speech recording are not formants. They are
   DC offset, mains hum, HVAC, desk rumble and the f0 harmonics themselves.

The result on this machine, written to ``speaker_profiles`` and compared
against for months::

    formant_f1_hz = 42.9      # F1 is ~500 Hz
    formant_f2_hz = 80.03     # F2 is ~1500 Hz

Both under 100 Hz — inaudible as vowel resonances, and an exact match for the
rumble floor of a desk microphone. On failure each copy returned a hardcoded
``(500, 1500)`` / ``[500, 1500, 2500, 3500]``: the textbook male average,
written into a *specific* speaker's profile as if it had been measured.

WHAT A FORMANT ACTUALLY IS
--------------------------
A resonance of the vocal tract — a pole of the transfer function, not a peak of
the signal spectrum. The signal spectrum is the *source* (glottal harmonics,
spaced at f0) multiplied by that filter, which is why picking spectral peaks
finds harmonics rather than resonances.

Linear prediction estimates the filter directly: fit an all-pole model to each
frame, then read the pole angles as frequencies. This module does that, and
only that:

  * pre-emphasis, then 25 ms Hamming frames at 10 ms hop;
  * silent and unvoiced frames dropped — a frame with no vocal tract excitation
    has no vocal tract resonance to find, and averaging it in is how noise
    becomes a formant;
  * autocorrelation, Levinson-Durbin to LPC coefficients of order 2 + fs/1000;
  * polynomial roots, upper half plane, angle to frequency, radius to bandwidth;
  * candidates outside the plausible formant range or with implausibly wide
    bandwidths are discarded — a wide "pole" is the model fitting noise;
  * per-formant MEDIAN across surviving frames, not the mean: a median is
    unmoved by a handful of frames where the root ordering slipped, which is
    the dominant error mode of root-based formant tracking.

WHEN IT CANNOT MEASURE, IT SAYS SO
----------------------------------
Every unresolvable formant is ``UNMEASURED`` (NaN). There is no fallback
constant anywhere in this file, by design: a default that looks like a
measurement is what put 500/1500 into a profile that had never been measured,
and NaN is the one value no downstream consumer can mistake for evidence.

Estimating from silence yields all-NaN rather than an exception — an extractor
that raises turns missing evidence into an outage on a verification path.
"""

from __future__ import annotations

import logging
import math
import os
from typing import List, Optional, Sequence

import numpy as np

try:  # pragma: no cover - depends on which root is on sys.path
    from backend.voice.biological_bounds import UNMEASURED, BOUNDS
except ImportError:  # pragma: no cover
    from voice.biological_bounds import UNMEASURED, BOUNDS

logger = logging.getLogger(__name__)

__all__ = ["estimate_formants", "FormantConfig", "lpc_coefficients"]


def _env_float(key: str, default: float) -> float:
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("[Formants] %s=%r is not a number — keeping %g", key, raw, default)
        return default
    return value if math.isfinite(value) else default


def _env_int(key: str, default: int) -> int:
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("[Formants] %s=%r is not an integer — keeping %d", key, raw, default)
        return default


class FormantConfig:
    """
    Analysis parameters, every one env-overridable.

    Defaults are the standard values for 16 kHz speech analysis; they are
    parameters of the *method*, not thresholds on the *speaker*, which is why
    they live here rather than in ``biological_bounds`` — that module owns what
    a human voice can be, this one owns how hard we look for it.
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = int(sample_rate)
        self.window_s = _env_float("JARVIS_FORMANT_WINDOW_S", 0.025)
        self.hop_s = _env_float("JARVIS_FORMANT_HOP_S", 0.010)
        self.preemphasis = _env_float("JARVIS_FORMANT_PREEMPHASIS", 0.97)
        # Silence gate: a frame must carry this fraction of the utterance's
        # RMS to be analysed. Speech is bursty, so an absolute threshold would
        # need per-microphone tuning; a relative one does not.
        self.energy_floor_ratio = _env_float("JARVIS_FORMANT_ENERGY_FLOOR_RATIO", 0.25)
        # A pole wider than this is the model fitting broadband noise, not a
        # resonance. 400 Hz is the usual cutoff in formant tracking.
        self.max_bandwidth_hz = _env_float("JARVIS_FORMANT_MAX_BANDWIDTH_HZ", 400.0)
        # Below this a "root" is DC drift or mains hum — the exact class of
        # thing the old peak-picking code was reporting as F1.
        self.min_formant_hz = _env_float("JARVIS_FORMANT_MIN_HZ", 90.0)
        # A formant supported by fewer frames than this was not tracked, it was
        # glimpsed. Reporting it would be the same overreach as the constants.
        self.min_supporting_frames = _env_int("JARVIS_FORMANT_MIN_FRAMES", 3)

    @property
    def frame_length(self) -> int:
        return max(64, int(round(self.window_s * self.sample_rate)))

    @property
    def hop_length(self) -> int:
        return max(1, int(round(self.hop_s * self.sample_rate)))

    @property
    def lpc_order(self) -> int:
        # 2 poles per expected formant plus 2 for spectral tilt — the standard
        # 2 + fs/1000 rule (18 at 16 kHz, resolving ~4 formants below Nyquist).
        return int(2 + self.sample_rate // 1000)

    @property
    def max_formant_hz(self) -> float:
        # Poles within 100 Hz of Nyquist are edge artefacts of the fit.
        return self.sample_rate / 2.0 - 100.0


def lpc_coefficients(frame: np.ndarray, order: int) -> Optional[np.ndarray]:
    """
    LPC coefficients ``[1, a1, ..., ap]`` by Levinson-Durbin, or ``None``.

    ``None`` when the frame carries no energy or the recursion becomes
    numerically degenerate — both mean "this frame has no all-pole structure to
    read", which is missing evidence and not a reason to raise.
    """
    frame = np.asarray(frame, dtype=np.float64)
    if frame.size <= order or not np.all(np.isfinite(frame)):
        return None

    # Autocorrelation up to `order` lags.
    autocorr = np.correlate(frame, frame, mode="full")[frame.size - 1:]
    if autocorr.size < order + 1:
        return None
    r = autocorr[: order + 1]
    if r[0] <= 0 or not np.all(np.isfinite(r)):
        return None

    a = np.zeros(order + 1, dtype=np.float64)
    a[0] = 1.0
    error = float(r[0])

    for i in range(1, order + 1):
        acc = r[i] + float(np.dot(a[1:i], r[i - 1:0:-1])) if i > 1 else float(r[i])
        if error <= 0:
            return None
        k = -acc / error
        if not math.isfinite(k) or abs(k) >= 1.0:
            # |k| >= 1 makes the resulting filter unstable; its "poles" would
            # be outside the unit circle and their angles are not frequencies.
            return None
        a[1:i] = a[1:i] + k * a[i - 1:0:-1]
        a[i] = k
        error *= (1.0 - k * k)

    return a if np.all(np.isfinite(a)) else None


def _frame_formants(frame: np.ndarray, cfg: FormantConfig) -> List[float]:
    """Ascending formant candidates for one frame; empty when it has none."""
    coeffs = lpc_coefficients(frame, cfg.lpc_order)
    if coeffs is None:
        return []

    try:
        roots = np.roots(coeffs)
    except (np.linalg.LinAlgError, ValueError):
        return []

    candidates: List[float] = []
    nyquist_factor = cfg.sample_rate / (2.0 * math.pi)
    for root in roots:
        if root.imag <= 0:
            continue  # conjugate pairs — one of each pair carries the frequency
        magnitude = abs(root)
        if not (0.0 < magnitude < 1.0):
            continue
        freq = math.atan2(root.imag, root.real) * nyquist_factor
        bandwidth = -2.0 * nyquist_factor * math.log(magnitude)
        if freq < cfg.min_formant_hz or freq > cfg.max_formant_hz:
            continue
        if bandwidth > cfg.max_bandwidth_hz:
            continue
        candidates.append(freq)

    candidates.sort()
    return candidates


def estimate_formants(
    audio: Sequence[float] | np.ndarray,
    sample_rate: int = 16000,
    n_formants: int = 4,
    config: Optional[FormantConfig] = None,
) -> List[float]:
    """
    Estimate ``n_formants`` formants in Hz, ``UNMEASURED`` where unresolvable.

    Always returns exactly ``n_formants`` entries so callers can index F1..F4
    positionally, and never raises: a formant that could not be measured is NaN,
    which every consumer of this repo's voice features treats as absent.
    """
    cfg = config or FormantConfig(sample_rate)
    result = [UNMEASURED] * n_formants

    samples = np.asarray(audio, dtype=np.float64).flatten()
    if samples.size < cfg.frame_length or not np.any(np.isfinite(samples)):
        logger.debug("[Formants] %d samples is too short to analyse", samples.size)
        return result

    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)

    # Pre-emphasis flattens the -6 dB/octave glottal tilt so the higher formants
    # are not swamped by F1 in the all-pole fit.
    emphasized = np.append(samples[0], samples[1:] - cfg.preemphasis * samples[:-1])

    frame_length, hop = cfg.frame_length, cfg.hop_length
    n_frames = 1 + (emphasized.size - frame_length) // hop
    if n_frames < 1:
        return result

    strides = np.lib.stride_tricks.sliding_window_view(emphasized, frame_length)[::hop]
    rms = np.sqrt(np.mean(strides ** 2, axis=1))
    overall_rms = float(np.sqrt(np.mean(emphasized ** 2)))
    if overall_rms <= 0:
        logger.debug("[Formants] signal is silent — no resonances to measure")
        return result

    # Only frames with real excitation. Analysing silence is precisely how a
    # rumble floor becomes an F1.
    voiced = strides[rms >= cfg.energy_floor_ratio * overall_rms]
    if voiced.shape[0] == 0:
        logger.debug("[Formants] no frame cleared the energy floor")
        return result

    window = np.hamming(frame_length)
    per_formant: List[List[float]] = [[] for _ in range(n_formants)]
    for frame in voiced:
        candidates = _frame_formants(frame * window, cfg)
        for index in range(min(n_formants, len(candidates))):
            per_formant[index].append(candidates[index])

    for index, values in enumerate(per_formant):
        if len(values) < cfg.min_supporting_frames:
            continue
        # Median, not mean: root ordering slips on a minority of frames and a
        # mean would carry those excursions into the reported value.
        result[index] = float(np.median(values))

    result = _enforce_ordering(result)
    result = _enforce_bounds(result)
    _log_outcome(result, len(voiced))
    return result


def _enforce_ordering(formants: List[float]) -> List[float]:
    """
    Drop any formant that is not above the last one resolved below it.

    ``per_formant[k]`` collects the k-th candidate *of each frame*, and that
    index only means "the k-th formant" while every frame resolves the same
    number of candidates. When some frames find three poles and others five,
    index 2 refers to different resonances in different frames and the median
    across them is a number with no referent.

    A non-ascending result is the observable symptom of exactly that, and it is
    detectable without knowing the true answer: formants are ordered by
    definition. Measured on white noise, this estimator returned
    ``[2508, 5069, 4212, 6929]`` — F3 below F2, so the set is incoherent.
    Reporting the offending entries as unmeasured is the honest response;
    keeping them would be publishing a number the method cannot support.
    """
    result = list(formants)
    highest = 0.0
    for index, value in enumerate(result):
        if not math.isfinite(value):
            continue
        if value <= highest:
            logger.info(
                "[Formants] F%d=%.0f Hz is not above F%d — frame candidate "
                "ordering was inconsistent; reporting it unmeasured",
                index + 1, value, index,
            )
            result[index] = UNMEASURED
            continue
        highest = value
    return result


def _enforce_bounds(formants: List[float]) -> List[float]:
    """
    Drop any formant outside the band that formant occupies in human speech.

    This is the mandate the two previous implementations lacked entirely: a
    value is validated against physiology *before* it is yielded, so an
    impossible number never reaches a database, a comparison or a verdict. It
    is the same registry the verifier reads, so the writer and the reader cannot
    hold different opinions about what is possible — the enrolled 42.9 Hz passed
    the writer precisely because the writer had no opinion at all.
    """
    result = list(formants)
    for index, value in enumerate(result):
        if not math.isfinite(value):
            continue
        bound = BOUNDS.get(f"formant_f{index + 1}_hz")
        if bound is None or bound.contains(value):
            continue
        logger.warning(
            "[Formants] F%d=%.0f Hz is outside %s — not a formant, reporting "
            "unmeasured", index + 1, value, bound.describe(),
        )
        result[index] = UNMEASURED
    return result


def _log_outcome(formants: Sequence[float], n_frames: int) -> None:
    """One line naming what was resolved, so a silent all-NaN is never silent."""
    unresolved = [f"F{i + 1}" for i, value in enumerate(formants) if not math.isfinite(value)]
    if not unresolved:
        return
    if len(unresolved) == len(formants):
        logger.warning(
            "[Formants] no formant resolved from %d voiced frame(s) — reporting "
            "unmeasured rather than a default", n_frames,
        )
    else:
        logger.info(
            "[Formants] %s unresolved from %d voiced frame(s) — reported unmeasured",
            "+".join(unresolved), n_frames,
        )


def plausible_band(index: int) -> Optional[str]:
    """The measurability band for formant ``index`` (0-based), for log lines."""
    bound = BOUNDS.get(f"formant_f{index + 1}_hz")
    return bound.describe() if bound else None

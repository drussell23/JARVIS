"""Deterministic synthetic audio generator for ECAPA speaker-embedding tests.

Slice 250.2b Phase 1 — the offline "vector injector" fixtures. This module
produces **byte-identical** synthetic voice waveforms via additive harmonic
synthesis with a formant envelope, so the speaker-embedding parity harness can
prove the embedder *discriminates* (same voice accepts, different voice
rejects) without ever touching the heavy/cloud ECAPA model.

Pipeline convention (matches ``backend/core/ecapa_facade.py``):
    16 kHz mono ``float32`` waveform in ``[-1, 1]``.

Determinism contract (NON-NEGOTIABLE)
-------------------------------------
Identical arguments produce identical bytes — in-process and across separate
processes. We use ONLY ``np.random.default_rng(seed)`` (a stateless, explicitly
seeded Generator). No global ``np.random`` calls, no ``random`` module, no time,
no mutable module/global state. This makes every clip a reproducible fixture.

Unified-memory / Apple-Silicon friendliness
-------------------------------------------
Everything is ``float32``, generated lazily one clip at a time, with no torch
tensors and no large batched allocations. A 3 s clip at 16 kHz is ~192 KB; the
full 3-clip ABC matrix is well under 1 MiB and astronomically far under the
16 GB unified-memory ceiling (see ``estimate_matrix_bytes`` / ``MAX_MATRIX_BYTES``).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000

# Documented cap for the offline matrix. The real matrix is ~3 clips * 192 KB
# (~0.55 MiB); 16 MiB is a comfortable, self-documenting headroom ceiling that
# stays ~1000x under the 16 GB unified-memory limit.
MAX_MATRIX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class VoiceProfile:
    """A synthetic speaker.

    ``formants`` is a tuple of ``(center_hz, bandwidth_hz, gain)`` triples. The
    harmonic amplitude at frequency ``f`` is the sum of Gaussian bumps, one per
    formant, each centered at ``center_hz`` with std ``bandwidth_hz`` and scaled
    by ``gain``. Distinct f0 + distinct formant sets => spectrally separable
    speakers.
    """

    name: str
    f0_hz: float
    formants: tuple[tuple[float, float, float], ...]


# Two canonical speakers tuned so the spectral embedder separates them with a
# clear margin (see tests/ml/test_speaker_parity_harness.py). ALPHA is a low,
# "chesty" voice; BRAVO is higher with formants shifted well up the spectrum.
ALPHA = VoiceProfile(
    name="ALPHA",
    f0_hz=110.0,
    formants=(
        (500.0, 90.0, 1.0),
        (1100.0, 110.0, 0.7),
        (2300.0, 160.0, 0.35),
    ),
)

BRAVO = VoiceProfile(
    name="BRAVO",
    f0_hz=170.0,
    formants=(
        (800.0, 110.0, 1.0),
        (1900.0, 150.0, 0.8),
        (3400.0, 220.0, 0.5),
    ),
)


def _formant_envelope(freqs: np.ndarray, profile: VoiceProfile) -> np.ndarray:
    """Sum-of-Gaussians spectral envelope evaluated at ``freqs`` (Hz)."""
    env = np.zeros_like(freqs, dtype=np.float64)
    for center, bandwidth, gain in profile.formants:
        bw = max(float(bandwidth), 1e-6)
        env += float(gain) * np.exp(-0.5 * ((freqs - float(center)) / bw) ** 2)
    return env


def generate_waveform(
    profile: VoiceProfile,
    *,
    seed: int,
    duration_s: float = 3.0,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Deterministic additive-synthesis voice waveform.

    Sums harmonics ``k = 1..K`` of ``profile.f0_hz`` (K up to Nyquist), each
    weighted by the formant envelope at ``k * f0``. Per-harmonic phases and a
    low-amplitude noise floor are drawn from a single ``default_rng(seed)`` so
    the result is byte-identical for identical arguments.

    Returns a ``float32`` array of shape ``(round(duration_s*sample_rate),)``,
    peak-normalized to 0.95, with values in ``[-1, 1]``.
    """
    rng = np.random.default_rng(seed)

    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)

    nyquist = sample_rate / 2.0
    f0 = float(profile.f0_hz)
    # Harmonics strictly below Nyquist.
    n_harmonics = max(1, int(np.floor((nyquist - 1e-9) / f0)))
    k = np.arange(1, n_harmonics + 1, dtype=np.float64)
    harmonic_freqs = k * f0

    amplitudes = _formant_envelope(harmonic_freqs, profile)
    # Draw ALL random quantities up front from the single rng, in a fixed order,
    # so byte-output depends only on (seed, profile, shape) — never call order.
    phases = rng.uniform(0.0, 2.0 * np.pi, size=n_harmonics)
    noise = rng.standard_normal(n).astype(np.float64) * 1e-3

    # Build the signal harmonic by harmonic (small K, tiny memory footprint).
    signal = np.zeros(n, dtype=np.float64)
    two_pi = 2.0 * np.pi
    for amp, freq, ph in zip(amplitudes, harmonic_freqs, phases):
        if amp <= 0.0:
            continue
        signal += amp * np.sin(two_pi * freq * t + ph)
    signal += noise

    peak = float(np.max(np.abs(signal)))
    if peak > 0.0:
        signal = signal * (0.95 / peak)

    out = signal.astype(np.float32)
    # Guard against float32 rounding nudging a sample fractionally past +/-1.
    np.clip(out, -1.0, 1.0, out=out)
    return out


@dataclass
class ABCMatrix:
    """The 3-clip discriminative matrix.

    A = ALPHA(seed_a), B = ALPHA(seed_b) (same voice, different utterance ->
    must ACCEPT vs A), C = BRAVO(seed_c) (different voice -> must REJECT vs A).
    """

    a: np.ndarray
    b: np.ndarray
    c: np.ndarray
    profile_a: VoiceProfile
    profile_b: VoiceProfile
    profile_c: VoiceProfile
    seed_a: int
    seed_b: int
    seed_c: int


def build_abc_matrix(
    *,
    seed_a: int = 101,
    seed_b: int = 202,
    seed_c: int = 303,
    duration_s: float = 3.0,
) -> ABCMatrix:
    """Build the deterministic A/B/C fixture matrix."""
    a = generate_waveform(ALPHA, seed=seed_a, duration_s=duration_s)
    b = generate_waveform(ALPHA, seed=seed_b, duration_s=duration_s)
    c = generate_waveform(BRAVO, seed=seed_c, duration_s=duration_s)
    return ABCMatrix(
        a=a,
        b=b,
        c=c,
        profile_a=ALPHA,
        profile_b=ALPHA,
        profile_c=BRAVO,
        seed_a=seed_a,
        seed_b=seed_b,
        seed_c=seed_c,
    )


def estimate_matrix_bytes(
    duration_s: float,
    sample_rate: int = SAMPLE_RATE,
    n_clips: int = 3,
) -> int:
    """Exact resident size (bytes) of the float32 ABC matrix.

    float32 == 4 bytes/sample. Used to assert the matrix stays far under
    ``MAX_MATRIX_BYTES`` and the 16 GB unified-memory ceiling.
    """
    samples_per_clip = int(round(duration_s * sample_rate))
    return n_clips * samples_per_clip * 4

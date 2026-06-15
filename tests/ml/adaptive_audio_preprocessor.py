"""Adaptive Feature Extraction Matrix -- AdaptiveAudioPreprocessor (Slice 250.2b
Phase 2).

A deterministic, pure-numpy preprocessing stage that turns variable-length raw
mono waveforms into fixed-length, level-normalized ``float32`` vectors suitable
for the speaker-embedding pipeline. No torch, no scipy.

Three responsibilities
----------------------
1. **fit_length** -- adaptively pad (short clips) or truncate (long clips) to an
   EXACT ``target_length``. Center or head/tail strategies, deterministic.
2. **rms_normalize** -- scale to a target RMS so loud/quiet recordings of the
   same voice land at a consistent level. Silence-guarded (no div-by-zero).
3. **preprocess** -- the composed pipeline: RMS-normalize FIRST, then fit_length.

Order rationale (load-bearing)
------------------------------
RMS normalization is applied to the *raw signal* BEFORE pad/truncate. If we
padded first, the appended zeros would dilute ``mean(x**2)`` and the normalizer
would compensate by amplitude-boosting the real signal (a short clip padded to
2x length would be scaled ~sqrt(2) louder than an unpadded twin of the same
voice). Normalizing the signal first preserves the signal's relative level;
the subsequent zero-padding then contributes exactly zero energy and never gets
amplified. Truncation after normalization is also benign -- it only drops
samples, it never injects energy.

Async surface
-------------
``preprocess_async`` offloads the CPU-bound math via ``asyncio.to_thread`` so the
event loop is never blocked (Manifesto 3: asynchronous tendrils, no event-loop
starvation). For heavier-than-a-clip batch jobs a ``ProcessPoolExecutor`` (true
parallelism past the GIL) is the appropriate escalation; for per-clip latency
``to_thread`` is the right, lighter default and is what we implement here.

Injection contract (Phase 3)
----------------------------
``RawAudioSource`` is a ``runtime_checkable`` structural Protocol that mirrors the
shape of the 250.2a ``AudioCapture`` seam WITHOUT importing it -- alignment is
structural, not by import, so the two sessions stay decoupled. If 250.2a's final
read method is named differently, a one-line adapter
(``lambda: capture.<their_method>()``) reconciles it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

# Default target length: 3 s @ 16 kHz mono == 48000 samples. Matches the Phase 1
# fixture clip length so the parity-preserved integration stays apples-to-apples.
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_TARGET_RMS = 0.1
_DEFAULT_TARGET_LENGTH = 48000

# Below this RMS the input is treated as silence and returned unchanged (no
# div-by-zero, no NaN/inf amplification of a noise floor).
_SILENCE_EPS = 1e-8


@runtime_checkable
class RawAudioSource(Protocol):
    """Structural injection contract for a raw mono-audio producer.

    Mirrors the 250.2a ``AudioCapture`` seam by SHAPE only (no import): a
    ``sample_rate`` attribute and a ``read()`` returning a 1-D mono float array.
    ``@runtime_checkable`` so a duck-typed mock or the real capture object both
    satisfy ``isinstance(obj, RawAudioSource)``.

    NOTE: ``runtime_checkable`` Protocols check method/attr *presence*, not
    signatures -- the real structural alignment is enforced by
    ``preprocess_from_source`` actually calling ``read()`` and consuming a
    ``np.ndarray``.
    """

    sample_rate: int

    def read(self) -> "np.ndarray":
        """Return the most recent raw mono waveform as a 1-D float array."""
        ...


@dataclass(frozen=True)
class PreprocessConfig:
    """Immutable preprocessing knobs.

    ``truncate``: ``"center"`` (keep the middle window) or ``"head"`` (keep the
    leading window). ``pad``: ``"center"`` (signal centered, pad split both
    sides) or ``"tail"`` (signal at front, pad appended at the end).
    """

    target_length: int
    sample_rate: int = _DEFAULT_SAMPLE_RATE
    target_rms: float = _DEFAULT_TARGET_RMS
    pad_value: float = 0.0
    truncate: str = "center"  # "center" | "head"
    pad: str = "center"  # "center" | "tail"

    def __post_init__(self) -> None:
        if self.target_length <= 0:
            raise ValueError(f"target_length must be > 0, got {self.target_length}")
        if self.truncate not in ("center", "head"):
            raise ValueError(f"truncate must be 'center'|'head', got {self.truncate!r}")
        if self.pad not in ("center", "tail"):
            raise ValueError(f"pad must be 'center'|'tail', got {self.pad!r}")

    @classmethod
    def from_env(cls) -> "PreprocessConfig":
        """Build from ``JARVIS_AUDIO_PREPROC_*`` env vars (defaults, no hardcoding)."""
        target_length = int(
            os.environ.get("JARVIS_AUDIO_PREPROC_TARGET_LENGTH", _DEFAULT_TARGET_LENGTH)
        )
        target_rms = float(
            os.environ.get("JARVIS_AUDIO_PREPROC_TARGET_RMS", _DEFAULT_TARGET_RMS)
        )
        sample_rate = int(
            os.environ.get("JARVIS_AUDIO_PREPROC_SAMPLE_RATE", _DEFAULT_SAMPLE_RATE)
        )
        return cls(
            target_length=target_length,
            sample_rate=sample_rate,
            target_rms=target_rms,
        )

    @classmethod
    def for_duration(
        cls,
        duration_s: float,
        *,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        target_rms: float = _DEFAULT_TARGET_RMS,
        pad_value: float = 0.0,
        truncate: str = "center",
        pad: str = "center",
    ) -> "PreprocessConfig":
        """Convenience: derive ``target_length`` from a wall-clock duration."""
        target_length = int(round(duration_s * sample_rate))
        return cls(
            target_length=target_length,
            sample_rate=sample_rate,
            target_rms=target_rms,
            pad_value=pad_value,
            truncate=truncate,
            pad=pad,
        )


class AdaptiveAudioPreprocessor:
    """Deterministic pure-numpy fixed-length + RMS-normalized preprocessor."""

    def __init__(self, config: PreprocessConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Length adaptation
    # ------------------------------------------------------------------ #
    def fit_length(self, x: "np.ndarray") -> "np.ndarray":
        """Pad/truncate ``x`` to EXACTLY ``config.target_length`` samples.

        - ``len(x) < target`` -> zero-pad (``pad_value``) per ``config.pad``.
        - ``len(x) > target`` -> truncate per ``config.truncate``.
        - ``len(x) == target`` -> returned unchanged (as float32 copy).

        Output length is ALWAYS exactly ``target_length``. Deterministic.
        """
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        target = self.config.target_length
        n = x.size

        if n == target:
            return x.copy()

        if n < target:
            deficit = target - n
            if self.config.pad == "center":
                left = deficit // 2
                right = deficit - left
            else:  # "tail"
                left = 0
                right = deficit
            return np.pad(
                x,
                (left, right),
                mode="constant",
                constant_values=float(self.config.pad_value),
            ).astype(np.float32, copy=False)

        # n > target -> truncate.
        if self.config.truncate == "center":
            start = (n - target) // 2
        else:  # "head"
            start = 0
        return x[start : start + target].astype(np.float32, copy=True)

    # ------------------------------------------------------------------ #
    # Level normalization
    # ------------------------------------------------------------------ #
    def rms_normalize(self, x: "np.ndarray") -> "np.ndarray":
        """Scale ``x`` so ``sqrt(mean(x**2)) == target_rms``.

        Silence guard: if the input RMS is below ``_SILENCE_EPS`` the array is
        returned unchanged (no div-by-zero, no NaN/inf). The final result is
        clipped to ``[-1, 1]``; clipping only engages when the target RMS pushes
        a high-crest-factor signal's peaks past unity (documented, deterministic).
        """
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        x64 = x.astype(np.float64)
        rms = float(np.sqrt(np.mean(x64 * x64))) if x64.size else 0.0

        if rms < _SILENCE_EPS:
            # Silence / DC-zero: nothing to normalize; return unchanged.
            return x.copy()

        scale = float(self.config.target_rms) / rms
        out = (x64 * scale).astype(np.float32)
        np.clip(out, -1.0, 1.0, out=out)
        return out

    # ------------------------------------------------------------------ #
    # Composed pipeline
    # ------------------------------------------------------------------ #
    def preprocess(self, x: "np.ndarray") -> "np.ndarray":
        """RMS-normalize FIRST, then fit_length. Returns float32 (target_length,).

        Order rationale: normalizing the raw signal before padding keeps the
        signal's relative level intact -- the subsequently appended zeros carry
        zero energy and are never amplitude-boosted. See module docstring.
        """
        normalized = self.rms_normalize(x)
        fitted = self.fit_length(normalized)
        # fit_length already returns float32 of exactly target_length.
        return np.asarray(fitted, dtype=np.float32)

    async def preprocess_async(self, x: "np.ndarray") -> "np.ndarray":
        """Off-thread ``preprocess`` so the event loop never blocks.

        Implemented with ``asyncio.to_thread`` (releases the GIL during numpy's
        C-level math). For heavier batch workloads a ``ProcessPoolExecutor``
        (``loop.run_in_executor(pool, ...)``) is the true-parallelism escalation;
        ``to_thread`` is the right per-clip default.
        """
        return await asyncio.to_thread(self.preprocess, x)

    async def preprocess_from_source(self, src: RawAudioSource) -> "np.ndarray":
        """Consume the structural injection contract: read raw audio + preprocess.

        Calls ``src.read()`` (the 250.2a-aligned seam), coerces to float32, and
        runs the full pipeline off-thread.
        """
        return await asyncio.to_thread(
            lambda: self.preprocess(np.asarray(src.read(), dtype=np.float32))
        )

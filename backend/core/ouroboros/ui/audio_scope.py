"""Braille oscilloscope — the audio plane made visible in the cockpit header.

Why Braille
-----------
A block character (``█``) gives ONE vertical level per terminal cell, so a
volume meter built from blocks is a chunky 1-bit staircase. A Unicode Braille
cell (U+2800..U+28FF) carries **8 independently addressable dots in a 2x4
grid**, so one cell encodes TWO horizontal samples at FOUR vertical levels
each — 8x the horizontal density and 4x the vertical resolution of blocks, at
identical cell cost. A 20-cell scope therefore shows 40 samples.

Dot bit layout (Unicode Braille, dots 1-8)::

      col0  col1
    row0  1(0x01)  4(0x08)
    row1  2(0x02)  5(0x10)
    row2  3(0x04)  6(0x20)
    row3  7(0x40)  8(0x80)

Bars fill from the BOTTOM row upward, so silence reads as a flat baseline
rather than a floating cloud.

Design constraints this module honours
-------------------------------------
* **No blocking math.** RMS is a single pass over a sample buffer, and the
  render path is pure integer/table work — no FFT, no numpy requirement, no
  allocation per frame beyond the output string.
* **No hardcoded scale.** The normalizer adapts to the observed signal via a
  decaying peak reference, so a quiet mic and a hot mic both fill the scope
  instead of one flatlining and the other clipping.
* **Stateless render.** :meth:`BrailleScope.render` is a pure function of the
  ring buffer, so the caller's existing clock-driven repaint animates it with
  zero extra tasks (the same discipline the mini-crest uses).
* **Degradation.** A terminal that cannot encode Braille falls back to an
  ASCII ramp rather than emitting replacement glyphs.
"""

from __future__ import annotations

import enum
import math
import os
import threading
from collections import deque
from typing import Deque, Iterable, Optional, Sequence, Tuple

_BRAILLE_BASE = 0x2800

# Bottom-to-top dot masks per sub-column. Filling from index 0 upward makes a
# louder sample a taller bar anchored to the baseline.
_COL0_BOTTOM_UP: Tuple[int, ...] = (0x40, 0x04, 0x02, 0x01)   # dots 7,3,2,1
_COL1_BOTTOM_UP: Tuple[int, ...] = (0x80, 0x20, 0x10, 0x08)   # dots 8,6,5,4

#: Vertical levels a single Braille sub-column can express.
LEVELS = 4
#: Horizontal samples a single Braille cell can express.
SAMPLES_PER_CELL = 2

# ASCII fallback ramp, lowest → highest.
_ASCII_RAMP = (" ", ".", ":", "|", "#")


class AudioPlane(str, enum.Enum):
    """Who is producing sound right now.

    The scope is a shared surface: colouring by plane is what makes it legible
    at a glance (is that me, or is that the organism talking back?)."""

    IDLE = "idle"
    USER = "user"          # microphone capture
    SYSTEM = "system"      # TTS playback


#: AudioPlane → semantic theme colour NAME (never a raw hex value, so the
#: reactive theme stays the single source of truth for palette + tier).
PLANE_ACCENT = {
    AudioPlane.IDLE: "muted",
    AudioPlane.USER: "cyan",
    AudioPlane.SYSTEM: "venom_green",
}


def scope_enabled() -> bool:
    """Master gate. Default ON — the scope is inert without a level stream."""
    return os.environ.get(
        "JARVIS_AUDIO_SCOPE_ENABLED", "true",
    ).strip().lower() in ("1", "true", "yes", "on")


def braille_available() -> bool:
    """Whether the terminal encoding can carry Braille. Falls back to ASCII on
    a legacy/POSIX locale rather than emitting replacement glyphs."""
    if os.environ.get("JARVIS_AUDIO_SCOPE_ASCII", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    try:
        "⣿".encode(
            os.environ.get("PYTHONIOENCODING")
            or getattr(__import__("sys").stdout, "encoding", None)
            or "utf-8"
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def rms(samples: Sequence[float]) -> float:
    """Root-mean-square of a normalized (-1.0..1.0) sample buffer.

    The canonical implementation. Six sites across ``backend/voice`` each
    inlined ``np.sqrt(np.mean(chunk**2))``; this is the shared seam that did
    not exist, and it needs no numpy so the UI layer stays dependency-light.
    Returns 0.0 for empty/garbage input — NEVER raises."""
    try:
        if samples is None:
            return 0.0
        n = len(samples)
        if n == 0:
            return 0.0
        total = 0.0
        for s in samples:
            try:
                v = float(s)
            except (TypeError, ValueError):
                continue
            total += v * v
        return math.sqrt(total / n)
    except Exception:  # noqa: BLE001
        return 0.0


class AdaptiveNormalizer:
    """Maps raw RMS to 0.0..1.0 against a DECAYING OBSERVED PEAK.

    A fixed divisor is wrong for audio: mic gain, distance and TTS loudness vary
    by orders of magnitude, so any constant either flatlines a quiet source or
    clips a hot one. Tracking a decaying peak means the scope always uses its
    full height for whatever signal is actually present, and recovers when the
    source changes.

    ``floor`` prevents division blow-up on near-silence and stops room noise
    from being amplified into a fake waveform."""

    def __init__(
        self, *, decay: float = 0.995, floor: float = 1e-3,
    ) -> None:
        self._decay = min(max(float(decay), 0.0), 0.999999)
        self._floor = max(float(floor), 1e-9)
        self._peak = self._floor
        self._lock = threading.Lock()

    def normalize(self, value: float) -> float:
        try:
            v = abs(float(value))
        except (TypeError, ValueError):
            return 0.0
        with self._lock:
            self._peak = max(v, self._peak * self._decay, self._floor)
            peak = self._peak
        if peak <= 0.0:
            return 0.0
        return min(1.0, max(0.0, v / peak))

    @property
    def peak(self) -> float:
        with self._lock:
            return self._peak

    def reset(self) -> None:
        with self._lock:
            self._peak = self._floor


def _level_for(value: float) -> int:
    """Normalized 0..1 → bar height 0..LEVELS.

    A nonzero-but-tiny sample must still show ONE dot: a visible baseline is
    how the operator distinguishes "listening, quiet" from "not listening"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v <= 0.0:
        return 0
    if v >= 1.0:
        return LEVELS
    return max(1, min(LEVELS, int(math.ceil(v * LEVELS))))


def cell_for(left: float, right: float) -> str:
    """Two normalized samples → one Braille cell. Pure; never raises."""
    try:
        mask = 0
        for i in range(_level_for(left)):
            mask |= _COL0_BOTTOM_UP[i]
        for i in range(_level_for(right)):
            mask |= _COL1_BOTTOM_UP[i]
        return chr(_BRAILLE_BASE + mask)
    except Exception:  # noqa: BLE001
        return chr(_BRAILLE_BASE)


def _ascii_cell(left: float, right: float) -> str:
    lvl = max(_level_for(left), _level_for(right))
    return _ASCII_RAMP[min(lvl, len(_ASCII_RAMP) - 1)]


class BrailleScope:
    """Fixed-width scrolling oscilloscope over a bounded sample ring.

    The deque is capped at ``width * SAMPLES_PER_CELL``, so appending past
    capacity evicts the oldest sample — the scroll is the data structure, not a
    slicing step, which is what keeps the render allocation-free and the index
    arithmetic incapable of running off the end."""

    def __init__(
        self,
        *,
        width: int = 20,
        plane: AudioPlane = AudioPlane.IDLE,
        normalizer: Optional[AdaptiveNormalizer] = None,
    ) -> None:
        self._width = max(1, int(width))
        self._cap = self._width * SAMPLES_PER_CELL
        self._samples: Deque[float] = deque(maxlen=self._cap)
        self._plane = plane
        self._norm = normalizer if normalizer is not None else AdaptiveNormalizer()
        self._lock = threading.Lock()

    # -- ingest ---------------------------------------------------------

    def push(self, value: float, *, normalized: bool = True) -> None:
        """Append one sample. ``normalized=False`` routes it through the
        adaptive normalizer first. NEVER raises."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if not normalized:
            v = self._norm.normalize(v)
        v = min(1.0, max(0.0, v))
        with self._lock:
            self._samples.append(v)

    def push_rms(self, samples: Sequence[float]) -> float:
        """Compute RMS over a raw audio buffer, normalize, append. Returns the
        normalized value so a caller can broadcast it without recomputing."""
        level = self._norm.normalize(rms(samples))
        self.push(level, normalized=True)
        return level

    def extend(self, values: Iterable[float], *, normalized: bool = True) -> None:
        for v in values or ():
            self.push(v, normalized=normalized)

    # -- state ----------------------------------------------------------

    def set_plane(self, plane: AudioPlane) -> bool:
        """Switch the active plane. Returns True iff it changed (so a caller
        can invalidate only on a real transition — zero-flicker discipline)."""
        with self._lock:
            if self._plane is plane:
                return False
            self._plane = plane
            return True

    @property
    def plane(self) -> AudioPlane:
        with self._lock:
            return self._plane

    @property
    def width(self) -> int:
        return self._width

    @property
    def accent(self) -> str:
        """Semantic theme colour name for the active plane."""
        return PLANE_ACCENT.get(self.plane, "muted")

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
        self._norm.reset()

    # -- render ---------------------------------------------------------

    def render(self) -> str:
        """The scope as a plain ``width``-char string, oldest sample leftmost.

        Right-to-left scroll falls out of the ring: new samples land at the
        tail, so the newest data is always at the right edge. Left-padded with
        empty cells until the buffer fills, which keeps the component a stable
        width from the very first frame (no layout jitter)."""
        try:
            with self._lock:
                buf = list(self._samples)
            use_braille = braille_available()
            pad = self._cap - len(buf)
            if pad > 0:
                buf = [0.0] * pad + buf
            out = []
            for i in range(0, self._cap, SAMPLES_PER_CELL):
                left = buf[i]
                right = buf[i + 1] if i + 1 < len(buf) else 0.0
                out.append(
                    cell_for(left, right) if use_braille
                    else _ascii_cell(left, right)
                )
            return "".join(out)
        except Exception:  # noqa: BLE001 — a frame never crashes the TUI
            return " " * self._width

    def render_rich(self) -> str:
        """Rich-markup wrapped in the plane's semantic colour. The theme owns
        the palette; this only names a colour."""
        body = self.render()
        try:
            return f"[{self.accent}]{body}[/{self.accent}]"
        except Exception:  # noqa: BLE001
            return body

    def is_silent(self) -> bool:
        with self._lock:
            return not any(v > 0.0 for v in self._samples)


__all__ = [
    "AdaptiveNormalizer",
    "AudioPlane",
    "BrailleScope",
    "LEVELS",
    "PLANE_ACCENT",
    "SAMPLES_PER_CELL",
    "braille_available",
    "cell_for",
    "rms",
    "scope_enabled",
]

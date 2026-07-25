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
import time
from collections import deque
from typing import Callable, Deque, Iterable, Optional, Sequence, Tuple

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


def _env_float(name: str, default: float) -> float:
    """Read a tunable from the environment, falling back on garbage.

    Every timing constant here is a *taste* parameter — how fast a starved
    wave should fall reads differently on a 60Hz repaint than on a laggy SSH
    session — so none of them are baked into a signature."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return float(default)


def _starve_after_s() -> float:
    """Silence longer than this means the STREAM stalled, not that the room
    went quiet. Derived from the pump's 20 FPS cap: ~50ms between frames, so
    60ms is one missed frame plus jitter — late enough not to fire on normal
    scheduling noise, early enough that a stall never looks like signal."""
    return max(0.0, _env_float("JARVIS_AUDIO_SCOPE_STARVE_S", 0.06))


def _gravity_tau_s() -> float:
    """Exponential time constant of the fall. ~50ms puts the trace on the
    baseline in about a quarter second — fast enough that a frozen spike is
    never mistaken for live sound, slow enough to read as motion rather than a
    cut."""
    return max(1e-3, _env_float("JARVIS_AUDIO_SCOPE_GRAVITY_TAU_S", 0.05))


def _gravity_floor() -> float:
    """Fraction of full scale below which the fall SNAPS to exactly 0.0.

    Exponential decay never reaches zero. Without a snap the ring would hold
    denormal-ish residue forever and ``is_silent`` would answer False for a
    scope that has been visually flat for minutes."""
    return max(0.0, _env_float("JARVIS_AUDIO_SCOPE_GRAVITY_FLOOR", 0.02))


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
        hold_frames: int = 40,
        squelch: bool = True,
        squelch_window_frames: int = 80,
        squelch_margin: float = 3.0,   # k in floor + k*spread
        squelch_epsilon: float = 1e-4,
        squelch_warmup_frames: int = 20,
    ) -> None:
        self._decay = min(max(float(decay), 0.0), 0.999999)
        self._floor = max(float(floor), 1e-9)
        self._peak = self._floor
        self._hold_frames = max(0, int(hold_frames))
        self._held = 0
        # --- dynamic noise-floor squelch -------------------------------
        # A real microphone never reaches digital zero: fans, HVAC and room
        # tone keep RMS hovering just above it, which renders as a permanently
        # twitching baseline. A fixed gate cannot solve this — the right
        # threshold in a quiet study is wrong in a coffee shop — so the floor
        # is LEARNED from the signal itself.
        self._squelch = bool(squelch)
        self._noise_floor = 0.0
        # Ambient SPREAD, not just its trough. A quiet room's tone ranges
        # ~0.002-0.006 — a 3x spread — so a gate pinned to the minimum is
        # sailed over by the noise's own peaks. Gating at floor + k*spread
        # covers the distribution instead of its lower edge, and adapts to
        # both a still study and a churning cafe without a constant.
        self._noise_spread = 0.0
        self._squelch_seen = 0
        self._squelch_warmup = max(0, int(squelch_warmup_frames))
        self._squelch_margin = max(0.0, float(squelch_margin))
        self._squelch_eps = max(0.0, float(squelch_epsilon))
        # EMA smoothing derived from the window rather than hardcoded, so
        # "about four seconds at 20 FPS" stays true if the framerate changes.
        n = max(1, int(squelch_window_frames))
        self._squelch_alpha = 2.0 / (n + 1.0)
        self._lock = threading.Lock()

    # -- noise-floor profiler -------------------------------------------

    def _update_noise_floor(self, v: float) -> None:
        """Track the TROUGH of the stream, asymmetrically. Caller holds lock.

        Down-fast / up-slow is the whole trick. A sample below the current
        floor is new evidence about how quiet the room can be, so it is adopted
        quickly. A sample above it might be speech, so the floor creeps upward
        at a fraction of that rate — otherwise a few seconds of talking would
        drag the floor up and squelch the speaker's own voice.

        Pure float math on two scalars: no allocation, no deque scan, nothing
        that could stall the caller."""
        if self._squelch_seen < self._squelch_warmup:
            # Warmup: adopt the running minimum outright. One sample cannot
            # characterise a room, and clamping before the profile exists would
            # squelch the first words spoken.
            if self._squelch_seen == 0:
                self._noise_floor = v
                self._noise_spread = 0.0
            else:
                self._noise_floor = min(self._noise_floor, v)
                self._noise_spread += (
                    abs(v - self._noise_floor) - self._noise_spread
                ) * 0.5          # converge fast during the short warmup
            return
        if v < self._noise_floor:
            a = self._squelch_alpha            # fall toward a quieter trough
        else:
            a = self._squelch_alpha * 0.05     # rise 20x slower — speech-proof
        self._noise_floor += (v - self._noise_floor) * a
        # Spread learns ONLY from samples the gate currently calls ambient.
        # Letting speech widen it would inflate the gate until the speaker
        # squelched themselves.
        if v <= self._gate_locked():
            self._noise_spread += (
                abs(v - self._noise_floor) - self._noise_spread
            ) * self._squelch_alpha

    def _gate_locked(self) -> float:
        """The ambient ceiling: trough + k*spread + epsilon. Caller holds lock.
        Single definition so the profiler and the clamp can never disagree
        about what counts as noise."""
        return (
            self._noise_floor
            + self._squelch_margin * self._noise_spread
            + self._squelch_eps
        )

    @property
    def noise_gate(self) -> float:
        with self._lock:
            return self._gate_locked()

    @property
    def noise_floor(self) -> float:
        with self._lock:
            return self._noise_floor

    @property
    def squelch_ready(self) -> bool:
        """True once enough frames have been seen to trust the profile."""
        with self._lock:
            return self._squelch_seen >= self._squelch_warmup

    def normalize(self, value: float) -> float:
        """Normalized 0..1 against the held/decaying peak.

        DIGITAL SILENCE (the TTS case). A microphone always has a noise floor;
        synthesized speech hits *absolute* 0.0 between words and between
        utterances. Two consequences are handled explicitly:

        1. **No division by zero, ever.** ``_floor`` clamps the peak from below
           and a defensive ``peak <= 0`` guard follows, so an all-zero buffer
           returns 0.0 instead of raising.
        2. **The scale must not collapse in the gaps.** A silent sample is the
           ABSENCE of signal, not evidence of a quieter source, so it never
           lowers the peak (``max`` already ignores it) — and for
           ``hold_frames`` after real signal the peak does not decay at all.
           Without that hold, an inter-syllable gap would shrink the reference
           and the next syllable would slam to full scale, making Karen's voice
           pulse like a strobe instead of breathing.
        """
        try:
            v = abs(float(value))
        except (TypeError, ValueError):
            return 0.0
        if v != v or v == float("inf"):     # NaN / inf are not signal
            return 0.0
        with self._lock:
            # ── Dynamic squelch, BEFORE scaling ────────────────────────────
            # The profiler learns from every sample (including squelched ones —
            # that is precisely where the room tone lives). Anything at or
            # below floor*margin + eps is declared ambient and clamped to a
            # hard 0.0, so the meter renders a dead-stable baseline instead of
            # amplifying HVAC into a waveform. Everything downstream then
            # operates on the squelched value, so a clamped sample is
            # indistinguishable from true digital silence — including to the
            # peak tracker, which must not treat room tone as signal.
            if self._squelch:
                self._update_noise_floor(v)
                self._squelch_seen += 1
                if self._squelch_seen >= self._squelch_warmup:
                    if v <= self._gate_locked():
                        v = 0.0
            if v > self._floor:
                # Real signal: re-arm the gap guard, but KEEP DECAYING. Holding
                # the peak on any above-floor sample would pin the reference at
                # an old loud value forever, so a source that drops from 1.0 to
                # 0.01 would flatline at 1% — the very failure the decay exists
                # to prevent. Decay here, hold only in the gaps below.
                self._held = self._hold_frames
                self._peak = max(v, self._peak * self._decay, self._floor)
            elif self._held > 0:
                # Inside a gap — hold the reference steady.
                self._held -= 1
            else:
                # Sustained silence: only now let the reference relax.
                self._peak = max(self._peak * self._decay, self._floor)
            peak = self._peak
        if peak <= 0.0:                     # unreachable via floor; belt-and-braces
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


def cell_for(left: float, right: float, *, baseline: bool = True) -> str:
    """Two normalized samples → one Braille cell. Pure; never raises.

    ``baseline`` draws the bottom dot row for a silent sample. This is not
    decoration — it is the difference between a working meter and an invisible
    one. U+2800 is the BLANK braille pattern, so a silent scope without a
    baseline renders as pure whitespace, which an operator cannot distinguish
    from "the feature isn't installed". A real oscilloscope shows a flat line
    at rest; so does this one now.

    (This module's docstring claimed a flat baseline from the start; the code
    never did it. Caught by looking at a live cockpit and seeing nothing.)"""
    try:
        mask = 0
        left_n, right_n = _level_for(left), _level_for(right)
        for i in range(left_n):
            mask |= _COL0_BOTTOM_UP[i]
        for i in range(right_n):
            mask |= _COL1_BOTTOM_UP[i]
        if baseline:
            # Silent sub-columns still get their bottom dot, so the trace is
            # continuous across quiet passages instead of breaking into
            # floating islands.
            if left_n == 0:
                mask |= _COL0_BOTTOM_UP[0]
            if right_n == 0:
                mask |= _COL1_BOTTOM_UP[0]
        return chr(_BRAILLE_BASE + mask)
    except Exception:  # noqa: BLE001
        return chr(_BRAILLE_BASE)


def _ascii_cell(left: float, right: float, *, baseline: bool = True) -> str:
    lvl = max(_level_for(left), _level_for(right))
    if lvl == 0 and baseline:
        return "_"          # visible flat line, same reason as the Braille path
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
        starve_after_s: Optional[float] = None,
        gravity_tau_s: Optional[float] = None,
        gravity_floor: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._width = max(1, int(width))
        self._cap = self._width * SAMPLES_PER_CELL
        self._samples: Deque[float] = deque(maxlen=self._cap)
        self._plane = plane
        self._norm = normalizer if normalizer is not None else AdaptiveNormalizer()
        # --- kinetic decay (client-side visual gravity) -----------------
        # When heavy STT inference starves the event loop, telemetry frames stop
        # arriving. Holding the last amplitude freezes the trace mid-spike,
        # which reads as "loud right now" — the exact lie a monitor must never
        # tell. Instead the wave FALLS under gravity toward the baseline, so a
        # starved stream looks like silence settling rather than a still frame.
        #
        # Purely visual and purely client-side: the daemon's AdaptiveNormalizer
        # is untouched, so nothing about measurement changes.
        self._starve_after = (
            max(0.0, float(starve_after_s)) if starve_after_s is not None
            else _starve_after_s()
        )
        self._gravity_tau = (
            max(1e-3, float(gravity_tau_s)) if gravity_tau_s is not None
            else _gravity_tau_s()
        )
        self._gravity_floor = (
            max(0.0, float(gravity_floor)) if gravity_floor is not None
            else _gravity_floor()
        )
        self._clock = clock or time.monotonic
        self._last_rx = self._clock()
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
            self._last_rx = self._clock()

    def push_rms(self, samples: Sequence[float]) -> float:
        """Compute RMS over a raw audio buffer, normalize, append. Returns the
        normalized value so a caller can broadcast it without recomputing."""
        level = self._norm.normalize(rms(samples))
        self.push(level, normalized=True)
        return level

    def extend(self, values: Iterable[float], *, normalized: bool = True) -> None:
        for v in values or ():
            self.push(v, normalized=normalized)

    def tick(self, now: Optional[float] = None) -> bool:
        """Apply visual gravity when telemetry has starved. Returns True iff it
        decayed this call.

        Called by the UI on every repaint — which is why the decay is
        TIME-BASED rather than per-frame: the repaint rate is set by terminal
        redraws, not by the audio stream, and a per-frame constant would fall
        at a different speed on a busy machine than an idle one. Elapsed time
        is the only frame-rate-independent basis.

        Decays the WHOLE ring, not just new samples. A spike that merely
        scrolled away would still show its peak for the width of the buffer;
        the operator would see a loud trace long after the sound stopped. The
        entire wave descends together, so a starved stream reads as settling.

        Snaps to exactly 0.0 below ``gravity_floor`` so the trace reaches the
        true squelched baseline instead of hovering asymptotically just above
        it, which would keep the meter looking faintly alive forever.

        NEVER raises."""
        try:
            t = self._clock() if now is None else float(now)
            with self._lock:
                idle = t - self._last_rx
                if idle < self._starve_after or not self._samples:
                    return False
                # Decay measured from the START of starvation, so one late
                # repaint after a long stall lands at the same place a steady
                # stream of repaints would have — no dependence on how often
                # the UI happened to tick.
                factor = math.exp(-(idle - self._starve_after) / self._gravity_tau)
                changed = False
                for i, v in enumerate(self._samples):
                    if v <= 0.0:
                        continue
                    nv = v * factor
                    if nv < self._gravity_floor:
                        nv = 0.0
                    if nv != v:
                        self._samples[i] = nv
                        changed = True
                return changed
        except Exception:  # noqa: BLE001 — gravity NEVER breaks a frame
            return False

    @property
    def starved(self) -> bool:
        """True when no telemetry has arrived within the starvation window."""
        with self._lock:
            return (self._clock() - self._last_rx) >= self._starve_after

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
            self._last_rx = self._clock()
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
        """Rich markup in the plane's colour, RESOLVED through the theme.

        Emitting the bare semantic name (``[venom_green]…``) looked correct and
        was invisible: Rich does not know that name, and an unknown style is
        dropped SILENTLY — no error, no colour. A PTY integration test caught
        it by inspecting the terminal bytes; every unit test had only compared
        the markup string, which was perfectly well-formed and meant nothing.

        ``theme.semantic()`` maps the name to a concrete style for the ACTIVE
        colour tier (hex on truecolor, the 8/16-colour equivalent on a limited
        terminal, and "" on a non-colour one). The theme still owns the palette
        — this asks it rather than guessing, and never hardcodes a hex value.
        """
        body = self.render()
        try:
            from backend.core.ouroboros.ui.theme import semantic
            style = semantic(self.accent)
            if not style:
                return body          # NONE tier: no colour is the right answer
            return f"[{style}]{body}[/{style}]"
        except Exception:  # noqa: BLE001
            return body

    def samples(self) -> list:
        """Locked snapshot of the ring, oldest → newest.

        A COPY, not a view: the audio thread appends to this deque while the
        render thread reads it, and handing out the live object would let a
        caller iterate a mutating deque (``RuntimeError`` on CPython, and a
        torn frame everywhere else)."""
        with self._lock:
            return list(self._samples)

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

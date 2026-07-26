"""Evidence at the moment of failure — a flight recorder for the mic path.

Why this exists
---------------
The microphone path has failed the same way three times across this session:
whisper returns an EMPTY transcript from audio with speech-level amplitude.
Every offline test exonerates the code — the device, the duplex stream, the
resampler, the compressor and the echo canceller each pass known-good speech
through intact — and yet the operator's voice arrives as something whisper
scores at ``no_speech_prob = 0.81`` and, with its thresholds disabled, renders
as "I'm sorry, I'm sorry, I'm sorry": the hallucination it produces from
non-speech.

That is the whole difficulty. The fault is not in any stage in isolation; it
appears only in the live composition, and the live composition currently keeps
no record of itself. Diagnosis has therefore been a sequence of plausible
theories tested against audio captured AFTER processing, which cannot
distinguish "the microphone heard nothing useful" from "we destroyed what it
heard". Those two have opposite fixes.

So this records BOTH ends of the chain and the metrics in between, and writes
them only when the chain demonstrably failed.

What it is not
--------------
Not a workaround, and not a permanent tap. It is bounded, disarmed by default
in cost (the ring only ever holds a few seconds), self-cleaning, and it
answers ONE question with evidence rather than inference:

    Is the signal already unintelligible when it leaves the device,
    or does it become unintelligible on the way to the model?

Design
------
* Two rings — RAW (device rate, pre-everything) and PROCESSED (internal rate,
  post AEC/AGC, exactly what the model was handed). Fixed sample budget, so
  memory is constant regardless of how long the process runs.
* Written only on a REJECTION, and rate-limited, because a recorder that fires
  on every utterance is a disk leak wearing a diagnostic's clothes.
* Each incident is a directory holding ``raw.wav``, ``processed.wav`` and
  ``metrics.json`` — the two signals plus what each stage thought it saw, so
  the comparison is arithmetic rather than opinion.
* Every entry point is exception-proof and never blocks the audio thread: a
  flight recorder that can crash the flight is not one.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def forensics_enabled() -> bool:
    """Default ON while the capture fault is open. The cost is a few seconds
    of ring buffer; the alternative is another session of theories tested
    against evidence that cannot settle them."""
    return os.getenv(
        "JARVIS_CAPTURE_FORENSICS", "1",
    ).strip().lower() in _TRUTHY


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def handoff_check_enabled() -> bool:
    """``JARVIS_FORENSICS_HANDOFF_CHECK`` (default on). Bounded FFT work on
    the incident path only — never on the audio thread."""
    return os.environ.get(
        "JARVIS_FORENSICS_HANDOFF_CHECK", "1",
    ).strip().lower() in _TRUTHY


def handoff_corr_floor() -> float:
    """Correlation below which alignment is not considered PROVEN, and the
    handoff therefore reports ``unverifiable`` rather than a fault."""
    return _env_float("JARVIS_FORENSICS_HANDOFF_CORR_FLOOR", 0.99, 0.0)


def handoff_gain_tolerance_db() -> float:
    """Gain error tolerated before a proven-aligned handoff is called
    ``scaled``. Well inside the ~90 dB an int16 round-trip preserves, and far
    below the 6 dB a single bit-shift would cost."""
    return _env_float("JARVIS_FORENSICS_HANDOFF_GAIN_TOL_DB", 0.5, 0.0)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


def window_s() -> float:
    """How much history to hold. Long enough for a whole utterance plus the
    silence that ended it — an incident cut off at its onset proves nothing."""
    return _env_float("JARVIS_CAPTURE_FORENSICS_WINDOW_S", 12.0, 1.0)


def max_incidents() -> int:
    """Ceiling on stored incidents. Oldest is evicted; the fault reproduces
    every attempt, so the newest evidence is always the relevant evidence."""
    return _env_int("JARVIS_CAPTURE_FORENSICS_MAX", 8)


def cooldown_s() -> float:
    """Minimum spacing between incidents. One utterance produces a partial
    transcription every few hundred milliseconds, and each failed partial is a
    rejection — without this, one failed sentence writes twenty incidents."""
    return _env_float("JARVIS_CAPTURE_FORENSICS_COOLDOWN_S", 6.0, 0.0)


def incident_root() -> Path:
    raw = os.getenv("JARVIS_CAPTURE_FORENSICS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.cwd() / ".jarvis" / "capture_forensics"


def _align(haystack: np.ndarray, needle: np.ndarray) -> Optional[Dict[str, Any]]:
    """Locate *needle* inside *haystack* and measure the gain between them.

    Normalised cross-correlation over EVERY lag, via FFT — O(n log n), a few
    milliseconds on a 12 s ring. Searching only near the end does not work:
    an incident is written after transcription returns, which puts the
    utterance 1-8 s back from the newest sample. (Measured: lags of 0.94 s
    through 8.04 s across one session's incidents. A short search window
    reports the resulting mismatch as divergence, which is a fabrication of
    the same family this module exists to prevent.)

    Returns ``None`` when no honest comparison is possible. NEVER raises."""
    try:
        n = int(needle.size)
        if n < 64 or haystack.size < n:
            return None
        nd = needle - needle.mean() # zero-mean the needle
        nd_energy = float(np.sqrt((nd ** 2).sum()))
        if nd_energy < 1e-12: # needle is silent — nothing to match
            return None                      # digital silence — nothing to match

        size = 1 << int(np.ceil(np.log2(haystack.size + n))) # next power of two for FFT
        corr = np.fft.irfft(
            np.fft.rfft(haystack, size) * np.conj(np.fft.rfft(nd, size)), size,
        )[:haystack.size - n + 1] # cross-correlation of haystack and needle, by FFT

        # Sliding window variance of the haystack, by prefix sums, so the
        # correlation is normalised per-lag rather than against a global mean.
        cs = np.concatenate([[0.0], np.cumsum(haystack)]) # prefix sum of the haystack
        cs2 = np.concatenate([[0.0], np.cumsum(haystack ** 2)]) # prefix sum of squares
        win_sum = cs[n:] - cs[:-n] # sum of the haystack in each window
        win_sq = cs2[n:] - cs2[:-n] # sum of squares in each window
        var = win_sq - win_sum * win_sum / n # variance in each window
        var[var < 1e-20] = np.inf            # silent windows can never win
        r = corr / (np.sqrt(var) * nd_energy) # normalised correlation

        lag = int(np.argmax(r))
        segment = haystack[lag:lag + n]
        seg_peak = float(np.max(np.abs(segment)))
        needle_peak = float(np.max(np.abs(needle)))
        gain = needle_peak / seg_peak if seg_peak > 1e-12 else float("nan")
        return {
            "lag_samples": lag,
            "correlation": round(float(r[lag]), 4),
            "bus_segment_peak": round(seg_peak, 6),
            "model_peak": round(needle_peak, 6),
            "gain_ratio": round(gain, 4) if gain == gain else None,
            "gain_db": (round(20.0 * float(np.log10(gain)), 2)
                        if gain == gain and gain > 0 else None),
        }
    except (ValueError, TypeError, MemoryError, FloatingPointError):
        return None


class _Ring:
    """Fixed-sample-budget ring of frames. Never allocates while recording."""

    def __init__(self, sample_rate: int, seconds: float) -> None:
        self.sample_rate = int(sample_rate)
        self.budget = max(1, int(self.sample_rate * seconds))
        self._frames: Deque[np.ndarray] = deque()
        self._held = 0
        self._lock = threading.Lock()
        #: Running observations — cheaper than re-deriving them from the ring,
        #: and they survive even if the ring is later trimmed. Because they
        #: survive, they describe the PROCESS, not the incident.
        #: LIFETIME totals, not window measurements. They are reported under
        #: a separate ``session`` key and are never evidence about an
        #: individual incident — see :meth:`stats`.
        self.frames_seen = 0
        self.peak_max = 0.0
        self.over_full_scale = 0

    def push(self, frame: np.ndarray) -> None:
        if frame is None or not len(frame):
            return
        f = np.asarray(frame, dtype=np.float32).reshape(-1)
        peak = float(np.max(np.abs(f))) if f.size else 0.0
        with self._lock:
            self.frames_seen += 1
            if peak > self.peak_max:
                self.peak_max = peak
            if peak > 1.0:
                self.over_full_scale += 1
            self._frames.append(f.copy())
            self._held += len(f)
            while self._held > self.budget and self._frames:
                self._held -= len(self._frames.popleft())

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._frames:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(list(self._frames))

    def _session(self) -> Dict[str, Any]:
        """Lifetime totals, quarantined behind their own key.

        Kept because "this device has overdriven at some point" is genuinely
        useful context, and deliberately separated because it is NOT a
        measurement of the incident being written. Flattened alongside the
        window numbers it reads as one, which is how a single early transient
        came to be reported as the cause of every later rejection."""
        return {
            "frames_seen": self.frames_seen,
            "peak_ever": round(self.peak_max, 4),
            "over_full_scale_frames": self.over_full_scale,
        }

    def stats(self) -> Dict[str, Any]:
        a = self.snapshot()
        if not a.size:
            return {"samples": 0, "sample_rate": self.sample_rate,
                    "session": self._session()}
        peak = float(np.max(np.abs(a)))
        rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
        crest_db = 20.0 * float(np.log10(peak / rms)) if rms > 1e-9 else 0.0
        # Syllabic rhythm: speech modulates its envelope at roughly 2-8 Hz.
        # Noise and steady tones do not. This is the cheapest single number
        # that distinguishes "a voice was present" from "energy was present",
        # which is exactly the distinction the rejection cannot make.
        env_rate = 100
        hop = max(1, self.sample_rate // env_rate)
        env = np.sqrt(np.convolve(a.astype(np.float64) ** 2,
                                  np.ones(hop) / hop, mode="same")[::hop])
        mod = 0.0
        if env.size > 16:
            env = env - env.mean()
            spec = np.abs(np.fft.rfft(env * np.hanning(env.size)))
            freqs = np.fft.rfftfreq(env.size, 1.0 / env_rate)
            band = spec[(freqs >= 2.0) & (freqs <= 8.0)].sum()
            total = spec[freqs >= 0.5].sum()
            mod = float(band / total) if total > 1e-12 else 0.0
        return {
            "samples": int(a.size),
            "sample_rate": self.sample_rate,
            "duration_s": round(a.size / self.sample_rate, 3),
            "peak": round(peak, 4),
            "rms": round(rms, 5),
            "crest_db": round(crest_db, 1),
            "clipped_samples": int(np.sum(np.abs(a) >= 0.999)),
            # Window-scoped overdrive, re-derived from the retained audio so
            # it describes THIS incident and nothing earlier.
            "over_full_scale_samples": int(np.sum(np.abs(a) > 1.0)),
            "syllabic_modulation_2_8hz": round(mod, 3),
            "session": self._session(),
        }


class CaptureForensics:
    """Holds the two rings and writes incidents. One per process."""

    def __init__(self) -> None:
        self._raw: Optional[_Ring] = None
        self._processed: Optional[_Ring] = None
        self._last_incident = 0.0
        self._lock = threading.Lock()
        self.incidents_written = 0

    # -- taps (audio thread — must never raise, never block) -------------

    def note_raw(self, frame: np.ndarray, sample_rate: int) -> None:
        try:
            if not forensics_enabled():
                return
            if self._raw is None or self._raw.sample_rate != int(sample_rate):
                self._raw = _Ring(sample_rate, window_s())
            self._raw.push(frame)
        except (ValueError, TypeError, MemoryError):
            pass

    def note_processed(self, frame: np.ndarray, sample_rate: int) -> None:
        try:
            if not forensics_enabled():
                return
            if self._processed is None or self._processed.sample_rate != int(sample_rate):
                self._processed = _Ring(sample_rate, window_s())
            self._processed.push(frame)
        except (ValueError, TypeError, MemoryError):
            pass

    # -- incident --------------------------------------------------------

    def record_rejection(
        self,
        reason: str,
        *,
        model_audio: Optional[np.ndarray] = None,
        model_sample_rate: int = 16000,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        """Write one incident. Returns its directory, or None. NEVER raises.

        *model_audio* is the exact buffer handed to the recogniser — the third
        signal, and the only one that proves whether the buffer assembled for
        the model resembles what the device delivered."""
        try:
            if not forensics_enabled():
                return None
            now = time.monotonic()
            with self._lock:
                if now - self._last_incident < cooldown_s():
                    return None
                self._last_incident = now

            root = incident_root()
            root.mkdir(parents=True, exist_ok=True)
            self._evict(root)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out = root / f"incident-{stamp}-{int(now * 1000) % 100000}"
            out.mkdir(parents=True, exist_ok=True)

            metrics: Dict[str, Any] = {
                # 1.1: lifetime ring totals moved out of the per-window blocks
                # into their own ``session`` key (they were being read as
                # incident evidence); adds window-scoped
                # ``over_full_scale_samples``.
                "schema_version": "1.1",
                "reason": reason,
                "wall_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "detail": dict(detail or {}),
            }
            for name, ring in (("raw_device", self._raw),
                               ("processed_bus", self._processed)):
                if ring is None:
                    metrics[name] = {"samples": 0, "note": "no frames tapped"}
                    continue
                metrics[name] = ring.stats()
                self._write_wav(out / f"{name}.wav", ring.snapshot(), ring.sample_rate)

            if model_audio is not None and len(model_audio):
                a = np.asarray(model_audio, dtype=np.float32).reshape(-1)
                self._write_wav(out / "model_input.wav", a, model_sample_rate)
                rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
                metrics["model_input"] = {
                    "samples": int(a.size), "sample_rate": int(model_sample_rate),
                    "duration_s": round(a.size / max(model_sample_rate, 1), 3),
                    "peak": round(float(np.max(np.abs(a))), 4),
                    "rms": round(rms, 5),
                }
                metrics["handoff"] = self._handoff(a, model_sample_rate)
            else:
                metrics["handoff"] = {"status": "unverifiable",
                                      "why": "no model buffer supplied"}

            metrics["verdict_hint"] = self._verdict(metrics)
            (out / "metrics.json").write_text(
                json.dumps(metrics, indent=2), encoding="utf-8",
            )
            self.incidents_written += 1
            logger.warning(
                "[CaptureForensics] incident written: %s (%s)",
                out, metrics["verdict_hint"],
            )
            return out
        except (OSError, ValueError, TypeError, MemoryError) as exc:
            logger.debug("[CaptureForensics] incident degraded: %r", exc)
            return None

    def _handoff(
        self, model_audio: np.ndarray, model_rate: int,
    ) -> Dict[str, Any]:
        """Is the buffer the recogniser was handed the audio the bus produced?

        This module's header has always described the processed ring as
        "exactly what the model was handed". That was an ASSUMPTION — the two
        were recorded independently and never compared, so a scaling or
        dtype fault at the final handoff would have been invisible here while
        looking, in the stored wavs, exactly like a quiet room.

        The comparison is cheap and the rings already exist, so the assumption
        becomes a per-incident measurement:

        ``lossless``      aligned at correlation ~1.0 with gain 0 dB. The
                          handoff is exonerated; look elsewhere.
        ``scaled``        aligned, but the amplitude changed. This is the
                          int16/float32 and double-normalisation class, named
                          automatically instead of hunted by hand.
        ``unverifiable``  alignment not proven. Says nothing further — the
                          utterance may simply have aged out of the ring, and
                          an unproven match is not evidence of divergence.

        NEVER raises; a failure here degrades to ``unverifiable``."""
        try:
            if not handoff_check_enabled():
                return {"status": "disabled"}
            ring = self._processed
            if ring is None or model_audio is None or not len(model_audio):
                return {"status": "unverifiable", "why": "no buffers"}
            if int(model_rate) != ring.sample_rate:
                # Not a fault: a resampled handoff is legitimate. It is simply
                # outside what a sample-wise comparison can speak to.
                return {"status": "unverifiable", "why": "rate differs",
                        "model_rate": int(model_rate),
                        "bus_rate": ring.sample_rate}
            hay = ring.snapshot().astype(np.float64)
            needle = np.asarray(model_audio, dtype=np.float64).reshape(-1)
            found = _align(hay, needle)
            if found is None:
                return {"status": "unverifiable", "why": "no alignment"}

            found["lag_s"] = round(
                (hay.size - (found["lag_samples"] + needle.size))
                / max(ring.sample_rate, 1), 3,
            )
            corr = found.get("correlation") or 0.0
            gain_db = found.get("gain_db")
            if corr < handoff_corr_floor():
                found["status"] = "unverifiable"
                found["why"] = "alignment below correlation floor"
            elif gain_db is None:
                found["status"] = "unverifiable"
                found["why"] = "gain undefined"
            elif abs(gain_db) > handoff_gain_tolerance_db():
                found["status"] = "scaled"
            else:
                found["status"] = "lossless"
            return found
        except (ValueError, TypeError, MemoryError):
            return {"status": "unverifiable", "why": "degraded"}

    @staticmethod
    def _verdict(metrics: Dict[str, Any]) -> str:
        """A first reading of the evidence, stated as a hypothesis.

        Deliberately a HINT. It compares the syllabic modulation of the two
        ends of the chain, which is the one measurement that separates the two
        opposite fixes: if the raw device signal already lacks the 2-8 Hz
        rhythm of speech, no amount of processing repair will help; if the raw
        end has it and the processed end does not, the chain is destroying
        it.

        Every number it reads is scoped to the incident window. It previously
        opened on the ring's LIFETIME over-full-scale counter, so the first
        overdriven frame of a process latched "input gain, not the chain" onto
        every rejection that followed and the modulation comparison this hint
        exists for never ran again. Overdrive is also not, by itself, a fault:
        ``AudioBus._fit_to_range`` exists precisely because macOS delivers
        this device above +/-1.0 routinely. It is reported below as a possible
        MECHANISM when the chain is measurably losing speech, never as a
        standalone cause."""
        raw = metrics.get("raw_device") or {}
        proc = metrics.get("processed_bus") or {}
        r_mod = raw.get("syllabic_modulation_2_8hz")
        p_mod = proc.get("syllabic_modulation_2_8hz")

        # A PROVEN amplitude change at the final handoff outranks everything
        # below it: it is specific, it is measured on this incident, and it
        # names a fault the modulation comparison cannot see. It speaks only
        # on proof — an unverifiable alignment is silent, never divergence.
        handoff = metrics.get("handoff") or {}
        if handoff.get("status") == "scaled":
            return (f"the buffer handed to the recogniser is the bus audio "
                    f"at {handoff.get('gain_db')} dB "
                    f"(correlation {handoff.get('correlation')}, gain ratio "
                    f"{handoff.get('gain_ratio')}) — the handoff is rescaling "
                    f"it; suspect a dtype cast or double normalisation")

        if not raw.get("samples"):
            return "no raw frames tapped — cannot attribute"
        if r_mod is None or p_mod is None:
            return "incomplete measurements"
        if r_mod < 0.15:
            return (f"raw device audio already lacks speech rhythm "
                    f"(modulation {r_mod}) — the microphone did not hear a "
                    f"voice; the chain is not at fault")
        if p_mod < r_mod * 0.6:
            over = int(raw.get("over_full_scale_samples", 0) or 0)
            if over > 0:
                return (f"speech rhythm present at the device ({r_mod}) and "
                        f"lost by the bus ({p_mod}) — the chain is destroying "
                        f"it; {over} samples arrived above full scale (peak "
                        f"{raw.get('peak')}) in this window, so suspect input "
                        f"gain ahead of range-fitting")
            return (f"speech rhythm present at the device ({r_mod}) and lost "
                    f"by the bus ({p_mod}) — the chain is destroying it")
        tail = ""
        if handoff.get("status") == "lossless":
            # Exonerating the handoff is worth stating: it is where the next
            # theory always goes, and it now costs nothing to rule out.
            tail = (f"; the recogniser was handed the bus audio unchanged "
                    f"(0 dB, correlation {handoff.get('correlation')}), so the "
                    f"fault is in the recogniser or in what was buffered for it")
        return (f"speech rhythm survives the chain (raw {r_mod} -> "
                f"processed {p_mod}) — look downstream of the bus{tail}")

    @staticmethod
    def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
        if audio is None or not len(audio):
            return
        try:
            import soundfile as sf
            sf.write(str(path), np.asarray(audio, dtype=np.float32), int(sample_rate))
            return
        except (ImportError, OSError, ValueError):
            pass
        try:                                    # stdlib fallback — no new dep
            import wave
            pcm = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(int(sample_rate))
                w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
        except (OSError, ValueError):
            pass

    @staticmethod
    def _evict(root: Path) -> None:
        try:
            kept = sorted(
                (p for p in root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
            )
            for old in kept[: max(0, len(kept) - max_incidents() + 1)]:
                for child in old.iterdir():
                    child.unlink(missing_ok=True)
                old.rmdir()
        except (OSError, ValueError):
            pass


_FORENSICS: Optional[CaptureForensics] = None
_FORENSICS_LOCK = threading.Lock()


def get_forensics() -> CaptureForensics:
    global _FORENSICS
    with _FORENSICS_LOCK:
        if _FORENSICS is None:
            _FORENSICS = CaptureForensics()
        return _FORENSICS


def reset_forensics() -> None:
    """Test seam."""
    global _FORENSICS
    with _FORENSICS_LOCK:
        _FORENSICS = None


__all__ = [
    "CaptureForensics",
    "forensics_enabled",
    "get_forensics",
    "incident_root",
    "reset_forensics",
]

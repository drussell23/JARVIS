"""Know when you cannot hear, and say so.

The failure this closes
-----------------------
The operator spoke thirty-four times and the pipeline logged ``EMPTY
transcript`` thirty-four times and said nothing. It had every number needed to
know why::

    at seating distance   rms 0.0033   crest 37.8dB   modulation 0.196
    at close range        rms 0.0114   crest 19.5dB   modulation 0.431

Speech sits at 15-21dB crest. At 37.8dB only the consonant bursts were
surviving and the sustained vowels had fallen into the room's noise floor —
temporal envelope smearing, which is why amplification could never have
rescued it. Whisper agreed: ``no_speech_prob`` 0.52 against 0.15.

Every one of those numbers was already being computed, by the capture
forensics recorder, on every rejection. Nothing consumed them. The loop was
OPEN: the system measured its own deafness and threw the measurement away.

What this adds
--------------
A Speech Quality Index over metrics that already exist, and a controller that
turns a sustained low score into a SPOKEN admission rather than another silent
empty transcript. "I'm picking up too much room echo to understand you" is a
conversation; thirty-four silent rejections is a broken assistant.

Why modulation leads the index
------------------------------
Amplitude is recoverable and rhythm is not. A quiet but well-articulated voice
can be scaled up; a smeared envelope cannot be unsmeared by any gain. So the
index weights syllabic modulation highest, crest factor second (it detects the
specific distance signature — peaks surviving, body gone), and level last.

DRY
---
``syllabic_modulation`` and the crest computation are the recorder's, imported
rather than reimplemented. A second copy of those formulas would drift from the
verdicts the forensics writes, and the two instruments must agree or neither
can be trusted.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def feedback_enabled() -> bool:
    """Master gate. OFF restores silent rejection, which is the behaviour this
    replaces and the only honest comparison for it."""
    return os.getenv(
        "JARVIS_ACOUSTIC_FEEDBACK", "true",
    ).strip().lower() in _TRUTHY


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


#: Below this, syllabic rhythm is too smeared to transcribe. Measured: 0.196
#: failed, 0.431 succeeded, known-good reference 0.44.
MODULATION_FLOOR = _env_float("JARVIS_ACOUSTIC_MOD_FLOOR", 0.25, 0.01, 0.9)

#: Above this, whisper is telling us it does not believe there is speech.
#: Measured: 0.52 on the failing capture, 0.15 on the good one.
NO_SPEECH_CEILING = _env_float("JARVIS_ACOUSTIC_NO_SPEECH_MAX", 0.45, 0.05, 0.99)

#: Speech crest sits at 15-21dB. Far above that means the body of the voice has
#: fallen into the floor and only transients remain — the distance signature.
CREST_CEILING_DB = _env_float("JARVIS_ACOUSTIC_CREST_MAX_DB", 30.0, 15.0, 60.0)

#: Consecutive degraded rejections before speaking. One bad utterance is a
#: cough or a door; a run of them is a room the operator cannot be heard in.
DEGRADED_RUN = _env_int("JARVIS_ACOUSTIC_DEGRADED_RUN", 3)

#: Minimum seconds between spoken complaints. An assistant that announces its
#: deafness every two seconds is worse than one that says nothing.
COMPLAINT_COOLDOWN_S = _env_float("JARVIS_ACOUSTIC_COMPLAINT_COOLDOWN_S", 45.0, 1.0, 3600.0)


@dataclass(frozen=True)
class QualitySample:
    """One rejection's measurements. Mirrors what the forensics already emits."""

    modulation: float = 0.0
    crest_db: float = 0.0
    rms: float = 0.0
    peak: float = 0.0
    no_speech_prob: float = 0.0
    device: str = ""

    @classmethod
    def from_audio(
        cls, audio: Any, sample_rate: int, *,
        no_speech_prob: float = 0.0, device: str = "",
    ) -> "QualitySample":
        """Measure a buffer. The ONE way audio becomes a QualitySample.

        Routed through the forensics ring so modulation and crest are computed
        by the recorder's own code. Two implementations of these formulas would
        drift, and then an incident file and a device score could disagree
        about the same audio — with no way to tell which was lying.

        NEVER raises: an unmeasurable buffer scores zero, which ranks last."""
        try:
            import numpy as _np

            from backend.audio.capture_forensics import _Ring

            x = _np.asarray(audio, dtype=_np.float32).reshape(-1)
            if not x.size:
                return cls(device=str(device))
            ring = _Ring(sample_rate, max(1.0, x.size / max(sample_rate, 1)))
            ring.push(x)
            st = ring.stats()
            return cls(
                modulation=float(st.get("syllabic_modulation_2_8hz", 0.0)),
                crest_db=float(st.get("crest_db", 0.0)),
                rms=float(st.get("rms", 0.0)),
                peak=float(st.get("peak", 0.0)),
                no_speech_prob=float(no_speech_prob),
                device=str(device),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Acoustic] could not measure buffer: %r", exc)
            return cls(device=str(device))

    @property
    def sqi(self) -> float:
        """Speech Quality Index in [0, 1]. Higher is more transcribable.

        Weighted toward what cannot be repaired downstream: rhythm first,
        because no gain stage restores a smeared envelope; crest second,
        because it names the distance signature specifically; level last,
        because level is the one thing the AGC can actually fix."""
        mod = max(0.0, min(1.0, self.modulation / 0.45))          # 0.45 == known good
        # Crest: 20dB ideal, degrading above. Below 10dB is over-compressed.
        if self.crest_db <= 0:
            crest = 0.5
        elif self.crest_db < 10.0:
            crest = 0.6
        else:
            crest = max(0.0, min(1.0, 1.0 - (self.crest_db - 20.0) / 20.0))
        level = max(0.0, min(1.0, self.rms / 0.02))               # 0.02 == comfortable
        belief = max(0.0, 1.0 - self.no_speech_prob)
        return round(0.45 * mod + 0.25 * crest + 0.15 * level + 0.15 * belief, 4)

    @property
    def degraded(self) -> bool:
        """Is this capture unusable for reasons gain cannot fix?"""
        if self.modulation and self.modulation < MODULATION_FLOOR:
            return True
        if self.no_speech_prob and self.no_speech_prob > NO_SPEECH_CEILING:
            return True
        if self.crest_db and self.crest_db > CREST_CEILING_DB:
            return True
        return False

    def diagnosis(self) -> str:
        """Which physical condition this looks like — used to choose what to
        SAY, so the spoken feedback is specific rather than a generic apology."""
        if self.crest_db > CREST_CEILING_DB and self.modulation < MODULATION_FLOOR:
            return "distance"          # transients survive, body gone
        if self.modulation < MODULATION_FLOOR:
            return "reverb"            # rhythm smeared
        if self.rms < 0.002:
            return "too_quiet"
        if self.no_speech_prob > NO_SPEECH_CEILING:
            return "not_speech"
        return "unclear"


#: What Karen says for each diagnosis. Phrased as HER limitation rather than
#: the operator's error — she is reporting what she can hear, not issuing an
#: instruction, and "sit closer" is exactly the abdication this exists to
#: avoid. Each is one short spoken sentence, because it interrupts.
_COMPLAINTS: Dict[str, Tuple[str, ...]] = {
    "distance": (
        "I can hear you, but you're far enough away that I'm only catching "
        "pieces of it.",
        "You're coming through faint — I'm losing most of the words at this "
        "distance.",
    ),
    "reverb": (
        "I'm picking up too much room echo to make out the words.",
        "The room's washing you out — I can hear something but not what.",
    ),
    "too_quiet": (
        "You're barely reaching the microphone at all.",
    ),
    "not_speech": (
        "I'm getting sound but I can't find any speech in it.",
    ),
    "unclear": (
        "Something's arriving but I can't make it out.",
    ),
}


def complaint_for(diagnosis: str, *, salt: str = "") -> str:
    """One spoken line. Deterministic per (diagnosis, salt) so repeated
    conditions do not produce a different sentence each time — an assistant
    that rephrases its own limitation every utterance sounds evasive."""
    import hashlib

    pool = _COMPLAINTS.get(diagnosis) or _COMPLAINTS["unclear"]
    key = f"{diagnosis}|{salt}".encode("utf-8", "replace")
    idx = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(pool)
    return pool[idx]


class AcousticFeedbackController:
    """Closes the loop: measure, decide, speak.

    Holds a short history rather than reacting to one sample, because a single
    degraded utterance is a cough, a chair, a door. A RUN of them is a room the
    operator cannot be heard in, and only the run is worth interrupting for."""

    def __init__(self, emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 speak: Optional[Callable[[str], Any]] = None) -> None:
        #: Injected so the controller can be exercised without a bus or a voice.
        self._emit = emit
        self._speak = speak
        self.samples: List[QualitySample] = []
        self._consecutive = 0
        self._last_complaint = 0.0
        self.events: List[Dict[str, Any]] = []          # observability + tests

    def observe(self, sample: QualitySample) -> Optional[Dict[str, Any]]:
        """Record one rejection. Returns the emitted event, or None.

        NEVER raises: this sits on the STT rejection path, and an instrument
        that can break the thing it measures is not an instrument."""
        try:
            if not feedback_enabled():
                return None
            self.samples.append(sample)
            if len(self.samples) > 32:
                self.samples = self.samples[-32:]

            if not sample.degraded:
                self._consecutive = 0
                return None
            self._consecutive += 1
            if self._consecutive < DEGRADED_RUN:
                return None

            now = time.monotonic()
            if now - self._last_complaint < COMPLAINT_COOLDOWN_S:
                return None
            self._last_complaint = now

            diagnosis = sample.diagnosis()
            event = {
                "type": "ACOUSTIC_DEGRADATION",
                "diagnosis": diagnosis,
                "sqi": sample.sqi,
                "modulation": round(sample.modulation, 3),
                "crest_db": round(sample.crest_db, 1),
                "no_speech_prob": round(sample.no_speech_prob, 3),
                "device": sample.device,
                "consecutive": self._consecutive,
                "spoken": complaint_for(diagnosis, salt=sample.device),
            }
            self.events.append(event)
            logger.warning(
                "[Acoustic] degradation: %s (sqi=%.2f mod=%.3f crest=%.1fdB) — %s",
                diagnosis, sample.sqi, sample.modulation, sample.crest_db,
                event["spoken"],
            )
            if self._emit is not None:
                try:
                    self._emit("acoustic_degradation", event)
                except (TypeError, ValueError, OSError, RuntimeError) as exc:
                    logger.debug("[Acoustic] emit degraded: %r", exc)
            if self._speak is not None:
                try:
                    self._speak(event["spoken"])
                except (TypeError, ValueError, OSError, RuntimeError) as exc:
                    logger.debug("[Acoustic] speak degraded: %r", exc)
            return event
        except (AttributeError, TypeError, ValueError):
            logger.debug("[Acoustic] observe degraded", exc_info=True)
            return None

    def reset(self) -> None:
        self._consecutive = 0
        self._last_complaint = 0.0


# ---------------------------------------------------------------------------
# Device ranking — measurement only. The live rebind is deliberately absent.
# ---------------------------------------------------------------------------


@dataclass
class DeviceScore:
    index: int
    name: str
    sqi: float = 0.0
    sample: Optional[QualitySample] = None
    error: str = ""
    is_continuity: bool = field(default=False)


def _looks_like_continuity(name: str, sd: Any = None) -> bool:
    """A Continuity microphone is an iPhone or iPad, named after its owner and
    wherever that person left it.

    Detected STRUCTURALLY rather than by matching a name: a device whose name
    contains no hardware word and does not match any known local audio device
    class is a personal device. Hardcoding "Derek J. Russell Microphone" would
    work on exactly one machine, and this is the fault that already wasted a
    measurement in this investigation."""
    low = str(name or "").lower()
    hardware_words = (
        "macbook", "imac", "mac mini", "mac studio", "built-in", "internal",
        "usb", "hdmi", "display", "webcam", "interface", "aggregate", "virtual",
        "blackhole", "loopback", "soundflower", "airpods", "headset", "headphone",
    )
    return not any(w in low for w in hardware_words)


def rank_devices(
    probe: Optional[Callable[[int], QualitySample]] = None, sd: Any = None,
) -> List[DeviceScore]:
    """Score every input device by measured speech quality, best first.

    *probe* is injected — the caller decides whether scoring means opening a
    stream, replaying a buffer, or consulting history. That keeps this function
    free of CoreAudio, which is what makes it testable and what stops it
    binding a device as a side effect of ranking one.

    NEVER raises."""
    scores: List[DeviceScore] = []
    try:
        if sd is None:
            import sounddevice as sd            # type: ignore[no-redef]
        devices = sd.query_devices()
    except Exception as exc:                    # noqa: BLE001 — enumeration is best-effort
        logger.debug("[Acoustic] device enumeration failed: %r", exc)
        return scores

    for idx, dev in enumerate(devices):
        try:
            if int(dev.get("max_input_channels", 0) or 0) <= 0:
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        name = str(dev.get("name", "?"))
        entry = DeviceScore(
            index=idx, name=name, is_continuity=_looks_like_continuity(name, sd),
        )
        if probe is not None:
            try:
                sample = probe(idx)
                entry.sample = sample
                entry.sqi = sample.sqi
            except Exception as exc:            # noqa: BLE001 — one bad device must not end the scan
                entry.error = f"{type(exc).__name__}: {exc}"
        scores.append(entry)

    scores.sort(key=lambda s: (-s.sqi, s.is_continuity, s.index))
    return scores


def best_device(scores: List[DeviceScore], *, margin: float = 0.15) -> Optional[DeviceScore]:
    """The device worth switching TO, or None to stay put.

    Requires a MARGIN over the incumbent rather than a bare maximum: swapping
    on noise would thrash the stream, and a rebind costs a dropped utterance.
    Returns None when nothing clears the bar — "stay" is a real answer."""
    if not scores:
        return None
    best = scores[0]
    if best.sqi <= 0.0:
        return None
    if len(scores) > 1 and best.sqi - scores[1].sqi < margin:
        return None
    return best


__all__ = [
    "CREST_CEILING_DB",
    "MODULATION_FLOOR",
    "NO_SPEECH_CEILING",
    "AcousticFeedbackController",
    "DeviceScore",
    "QualitySample",
    "best_device",
    "complaint_for",
    "feedback_enabled",
    "rank_devices",
]
